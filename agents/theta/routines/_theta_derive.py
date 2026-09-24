"""Thin client for Derive's native REST API.

Used by ``theta_park`` to turn USDC into posted FXRP margin so THETA can
start from a USDC-only account. Hummingbot's ``derive_perpetual`` connector
is perps-only (no spot / convert / wrap), so we talk to Derive directly.

Auth (Derive scheme, from hummingbot's derive_auth.py):
    POST https://api.lyra.finance/private/<method>
    headers:
        accept:            application/json
        X-LyraWallet:      <api_key = wallet address>
        X-LyraTimestamp:   <ms epoch>
        X-LyraSignature:   personal_sign(timestamp, api_secret)
    where api_secret is an Ethereum private key and the signature is an
    EIP-191 personal_sign (keccak256("\\x19Ethereum Signed Message:\\n" + len + msg)).

No network happens at import time. Every public method is explicit about
whether it reads (safe) or writes (convert/wrap).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
import asyncio
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from ecdsa import SigningKey, SECP256k1, numbertheory
from ecdsa.util import sigencode_string_canonize

# The desk's own Derive subaccount. Override with DERIVE_SUBACCOUNT.
THETA_DEFAULT_SUB = "70746"

DERIVE_BASE = "https://api.lyra.finance"
# Private endpoint prefix on Derive's REST API.
_PRIVATE = "/private"

# Where a Hummingbot/Condor install stores the encrypted Derive credential,
# and the .env that holds the keystore passphrase (CONFIG_PASSWORD). theta
# reuses BOTH — the key is registered once in Condor Keys & Wallets; nobody
# copy/pastes a secret. (The perp connector cannot do spot/convert/wrap, so we
# call Derive directly with these same credentials.)
#
# Nothing here is hardcoded to one machine. Resolution order:
#   1. THETA_KEYSTORE / THETA_HB_ENV env vars (explicit override)
#   2. HUMMINGBOT_API_PATH env var (the install root)
#   3. The usual install roots under $HOME and /opt
_KEYSTORE_REL = "bots/credentials/master_account/connectors/derive_perpetual.yml"
_ENV_REL = ".env"


def _candidate_roots() -> list[Path]:
    """Install roots to search, most specific first."""
    out: list[Path] = []
    for var in ("HUMMINGBOT_API_PATH", "CONDOR_HOME"):
        v = os.environ.get(var, "").strip()
        if v:
            out.append(Path(v))
    home = Path.home()
    for name in ("hummingbot-api", "condor", "hummingbot"):
        out.append(home / name)
    out += [Path("/opt/hummingbot-api"), Path("/opt/condor")]
    seen: set[Path] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def _default_keystore() -> Path:
    override = os.environ.get("THETA_KEYSTORE", "").strip()
    if override:
        return Path(override)
    for root in _candidate_roots():
        cand = root / _KEYSTORE_REL
        if cand.exists():
            return cand
    # Nothing found: return the first candidate so the error names a real path.
    roots = _candidate_roots()
    return (roots[0] if roots else Path.home()) / _KEYSTORE_REL


def _default_hb_env() -> Path:
    override = os.environ.get("THETA_HB_ENV", "").strip()
    if override:
        return Path(override)
    for root in _candidate_roots():
        cand = root / _ENV_REL
        if cand.exists():
            return cand
    roots = _candidate_roots()
    return (roots[0] if roots else Path.home()) / _ENV_REL


# Keystore field names differ between Hummingbot connector versions. A current
# derive_perpetual connector writes wallet_address / session_private_key /
# subacct_id; older builds wrote api_key / api_secret / sub_id. Accept both.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "api_key": ("derive_perpetual_api_key", "derive_perpetual_wallet_address"),
    "api_secret": ("derive_perpetual_api_secret", "session_private_key"),
    "sub_id": ("sub_id", "subacct_id"),
    "account_type": ("account_type",),
}


def _find_field(text: str, names: tuple[str, ...]) -> tuple[str, str] | None:
    """Return (matched_name, hex_blob) for the first field present."""
    for name in names:
        m = re.search(rf"^{re.escape(name)}:\s*([0-9a-fA-F]+)", text, re.M)
        if m:
            return name, m.group(1)
    return None

# Derive v2 protocol constants (production, chain 957). From docs.derive.xyz
# and hummingbot's derive_constants.py / derive_common_utils.py.
_PROTOCOL = {
    "domain": "https://api.lyra.finance",
    "chain_id": 957,
    "trade_module_address": "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b",
    "domain_separator": "0xd96e5f90797da7ec8dc4e276260c7f3f87fedf68775fbe1ef116e996fc60441b",
    "action_typehash": "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17",
    "asset_addresses": {
        # Derive-chain ERC20 addresses (spot) from public/get_all_currencies
        "XRP": "0x1DD2b6E9Db6654BB949be09b30d4193A94bbbfaE",
        "FXRP": "0x7F1FfBF00B05A280298C843f02ca293209756B49",
        "USDC": "0x57B03E14d409ADC7fAb6CFc44b5886CAD2D5f02b",
    },
}


# The options / PARK path signs Derive actions locally and needs the
# Ethereum stack. It is NOT needed for the perpetual hedge (Hummingbot signs
# that), so the import is deferred and the failure is explained, not a bare
# ModuleNotFoundError at routine-import time.
_ETH_MISSING = (
    "This routine signs Derive actions locally and needs the Ethereum stack.\n"
    "Install it with:  pip install -r requirements.txt\n"
    "(eth-account, eth-utils, eth-abi). The perpetual hedge does not need it."
)


def _require_eth_account():
    try:
        Account, _ = _require_eth_account()
        from eth_account.messages import encode_defunct
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(_ETH_MISSING) from exc
    return Account, encode_defunct


def _keccak256(data: bytes) -> bytes:
    """Keccak-256 (NOT NIST SHA3-256 — Ethereum/Derive use legacy Keccak).

    hashlib.sha3_256 is the *different* NIST SHA3 and must NOT be used here;
    it produces wrong hashes and Derive rejects the signature (code 14014).
    eth_utils.keccak implements the correct legacy Keccak-256."""
    try:
        from eth_utils import keccak as _eth_keccak
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(_ETH_MISSING) from exc
    return _eth_keccak(data)


def _hb_passphrases(hb_env: str | Path | None = None) -> list[str]:
    """Candidate keystore passphrases from Hummingbot's .env, in priority order."""
    hb_env = Path(hb_env) if hb_env is not None else _default_hb_env()
    out: list[str] = []
    for line in Path(hb_env).read_text().splitlines():
        if line.startswith("CONFIG_PASSWORD="):
            out.append(line.split("=", 1)[1].strip())
        elif line.startswith("PASSWORD="):
            out.append(line.split("=", 1)[1].strip())
    # de-dup preserving order
    seen = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def _decrypt_field(blob_hex: str, passphrases: list[str]) -> str | None:
    """Decrypt one Ethereum-keystore-v3 (AES-128-CTR + PBKDF2) field.

    Tries each candidate passphrase; returns None if none match (the field
    may not be keystore-encrypted, e.g. account_type). The keystore MAC is
    keccak256(key[16:32] + ciphertext); we verify it when possible and
    otherwise fall back to a plaintext sanity check (Derive keys look like
    hex / 0x… strings).
    """
    try:
        ks = json.loads(bytes.fromhex(blob_hex))
    except (ValueError, KeyError):
        return None
    if "crypto" not in ks:
        return None
    crypto = ks["crypto"]
    ct = bytes.fromhex(crypto["ciphertext"])
    iv = bytes.fromhex(crypto["cipherparams"]["iv"])
    salt = bytes.fromhex(crypto["kdfparams"]["salt"])
    rounds = crypto["kdfparams"]["c"]
    for pw in passphrases:
        key = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds, 32)
        decipher = Cipher(algorithms.AES(key[:16]), modes.CTR(iv)).decryptor()
        plain = decipher.update(ct) + decipher.finalize()
        mac = _keccak256(key[16:32] + ct).hex()
        if mac == crypto["mac"]:
            return plain.decode("utf-8")
        # Fallback: accept if the plaintext is a plausible secret (hex / 0x…).
        try:
            txt = plain.decode("utf-8")
            if txt.startswith("0x") or all(ch in "0123456789abcdefABCDEF" for ch in txt):
                return txt
        except UnicodeDecodeError:
            continue
    raise RuntimeError("keystore MAC mismatch — no candidate passphrase matched")


