# THETA

**Hodl the coins. Hedge the dump. Earn when Derive pays.**

Principal carry desk on **Derive** — not a forecast bot.  
Park wrapped XRP (FXRP) as margin, short a matching **XRP-USDC** perpetual so a dump cannot empty the wallet, and take the **hourly funding** print while that hedge is on. When the options tape says the week is expensive, optionally sell **one** covered call. When the week is cheap, **SIT**. Funding still prints either way.

One book. One underlying. Two tickets at most.

| | |
|---|---|
| **Category** | Principal carry · hourly funding · options-aware wing |
| **House** | Derive only (`derive_perpetual` + isolated options ticket) |
| **Pair** | XRP-USDC |
| **Cadence** | Condor tick every **300s** |
| **Race envelope** | **$800** (half wallet working, half sleeve) |

---

## Why it exists

Idle XRP earns nothing. Posted FXRP on Derive can collect a rate **without selling the coins**. A matching **1×** perpetual short covers the dump. A small pump stop (**+4%**) takes the short off so the pile can run. If shorts start paying, the desk flattens and sits.

Three holder verbs (same as the deck cover):

| Verb | Duty |
|---|---|
| **HODL / PARK** | FXRP stays posted as bank **and** margin. Never sell the pile for cash. |
| **HEDGE** | Matching XRP perpetual short. Dump stays hedged. Pump cut at +4% so the pile can run. |
| **EARN / COLLECT** | Hourly funding is the paycheck. One real call only on **WRITE**. |

THETA’s edge on Derive: **one wrapped asset (FXRP) is simultaneously the hold and the margin** — so the spot+perp carry needs no second pile, and the same book can also sell a call.

---

## Funding the desk — wrapping XRP into FXRP

THETA's margin is **FXRP**, wrapped XRP on Flare. Everything the desk does starts here, so the on-ramp ships with the agent rather than living in someone's shell history.

**Working code:** [`tools/fxrp_onramp/`](tools/fxrp_onramp/) — dry-run by default, key-free, resolves the Core Vault and every fee live from the Flare registry.

```
XRPL XRP  ──Payment + 32-byte memo──▶  FAssets Core Vault
                                              │
                                              ▼
                                    FXRP on Flare (chain 14)
                                              │
                                     Derive UI: Deposit → Flare → FXRP
                                              │
                                              ▼
                              THETA subaccount margin  ──▶  theta_park
```

### How to do it

```bash
cd tools/fxrp_onramp && npm install
npm run preflight                                    # read-only: vault, token, fees
cp .env.example .env                                 # fill in recipient + amount
node mint_fxrp.mjs --recipient 0x<flare-eoa> --amount 30            # dry run
node mint_fxrp.mjs --recipient 0x<flare-eoa> --amount 30 --broadcast
```

Then move it onto Derive: **Derive → Deposit → Flare → FXRP**.

### What the mint costs

The XRPL payment is `net + max(net × feeBIPS / 10000, minimumFee) + executorFee`. Read live on mainnet:

| Setting | Value |
|---|---|
| `getDirectMintingFeeBIPS` | 10 (0.10%) |
| `getDirectMintingMinimumFeeUBA` | 0.1 XRP |
| `getDirectMintingExecutorFeeUBA` | 0.2 XRP |

The minimum binds at small sizes, so converting **30 XRP costs 30.30 XRP** — not 30.03. `npm run preflight` always prints current values; nothing is hardcoded.

### Two things that will cost you money

- **Mint only to a Flare EOA whose private key you hold.** The mint recipient is not the same thing as a Derive address. Minting to a contract-wallet address strands the FXRP — it is a contract on Derive (chain 957) and a *keyless* EOA on Flare (chain 14), so no session key can ever spend it. `mint_fxrp.mjs` refuses one known address of exactly this kind.
- **The Derive hop is manual, and cannot be automated.** Flare is chain **14**, Derive is chain **957**, and FXRP is a different contract on each. Derive's REST `/private/deposit` assumes the asset is *already* on 957 — it does not bridge, and there is no supported programmatic bridge. This is the one manual step in THETA's funding path, and the reason `theta_park` cannot self-fund.

The mint is also not instant: `tesSUCCESS` on XRPL means the payment landed, then Flare executors finalise it. Poll the FXRP balance; do not re-send.

---


---

## Architecture

