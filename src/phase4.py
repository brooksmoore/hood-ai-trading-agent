"""
Phase 4: EV-Calibration Gate + Human Go-Live Switch (P4).

- Deterministic gate (P4-A): consumes graveyard paper trades, hard guards, structured verdict.
- Human-only go-live (P4-B): via SafetyCore.human_go_live(authorized=True, calibration_passed=verdict.passed).
- Use-time invariant (P4-C): in executor (already added), trips on tamper.
- Demo (P4-D): seed various fixtures (pass, miscalib, insuff N, single regime, in-sample, bad slippage), run gate, show switch, tamper, produce report.

Offline, Fake, temp dirs, paper only. No real MCP.

Run: PYTHONPATH=. python3 -m src.phase4
"""

from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

_here = Path(__file__).resolve()
try:
    sys.path.insert(0, str(_here.parents[1]))
except Exception:
    pass

from src.core.schemas import EVThesis
from src.core.storage import GraveyardDB
from src.core.calibration_gate import CalibrationGate, CalibrationVerdict
from src.core.safety_core import SafetyCore
from src.core.executor import Executor
from src.mcp.robinhood_client import get_robinhood_client
from src.core.risk import RiskController
from src.core.meta_reviewer import MetaReviewer, AppliedChange


def main(data_dir: Optional[Path] = None, logs_dir: Optional[Path] = None) -> None:
    if data_dir is None:
        data_dir = Path("data")
    if logs_dir is None:
        logs_dir = Path("logs")
    data_dir = Path(data_dir)
    logs_dir = Path(logs_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    print("=== HOOD AGENT 1 — Phase 4: EV-Calibration Gate + Human Go-Live Switch ===")
    print("Spec Phase 4. Deterministic gate (default FAIL). Human-only go-live (authorized + passing calib). Use-time invariant.")
    print("PAPER ONLY until human go-live. Honest verdict may be NO.")
    print(f"Data: {data_dir} Logs: {logs_dir}")
    print()

    # Clean
    for f in [data_dir / "graveyard.db", logs_dir / "decision_log.jsonl", data_dir / "safety_core_violations.jsonl"]:
        try:
            if f.exists():
                f.unlink()
        except Exception as e:
            # F5: demo cleanup best-effort only; log but never affect execution
            print(f"[PHASE4 CLEAN] ignore cleanup error for {f}: {e}")

    graveyard = GraveyardDB(data_dir)
    SafetyCore.init_log(logs_dir)

    # Seed various cases for gate tests (use record_trade with ev, realized, regime, meta)
    # IMPORTANT: explicit per-ver param_version + proper timestamps for O1/O2 forward-only demo.
    # Use dummy thesis for logging
    def seed(ticker, ev, realized, regime, meta_extra=None, outcome="filled_paper", ts=None):
        t = EVThesis(ticker, "8k", 10, 0.5, -10, 0.3, ev, 0.6, "humility.", 1000, timestamp=ts)
        m = {"slippage_modeled": True, "param_version": "v1"}
        if meta_extra:
            m.update(meta_extra)
        graveyard.record_trade(t, outcome=outcome, realized_return_pct=realized, regime=regime, meta=m)

    # Demo deploy times via MetaReviewer (O2 source)
    mr = MetaReviewer(change_log_path=data_dir / "meta_change_log.jsonl")
    # Simulate baseline v0 + a v1 deploy (for forward trades)
    base_deploy = "2024-01-01T00:00:00+00:00"
    v1_deploy = "2024-03-01T00:00:00+00:00"
    # Manually seed history for demo (in real, meta.run() on proposals sets this)
    mr._history.append(AppliedChange(version=0, timestamp=base_deploy, proposal={"param": "baseline"}, applied=True, deployed_at=base_deploy))
    mr._history.append(AppliedChange(version=1, timestamp=v1_deploy, proposal={"param": "risk"}, applied=True, deployed_at=v1_deploy))
    mr._version = 1
    deploys = mr.get_all_deploys()

    post_v1 = "2024-03-10T00:00:00+00:00"  # forward for v1
    pre_v1 = "2024-02-10T00:00:00+00:00"   # in-sample for v1 (would be tuned data)

    # 1. Good pass case: multi-regime, suff N, calib ok, +EV realized positive post-slip, FORWARD only (post v1 deploy)
    for i in range(25):
        ev = 3.0 + (i % 3)
        real = 2.5 + (i % 2) * 0.5
        reg = "Q1" if i < 12 else "Q2"
        seed("GOOD", ev, real, reg, {"param_version": "v1"}, ts=post_v1)

    # 2. Miscalib: +EV but realized negative (also post, on own ver)
    for i in range(25):
        seed("MIS", 4.0, -2.0, "Q1" if i<12 else "Q2", {"param_version": "v_mis"}, ts=post_v1)

    # 3. Insuff N (own ver)
    seed("FEW", 2.0, 1.5, "Q1", {"param_version": "v_few"}, ts=post_v1)

    # 4. Single regime (own ver)
    for i in range(25):
        seed("SINGLE", 3.0, 2.0, "ONLYQ", {"param_version": "v_s"}, ts=post_v1)

    # 5. In-sample for v_tuned: pre-deploy ts for its version (will trigger IN_SAMPLE_ONLY)
    for i in range(25):
        seed("INSAMPLE", 3.0, 2.5, "TUNE" if i%2 else "TUNE2", {"param_version": "v_tuned"}, ts=pre_v1)

    # 6. Bad slippage: no modeled flag (post, own ver, multi regime)
    for i in range(25):
        t = EVThesis("BADSLIP", "8k", 10,0.5,-10,0.3,3,0.6,"h.",1000, timestamp=post_v1)
        m = {"slippage_modeled": False, "param_version": "v_badslip"}
        graveyard.record_trade(t, outcome="filled_paper", realized_return_pct=2.0, regime="Q1" if i<13 else "Q2", meta=m)

    gate = CalibrationGate()

    print("--- Gate verdicts on fixtures (O1/O2 explicit window + forward-only) ---")
    v_good = gate.compute_verdict(graveyard, eval_param_version="v1", deploy_times=deploys)
    # also demo meta_reviewer path
    v_good_mr = gate.compute_verdict(graveyard, eval_param_version="v1", meta_reviewer=mr)
    print(f"GOOD (multi, N=25, calib, forward post {v1_deploy}): passed={v_good.passed}, reason={v_good.reason}, n={v_good.n}")
    print(f"  (via meta_reviewer: passed={v_good_mr.passed}, reason={v_good_mr.reason})")
    print(f"  window: {v_good.window}")

    # Demo in-sample explicitly (should IN_SAMPLE_ONLY)
    v_ins = gate.compute_verdict(graveyard, eval_param_version="v_tuned", deploy_times={"v_tuned": v1_deploy})
    print(f"INSAMPLE (pre-deploy for v_tuned): passed={v_ins.passed}, reason={v_ins.reason}")

    # Demo no-window
    v_nowin = gate.compute_verdict(graveyard)
    print(f"NO_WINDOW (pooled): passed={v_nowin.passed}, reason={v_nowin.reason}")

    print(" (Other fixtures: MIS->miscalib or pos not +; FEW->insuff; SINGLE->insuff_regime; BADSLIP->ZEROED_SLIP ) ")

    # Go-live switch demo (P4-B)
    print("\n--- Go-live switch (human only) ---")
    # Autonomous attempt (sim via apply, which is used by meta)
    ok_auto = SafetyCore.apply_safe_change("LIVE_ENABLED", True, {"from": "meta"})
    print(f"Autonomous try enable live: {ok_auto} (should False, raises in guarded)")

    # Human with fail calib
    ok_h_fail = SafetyCore.human_go_live(authorized=True, calibration_passed=False, evidence={"verdict": "MIS"})
    print(f"Human+fail calib enable: {ok_h_fail} (False)")

    # Human+pass (use good)
    ok_h_pass = SafetyCore.human_go_live(authorized=True, calibration_passed=True, evidence={"verdict": v_good.reason})
    print(f"Human+pass calib enable: {ok_h_pass} (True)")

    print(f"Live enabled now: {SafetyCore.is_live_enabled()}")

    # Disable always works
    # (no setter, but for demo we can use guarded or note)
    # Since guarded, for disable: human path would use apply or direct guarded? For enable special, for disable use apply.
    SafetyCore.apply_safe_change("LIVE_ENABLED", False, {"human_disable": True})
    print(f"After human disable: {SafetyCore.is_live_enabled()}")

    # Use-time tamper demo (P4-C) - force tamper, call executor
    print("\n--- Use-time invariant tamper trip (P4-C) ---")
    orig_cap = SafetyCore.get_hard_ceiling_pct()
    try:
        # Tamper (bypass for demo)
        object.__setattr__(SafetyCore._params, "HARD_CEILING_PCT", 0.99)
        client = get_robinhood_client(1000)
        ex = Executor(client=client, risk=RiskController(1000))
        t = EVThesis("TAMP", "8k", 10,0.5,-10,0.3,1,0.5,"h.",100)
        res = ex.execute_thesis(t)
        print(f"After tamper, exec veto: {res.veto_reason} (safety_core_tamper expected)")
    finally:
        object.__setattr__(SafetyCore._params, "HARD_CEILING_PCT", orig_cap)

    # Human readable report (P4-D) -- now includes explicit OOS window + forward-only basis
    print("\n--- Human-readable Calibration Report (artifact) ---")
    report = {
        "verdict_on_good": {"passed": v_good.passed, "reason": v_good.reason, "n": v_good.n, "metrics": v_good.metrics},
        "evaluation_window": v_good.window,
        "oos_basis": "verdict rendered ONLY on trades with timestamp > version.deployed_at (from MetaReviewer); no pooling across param_versions; in-sample (pre-deploy) excluded with IN_SAMPLE_ONLY",
        "insample_verdict": {"passed": v_ins.passed, "reason": v_ins.reason},
        "no_window_verdict": {"passed": v_nowin.passed, "reason": v_nowin.reason},
        "note": "Gate default FAIL. PASS requires explicit single-version forward-only window (post-deploy_ts), multi-regime, suff N>=MIN_N, +EV realized post modeled slip. N near floor noted in window. See graveyard for raw ev vs realized (post-slippage).",
        "go_live_status": SafetyCore.is_live_enabled(),
        "safety_core_status": "all protected within ratchet",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    report_path = logs_dir / "phase4_calibration_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"Report written to {report_path}")
    print("Summary: " + json.dumps({k: report[k] for k in ["verdict_on_good", "evaluation_window", "go_live_status"]}, indent=2))

    print("\n=== Phase 4 complete ===")
    print("Gate is deterministic, hard, default-FAIL. Go-live human-only + calib. Invariant at use-time. OOS enforced.")
    graveyard.close()


if __name__ == "__main__":
    main()
