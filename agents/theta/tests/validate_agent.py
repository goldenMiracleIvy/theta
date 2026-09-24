"""Validate THETA against Condor's real loaders (no network, no trading).

Organizers: run from the Condor repo root
  uv run python agents/theta/tests/validate_agent.py
"""
import ast
import inspect
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
os.chdir(REPO)

ok = True


def _env_list(var: str) -> tuple[str, ...]:
    """Comma-separated names from the environment (empty by default).

    Kept in env so this file never publishes a list of vendors or rival
    entries. Set e.g.
      THETA_BANNED_PROVIDERS=acme,other-vendor
      THETA_BANNED_ENTRIES=rival_one,rival_two
    to run the checks.
    """
    raw = os.environ.get(var, "")
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())

from routines.base import discover_routines_from_path

rdir = REPO / "agents/theta/routines"
found = discover_routines_from_path(rdir, agent_slug="theta")
print("=== ROUTINE DISCOVERY ===")
for name in sorted(found):
    info = found[name]
    fields = list(info.config_class.model_fields) if getattr(info, "config_class", None) else []
    print(f"  [OK] {name:<22} category={info.category:<12} config_fields={fields}")
for expected in ("theta_tape", "theta_margin", "theta_option_ticket", "theta_park"):
    if expected not in found:
        print(f"  [FAIL] {expected} NOT discovered")
        ok = False
if "_theta_math" in found:
    print("  [FAIL] _theta_math should not be a routine (helper only)")
    ok = False

print("\n=== ROUTINE CONTRACT ===")
# Categories Condor ships. It does NOT enforce an enum — `routines/base.py`
# falls back to the free string "Uncategorized" — so this set is the vocabulary
# the dashboard actually groups by, plus this entry's own "Setup".
SHIPPED_CATEGORIES = {
    "Market Data",
    "Analysis",
    "Arbitrage",
    "Monitoring",
    "Bot Analysis",
    "Developer Tools",
}
ENTRY_CATEGORIES = {"Setup"}
VALID = SHIPPED_CATEGORIES | ENTRY_CATEGORIES
for name, info in sorted(found.items()):
    has_run = inspect.iscoroutinefunction(info.run_fn)
    cat_ok = bool(info.category) and str(info.category) in VALID
    good = has_run and info.config_class is not None and cat_ok
    ok &= good
    print(
        f"  [{'OK' if good else 'FAIL'}] {name:<22} "
        f"async_run={has_run} Config={bool(info.config_class)} CATEGORY={info.category!r}"
    )

print("\n=== AGENT / STRATEGY LOADING ===")
from condor.agents.agent import AgentStore
# `_slugify` became public as `slugify` in current Condor; support both.
from condor.agents.strategy import StrategyStore
try:  # pragma: no cover - version dependent
    from condor.agents.strategy import slugify as _slugify
except ImportError:  # pragma: no cover - older Condor
    from condor.agents.strategy import _slugify

agent = AgentStore().get("theta")
if not agent:
    print("  [FAIL] agent 'theta' not loaded")
    ok = False
else:
    print(f"  [OK] agent slug={agent.slug} key={agent.agent_key} created_by={agent.created_by}")
    body = (agent.instructions or "").lower()
    leaked = [b for b in _env_list("THETA_BANNED_PROVIDERS") if b in body]
    if leaked:
        print(f"  [FAIL] AGENT.md body leaks provider names: {leaked}")
        ok = False
    else:
        print("  [OK] AGENT.md body has no provider names")
    banned = _env_list("THETA_BANNED_ENTRIES")
    hit = [b for b in banned if b in body]
    if hit:
        print(f"  [FAIL] AGENT.md names another entry: {hit}")
        ok = False
    else:
        print("  [OK] AGENT.md names no other entry")

strats = [s for s in StrategyStore().list_all() if s.agent_slug == "theta"]
if not strats:
    print("  [FAIL] no strategy loaded for theta")
    ok = False
for s in strats:
    rl = (s.default_config or {}).get("risk_limits") or {}
    print(f"  [OK] strategy key={s.key} name={s.name}")
    print(f"       freq={s.default_config.get('frequency_sec')}s risk={rl}")
    if not isinstance(rl, dict) or not rl.get("require_triple_barrier"):
        print("  [FAIL] risk_limits must be nested and require_triple_barrier=true")
        ok = False
    if rl.get("max_position_size_quote", 0) > 400:
        print("  [FAIL] short cap must be ≤ $400")
        ok = False
    exp = _slugify(s.name)
    d = (REPO / "agents/theta/strategies" / exp).is_dir()
    ok &= d
    print(f"  [{'OK' if d else 'FAIL'}] folder '{exp}' matches slugified name")

print("\n=== ISOLATION ===")
imports = []
for py in (REPO / "agents/theta").rglob("*.py"):
    tree = ast.parse(py.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append((py.name, node.module))
        elif isinstance(node, ast.Import):
            for a in node.names:
                imports.append((py.name, a.name))
# Cross-agent isolation: this entry may only import Condor's own runtime and
# its own siblings. Rival entry names are supplied by env, never shipped here.
_banned_mods = [b.replace("-", "_") for b in _env_list("THETA_BANNED_ENTRIES")]
bad = [
    f"{f}: {m}"
    for f, m in imports
    if m.startswith("agents.")
    or any(b in m for b in _banned_mods)
]
if bad:
    print("  [FAIL] cross-agent imports:", bad)
    ok = False
else:
    print("  [OK] no other-agent imports")

print("\n=== RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT", "===")
sys.exit(0 if ok else 1)
