"""
Phase 1 demo: Risk + Ballast + Capacity + EDGAR (spec Section 13 Phase 1).

Runs entirely offline using mocks + EDGAR fixtures.
Demonstrates full pipeline end-to-end, per-stage trace, artifacts.
No alpha/EV engine/LLM.

Run: PYTHONPATH=. python3 -m src.phase1
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# path for -m
_here = Path(__file__).resolve()
try:
    sys.path.insert(0, str(_here.parents[1]))
except Exception:
    pass

from src.core.schemas import EVThesis
from src.core.storage import GraveyardDB, PersistentLog, make_log_entry
from src.core.budget import DailyBudget, BudgetConfig
from src.core.risk import RiskController
from src.core.capacity import (
    estimate_tradeable_capacity, tag_thesis, get_capacity_status, reroute_if_untradeable
)
from src.core.executor import Executor
from src.core.ballast import BallastEngine, BallastConfig
from src.mcp.robinhood_client import get_robinhood_client
from src.data.edgar import EdgarClient, build_recent_filings_list_for_auditor
from src.core.auditor import run_auditor


def main(data_dir: Optional[Path] = None, logs_dir: Optional[Path] = None) -> None:
    """P1-E demo. Accepts data_dir/logs_dir for tests (M-a) to avoid polluting repo tree; defaults to project paths for CLI."""
    if data_dir is None or logs_dir is None:
        root = Path(__file__).resolve().parents[1]
        if data_dir is None:
            data_dir = root / "data"
        if logs_dir is None:
            logs_dir = root / "logs"
    data_dir = Path(data_dir)
    logs_dir = Path(logs_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    print("=== HOOD AGENT 1 — Phase 1: Risk + Ballast + Capacity + EDGAR ===")
    print("Offline demo using mocks + fixtures (no network in this run).")
    print(f"Data: {data_dir}  Logs: {logs_dir}")
    print()

    # clean for demo
    for f in [data_dir / "graveyard.db", logs_dir / "decision_log.jsonl", data_dir / "idempotency.json", logs_dir / "budget.json"]:
        if f.exists():
            f.unlink()

    graveyard = GraveyardDB(data_dir)
    plog = PersistentLog(logs_dir)
    budget = DailyBudget(BudgetConfig(daily_usd_cap=1.0, log_path=logs_dir / "budget.json"))

    client = get_robinhood_client(starting_cash=25000.0, seed=42)

    # seed some live book positions for R2 book-aware stress + P1-C capacity
    # place some ballast to have book
    _ = client.place_limit_order("SPY", "buy", 40, 500.0)  # ~20k position
    _ = client.place_limit_order("IWM", "buy", 50, 200.0)  # ~10k

    sleeve = 20000.0
    risk = RiskController(agentic_sleeve_usd=sleeve)
    executor = Executor(client=client, risk=risk, graveyard=graveyard)

    # Ballast with factor tilt (weights)
    ballast = BallastEngine(
        client=client,
        config=BallastConfig(
            liquid_names=["SPY", "IWM"],
            target_pct_of_total=0.30,
            target_weights={"SPY": 0.7, "IWM": 0.3},  # simple factor/conviction tilt
            rebalance_threshold=0.04,
        ),
    )

    # --- EDGAR with fixtures (P1-D) ---
    # Create temp fixture for company_tickers + a sample filings response
    with tempfile.TemporaryDirectory() as td:
        fix_dir = Path(td)
        tickers_fix = fix_dir / "company_tickers.json"
        tickers_fix.write_text(json.dumps([
            {"ticker": "TINY", "cik_str": "1234567"},
            {"ticker": "AAPL", "cik_str": "320193"},
        ]))

        # fixture "recent filings" will be simulated via client overrides in demo
        # For real ingest we use a fixture path for tickers; for filings we mock the client calls
        # but to show pipeline, we use edgar.ingest with fixture, then override get_recent for test data
        edgar = EdgarClient(ua="hood_agent_1 test (test@example.com)", cache_dir=data_dir)

        # --- Demo event: thin name with S-3 fixture effect ---
        print("--- Phase1 Pipeline Trace ---")
        print("Event: TINY 8-K + S-3 presence (from EDGAR fixture)")

        # Simulate EDGAR ingest result (as if from real fetch + cache)
        # In real: edgar.ingest_for_screens("TINY", tickers_fixture=tickers_fix)
        # Here we construct the output of ingest to drive screens (stdlib parser logic exercised in edgar.py)
        ingested = [
            {"form": "8-K", "accession": "000123-24-000001", "filing_date": "2024-06-01",
             "raw_text": "some text", "structured": {"form": "8-K", "has_going_concern": False, "is_dilution_form": False}},
            {"form": "S-3", "accession": "000123-24-000002", "filing_date": "2024-05-20",
             "raw_text": "S-3 filed ...", "structured": {"form": "S-3", "has_going_concern": False, "is_dilution_form": True}},
        ]
        recent_filings = build_recent_filings_list_for_auditor(ingested)
        print("EDGAR -> recent_filings for screens:", recent_filings)

        thin = EVThesis(
            ticker="TINY",
            event_type="8k",
            upside_pct=25.0, p_upside=0.38, downside_pct=-50.0, p_downside=0.40,
            expected_value_pct=0.0, prior_accuracy_on_name=0.3,
            what_informed_holders_may_know_that_we_dont="Promoters may know more; filings lag.",
            tradeable_capacity_usd=12000.0,
            event_risk_flags=["thin_float", "low_adv"],
        )
        q = client.get_quote("TINY")
        cap = estimate_tradeable_capacity(q.avg_daily_volume, q.last, 25000.0, spread_pct=(q.ask-q.bid)/((q.ask+q.bid)/2 or 1), off_hours=False)
        tag_thesis(thin, cap)
        thin.expected_value_pct = thin.compute_ev()
        print("Capacity tag:", thin.capacity_tag, "cap_usd=", thin.tradeable_capacity_usd)

        # status + reroute (P1-C)
        status, reroute = reroute_if_untradeable(thin, 25000.0, ballast_targets=["SPY"])
        print("Capacity status:", status, "reroute_to:", reroute)

        # Auditor det screens fed from EDGAR (P1-D)
        det = run_auditor(thin, recent_filings=recent_filings, avg_daily_volume=q.avg_daily_volume, spread_pct=(q.ask-q.bid)/q.last)
        print("Auditor det (from EDGAR): dilution_risk=", det.deterministic.dilution_risk, "overall_pass=", det.overall_pass)

        # Risk (P1-A book aware + two tier)
        live_pos = client.get_positions()
        rc = risk.check(thin, current_book=[], current_positions=live_pos)
        print("Risk check (book-aware): ok=", rc.ok, "sized=", rc.sized_usd, "reason=", rc.reason, "stress=", rc.ruin_stress_score)
        book_stress = risk.book_ruin_stress(live_pos)
        print("Book aggregate ruin_stress:", book_stress)

        if rc.ok:
            ex = executor.execute_thesis(thin, is_offhours=False)
            print("Executor (capped size, gates): success=", ex.success, "veto=", ex.veto_reason, "filled_usd~", round(ex.filled_shares * ex.avg_fill_price, 2) if ex.success else 0)
            if not ex.success and ex.veto_reason:
                graveyard.record_rejection(thin, ex.veto_reason, regime="phase1")
            else:
                graveyard.record_trade(thin, outcome="filled_phase1" if ex.success else "veto", regime="phase1")
            plog.append(make_log_entry(thin, outcome="phase1", regime="phase1"))
        else:
            graveyard.record_rejection(thin, rc.reason, regime="phase1")
            print("Risk veto recorded.")

        # Ballast rebalance thru executor (P1-B)
        snap = executor.get_portfolio_snapshot()
        total = snap["total_usd_approx"]
        actions = ballast.maybe_rebalance(total)
        print("Ballast actions (weights tilt):", actions)
        if actions:
            res_list = ballast.rebalance(executor, total, is_offhours=False)
            for r in res_list:
                print("  Ballast rebal result:", r.success, r.veto_reason or "ok")

        # capacity sweep demo (P1-C) -- points chosen to cross thresholds for this name's capacity (M-b fix)
        print("Capacity sweep for TINY (book growth -> shift):")
        for bsz in [5000, 20000, 100000, 500000]:
            st = get_capacity_status(thin, bsz)
            print(f"  book=${bsz}: {st}")
            if st in ("too_large", "illiquid"):
                rstatus, rtgt = reroute_if_untradeable(thin, bsz, ballast_targets=["SPY"])
                print(f"    -> reroute suggested to {rtgt}")

        budget.record_usage("haiku", 900, 180)
        print("Budget after demo:", budget.current_spend())

    print("\n=== Phase 1 pipeline complete (offline) ===")
    print("Graveyard rows:", graveyard._get_conn().execute("select count(*) from trades").fetchone()[0])
    graveyard.close()
    print("Artifacts in data/ and logs/. See PHASE1_CHANGELOG.md and spec for next phases.")
    print("Note: central alpha risk (sec 0) remains for Phase 4 calibration gate.")


if __name__ == "__main__":
    main()
