---
name: THETA Funding Desk
description: >-
  Parks half the wallet as FXRP, shorts a matching XRP perpetual on Derive,
  collects hourly funding, and reads the options tape every tick. Writes one
  call only when the isolated ticket is armed and the tape says WRITE.
agent_key: null
skills: []
default_config:
  frequency_sec: 300
  execution_mode: loop
  server_name: local
  total_amount_quote: 800
  risk_limits:
    max_position_size_quote: 400
    max_open_executors: 1
    max_drawdown_pct: 8
    max_leverage: 1
    require_triple_barrier: true
    require_trailing_stop: false
default_trading_context: 'Trade XRP-USDC on derive_perpetual. Post FXRP as margin. Leave half the wallet as USDC sleeve.'
park_auto: true
park_dry_run: true
created_by: 0
created_at: '2026-08-19T00:00:00+00:00'
---

# THETA Funding Desk

> Identity and risk philosophy live in `AGENT.md`. This file is the tick:
> connector, sizes, barriers, exits. Follow it exactly. The risk gate
> enforces the caps, so your job is to size honestly inside them.

Venue is `derive_perpetual`. Pair is `XRP-USDC`. Do not invent another
house or another underlying. kHYPE is not this book.

## Locked wallet (one $800 race)

| Line | Size | Duty |
|---|---|---|
| FXRP posted | **$400** | Bank + margin. Coins stay. |
| USDC sleeve | **$400** | Liquidation room. Never inventory. |
| XRP perpetual short | **$400** | Hedge. Sized to the pile, not the whole wallet. |
| Optional call | 0 or 1 | Only on WRITE, and only via `theta_option_ticket`. |

Never short the full wallet. Haircut on FXRP makes that tight.

## Smoke test ($60)

Not the race. Half still works, half is still the sleeve.

| Line | Size |
|---|---|
| Wallet | **$60** |
| FXRP posted | **$30** |
| USDC sleeve | **$30** |
| XRP short | **$30** |

Pass `wallet_usd=60` to `theta_margin`. Do **not** post $60 FXRP and short $60.
Pump cut at +4% is −$1.20 on the $30 short. Quiet funding is pennies.

Race numbers below stay $400. Swap them for the $30 row only when the
session is a smoke test.

## Barriers (this short only)

Account `max_drawdown_pct: 8` is **scaling**, not the position stop.

```
create_position_executor(
  connector_name="derive_perpetual",
  trading_pair="XRP-USDC",
  side=2,                  # 2 = SHORT
  amount=<base XRP>,       # NOT quote — amount is base units
  stop_loss=0.04,          # +4% pump cut
  take_profit=0.50,        # wide: a dump must NOT close the hedge
  time_limit=172800,       # 48h race window
  open_order_type=2,       # LIMIT_MAKER
)
```

These are **flat keyword arguments**. There is no `triple_barrier_config`
dict and no `executor_type` argument — passing either raises `TypeError`.

- **stop_loss 4%** — XRP **up** 4% covers the short (−$16 on $400). The
  pile then runs. This is the only cut.
- **take_profit 50%** — wide on purpose. A −20% dump must **not** close
  the hedge. Do not use 4% TP. Do not attach a trailing stop.
- **time_limit 48h** — the race window. Re-open next tick if the desk
  still wants the hedge and funding has not flipped.

No trail. If you ever do arm one, the parameter names are
`trailing_stop_activation_price` and `trailing_stop_trailing_delta` — there
is no `activation_price` / `trailing_delta` argument on this tool. Prefer no
trail: a trail can close the hedge on a dump, which is the whole point of
holding it.

## Drawdown response (not a dead stop)

| Drawdown | Short size | Behavior |
|---|---|---|
| 0–4% | $400 | Full hedge |
| 4–8% | $200 | Half hedge. Keep the sleeve. |
| >8% | $0 new | Do not add. Hold or flatten if funding flipped. Stay alive. |

Never increase size while in drawdown.

## Tick sequence