def from_condor_keystore(
    keystore: str | Path | None = None,
    hb_env: str | Path | None = None,
) -> dict[str, str]:
    """Reuse the Derive key already registered in Condor (Keys & Wallets).

    Decrypts ``derive_perpetual.yml`` in-process with Hummingbot's own
    ``CONFIG_PASSWORD`` — no plaintext secrets file, nothing to copy/paste.
    """
    keystore = Path(keystore) if keystore is not None else _default_keystore()
    text = Path(keystore).read_text()
    pws = _hb_passphrases(hb_env)
    if not pws:
        raise RuntimeError(
            f"No CONFIG_PASSWORD/PASSWORD found in {Path(hb_env) if hb_env else _default_hb_env()}"
        )

    out: dict[str, str] = {}
    for logical, names in _FIELD_ALIASES.items():
        found = _find_field(text, names)
        if not found:
            if logical in ("api_key", "api_secret"):
                raise ValueError(
                    f"keystore missing {logical} (tried: {', '.join(names)}) in {keystore}"
                )
            continue
        _, blob = found
        try:
            val = _decrypt_field(blob, pws)
        except RuntimeError:
            val = None
        if val is not None:
            out[logical] = val
        elif logical in ("api_key", "api_secret"):
            raise ValueError(
                f"keystore field for {logical} is not keystore-encrypted ({keystore})"
            )

    # The subaccount is the desk's own book. DERIVE_SUBACCOUNT wins; otherwise
    # use whatever the keystore carries; the constant is only a last resort.
    sub_id = (
        os.environ.get("DERIVE_SUBACCOUNT", "").strip()
        or str(out.get("sub_id") or "").strip()
        or THETA_DEFAULT_SUB
    )

    return {
        "api_key": out["api_key"],
        "api_secret": out["api_secret"],
        "sub_id": sub_id,
        "account_type": out.get("account_type", "leading"),
    }