Four clerk scripts print numbers. The Condor tick signs **one** line. Hummingbot fills the perpetual. The Hummingbot connector has **no** options path — a call is sold only through `theta_option_ticket`, and only when that ticket is armed live.

```
theta_park           → post FXRP as margin; leave the USDC sleeve free
theta_tape           → SIT or WRITE + this hour’s funding (public board)
theta_margin         → free-margin cap for the short; never full-wallet size
theta_option_ticket  → dry-run by default; live fill only when armed
        ↓
YOU (Condor tick)    → hold / cut / RELOAD / flatten / WRITE → journal
        ↓
Hummingbot           → position_executor on derive_perpetual (the short only)
```

Routines never place exchange orders for the perp. Dry-run option premium is **not** P&L.

```
agents/theta/
  AGENT.md                              identity + risk philosophy
  strategies/theta_funding_desk/        tick playbook (sizes, barriers, exits)
    strategy.md
  routines/
    _theta_math.py                      pure sizing + pump/dump P&L
    _theta_derive.py                    THETA-only signed Derive REST (options)
    theta_park.py                       post FXRP / keep sleeve
    theta_tape.py                       public board → SIT | WRITE
    theta_margin.py                     free-margin room + allowed short
    theta_option_ticket.py              covered-call wing (armored)
  tests/
    test_theta_pure.py                  math + ticket gates first
    validate_agent.py                   loader / strategy shape
    live_tape.py                        public board, no orders
    dry_run_tick.py                     dry tick walk
```

---

## Decision cycle (every ~5 minutes)

| Step | Source | Decision |
|---:|---|---|
| 0 · Park | `theta_park` | Post FXRP if missing; leave USDC sleeve free (`park_dry_run` until flipped) |
| 1 · Tape | `theta_tape` | **SIT** or **WRITE** · hour funding · nearest week call. Dead board → SIT. Never invent a strike. |
| 2 · Room | `theta_margin` | Leftover IM · allowed short (free-margin cap) · TIGHT or not. TIGHT → no add. |
| 3 · Book | portfolio | Net XRP-USDC perp. Open only if \|net\| ≥ $1. |
| 4 · Hedge | Condor tick | open / leave / CUT / RELOAD / flatten |
| 5 · Wing | `theta_option_ticket` | Default SIT. WRITE only if tape=WRITE **and** hedge on **and** armed. |
| 6 · Journal | tick | tape · funding %/h · FXRP · short · sleeve · action · premium filled? |

### Hedge rails

- No short, room fine, funding pays shorts → open short to the **free-margin cap** (not a blind `$400` ticket), with barriers.
- Short on and XRP dumped → **leave it**. Do not take profit on the dump.
- Stopped out on **+4%** pump → journal **CUT** (−$16 on a full $400 short). **RELOAD** if price is back inside **+2%** of last entry.
- Funding flipped (you pay) → **flatten**. Sit. Re-open only when the tape pays shorts again.
- Never open a second short on the same pair.

### Wing rails

- Default is **SIT**.
- Call the ticket only when: tape printed **WRITE** · hedge is on · `theta.option_live = yes` **or** still dry-run.
- Dry-run → journal would-be strike · **$0** premium in P&L.
- Live fill → journal board vs fill. Buy back only if the same routine later says the wing turned.
- Derive option minimum: **100** contracts.
- Same-day expiry caveat: don’t leave a call resting past cutoff — Derive auto-expires it; the wing reloads on the next WRITE tick.

---

## Capital map ($800 race)

| Line | Size | Duty |
|---|---|---|
| FXRP posted | **$400** | Bank + margin. Coins stay. Half the wallet. |
| USDC sleeve | **$400** | Liquidation room. Never inventory. Half the wallet. |
| XRP perpetual short | free-margin capped (pile-cap $400) | Hedge. Live size is leftover IM after FXRP locks its own margin — often well below $400. |
| Optional call | 0 or 1 | Only on WRITE, only via isolated ticket. |

`max_drawdown_pct: 8` **scales** the desk. It is **not** the short’s stop.

| Drawdown | Short size |
|---|---|
| 0–4% | full allowed short |
| 4–8% | half |
| >8% | $0 new — hold or flatten if funding flipped |

Smoke test ($60): $30 / $30 / $30 — pass `wallet_usd=60` to clerks.

### Stress (teaching numbers, $400 pile / $400 short)