**0 — Park (setup, automated).** If `park_auto` is on, run
`manage_routines(action="run", routine="theta_park")` first. It reads
Derive balances: if FXRP is already posted it does nothing; otherwise it
converts spare USDC (above the sleeve) to XRP and wraps it to FXRP, posted
as perp margin. With `park_dry_run: true` it reports the plan but places no
convert/wrap — flip it to `false` to execute. This is the only spot step;
once FXRP is posted the loop below never touches spot again.

**1 — Tape.** `manage_routines(action="run", routine="theta_tape")`.
Expect `SIT` or `WRITE`, the printed funding rate, and the nearest week
call the clerk marked. If the routine says the board is dead, treat as
**SIT**. Do not invent a strike.

**2 — Room.** `manage_routines(action="run", routine="theta_margin")`.
Expect leftover IM, the allowed short notional (never above $400), and
whether the book is tight. If it says **TIGHT**, do not add size.

**3 — Portfolio.** `get_portfolio_overview(connector=derive_perpetual)`.
Judge the book by **net** XRP-USDC perp, not leftover dust legs.
A pair is open only if |net notional| ≥ $1.

**4 — Manage the hedge.**

- No short and room is fine and funding pays shorts → open the $400
  short (or the drawdown-scaled size) with the barrier block below.
- Short is on and XRP dumped → **leave it**. Do not take profit.
- Short stopped out on a +4% pump → journal the cut. **RELOAD** if
  price has come back inside +2% of the last entry mark. Do not sit
  naked on a snap-back dump.
- Funding flipped (you pay) → flatten the short. Sit. Do not re-open
  until the tape prints a positive rate for shorts again.
- Do not open a second short on the same pair.

**5 — Wing.** Default **SIT**. Call `theta_option_ticket` only when
**all** of these hold:

- tape printed **WRITE**
- hedge is on
- the ticket routine is still in `dry_run` **or** the session note
  `theta.option_live` is exactly `yes`

If the ticket is dry-run, journal the would-be strike and do **not**
count premium as P&L. If it reports a live fill, journal board $X vs
fill $Y. Buy back only if the same routine later says the wing turned.

**6 — Journal.** One entry per tick: tape gate, funding %/h, FXRP
posted, short net, sleeve, action (HOLD / CUT / RELOAD / FLATTEN /
SIT / WRITE), and whether any premium was actually filled.

## Open the hedge

Fetch the schema first, then create. Quote notional is required:

**Size the short to the `theta_margin` routine's "Allowed short" (the
free-margin cap), NOT `total_amount_quote / 2`.** On Derive PM cross-margin
the posted FXRP already locks its own initial margin, so only the account's
FREE margin can back a new short. The margin routine reads live collateral
and returns the real ceiling (typically ~$65–70 here, far below the $125 a
naive `wallet/2` split would suggest). A short above that ceiling is
rejected by Derive with `INSUFFICIENT_BALANCE`. Use the routine's number.

Read the **live** wallet first — never assume `total_amount_quote`. The
`800` above is the race ceiling, not this account's balance:

```
wallet = get_portfolio_overview()          # live quote balance on the venue
margin = run(theta_margin, config={"wallet_usd": wallet})
short_quote = margin["allowed_short"]      # the real free-margin ceiling
```

Then open the short. Barriers are **flat keyword arguments** — there is no
`executor_type` argument and no `triple_barrier_config` dict:

```python
create_position_executor(
  connector_name="derive_perpetual",
  trading_pair="XRP-USDC",
  side=2,                        # 2 = SHORT
  amount=short_quote / entry_price,   # BASE XRP, not quote
  entry_price=entry_price,
  leverage=1,
  stop_loss=0.04,                # +4% pump cut
  take_profit=0.50,
  time_limit=172800,
  open_order_type=2,             # 2 = LIMIT_MAKER
)
```

`amount` is base XRP. `total_amount_quote` is **not** a parameter of this tool —
size it yourself from the margin clerk's number, and never above it: Derive
rejects an oversized short with `INSUFFICIENT_BALANCE`.

## Do not

- Do not sell FXRP for USDC to “simplify” margin.
- Do not mix another underlying into this book.
- Do not write a call through Hummingbot. It has no options path.
- Do not retry a killed sequence.
- Do not treat 8% drawdown as the short’s stop-loss.
