# XRP → FXRP on-ramp

THETA's margin is **FXRP** — wrapped XRP on Flare. This tool wraps XRP into FXRP
using **FAssets direct minting**, so the desk can be funded from a plain XRPL
wallet without touching a centralised exchange.

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

## Install

```bash
cd tools/fxrp_onramp
npm install
```

## Step 0 — pre-flight (read-only, no key needed)

Resolves everything live from the Flare registry and shows what the mint costs.
Run this before committing any XRP.

```bash
npm run preflight
```

It prints the Core Vault address you must pay, the FXRP token, and the fee
schedule. Nothing is hardcoded — a stale vault address sends XRP nowhere.

## Step 1 — mint (dry run by default)

```bash
cp .env.example .env      # then fill it in
node mint_fxrp.mjs --recipient 0x<your-flare-eoa> --amount 30
```

This prints the resolved vault, the memo, and the exact XRPL payment. **It does
not sign or send anything.** Add `--broadcast` when the numbers are right:

```bash
node mint_fxrp.mjs --recipient 0x<your-flare-eoa> --amount 30 --broadcast
```

`--broadcast` needs `XRPL_SEED` and `XRPL_RPC_URL` in the environment. The seed
is read from env only — never a file, never an argument, never logged.

## Step 2 — the Derive hop (manual, by design)

**The mint lands FXRP on Flare (chain 14). Derive is chain 957.** These are
different chains and the FXRP token is a different contract on each.

Derive's REST `/private/deposit` **assumes the asset is already on chain 957** —
it does not bridge. There is no supported programmatic bridge, so this hop is a
UI action:

> Derive → Deposit → **Flare** → **FXRP**

Once the FXRP is on your Derive subaccount, `theta_park` can post it as margin
and the hedge can run. This is the one manual step in THETA's funding path, and
it is why `theta_park` cannot self-fund.

## Fees (read live, never hardcoded)

The XRPL payment is:

```
payment = net + max(net × feeBIPS / 10000, minimumFee) + executorFee
```

Mainnet values observed:

| Setting | Value |
|---|---|
| `getDirectMintingFeeBIPS` | 10 (0.10%) |
| `getDirectMintingMinimumFeeUBA` | 0.1 XRP |
| `getDirectMintingExecutorFeeUBA` | 0.2 XRP |

The minimum binds at small sizes, so converting 30 XRP costs **30.30 XRP** —
not 30.03. `npm run preflight` always prints the current numbers.

## The direct-minting memo

32 bytes, hex-encoded, attached as `MemoData` on the XRPL Payment:

```
[8-byte prefix "4642505266410018"] [4-byte zero padding "00000000"] [20-byte recipient, no 0x, lowercase]
```

The recipient must be a **Flare EOA whose private key you hold.** Minting to a
contract-wallet address strands the FXRP — it is a contract on Derive 957 and a
keyless EOA on Flare 14, so no session key can spend it. `mint_fxrp.mjs` refuses
a known address of exactly this kind.

## Files

| File | Purpose |
|---|---|
| `resolve_core_vault.mjs` | read-only pre-flight; resolves vault, token, fees |
| `mint_fxrp.mjs` | the mint; dry-run default, `--broadcast` to send |
| `.env.example` | env template — copy to `.env` (gitignored) |

## Notes

- The mint is **not instant**. `tesSUCCESS` on XRPL means the payment landed;
  Flare executors finalise the mint afterwards. Poll the FXRP balance — do not
  re-send.
- XRPL `Amount` for XRP is a string in XRP (not drops) — the script formats it.
- `FLARE_NETWORK=coston2` points everything at testnet for a rehearsal.