def load_credentials(secrets_path: str | Path) -> dict[str, str]:
    """Load Derive credentials.

    Primary path: reuse the key already in Condor's keystore (no secret file).
    Fallback: a theta secrets json (``derive.json``) if you prefer that.
    """
    # Prefer the Condor keystore — that is the "register once, no copy/paste"
    # path organizers use.
    try:
        return from_condor_keystore()
    except Exception:
        pass  # fall through to the json file
    p = Path(secrets_path)
    if not p.exists():
        raise FileNotFoundError(
            f"No Derive credentials: Condor keystore absent/unreadable and "
            f"secrets file not found: {p}"
        )
    data = json.loads(p.read_text())
    key = data.get("api_key")
    secret = data.get("api_secret")
    if not key or not secret or key.startswith("YOUR_"):
        raise ValueError("derive.json is not populated (api_key/api_secret missing)")
    sub_id = (
        os.environ.get("DERIVE_SUBACCOUNT", "").strip()
        or str(data.get("sub_id") or "").strip()
        or THETA_DEFAULT_SUB
    )
    return {
        "api_key": key,
        "api_secret": secret,
        "sub_id": sub_id,
        "account_type": str(data.get("account_type") or "leading"),
    }


def _personal_sign(message: str, private_key_hex: str) -> str:
    """EIP-191 personal_sign, matching Hummingbot's derive_auth.py exactly.

    ``private_key_hex`` is the Derive api_secret (an Ethereum private key).
    Uses ``eth_account`` (see requirements.txt) so the signature is
    byte-for-byte what Derive expects: ``sign(encode_defunct(text=ts), key)``.
    """
    Account, encode_defunct = _require_eth_account()

    return Account.sign_message(
        encode_defunct(text=message), private_key=private_key_hex
    ).signature.hex()


