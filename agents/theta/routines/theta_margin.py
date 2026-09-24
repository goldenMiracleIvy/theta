"""Haircut-aware room for the THETA book. Never places an order."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

import importlib.util
from pathlib import Path as _P

import asyncio


def _theta_mod(name: str):
    path = _P(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("theta_" + name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_math = _theta_mod("_theta_math")
FXRP_IM_HAIRCUT = _math.FXRP_IM_HAIRCUT
FXRP_MM_HAIRCUT = _math.FXRP_MM_HAIRCUT
PERP_IM_CONTINGENCY = _math.PERP_IM_CONTINGENCY
PERP_MM_CONTINGENCY = _math.PERP_MM_CONTINGENCY
allowed_short = _math.allowed_short
book_is_tight = _math.book_is_tight
drawdown_short = _math.drawdown_short
split_wallet = _math.split_wallet
free_margin_for_short = _math.free_margin_for_short

logger = logging.getLogger(__name__)

CATEGORY = "Monitoring"


async def _derive_collaterals_value() -> tuple[float, float]:
    """Live theta-pm2 collateral value and locked initial margin.

    Returns (collaterals_value, sum_initial_margin). Both 0.0 if Derive is
    unreachable so the routine falls back to the offline haircut estimate
    instead of failing the tick.
    """
    try:
        _derive = _theta_mod("_theta_derive")
        creds = _derive.from_condor_keystore()
        import aiohttp

        async with aiohttp.ClientSession() as s:
            headers = _derive.auth_headers(creds)
            headers["Content-Type"] = "application/json"
            body = {
                "wallet": creds["api_key"],
                "subaccount_id": int(creds["sub_id"]),
            }
            async with s.post(
                "https://api.lyra.finance/private/get_subaccount",
                data=__import__("json").dumps(body),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                d = __import__("json").loads(await r.read())
                res = d.get("result") or d
                value = float(res.get("collaterals_value") or 0.0)
                locked = 0.0
                for c in res.get("collaterals") or []:
                    try:
                        locked += float(c.get("initial_margin") or 0.0)
                    except (TypeError, ValueError):
                        pass
                return value, locked
    except Exception as exc:  # noqa: BLE001
        logger.warning("theta_margin: derive collateral read failed: %s", exc)
        return 0.0, 0.0


class Config(BaseModel):
    """Size the matching short from posted FXRP and the USDC sleeve."""

    wallet_usd: float = Field(
        default=0.0,
        description="If >0, split half FXRP / half sleeve / short cap. 0 = use the three fields below.",
    )
    fxrp_posted: float = Field(default=400.0, description="FXRP posted as margin, quote")
    usdc_sleeve: float = Field(default=400.0, description="USDC shock sleeve, quote")
    pile_cap: float = Field(default=400.0, description="Never short more than this")
    drawdown_pct: float = Field(
        default=0.0, description="Account drawdown as a fraction (0.08 = 8%)"
    )
    min_excess: float = Field(default=50.0, description="Tight if MM excess is below this")


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    del context
    fxrp, sleeve, cap = config.fxrp_posted, config.usdc_sleeve, config.pile_cap
    min_excess = config.min_excess
    if config.wallet_usd > 0:
        fxrp, sleeve, cap = split_wallet(config.wallet_usd)
        min_excess = max(8.0, round(config.wallet_usd * 0.10, 2))
    # Derive cross-margin: posted FXRP already locks its own IM, so the short
    # can only consume the account's FREE margin. Cap the short there.
    collaterals_value, locked_im = await _derive_collaterals_value()
    free_short = free_margin_for_short(fxrp, sleeve, collaterals_value, locked_im)
    raw = allowed_short(fxrp, sleeve, cap)
    # The honest cap is the smaller of the desk rule and what Derive will fund.
    raw = min(raw, free_short) if free_short > 0 else raw
    sized = drawdown_short(raw, config.drawdown_pct)
    tight = book_is_tight(
        fxrp,
        sleeve,
        sized,
        mm_haircut=FXRP_MM_HAIRCUT,
        perp_mm=PERP_MM_CONTINGENCY,
        min_excess=min_excess,
    )
    isolated_im = sized * PERP_IM_CONTINGENCY
    book_im = fxrp * FXRP_IM_HAIRCUT + isolated_im
    status = "TIGHT" if tight else "ROOM"
    mode = f"wallet {config.wallet_usd:.0f} split" if config.wallet_usd > 0 else "manual legs"
    cap_note = (
        f"Free-margin cap {free_short:.2f}"
        if free_short > 0
        else "free-margin cap unavailable (offline est)"
    )
    text = "\n".join(
        [
            "THETA margin — numbers only. No order.",
            f"Mode {mode}  FXRP posted {fxrp:.2f}  sleeve {sleeve:.2f}  pile cap {cap:.2f}",
            f"Haircut IM {FXRP_IM_HAIRCUT:.0%}  MM {FXRP_MM_HAIRCUT:.0%}  perp IM {PERP_IM_CONTINGENCY:.0%}",
            f"Free-margin short cap {free_short:.2f}  ({cap_note})",
            f"Allowed short {raw:.2f}  after drawdown {sized:.2f}  ({config.drawdown_pct:.1%})",
            f"Isolated perp IM ~{isolated_im:.2f}  book IM ~{book_im:.2f}  excess floor {min_excess:.2f}  status {status}",
            "Never size to the full wallet. Sleeve stays posted.",
        ]
    )
    rid = await _math.save_report(
        "THETA margin",
        "theta_margin",
        [
            ("Status", status, None, "down" if tight else "up"),
            ("FXRP posted", f"{fxrp:.2f}"),
            ("Sleeve", f"{sleeve:.2f}"),
            ("Free-margin cap", f"{free_short:.2f}"),
            ("Allowed short", f"{sized:.2f}"),
        ],
        "Numbers only. No order. Never size to the full wallet — sleeve stays posted.",
        section="01 / BOOK",
        section_note=f"Mode: {mode}.",
        tables=[
            (
                [
                    {
                        "Line": "FXRP posted",
                        "Quote": f"{fxrp:.2f}",
                        "Note": "bank + margin",
                    },
                    {
                        "Line": "USDC sleeve",
                        "Quote": f"{sleeve:.2f}",
                        "Note": "shock room",
                    },
                    {
                        "Line": "Pile cap",
                        "Quote": f"{cap:.2f}",
                        "Note": "never short more",
                    },
                    {
                        "Line": "Free-margin cap",
                        "Quote": f"{free_short:.2f}",
                        "Note": "Derive cross-margin limit",
                    },
                    {
                        "Line": "Allowed short",
                        "Quote": f"{raw:.2f}",
                        "Note": "before drawdown",
                    },
                    {
                        "Line": "After drawdown",
                        "Quote": f"{sized:.2f}",
                        "Note": f"{config.drawdown_pct:.1%}",
                    },
                ],
                ["Line", "Quote", "Note"],
            ),
            (
                [
                    {
                        "Rule": "FXRP IM haircut",
                        "Value": f"{FXRP_IM_HAIRCUT:.0%}",
                    },
                    {
                        "Rule": "FXRP MM haircut",
                        "Value": f"{FXRP_MM_HAIRCUT:.0%}",
                    },
                    {
                        "Rule": "Perp IM",
                        "Value": f"{PERP_IM_CONTINGENCY:.0%}",
                    },
                    {
                        "Rule": "Isolated perp IM",
                        "Value": f"{isolated_im:.2f}",
                    },
                    {
                        "Rule": "Book IM",
                        "Value": f"{book_im:.2f}",
                    },
                    {
                        "Rule": "Excess floor",
                        "Value": f"{min_excess:.2f}",
                    },
                    {
                        "Rule": "Status",
                        "Value": status,
                    },
                ],
                ["Rule", "Value"],
                "02 / HAIRCUT",
                "IM / MM used to size the matching short.",
            ),
        ],
    )
    if rid:
        text = f"{text}\n\n📊 Report: {rid}"
    return text
