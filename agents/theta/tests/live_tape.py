"""Read-only live check: public Derive board + margin clerk. Places NO orders."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
os.chdir(REPO)


class _Ctx:
    _chat_id = 0
    user_data: dict = {}


async def main() -> int:
    from routines.base import discover_routines_from_path

    found = discover_routines_from_path(REPO / "agents/theta/routines", agent_slug="theta")
    tape = found["theta_tape"]
    margin = found["theta_margin"]
    ticket = found["theta_option_ticket"]

    tape_out = await tape.run_fn(tape.config_class(), _Ctx())
    print("=== THETA TAPE (live, public, no order) ===")
    print(tape_out)
    print()

    margin_out = await margin.run_fn(margin.config_class(), _Ctx())
    print("=== THETA MARGIN ===")
    print(margin_out)
    print()

    sit = ticket.config_class(gate="SIT", instrument="XRP-DEAD-C", limit_price=0.01)
    write_dry = ticket.config_class(
        gate="WRITE",
        instrument="XRP-DEAD-C",
        amount=100,          # Derive option minimum — without it the ticket
        limit_price=0.01,    # is refused for a zero size before the dry-run branch
        dry_run=True,
    )
    sit_out = await ticket.run_fn(sit, _Ctx())
    dry_out = await ticket.run_fn(write_dry, _Ctx())
    print("=== THETA TICKET (must refuse SIT, dry-run WRITE) ===")
    print(sit_out)
    print("---")
    print(dry_out)

    bad = False
    if "No order" not in tape_out:
        print("[FAIL] tape must say it placed no order")
        bad = True
    if "Allowed short" not in margin_out:
        print("[FAIL] margin missing allowed short")
        bad = True
    if "REFUSED SIT" not in sit_out:
        print("[FAIL] SIT must refuse a sell")
        bad = True
    if not dry_out.startswith("DRY-RUN"):
        print("[FAIL] WRITE without live arm must dry-run")
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
