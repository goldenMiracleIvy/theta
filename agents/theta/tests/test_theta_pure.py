"""Pure-function tests for the THETA desk. No network. No orders."""

from __future__ import annotations

import sys
from pathlib import Path

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(ROUTINES))

from _theta_math import (  # noqa: E402
    SIT,
    WRITE,
    allowed_short,
    book_is_tight,
    call_premium_pct,
    drawdown_short,
    dump_book,
    pump_book,
    quiet_funding,
    split_wallet,
    tape_gate,
)
from theta_option_ticket import Config, ticket_verdict  # noqa: E402


def test_tape_gate():
    assert tape_gate(None, 0.80) == SIT
    assert tape_gate(0.35, 0.80) == SIT
    assert tape_gate(0.80, 0.80) == WRITE
    assert tape_gate(1.20, 0.80) == WRITE
    assert tape_gate(1.20, 0.0) == SIT
    assert call_premium_pct(None, 1.0) is None
    assert call_premium_pct(0.008, 1.0) == 0.8
    from _theta_math import strike_from_name

    assert strike_from_name("XRP-20260821-1_05-C") == 1.05
    assert strike_from_name("XRP-20260821-0_9-P") == 0.9
    assert strike_from_name("bad") is None


def test_half_wallet_short_is_400():
    assert allowed_short(400, 400, 400) == 400
    assert allowed_short(800, 0, 400) == 400
    assert allowed_short(0, 400, 400) == 0 or allowed_short(0, 400, 400) <= 400
    assert allowed_short(400, 400, 0) == 0
    assert allowed_short(-10, 400, 400) == 0


def test_drawdown_scales_not_stops():
    assert drawdown_short(400, 0.02) == 400
    assert drawdown_short(400, 0.05) == 200
    assert drawdown_short(400, 0.09) == 0


def test_pump_and_dump_match_deck():
    pump = pump_book(400, 400, 0.20, 0.04, funding=1.0)
    assert pump["cut"] is True
    assert pump["fxrp"] == 80
    assert pump["short"] == -16
    assert pump["net"] == 65
    dump = dump_book(400, 400, 0.20, funding=2.0)
    assert dump["cut"] is False
    assert dump["fxrp"] == -80
    assert dump["short"] == 80
    assert dump["net"] == 2


def test_quiet_funding_is_48h_not_per_hour():
    # 400 * 0.0001/h * 48h = 1.92 ≈ teaching $2
    assert abs(quiet_funding(400, 0.0001, 48) - 1.92) < 1e-9
    assert quiet_funding(400, 0.0001, 1) == 0.04


def test_sleeve_keeps_book_loose():
    assert book_is_tight(400, 400, 400) is False
    assert book_is_tight(50, 0, 400, min_excess=50) is True


def test_ticket_never_fills_on_sit_or_dry_run():
    sit = Config(gate="SIT", instrument="XRP-20260828-0_70-C", amount=100, limit_price=0.01)
    assert "REFUSED SIT" in ticket_verdict(sit)
    dry = Config(gate="WRITE", instrument="XRP-20260828-0_70-C", amount=100, limit_price=0.01, dry_run=True)
    out = ticket_verdict(dry)
    assert out.startswith("DRY-RUN")
    live = Config(gate="WRITE", instrument="XRP-20260828-0_70-C", amount=100, limit_price=0.01, dry_run=False)
    assert "REFUSED live" in ticket_verdict(live, live_env="")
    # Pure verdict only authorizes; run() owns the wire / signer.
    assert "ARMED" in ticket_verdict(live, live_env="yes")


def test_split_wallet_smoke_and_race():
    assert split_wallet(800) == (400.0, 400.0, 400.0)
    assert split_wallet(60) == (30.0, 30.0, 30.0)
    assert split_wallet(0) == (0.0, 0.0, 0.0)
    fxrp, sleeve, cap = split_wallet(60)
    assert allowed_short(fxrp, sleeve, cap) == 30
    assert book_is_tight(fxrp, sleeve, 30, min_excess=8) is False
    pump = pump_book(30, 30, 0.20, 0.04, funding=0.1)
    assert pump["short"] == -1.2
    assert abs(pump["net"] - 4.9) < 1e-9


if __name__ == "__main__":
    test_tape_gate()
    test_half_wallet_short_is_400()
    test_drawdown_scales_not_stops()
    test_pump_and_dump_match_deck()
    test_quiet_funding_is_48h_not_per_hour()
    test_sleeve_keeps_book_loose()
    test_ticket_never_fills_on_sit_or_dry_run()
    test_split_wallet_smoke_and_race()
    print("theta pure tests OK")
