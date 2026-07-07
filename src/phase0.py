"""
Phase 0 — Plumbing (no alpha) per spec Section 13.

- MCP connection (mock for now)
- append-only logging + Graveyard DB
- kill switch
- daily-budget circuit breaker
- Tier-1 monitoring loop
- capacity-check math
- hard-coded validate_execution_safety gate
- Run near-passive ballast with fractional shares.

This proves the skeleton can place (mock) orders, log everything, respect budget/halt/spread, and not blow up.
Later phases add the real EV engine + Auditor + Reaction.

Run: python -m src.phase0
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# local imports (run as module or from root)
_here = Path(__file__).resolve()
try:
    sys.path.insert(0, str(_here.parents[1]))  # hood_agent_1/
    if len(_here.parents) > 2:
        sys.path.insert(0, str(_here.parents[2]))
except Exception:
    pass
# When run as `PYTHONPATH=. python3 -m src.phase0` the package imports "from src.xxx" resolve via current dir in path[0]

from src.core.schemas import EVThesis, validate_execution_safety
from src.core.storage import GraveyardDB, PersistentLog, make_log_entry
from src.core.budget import DailyBudget, BudgetConfig, estimate_cost
from src.core.risk import RiskController
from src.core.capacity import estimate_tradeable_capacity, tag_thesis
from src.core.executor import Executor
from src.core.ballast import BallastEngine, BallastConfig
from src.mcp.robinhood_client import get_robinhood_client


# --- Kill switch (simple file + signal) ---
KILL_FILE = Path("data/KILL_SWITCH")
KILLED = False


def _install_kill_handlers():
    global KILLED

    def _kill(*_):
        global KILLED
        KILLED = True
        print("\n[KILL] Received signal / kill file. Halting new actions.", flush=True)

    signal.signal(signal.SIGINT, _kill)
    signal.signal(signal.SIGTERM, _kill)
    # Per remediation: presence of the file = kill engaged (unambiguous, no auto-create footgun).
    # User or operator creates `touch data/KILL_SWITCH` to engage; delete or never create to allow run.
    KILL_FILE.parent.mkdir(parents=True, exist_ok=True)


def is_killed() -> bool:
    if KILLED:
        return True
    # presence of file (regardless of content) means kill engaged
    if KILL_FILE.exists():
        return True
    return False


def main():
    root = Path(__file__).resolve().parents[1]
    data_dir = root / "data"
    logs_dir = root / "logs"
    data_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    print("=== HOOD AGENT 1 — Phase 0 Plumbing ===")
    print("Spec: agentic-trading-architecture.md")
    print(f"Data: {data_dir}")
    print(f"Logs: {logs_dir}")
    print("Using MOCK Robinhood client (no real capital).")
    print()

    _install_kill_handlers()

    # Storage
    graveyard = GraveyardDB(data_dir)
    plog = PersistentLog(logs_dir)

    # Budget (hard cap, degrade at 80%)
    budget = DailyBudget(BudgetConfig(daily_usd_cap=1.0, log_path=logs_dir / "budget.json"))

    # MCP (mock)
    client = get_robinhood_client(starting_cash=2500.0, seed=123)

    # Risk + Executor + Ballast
    sleeve = 2000.0  # pretend agentic sleeve for Phase 0 sizing
    risk = RiskController(agentic_sleeve_usd=sleeve)
    executor = Executor(client=client, risk=risk, graveyard=graveyard)
    ballast = BallastEngine(client=client, config=BallastConfig(liquid_names=["SPY", "IWM"], target_pct_of_total=0.30))

    # --- Phase 0 "monitoring loop" (Tier-1 only, fake events) ---
    # In real: subscribe to calendar + EDGAR push via MCP, never poll.
    # Here we simulate a few low-conviction ballast rebalances + one safety test.

    print("Starting Phase 0 loop (Ctrl-C or data/KILL_SWITCH to stop)...")
    print("Will run a few demo cycles then exit (or until killed).\n")

    cycles = 0
    max_cycles = 5  # demo length

    while cycles < max_cycles and not is_killed():
        cycles += 1
        now = datetime.now(timezone.utc).isoformat()
        print(f"--- Cycle {cycles} @ {now} ---")

        # 1. Budget check (circuit breaker demo)
        spend, frac = budget.current_spend()
        print(f"Budget: ${spend:.4f} / $1.00 ({frac*100:.1f}%)  degraded={budget.is_degraded()}")

        if not budget.can_start_tier3():
            print("  [BUDGET] Tier-3 blocked (degrade mode). Only T1 allowed.")
            # still allow ballast T1 work

        # 2. Ballast (T1)
        snap = executor.get_portfolio_snapshot()
        total = snap["total_usd_approx"]
        ballast_actions = ballast.maybe_rebalance(total)
        if ballast_actions:
            print("  [BALLAST] would rebalance:", ballast_actions)
            # In Phase 0 we do NOT actually trade ballast unless we want to prove Executor.
            # For demo, do one tiny "rebalance" buy via Executor on a liquid name if cash allows.
            if cycles == 2 and total > 500:
                # fabricate a super-safe thesis for ballast (SPY)
                safe = EVThesis(
                    ticker="SPY",
                    event_type="ballast",
                    upside_pct=0.5,
                    p_upside=0.5,
                    downside_pct=-0.8,
                    p_downside=0.3,
                    expected_value_pct=0.1,
                    prior_accuracy_on_name=0.9,
                    what_informed_holders_may_know_that_we_dont="Broad index; we know exactly what we hold.",
                    tradeable_capacity_usd=300.0,
                )
                # capacity + safety will be fine
                tag = estimate_tradeable_capacity(50_000_000, 520.0, total, spread_pct=0.0003)
                tag_thesis(safe, tag)
                ex_res = executor.execute_thesis(safe, is_offhours=False)
                print("  [EXEC] ballast demo order:", ex_res.success, ex_res.note or ex_res.veto_reason)
                # log it
                entry = make_log_entry(safe, outcome="filled" if ex_res.success else "vetoed", regime="phase0_ballast")
                plog.append(entry)
                # note: Executor now records its own vetoes to graveyard (R1); record_trade only for success path here
                if ex_res.success:
                    graveyard.record_trade(safe, outcome="filled_demo", regime="phase0")

        # 3. Fake "event" that triggers capacity + safety gate demo (the important plumbing)
        if cycles == 3:
            print("  [DEMO] Injecting a thin-name reaction candidate to test gates...")
            thin = EVThesis(
                ticker="TINY",
                event_type="8k",
                upside_pct=28.0,
                p_upside=0.38,
                downside_pct=-55.0,
                p_downside=0.42,
                expected_value_pct=0.0,
                prior_accuracy_on_name=0.35,
                what_informed_holders_may_know_that_we_dont="Promoter background; possible related-party transactions not fully disclosed in the 8-K.",
                tradeable_capacity_usd=0.0,
                event_risk_flags=["thin_float", "low_adv", "S-3_on_file"],
            )
            q = client.get_quote("TINY")
            cap = estimate_tradeable_capacity(q.avg_daily_volume, q.last, total, spread_pct=(q.ask - q.bid) / ((q.ask + q.bid) / 2))
            tag_thesis(thin, cap)
            thin.expected_value_pct = thin.compute_ev()

            # Run risk (will likely size down or veto on ruin)
            rc = risk.check(thin, current_book=[])
            print(f"    Risk: ok={rc.ok} sized=${rc.sized_usd} reason={rc.reason} stress={rc.ruin_stress_score}")

            if rc.ok:
                ex = executor.execute_thesis(thin, is_offhours=True)
                print(f"    Executor: success={ex.success} veto={ex.veto_reason} safety={ex.safety}")
                # Executor records its vetoes itself when graveyard provided (R1); we only record risk veto here
            else:
                # record rejection
                graveyard.record_rejection(thin, rc.reason, regime="phase0_demo")
                print("    Recorded rejection in Graveyard.")

            # Always log
            plog.append(make_log_entry(thin, outcome="demo_veto_or_fill", regime="phase0_demo"))

        # 4. Heartbeat / budget record (fake a cheap T2 call) -- use hard can_spend 100% gate (M6)
        t2_est = estimate_cost("t2_triage", "haiku")
        if not budget.can_spend(t2_est):
            print("  [BUDGET] hard 100% cap stop: would exceed daily_usd_cap, skipping record_usage")
        else:
            budget.record_usage("haiku", 1100, 220)
        time.sleep(0.15)  # short for demo

        # 5. Kill check inside
        if is_killed():
            break

    print("\n=== Phase 0 loop ended ===")
    print("Final budget:", budget.current_spend())
    print("Graveyard sample (last 3):")
    for r in list(graveyard.query_failures(limit=5))[-3:]:
        print("  ", r.get("ticker"), r.get("reject_reason") or r.get("outcome"))
    print("Persistent log tail available at", plog.path)

    # Demo: show hard safety function still works standalone
    print("\n[VERIFY] Hard safety gate (from spec):")
    bad = validate_execution_safety(1.0, 1.12, 80000, 12000, False)
    print("  Wide spread + large vs ADV:", bad)

    graveyard.close()
    print("\nPhase 0 plumbing complete. Ready for Phase 1 (Risk + Ballast + Capacity + EDGAR).")
    print("See agentic-trading-architecture.md for full spec and EV-calibration gate warning.")


if __name__ == "__main__":
    main()
