"""Read Derive's public XRP board. Print SIT or WRITE. Never place an order.

Public REST only. This clerk does not sign, does not POST /private/*, and
does not talk to Hummingbot. A dead board is SIT.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

import importlib.util
from pathlib import Path as _P


def _theta_mod(name: str):
    path = _P(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("theta_" + name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_math = _theta_mod("_theta_math")
WRITE = _math.WRITE
call_premium_pct = _math.call_premium_pct
tape_gate = _math.tape_gate
strike_from_name = _math.strike_from_name

logger = logging.getLogger(__name__)

CATEGORY = "Market Data"

_PUBLIC = "https://api.lyra.finance/public"


class Config(BaseModel):
    """Read the XRP options tape and this hour's funding."""

    currency: str = Field(default="XRP", description="Options underlying")
    perp_instrument: str = Field(
        default="XRP-PERP", description="Perp ticker for spot + funding"
    )
    write_min_pct: float = Field(
        default=0.80,
        description="WRITE when nearest OTM call mark ≥ this % of spot",
    )
    max_dte: int = Field(default=10, description="Ignore expiries beyond this many days")
    otm_min_pct: float = Field(
        default=5.0, description="Call must be at least this % above spot"
    )


async def _post(session: aiohttp.ClientSession, path: str, payload: dict) -> dict:
    url = f"{_PUBLIC}{path}"
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
        resp.raise_for_status()
        body = await resp.json()
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        return body if isinstance(body, dict) else {}


async def _perp(session: aiohttp.ClientSession, instrument: str) -> dict:
    try:
        return await _post(session, "/get_ticker", {"instrument_name": instrument})
    except Exception as exc:  # noqa: BLE001
        logger.warning("theta_tape perp failed: %s", exc)
        return {}