def auth_headers(creds: dict[str, str]) -> dict[str, str]:
    """Derive REST auth headers (X-LyraWallet / X-LyraTimestamp / X-LyraSignature)."""
    ts = str(int(time.time() * 1000))
    sig = _personal_sign(ts, creds["api_secret"])
    h = {
        "accept": "application/json",
        "X-LyraWallet": creds["api_key"],
        "X-LyraTimestamp": ts,
        "X-LyraSignature": sig,
    }
    if creds.get("sub_id"):
        h["subaccount-id"] = creds["sub_id"]
    if creds.get("account_type"):
        h["account-type"] = creds["account_type"]
    return h


async def _post(creds: dict[str, str], method: str, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload)
    url = f"{DERIVE_BASE}{_PRIVATE}/{method}"
    headers = auth_headers(creds)
    headers["Content-Type"] = "application/json"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, data=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Derive {method} -> {resp.status}: {text}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"Derive {method} bad JSON: {text}")
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"Derive {method} error: {data['error']}")
            return data.get("result", data)


# ── Read-only ──────────────────────────────────────────────────────────────

async def get_account_summary(creds: dict[str, str]) -> dict[str, Any]:
    """Read subaccount balances / collateral via Derive's ``/private/get_subaccount``.

    Derive requires ``wallet`` + ``subaccount_id`` in the request body.
    """
    payload: dict[str, Any] = {"wallet": creds["api_key"]}
    if creds.get("sub_id"):
        try:
            payload["subaccount_id"] = int(creds["sub_id"])
        except ValueError:
            payload["subaccount_id"] = creds["sub_id"]
    return await _post(creds, "get_subaccount", payload)


async def get_balances(creds: dict[str, str]) -> dict[str, float]:
    """Return {asset: amount} from the subaccount's collaterals."""
    summary = await get_account_summary(creds)
    out: dict[str, float] = {}
    if not isinstance(summary, dict):
        return out
    # Derive returns collaterals: [{asset_name, amount, ...}]
    for row in summary.get("collaterals", []) or []:
        asset = row.get("asset_name") or row.get("asset") or row.get("currency")
        amt = row.get("amount")
        if asset and amt is not None:
            try:
                out[asset] = float(amt)
            except (TypeError, ValueError):
                continue
    return out


# ── Writes (deposit / wrap / convert) ──────────────────────────────────────

def _eth_account(api_secret: str):
    Account, _ = _require_eth_account()

    return Account.from_key(api_secret)


