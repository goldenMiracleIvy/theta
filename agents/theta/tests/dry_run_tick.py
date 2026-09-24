"""One Condor dry-run tick for THETA. No trading capability. No fills."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
os.chdir(REPO)


async def main() -> int:
    from condor.agents.agent import AgentStore
    from condor.agents.config import load_full_config
    from condor.agents.engine import TickEngine
    from condor.agents.strategy import StrategyStore

    agent = AgentStore().get("theta")
    strategy = StrategyStore().get("theta", "theta_funding_desk")
    if agent is None or strategy is None:
        print("THETA agent or strategy not loaded")
        return 1

    # `Strategy` exposes its directory as `.home`; `.dir` never existed.
    config = load_full_config(strategy.home, strategy.default_config)
    config["execution_mode"] = "dry_run"
    config["frequency_sec"] = 60
    config["server_name"] = (
        config.get("server_name")
        or os.environ.get("THETA_SERVER_NAME", "").strip()
        or "local"
    )

    engine = TickEngine(
        agent=agent,
        strategy=strategy,
        config=config,
        # 0 = web-launched session: no Telegram chat, no personal id baked in.
        # Override with THETA_CHAT_ID / THETA_USER_ID when driving from Telegram.
        chat_id=int(os.environ.get("THETA_CHAT_ID", "0") or 0),
        user_id=int(os.environ.get("THETA_USER_ID", "0") or 0),
    )
    print(f"starting dry-run {engine.agent_id}")
    await engine.start()
    assert engine._task is not None
    try:
        await asyncio.wait_for(engine._task, timeout=240)
    except TimeoutError:
        print("dry-run timed out — stopping")
        await engine.stop()
        return 1
    except Exception:
        await engine.stop()
        raise

    exp = REPO / "agents/theta/strategies/theta_funding_desk/dry_runs"
    files = sorted(exp.glob("experiment_*.md")) if exp.is_dir() else []
    print("dry-run complete")
    if files:
        latest = files[-1]
        print("snapshot", latest)
        text = latest.read_text()
        print(text[:2500])
        print("...")
        print(f"chars={len(text)}")
    else:
        print("no experiment snapshot written")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