| Path | What happens | Book |
|---|---|---|
| Pump +20% | Short cut at +4% (−$16); pile keeps running | net ≈ **+$65** (with ~$1 funding) |
| Dump −20% | Hedge stays; short gain cancels FXRP drop | net ≈ **flat** (+ funding) |

---

## Markets

| Item | Value |
|---|---|
| House | Derive |
| Connector | `derive_perpetual` |
| Pair | **XRP-USDC** |
| Margin | Posted FXRP + USDC sleeve |
| Style | Parked coins · matching short · hourly funding · optional call |
| Perp execution | Hummingbot `position_executor` |
| Options | Isolated `theta_option_ticket` (THETA-only signed REST) |
| Validated | Pure tests + loader + public tape (live Derive board, no orders). The perp hedge and the covered call have been placed as resting orders on a dedicated subaccount and closed. **PARK is manual** — wrapping USDC→FXRP is not automated, so FXRP must be posted before the desk can hedge against it. |

**Hard no’s**

- Do not invent another house or another underlying.
- Do not write a call through Hummingbot.
- Do not short the full wallet.
- Do not count dry-run premium as P&L.

**Open / keep the hedge only when all hold:** funding pays shorts · margin not TIGHT · one hedge (or flat) · drawdown allows size.

---

## Key parameters

From `strategies/theta_funding_desk/strategy.md`:

| Control | Value |
|---|---|
| `frequency_sec` | 300 |
| `total_amount_quote` | 800 |
| FXRP / sleeve / pile-cap | **400 / 400 / 400** |
| Live short | `theta_margin.allowed_short` (free-margin) |
| `max_position_size_quote` | 400 |
| `max_open_executors` | 1 |
| `max_leverage` | 1 |
| `max_drawdown_pct` | 8 (scale) |
| Barriers | SL **0.04** · TP 0.50 · time 172800 · no trail · `open_order_type: 2` |
| RELOAD band | inside +2% of last entry |
| Pump cut | +4% |
| `park_auto` / `park_dry_run` | true / true until flipped |

Executor size = free-margin cap from the margin clerk (not `wallet/2`):

```text
# total_amount_quote (800) is the RACE CEILING, not this account's balance.
# Read the live quote balance first, or the clerk sizes a short the account
# cannot margin and Derive refuses it with INSUFFICIENT_BALANCE.
wallet = get_portfolio_overview()
margin = run(theta_margin, config={"wallet_usd": wallet})
short_quote = margin["allowed_short"]

# create_position_executor(          # flat kwargs — no triple_barrier_config
#   connector_name="derive_perpetual", trading_pair="XRP-USDC",
#   side=2, amount=short_quote / entry_price,   # amount is BASE XRP
#   leverage=1, stop_loss=0.04, take_profit=0.50,
#   time_limit=172800, open_order_type=2,
# )
```

---

## Desk states

| Code | Meaning |
|---|---|
| SIT | Wing closed; funding may still print |
| WRITE | Tape expensive — ticket only if armed |
| HEDGED | Short on (free-margin sized) |
| CUT | +4% pump stopped the short |
| RELOAD | Snap-back inside +2% — re-open short |
| FLAT | Funding flip or no room |
| TIGHT | Do not add size |
| DRY_RUN | Would-be call, no fill |

---

## Run checks

From a Condor checkout with this tree at `agents/theta/`:

```bash
cd /path/to/condor
uv run python agents/theta/tests/test_theta_pure.py
uv run python agents/theta/tests/validate_agent.py
uv run python agents/theta/tests/live_tape.py      # public board only — no orders
```

**Dependencies:** the perpetual hedge runs entirely through Hummingbot's
`derive_perpetual` connector and needs nothing extra. Only the **options /
PARK** path signs Derive actions locally, and it needs the Ethereum stack:

```bash
pip install -r requirements.txt   # eth-account, eth-utils, eth-abi
```

Without it those two routines raise an explanatory error; the hedge is
unaffected.

**Arm the wing (live call write):** set session note `theta.option_live` to exactly `yes` (or env `THETA_OPTION_LIVE=yes`). Leave dry-run until then.

**Do not ship:** `secrets/`, wallets, `.env`, sessions, `__pycache__`.

---

## What this is not

Not an XRPL market maker. Not a multi-venue inventory shield. Not a CEX hybrid spot+perp MM.  
**One underlying (XRP). One house (Derive). Parked FXRP as margin.**