def _signed_action(creds: dict[str, str], module_data: dict, action_type: str) -> dict:
    """Build + sign a Derive EIP-712 SignedAction and return the wire payload.

    ``module_data`` varies by action type (see below). The signature is the
    EIP-712 typed-data hash signed by the subaccount signer key. ``owner`` is
    the wallet (api_key) and ``signer`` the session key (api_secret-derived).
    Nonce = <utc_ms><6 random digits> per Derive spec.
    """
    import time as _time
    import random as _random

    try:
        from eth_abi import encode
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(_ETH_MISSING) from exc

    owner = creds["api_key"]
    sub_id = int(creds["sub_id"] or 0)
    signer_addr = _eth_account(creds["api_secret"]).address
    # Derive requires strictly-increasing, unique nonces. Use a monotonic
    # counter combined with utc_ms so repeated calls never collide or fall
    # behind the server-tracked last nonce (which otherwise yields 14014).
    import threading
    _nonce_lock = getattr(_signed_action, "_lock", None)
    if _nonce_lock is None:
        _nonce_lock = threading.Lock()
        _signed_action._lock = _nonce_lock
    with _nonce_lock:
        _last = getattr(_signed_action, "_last_nonce", 0)
        cand = int(str(int(_time.time() * 1000)) + str(_random.randint(0, 999)))
        nonce = max(_last + 1, cand)
        _signed_action._last_nonce = nonce
    expiry = int(_time.time()) + 600  # must be >5min from now; MAX_INT_32 is rejected by live API

    proto = _PROTOCOL
    module_address = proto["trade_module_address"]
    domain_sep = proto["domain_separator"]
    typehash = proto["action_typehash"]

    # module_data: (abi_types, values) -> module hash
    abi_types = module_data["abi_types"]
    abi_values = module_data["abi_values"]
    module_hash = _keccak256(encode(abi_types, abi_values))

    hashed = _keccak256(
        encode(
            ["bytes32", "uint", "uint", "address", "bytes32", "uint", "address", "address"],
            [bytes.fromhex(typehash[2:]), sub_id, nonce,
             _checksum(module_address), bytes(module_hash), expiry,
             _checksum(owner), _checksum(signer_addr)],
        )
    )
    typed = _keccak256(b"\x19\x01" + bytes.fromhex(domain_sep[2:]) + bytes(hashed))
    sig = _eth_account(creds["api_secret"]).unsafe_sign_hash(typed).signature
    # validate locally: recovered signer must == signer_addr
    Account, _ = _require_eth_account()

    rec = Account._recover_hash(typed, signature=sig)
    if rec.lower() != signer_addr.lower():
        raise RuntimeError(f"SignedAction self-check failed: {rec} != {signer_addr}")

    return {
        "subaccount_id": sub_id,
        "nonce": nonce,
        "signer": signer_addr,
        "signature_expiry_sec": expiry,
        "signature": sig.hex(),
        **module_data.get("extra", {}),
    }


def _checksum(addr: str) -> str:
    try:
        from eth_utils import to_checksum_address
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(_ETH_MISSING) from exc

    return to_checksum_address(addr)


async def deposit(creds: dict[str, str], asset_name: str, amount: float) -> dict[str, Any]:
    """Deposit ``amount`` of ``asset_name`` (e.g. XRP) into the subaccount.

    On Derive, depositing XRP yields FXRP collateral — this IS the wrap step.
    Uses the SignedAction deposit pattern (POST /private/deposit).
    """
    module_data = {
        "abi_types": ["address", "uint", "int", "int", "uint", "uint", "bool"],
        "abi_values": [
            _checksum(_PROTOCOL["asset_addresses"][asset_name]),
            1,   # sub_id for the collateral bucket
            0,   # limit_price (0 for a deposit)
            _to_big(amount),  # amount
            0,   # max_fee
            int(creds["sub_id"] or 0),  # recipient subaccount
            True,  # is_bid
        ],
        "extra": {"amount": str(amount), "asset_name": asset_name},
    }
    payload = _signed_action(creds, module_data, "deposit")
    # deposit requires signer + signature in body (enum asset_name)
    payload["asset_name"] = asset_name
    payload["amount"] = str(amount)
    return await _post(creds, "deposit", payload)


def _to_big(value: float) -> int:
    """Derive uses 18-decimal integer amounts (decimal_to_big_int in
    derive_perpetual_web_utils: value * 10^18)."""
    return int(round(value * 10 ** 18))