async def _instruments(session: aiohttp.ClientSession, currency: str) -> list[dict]:
    try:
        data = await _post(
            session,
            "/get_instruments",
            {"currency": currency, "expired": False, "instrument_type": "option"},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("instruments") or data.get("result") or []
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("theta_tape instruments failed: %s", exc)
        return []


async def _tickers(session: aiohttp.ClientSession, currency: str, expiry: str) -> list[dict]:
    try:
        data = await _post(
            session,
            "/get_tickers",
            {
                "currency": currency,
                "expired": False,
                "instrument_type": "option",
                "expiry_date": expiry,
            },
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            raw = data.get("tickers")
            if isinstance(raw, dict):
                out = []
                for name, row in raw.items():
                    if not isinstance(row, dict):
                        continue
                    item = dict(row)
                    item.setdefault("instrument_name", name)
                    out.append(item)
                return out
            if isinstance(raw, list):
                return raw
            if isinstance(data.get("result"), list):
                return data["result"]
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("theta_tape tickers %s failed: %s", expiry, exc)
        return []


def _expiry_yyyymmdd(inst: dict) -> str | None:
    details = inst.get("option_details") or {}
    exp = details.get("expiry")
    if exp is None:
        return None
    try:
        ts = float(exp)
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError):
        return None


def _is_call(inst: dict) -> bool:
    details = inst.get("option_details") or {}
    kind = str(details.get("option_type") or inst.get("option_type") or "").lower()
    name = str(inst.get("instrument_name") or "")
    return kind in {"c", "call"} or name.endswith("-C") or name.endswith("-CALL")


def _strike(inst: dict) -> float | None:
    details = inst.get("option_details") or {}
    raw = details.get("strike") or inst.get("strike")
    if raw is None:
        return strike_from_name(str(inst.get("instrument_name") or ""))
    try:
        val = float(str(raw).replace("_", "."))
    except (TypeError, ValueError):
        return strike_from_name(str(inst.get("instrument_name") or ""))
    return val if val > 0 else None


def _mark(ticker: dict) -> float | None:
    pricing = ticker.get("option_pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    for raw in (
        ticker.get("M"),
        ticker.get("mark_price"),
        ticker.get("mark"),
        pricing.get("m"),
        ticker.get("b"),
        ticker.get("best_bid_price"),
    ):
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val >= 0:
            return val
    return None


def _pick_otm_call(tickers: list[dict], spot: float, otm_min_pct: float) -> dict | None:
    floor = spot * (1.0 + otm_min_pct / 100.0)
    scored: list[tuple[float, dict]] = []
    for t in tickers:
        if not _is_call(t):
            continue
        k = _strike(t)
        if k is None or k < floor:
            continue
        scored.append((k, t))
    if not scored:
        return None
    scored.sort(key=lambda row: row[0])
    return scored[0][1]


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return a one-page blotter. Never a fill."""
    del context
    async with aiohttp.ClientSession() as session:
        perp, instruments = await asyncio.gather(
            _perp(session, config.perp_instrument),
            _instruments(session, config.currency),
        )

        spot = 0.0
        for key in ("index_price", "mark_price", "last_price"):
            try:
                spot = float(perp.get(key) or 0)
            except (TypeError, ValueError):
                spot = 0.0
            if spot > 0:
                break

        funding = 0.0
        details = perp.get("perp_details") or {}
        try:
            funding = float(details.get("funding_rate") or perp.get("funding_rate") or 0)
        except (TypeError, ValueError):
            funding = 0.0

        today = datetime.now(tz=timezone.utc)
        expiries: list[str] = []
        seen: set[str] = set()
        for inst in instruments:
            if not inst.get("is_active", True):
                continue
            exp = _expiry_yyyymmdd(inst)
            if not exp or exp in seen:
                continue
            try:
                dte = (datetime.strptime(exp, "%Y%m%d").replace(tzinfo=timezone.utc) - today).days
            except ValueError:
                continue
            if 0 <= dte <= config.max_dte:
                seen.add(exp)
                expiries.append(exp)
        expiries.sort()

        pick = None
        pick_exp = None
        if spot > 0:
            for exp in expiries:
                tickers = await _tickers(session, config.currency, exp)
                cand = _pick_otm_call(tickers, spot, config.otm_min_pct)
                if cand is not None:
                    pick = cand
                    pick_exp = exp
                    break

    premium = call_premium_pct(_mark(pick) if pick else None, spot if spot > 0 else None)
    gate = tape_gate(premium, config.write_min_pct)
    name = (pick or {}).get("instrument_name") or "—"
    strike = _strike(pick) if pick else None

    lines = [
        "THETA tape — public board only. No order.",
        f"Spot {spot:.4f}  funding {funding * 100:.4f}%/h  gate {gate}",
        f"Week {pick_exp or 'none'}  call {name}  strike {strike if strike else '—'}",
        f"Premium {premium:.3f}% of spot" if premium is not None else "Premium none — SIT",
        f"WRITE floor {config.write_min_pct:.2f}%   SIT = wait, funding still prints",
    ]
    if gate == WRITE:
        lines.append(f"WRITE {name} — ticket is a separate clerk. Do not invent a fill.")
    else:
        lines.append("SIT — do not write.")
    text = "\n".join(lines)
    rid = await _math.save_report(
        "THETA tape",
        "theta_tape",
        [
            ("Gate", gate, None, "up" if gate == WRITE else "neutral"),
            ("Spot", f"{spot:.4f}" if spot else "—"),
            ("Funding", f"{funding * 100:.4f}%/h"),
            ("Premium", f"{premium:.3f}%" if premium is not None else "none"),
        ],
        "Public board only. No order. SIT waits; funding still prints. WRITE is a separate ticket.",
        rows=[
            {
                "Call": name,
                "Expiry": pick_exp or "none",
                "Strike": f"{strike:.2f}" if isinstance(strike, (int, float)) else "—",
                "Premium %": f"{premium:.3f}" if premium is not None else "—",
                "Floor %": f"{config.write_min_pct:.2f}",
                "Gate": gate,
            }
        ],
        columns=["Call", "Expiry", "Strike", "Premium %", "Floor %", "Gate"],
        section="01 / BOARD",
        section_note="Nearest OTM XRP call inside the DTE window.",
    )
    if rid:
        text = f"{text}\n\n📊 Report: {rid}"
    return text
