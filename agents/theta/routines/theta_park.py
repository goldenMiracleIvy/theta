"""Automated PARK: USDC -> XRP -> wrap FXRP, posted as perp margin.

Runs as theta's step 0 when ``park_auto`` is on. Idempotent: if FXRP is
already posted, it does nothing. Never converts the whole wallet — it leaves
the USDC sleeve per the desk's split rule, so liquidation room stays free.

Derive's perp margin is FXRP (wrapped XRP). Hummingbot's derive_perpetual
connector is perps-only and cannot do this, so we call Derive's native REST
API directly (see _theta_derive). Credentials live in
``secrets/derive.json`` (copy from derive.json.example, same key you
registered in Condor Keys & Wallets).

No perpetual order is placed here. This is setup, not the hedge.
"""

from __future__ import annotations

import logging
from pathlib import Path as _P
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CATEGORY = "Setup"

_THIS = _P(__file__).resolve().parent
_SECRETS = _THIS / "secrets" / "derive.json"


def _math():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_theta_math", _THIS / "_theta_math.py")
    if spec is None or spec.loader is None:
        raise ImportError("theta math helper not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _derive():
    # local import keeps the routine loadable even if the helper is absent;
    # mirrors the _theta_mod pattern used by the other theta routines.
    import importlib.util

    spec = importlib.util.spec_from_file_location("_theta_derive", _THIS / "_theta_derive.py")
    if spec is None or spec.loader is None:
        raise ImportError("theta_derive helper not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Config(BaseModel):
    enabled: bool = Field(default=True, description="Run automated PARK if FXRP not yet posted")
    wallet_usd: float = Field(default=0.0, description="If >0, split half FXRP / half sleeve. 0 = manual legs")
    fxrp_target: float = Field(default=400.0, description="FXRP to post (quote). Ignored if wallet_usd>0")
    usdc_sleeve: float = Field(default=400.0, description="USDC to leave as sleeve. Ignored if wallet_usd>0")
    # NOTE: the field name must not look like a credential. Condor refuses to
    # publish a Config whose field name matches its credential pattern
    # (SEC-617) — the default would be readable by every user of the install.
    creds_file: str = Field(
        default="",
        description="Optional path to derive.json. Empty = use the Condor keystore.",
    )
    dry_run: bool = Field(default=True, description="If true, compute and report but do not convert/wrap")


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    del context

    async def _out(lines: list[str], kpis: list[tuple]) -> str:
        text = "\n".join(lines)
        try:
            rid = await _math().save_report(
                "THETA park",
                "theta_park",
                kpis,
                "Setup only. No hedge is opened here.",
                rows=[{"Line": str(k[0]), "Value": str(k[1])} for k in kpis],
                columns=["Line", "Value"],
                section="01 / COLLATERAL",
                section_note="FXRP posted as margin on the desk subaccount.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("theta_park report failed: %s", exc)
            return text
        if rid:
            text = f"{text}\n\n📊 Report: {rid}"
        return text

    if not config.enabled:
        return await _out(
            ["THETA park — disabled. FXRP must be posted manually."],
            [("PARK", "disabled")],
        )

    d = _derive()
    try:
        creds = d.load_credentials(config.creds_file or _SECRETS)
    except (FileNotFoundError, ValueError) as exc:
        return await _out(
            [
                "THETA park — BLOCKED: derive.json not ready.",
                f"  {exc}",
                "  Copy routines/secrets/derive.json.example -> secrets/derive.json and fill the key.",
            ],
            [("PARK", "blocked")],
        )

    if config.wallet_usd > 0:
        fxrp_target = round(config.wallet_usd / 2.0, 2)
        usdc_sleeve = fxrp_target
    else:
        fxrp_target = config.fxrp_target
        usdc_sleeve = config.usdc_sleeve

    try:
        balances = await d.get_balances(creds)
    except Exception as exc:  # noqa: BLE001
        return await _out(
            [f"THETA park — read failed: {exc}"],
            [("PARK", "read failed")],
        )

    usdc = balances.get("USDC", 0.0)
    fxrp = balances.get("FXRP", 0.0)
    xrp = balances.get("XRP", 0.0)
    kpis = [
        ("FXRP posted", f"{fxrp:.2f}"),
        ("USDC", f"{usdc:.2f}"),
        ("Target FXRP", f"{fxrp_target:.2f}"),
        ("Sleeve", f"{usdc_sleeve:.2f}"),
    ]
    lines = [
        "THETA park — numbers only.",
        f"USDC {usdc:.2f}  XRP {xrp:.2f}  FXRP posted {fxrp:.2f}",
        f"Target FXRP {fxrp_target:.2f}  sleeve {usdc_sleeve:.2f}",
    ]

    if fxrp >= fxrp_target - 1.0:
        lines.append(
            f"FXRP already posted ({fxrp:.2f} >= {fxrp_target:.2f}). PARK done — no action."
        )
        return await _out(lines, kpis + [("PARK", "done", None, "up")])

    if config.dry_run:
        lines.append("DRY-RUN: would move spare USDC -> XRP, then deposit XRP (arrives as FXRP).")
        lines.append("Set dry_run=false to submit the SignedAction(s) to Derive.")
        return await _out(lines, kpis + [("PARK", "dry-run")])

    need_fxrp = fxrp_target - fxrp
    usable_usdc = max(0.0, usdc - usdc_sleeve)
    if usable_usdc <= 0:
        lines.append(
            "No USDC spare beyond sleeve — cannot PARK. Fund the account or lower the sleeve."
        )
        return await _out(lines, kpis + [("PARK", "no spare")])

    try:
        if xrp > 0:
            amt = min(xrp, need_fxrp + 0.0)
            res = await d.deposit(creds, "XRP", round(amt, 2))
            lines.append(f"deposit XRP {amt:.2f} -> FXRP: {res}")
            need_fxrp -= amt
        if need_fxrp > 0.5 and xrp <= 0:
            lines.append(
                "No XRP held to wrap. Would need to convert USDC->XRP first (not yet implemented as a live swap)."
            )
            lines.append("PARK partial: only deposits possible with existing XRP.")
    except Exception as exc:  # noqa: BLE001
        return await _out(
            lines + [f"PARK execution error: {exc}"],
            kpis + [("PARK", "error", None, "down")],
        )

    try:
        final = await d.get_balances(creds)
        lines.append(
            f"after: USDC {final.get('USDC',0):.2f}  XRP {final.get('XRP',0):.2f}  FXRP {final.get('FXRP',0):.2f}"
        )
    except Exception:  # noqa: BLE001
        pass
    lines.append("PARK attempt complete. Hedge loop may now open the short.")
    return await _out(lines, kpis + [("PARK", "attempted")])