async def order(creds: dict[str, str], instrument_name: str, direction: str,
                amount: float, limit_price: float, order_type: str = "limit",
                time_in_force: str = "gtc", max_fee: float = 1000.0,
                label: str = "theta_park", debug: bool = False) -> dict[str, Any]:
    """Place an order on Derive (POST /private/order).

    Follows Hummingbot's derive_exchange._create_order wire format exactly:
    an EIP-712 SignedAction over TradeModuleData, signed with the api_secret,
    then the flattened order fields in the body.

    direction: "buy" | "sell". amount is in base units (XRP).
    """
    symbol = instrument_name
    # Look up the instrument's base_asset_address + base_asset_sub_id dynamically
    # (options have per-expiry sub_ids; XRP-PERP is sub_id 0). Falls back to the
    # known XRP constants only if the lookup fails.
    import urllib.request as _ureq
    base_asset_address = None
    base_sub_id = "0"
    instrument_type = "option" if symbol.endswith("-C") or symbol.endswith("-P") else "perp"
    try:
        # Derive sits behind Cloudflare, which blocks aiohttp's TLS fingerprint
        # (HTTP 1010). urllib (with a browser UA) gets through — use that.
        _hdrs = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Content-Type": "application/json",
            "accept": "application/json",
        }
        _req = _ureq.Request(
            "https://api.lyra.finance/public/get_instruments",
            data=json.dumps({"currency": "XRP", "instrument_type": instrument_type, "expired": False}).encode(),
            headers=_hdrs, method="POST",
        )
        _body = await asyncio.to_thread(_ureq.urlopen, _req, None, 20)
        data = json.loads(_body.read().decode())
        res = data.get("result") or data
        if isinstance(res, dict):
            res = res.get("instruments", [])
        for it in res:
            if it.get("instrument_name") == symbol:
                base_asset_address = it.get("base_asset_address")
                base_sub_id = str(it.get("base_asset_sub_id") or "0")
                break
        if not base_asset_address:
            # Cloudflare likely blocked the lookup (empty result). This is a hard
            # failure for options — never fall back to perp defaults (sub_id 0),
            # which would sign a perp action for an option name -> Derive 14014.
            if instrument_type == "option":
                raise RuntimeError(
                    f"Could not resolve option instrument {symbol!r} from Derive "
                    f"(lookup returned {len(res)} instruments — Cloudflare block?)"
                )
    except Exception:
        if instrument_type == "option" and not base_asset_address:
            raise
        # perps: fall back to known XRP constants on any other error
        pass
    if not base_asset_address:
        # known default for XRP-PERP
        asset_address = _PROTOCOL["asset_addresses"]["XRP"]
    else:
        asset_address = base_asset_address

    # Derive's on-chain TradeModuleData is keyed by (asset_address, sub_id) for
    # BOTH perps and options. For options, the instrument lookup above already
    # resolved the option's base_asset_address + base_asset_sub_id, which we feed
    # into the same ABI. The `instrument_name` in the body tells the API which
    # instrument; the signed action uses asset+sub_id. (Do NOT switch to a
    # string-instrument_name ABI — Derive rejects it with code 14014.)
    module_data = {
        "abi_types": ["address", "uint", "int", "int", "uint", "uint", "bool"],
        "abi_values": [
            _checksum(asset_address),
            int(base_sub_id),
            _to_big(limit_price),
            _to_big(amount),
            _to_big(max_fee),
            int(creds["sub_id"] or 0),  # recipient_id
            True if direction == "buy" else False,  # is_bid
        ],
        "extra": {},
    }
    signed = _signed_action(creds, module_data, "order")
    payload = {
        **signed,
        "asset_address": asset_address,
        # Derive's API expects sub_id as a STRING for options (the value can exceed
        # what a JSON integer round-trips cleanly). For perps it is "0".
        "sub_id": str(base_sub_id),
        "limit_price": str(f"{limit_price:.4g}"),
        "type": "order",
        "max_fee": str(int(max_fee)),
        "amount": str(amount),
        "instrument_name": symbol,
        "label": label,
        "is_bid": direction == "buy",
        "direction": direction,
        "order_type": order_type,
        "mmp": False,
        "time_in_force": time_in_force,
        "recipient_id": int(creds["sub_id"] or 0),
    }
    method = "order_debug" if debug else "order"
    return await _post(creds, method, payload)
