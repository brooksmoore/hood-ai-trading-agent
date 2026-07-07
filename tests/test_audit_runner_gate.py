"""
AUDITOR-OWNED GATE FILE — DO NOT EDIT (builder/Grok).
Written by: Claude (auditor), 2026-06-10.
Purpose: lock the hardest-won invariants so they cannot silently regress.

Rules:
- Only the auditor (Claude) may add or modify tests in this file.
- Grok/builder may add supporting tests elsewhere but NOT here.
- Every test must be able to FAIL against broken code — that is their only job.
- No network calls. All hermetic (fake LLM, fake MD, fake feed, tempdir).

Eleven gates:
  G1 — CalibrationGate is fail-closed (empty/bad data → FAIL, never PASS)
  G2 — Section 11 safety veto fires (halted, no_two_sided_market, spread_too_wide)
  G3 — EV engine returns None on kill/budget/malformed (no fabrication)
  G4 — SafetyCore rejects direct loosening of protected params
  G5 — Runner e2e: run_paper opens+resolves with series-derived realized (not a constant)
  G6 — Tenant isolation: hood never sells a position it didn't open; confirm-fail
       rolls back the ownership ledger (no phantom credit on an unconfirmed buy)
  G7 — Live gate hard-raises: RunMode.LIVE with SafetyCore.is_live_enabled()==False
       is vetoed before any sizing/quote/risk work, every time, no exceptions
  G8 — Watcher/reasoner split: idle cycles make zero LLM calls
  G9 — Hard daily spend breaker: near-zero cap blocks LLM call AND trade
  G10 — DynamicEDGARFeed's event_type must pass ReactionLayer's tier1 filter (the
        candidate-events-flagged-but-zero-LLM-calls silent-death bug)
  G11 — Filing text must be HTML/XBRL-stripped before truncation (the
        100%-rejected-as-header-only silent-death bug)
"""

import sys
import unittest
import tempfile
import json
from pathlib import Path

# Ensure project root is on path (works from repo root or tests/ dir)
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.storage import GraveyardDB


# ---------------------------------------------------------------------------
# G1 — CalibrationGate is fail-closed
# ---------------------------------------------------------------------------

class GateCalibrationFailClosed(unittest.TestCase):
    """Gate 1: the calibration gate must never PASS on empty or insufficient data.
    Fail-before: a naive gate that always returns PASS would fail this.
    Fail-before: a gate with no forward-only check would pass in-sample data.
    """

    def test_g1_empty_graveyard_returns_fail_not_pass(self):
        """Empty DB has zero resolved trades → INSUFFICIENT_N → FAIL."""
        from src.core.calibration_gate import CalibrationGate
        with tempfile.TemporaryDirectory() as td:
            g = GraveyardDB(Path(td))
            gate = CalibrationGate()
            verdict = gate.compute_verdict(g, eval_param_version="v0")
            self.assertFalse(verdict.passed,
                f"Gate must FAIL on empty data — got PASS. Reason: {verdict.reason}. "
                "Pre-fix this asserted True (naive gate always PASS).")
            self.assertIsNotNone(verdict.reason,
                "Gate must provide a failure reason, not silent PASS.")
            g.close()

    def test_g1_fabricated_rows_do_not_earn_pass(self):
        """Rows with fabricated (constant) realized_return_pct must not earn PASS.
        Specifically: slippage_modeled=False rows are the in-sample/fabricated signal.
        Gate must either FAIL on ZEROED_OR_UNMODELED_SLIPPAGE or INSUFFICIENT forward rows.
        Fail-before: a gate with no slippage check would PASS on these rows.
        """
        from src.core.calibration_gate import CalibrationGate
        from src.core.schemas import EVThesis
        with tempfile.TemporaryDirectory() as td:
            g = GraveyardDB(Path(td))
            # Seed fabricated-looking rows (slippage_modeled=False = spec violation)
            for i in range(25):
                th = EVThesis(
                    ticker="FAKE", event_type="8k",
                    upside_pct=5.0, p_upside=0.6,
                    downside_pct=-3.0, p_downside=0.3,
                    expected_value_pct=2.1,
                    prior_accuracy_on_name=0.5,
                    what_informed_holders_may_know_that_we_dont="test",
                    tradeable_capacity_usd=100.0,
                    timestamp=f"2026-01-{i+1:02d}T10:00:00Z",
                )
                g.record_trade(th, outcome="filled_paper_resolved",
                               realized_return_pct=0.03,  # constant = fabrication signal
                               regime="bull",
                               meta={"slippage_modeled": False, "param_version": "v0"})
            gate = CalibrationGate()
            verdict = gate.compute_verdict(g, eval_param_version="v0")
            # Unmodeled slippage → ZEROED_OR_UNMODELED_SLIPPAGE or INSUFFICIENT after filter
            self.assertFalse(verdict.passed,
                f"Fabricated rows (slippage_modeled=False) must not earn PASS. Got: {verdict.reason}. "
                "Pre-fix: gate passed on these because slippage flag was unchecked.")
            g.close()

    def test_g1_regime_floor_and_unknown_discount(self):
        """Multi-regime coverage must be EARNED: >=2 distinct REAL regimes EACH with
        >= MIN_PER_REGIME trades. A thin off-regime tail, or a pile of 'unknown', must NOT
        manufacture coverage. Fail-before: old gate accepted any 2 distinct regime strings.
        """
        from src.core.calibration_gate import CalibrationGate
        from src.core.schemas import EVThesis

        def seed(g, dist):
            i = 0
            for regime, count in dist.items():
                for _ in range(count):
                    i += 1
                    th = EVThesis(ticker="RG", event_type="8k", upside_pct=5.0, p_upside=0.6,
                                  downside_pct=-3.0, p_downside=0.3, expected_value_pct=2.1,
                                  prior_accuracy_on_name=0.5,
                                  what_informed_holders_may_know_that_we_dont="ok",
                                  tradeable_capacity_usd=100.0,
                                  timestamp=f"2026-02-{i+1:02d}T10:00:00Z")
                    g.record_trade(th, outcome="filled_paper_resolved", realized_return_pct=2.0,
                                   regime=regime, meta={"slippage_modeled": True, "param_version": "v0"})

        gate = CalibrationGate(min_n=5)  # isolate the regime check from the N floor

        # (1) Thin off-regime tail: riskoff (2) below the floor -> only 1 qualifying regime -> FAIL.
        with tempfile.TemporaryDirectory() as td:
            g = GraveyardDB(Path(td)); seed(g, {"riskon_calm": 6, "riskoff_calm": 2})
            v = gate.compute_verdict(g, eval_param_version="v0")
            self.assertFalse(v.passed)
            self.assertEqual(v.reason, "INSUFFICIENT_REGIME_COVERAGE")
            g.close()

        # (2) 'unknown' must not count toward diversity -> only riskon qualifies -> FAIL.
        with tempfile.TemporaryDirectory() as td:
            g = GraveyardDB(Path(td)); seed(g, {"riskon_calm": 6, "unknown": 10})
            v = gate.compute_verdict(g, eval_param_version="v0")
            self.assertFalse(v.passed)
            self.assertEqual(v.reason, "INSUFFICIENT_REGIME_COVERAGE")
            g.close()

        # (3) Control: two REAL regimes each >= floor -> regime coverage satisfied (PASS overall).
        with tempfile.TemporaryDirectory() as td:
            g = GraveyardDB(Path(td)); seed(g, {"riskon_calm": 6, "riskoff_calm": 6})
            v = gate.compute_verdict(g, eval_param_version="v0")
            self.assertNotEqual(v.reason, "INSUFFICIENT_REGIME_COVERAGE",
                                "two real regimes each >= floor must satisfy coverage")
            self.assertTrue(v.passed, f"control should PASS, got {v.reason}")
            g.close()


# ---------------------------------------------------------------------------
# G2 — Section 11 safety veto fires
# ---------------------------------------------------------------------------

class GateSectionElevenVeto(unittest.TestCase):
    """Gate 2: validate_execution_safety must veto on every dangerous condition.
    These are hard structural guards — they must never be silently bypassed.
    Fail-before: removing any check would cause the assertion to fail.
    """

    def setUp(self):
        from src.core.schemas import validate_execution_safety
        self.ves = validate_execution_safety

    def test_g2_halted_ticker_vetoed(self):
        r = self.ves(bid=1.0, ask=1.02, avg_daily_volume=500000,
                     order_size_shares=100, is_halted=True)
        self.assertFalse(r.ok, "Halted ticker must be vetoed.")
        self.assertEqual(r.reason, "halted",
            f"Reason must be 'halted'; got '{r.reason}'.")

    def test_g2_no_two_sided_market_vetoed(self):
        """bid=0 or ask=0 → no_two_sided_market. CRKN-like delisted names hit this."""
        r = self.ves(bid=0.0, ask=0.0, avg_daily_volume=500000,
                     order_size_shares=10, is_halted=False)
        self.assertFalse(r.ok, "Zero bid/ask must be vetoed.")
        self.assertEqual(r.reason, "no_two_sided_market")

    def test_g2_wide_spread_vetoed(self):
        """Spread >2% (default threshold) must be vetoed. Thin microcap case."""
        # bid=0.10, ask=0.20 → spread = (0.20-0.10)/0.15 = 66.7% >> 2%
        r = self.ves(bid=0.10, ask=0.20, avg_daily_volume=500000,
                     order_size_shares=100, is_halted=False)
        self.assertFalse(r.ok, "Wide spread must be vetoed.")
        self.assertIn("spread_too_wide", r.reason,
            f"Reason must contain 'spread_too_wide'; got '{r.reason}'.")

    def test_g2_liquid_narrow_spread_passes(self):
        """Control: a liquid name with narrow spread must NOT be vetoed."""
        r = self.ves(bid=10.00, ask=10.02, avg_daily_volume=1_000_000,
                     order_size_shares=100, is_halted=False)
        self.assertTrue(r.ok,
            f"Liquid narrow-spread trade must pass safety. Got: {r.reason}.")


# ---------------------------------------------------------------------------
# G3 — EV engine returns None (no fabrication) on kill / budget / malformed
# ---------------------------------------------------------------------------

class GateEVEngineNoFab(unittest.TestCase):
    """Gate 3: build_ev_thesis must return None — never a fabricated thesis — on:
      - LLM reports killed
      - LLM reports budget_refused
      - LLM returns malformed (non-JSON) text
    Fail-before: the Phase 2 bug that fabricated a thesis on these paths would fail every assert.
    """

    def _run(self, llm, ticker="GATE"):
        from src.core.ev_engine import build_ev_thesis
        with tempfile.TemporaryDirectory() as td:
            g = GraveyardDB(Path(td))
            result = build_ev_thesis(
                ticker=ticker, event_type="8k",
                raw_filing_text="filing text " * 10,
                tradeable_capacity_usd=100.0,
                llm=llm, graveyard=g,
            )
            # Also check graveyard recorded a rejection (not a fill)
            rows = g._get_conn().execute(
                "SELECT COUNT(*) FROM trades WHERE ticker=? AND outcome='rejected'", (ticker,)
            ).fetchone()[0]
            g.close()
        return result, rows

    def test_g3_killed_returns_none_not_thesis(self):
        from src.core.llm_client import FakeLLMClient
        llm = FakeLLMClient(is_killed=lambda: True)
        result, rejections = self._run(llm)
        self.assertIsNone(result,
            "Killed path must return None, not a fabricated thesis. "
            "Pre-fix: returned a synthetic EVThesis with EV=-0.50.")
        self.assertGreater(rejections, 0,
            "Killed path must log rejection to graveyard.")

    def test_g3_budget_refused_returns_none(self):
        from src.core.llm_client import FakeLLMClient
        from src.core.budget import DailyBudget, BudgetConfig
        # Exhaust budget: cap=$0.0001 so any spend refuses
        budget = DailyBudget(BudgetConfig(daily_usd_cap=0.0001))
        budget.record_usage("haiku", 10000, 10000)  # exhaust it
        llm = FakeLLMClient(budget=budget)
        result, rejections = self._run(llm)
        self.assertIsNone(result,
            "Budget-refused path must return None, not a fabricated thesis.")

    def test_g3_malformed_response_returns_none(self):
        """LLM returns non-JSON → malformed_llm_output → None."""
        from src.core.llm_client import FakeLLMClient
        llm = FakeLLMClient()
        # Override canned to return non-JSON garbage
        if hasattr(llm, "set_canned"):
            llm.set_canned("GATE", "this is not json at all { broken")
        result, rejections = self._run(llm, ticker="GATE")
        self.assertIsNone(result,
            "Malformed LLM output must return None, not a fabricated thesis.")


# ---------------------------------------------------------------------------
# G4 — SafetyCore rejects direct loosening of protected params
# ---------------------------------------------------------------------------

class GateSafetyCoreFailClosed(unittest.TestCase):
    """Gate 4: direct mutation of protected SafetyCore params must raise SafetyCoreViolation.
    The 25% hard ceiling, event-risk cap, and budget cap are immutable via direct setattr.
    Fail-before: Phase 3 bug where _params.HARD_CEILING_PCT = 0.99 silently succeeded.
    """

    def setUp(self):
        # SafetyCore violation logger needs a logs dir; use a per-test temp dir.
        from src.core.safety_core import SafetyCore
        self._td = tempfile.mkdtemp()
        self._logs = Path(self._td) / "logs"
        self._logs.mkdir()
        SafetyCore.init_log(self._logs)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._td, ignore_errors=True)

    def test_g4_direct_setattr_ceiling_raises_violation(self):
        from src.core.safety_core import SafetyCore, SafetyCoreViolation
        original = SafetyCore.get_hard_ceiling_pct()
        try:
            with self.assertRaises(SafetyCoreViolation,
                    msg="Direct mutation of HARD_CEILING_PCT must raise SafetyCoreViolation. "
                        "Pre-fix: silently set 99% cap."):
                SafetyCore._params.HARD_CEILING_PCT = 0.99
        finally:
            # Value must be unchanged regardless
            self.assertEqual(SafetyCore.get_hard_ceiling_pct(), original,
                "Protected param value must not change after rejected mutation.")

    def test_g4_apply_safe_change_loosening_rejected(self):
        """apply_safe_change with a value ABOVE current ceiling must be rejected."""
        from src.core.safety_core import SafetyCore
        original = SafetyCore.get_hard_ceiling_pct()
        looser = min(original + 0.10, 0.99)  # definitely above current
        result = SafetyCore.apply_safe_change("HARD_CEILING_PCT", looser, evidence={})
        self.assertFalse(result,
            f"apply_safe_change loosening ({original} → {looser}) must return False. "
            "Pre-fix: would have applied and raised the cap.")
        self.assertEqual(SafetyCore.get_hard_ceiling_pct(), original,
            "Cap must not have changed after rejected loosening.")

    def test_g4_tighter_change_allowed(self):
        """Control: a tighter (lower) ceiling change must be accepted."""
        from src.core.safety_core import SafetyCore
        original = SafetyCore.get_hard_ceiling_pct()
        tighter = max(original - 0.05, 0.05)
        result = SafetyCore.apply_safe_change("HARD_CEILING_PCT", tighter, evidence={"reason": "gate_test"})
        # Restore for test isolation — down-only so can't go back autonomously; use human_restore
        try:
            if result:
                SafetyCore.human_restore("HARD_CEILING_PCT", original, authorized=True)
        except Exception:
            pass
        self.assertTrue(result,
            f"Tighter ceiling change ({original} → {tighter}) must be accepted by SafetyCore.")


# ---------------------------------------------------------------------------
# G5 — Runner e2e: run_paper opens + resolves with series-derived realized
# ---------------------------------------------------------------------------

class GateRunnerE2E(unittest.TestCase):
    """Gate 5: run_paper must open AND resolve a position, recording a realized_return_pct
    that is derived from the price series — NOT a hardcoded constant (e.g. 0.03 or -0.05)
    and NOT null on a successful round-trip.
    Fail-before: the R4 theater test never called run_paper; the old fabricated resolve
    recorded realized=const instead of series-derived.
    This test calls the ACTUAL run_paper entry point (not a reimplementation).
    """

    def test_g5_run_paper_opens_resolves_with_series_realized(self):
        from run_paper import run_paper
        from src.core.market_data import MockMarketData
        from src.core.llm_client import get_llm_client
        from src.core.reaction_layer import Trigger

        TICKER = "GRUN"
        ENTRY_PRICE = 10.0
        EXIT_PRICE = 9.5   # price drops after entry → pessimistic bid exit

        class OneShotFeed:
            def __init__(self, trig):
                self.trig = trig
                self._done = False
            def next_events(self, max_n=10):
                if not self._done:
                    self._done = True
                    return [self.trig]
                return []

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Two-price series: t0=entry, t1=lower exit (pessimistic paper fill from bid)
            md = MockMarketData(price_series={TICKER: [("t0", ENTRY_PRICE), ("t1", EXIT_PRICE)]})
            llm = get_llm_client(fake=True)
            if hasattr(llm, "set_canned"):
                llm.set_canned(TICKER, json.dumps({
                    "ticker": TICKER, "event_type": "8k",
                    "upside_pct": 6, "p_upside": 0.55,
                    "downside_pct": -4, "p_downside": 0.3,
                    "expected_value_pct": 2.1,
                    "prior_accuracy_on_name": 0.6,
                    "what_informed_holders_may_know_that_we_dont":
                        "Management insiders may know about imminent contract award not yet public.",
                    "tradeable_capacity_usd": 80.0,
                }))

            trig = Trigger(f"e-{TICKER}-001", TICKER, "8k", "filing text " * 20, False)
            summary = run_paper(
                data_dir=d, logs_dir=d / "logs",
                tickers=[TICKER], use_real_llm=False,
                market_data=md, event_feed=OneShotFeed(trig),
                max_cycles=2, hold_bars=0,  # hold_bars=0 → immediate resolve
                llm=llm, positions_path=d / "p.json", source="fake",
            )

            actions = [r.get("action") for r in summary.get("results", [])]
            self.assertIn("opened", actions,
                f"run_paper must open a position; got results={summary.get('results')}. "
                "Pre-fix (R4 theater): test never called run_paper at all.")
            self.assertIn("resolved", actions,
                f"run_paper must resolve the position (hold_bars=0); got {summary.get('results')}.")

            # realized must be non-null and derived from the price series
            resolved = [r for r in summary.get("results", []) if r.get("action") == "resolved"]
            self.assertTrue(resolved, "Must have at least one resolved result.")
            realized = resolved[0].get("realized")
            self.assertIsNotNone(realized,
                "realized_return_pct must not be null — no-fab rule. "
                "Pre-fix: returned NULL or constant 0.03/-0.05.")
            # Not a hardcoded constant from the old fabrication
            self.assertNotIn(realized, (0.03, -0.05, 3.0, -5.0),
                f"realized={realized} looks like a fabrication constant. Must be series-derived.")
            # Must be a sensible float (not EV% used as price proxy)
            self.assertIsInstance(realized, float,
                f"realized must be a float, got {type(realized)}.")
            self.assertGreater(abs(realized), 0.0,
                "realized must be non-zero (price series has movement).")

            # Graveyard must have a filled_paper_resolved row with real realized
            g = GraveyardDB(d)
            rows = g._get_conn().execute(
                "SELECT realized_return_pct FROM trades "
                "WHERE ticker=? AND outcome='filled_paper_resolved'",
                (TICKER,)
            ).fetchall()
            g.close()
            self.assertTrue(rows, "Graveyard must have a filled_paper_resolved row.")
            db_realized = rows[0][0]
            self.assertIsNotNone(db_realized,
                "Graveyard realized_return_pct must not be null on successful resolve.")


# ---------------------------------------------------------------------------
# G6 — Tenant isolation: ownership ledger gate + confirm-fail rollback
# ---------------------------------------------------------------------------

class GateTenantIsolation(unittest.TestCase):
    """Gate 6: hood shares the account with other fleet agents. It must never sell a
    position it did not itself open, and a confirm-fail on a buy must not leave a
    phantom credit in hood's own ledger.

    Fail-before (verified by the auditor by reverting the fix and re-running):
    - test_g6_foreign_position_not_sold: with the sell-gate removed, the broker mock
      reports the foreign position and the mock fill logic happily sells it —
      AssertionError: False != True (result.success was True, should be False).
    - test_g6_confirm_fail_rolls_back_ledger: with the ledger-rollback call removed
      from run_paper's confirm-fail branch, the ledger still reports has_position=True
      after the unwind — AssertionError: True is not false.
    """

    def test_g6_foreign_position_not_sold(self):
        """A position appears in the shared broker account that hood never bought
        (e.g. opened by another fleet agent on the same account). Hood's own ledger
        has no record of it. The executor must veto any sell with not_in_hood_ledger,
        regardless of what the broker reports.
        """
        from src.core.executor import Executor
        from src.core.schemas import RunMode, EVThesis
        from src.core.ownership_ledger import OwnershipLedger
        from src.mcp.robinhood_client import MockRobinhoodClient, Position

        with tempfile.TemporaryDirectory() as td:
            ledger = OwnershipLedger(Path(td))  # empty: hood owns nothing
            client = MockRobinhoodClient(starting_cash=100000.0)
            # Simulate another tenant's position sitting in the shared broker account.
            client._positions["FOREIGN"] = Position("FOREIGN", 50.0, 8.0, 400.0)

            ex = Executor(client=client, run_mode=RunMode.PAPER, ownership_ledger=ledger)
            thesis = EVThesis(
                ticker="FOREIGN", event_type="8k",
                upside_pct=10, p_upside=0.4, downside_pct=-10, p_downside=0.3,
                expected_value_pct=1.0, prior_accuracy_on_name=0.5,
                what_informed_holders_may_know_that_we_dont="humility " * 5,
                tradeable_capacity_usd=100,
            )
            result = ex.execute_thesis(thesis, side="sell")

            self.assertFalse(result.success,
                f"Hood must not be able to sell a position it didn't open. "
                f"Pre-fix (no ledger gate): result.success was True (mock happily sold it).")
            self.assertEqual(result.veto_reason, "not_in_hood_ledger",
                f"Expected veto reason 'not_in_hood_ledger', got {result.veto_reason!r}.")
            # The foreign position must be untouched in the broker book.
            self.assertEqual(client._positions["FOREIGN"].shares, 50.0,
                "Foreign position must be untouched after the vetoed sell attempt.")

    def test_g6_confirm_fail_rolls_back_ledger(self):
        """A buy order is submitted and the broker acks success (fill price returned),
        but the post-submit confirm check (get_positions) does not show the position —
        a submit/book mismatch. run_paper must unwind (cancel_all) AND must not leave
        the ownership ledger believing hood owns shares it never actually confirmed.
        Without the rollback, the ledger would retain a phantom credit from the
        executor's optimistic buy-fill write, and a LATER real position on the same
        ticker would silently blend its cost basis with phantom shares.
        """
        from run_paper import run_paper
        from src.core.market_data import MockMarketData
        from src.core.llm_client import get_llm_client
        from src.core.reaction_layer import Trigger
        from src.core.ownership_ledger import OwnershipLedger
        from src.mcp.robinhood_client import MockRobinhoodClient, OrderResult

        class FakeBrokerConfirmFail(MockRobinhoodClient):
            """Acks the buy (so the executor's optimistic ledger write fires), but
            get_positions() always returns [] — the post-submit confirm mismatch.
            """
            def place_limit_order(self, ticker, side, shares, limit_price, time_in_force="day", quote=None):
                oid = f"confirm-fail-{len(self._orders)}"
                fill_price = float(limit_price) if limit_price else 10.0
                return OrderResult(True, oid, shares, fill_price, "sim-ack-no-book-pos")

            def get_positions(self):
                return []

        class OneShotFeed:
            def __init__(self, trig):
                self.trig = trig
                self._done = False
            def next_events(self, max_n=10):
                if not self._done:
                    self._done = True
                    return [self.trig]
                return []

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            md = MockMarketData(price_series={"RBACK": [("t0", 12.0)]})
            llm = get_llm_client(fake=True)
            if hasattr(llm, "set_canned"):
                llm.set_canned("RBACK", json.dumps({
                    "ticker": "RBACK", "event_type": "8k", "upside_pct": 4, "p_upside": 0.5,
                    "downside_pct": -8, "p_downside": 0.3, "expected_value_pct": 0.0,
                    "prior_accuracy_on_name": 0.6,
                    "what_informed_holders_may_know_that_we_dont": "Sufficient humility for confirm-fail test.",
                    "tradeable_capacity_usd": 50.0,
                }))
            trig = Trigger("e-rollback-001", "RBACK", "8k", "text " * 20, False)
            fake_client = FakeBrokerConfirmFail(starting_cash=100000.0)

            run_paper(
                data_dir=d, logs_dir=d / "l", tickers=["RBACK"], use_real_llm=False,
                market_data=md, event_feed=OneShotFeed(trig), max_cycles=1, hold_bars=0,
                llm=llm, positions_path=d / "p.json", source="fake",
                client=fake_client,
            )

            # run_paper's _make_paper_executor creates the ledger at data_dir; re-open it
            # the same way to see the actual persisted state after the run.
            ledger = OwnershipLedger(d)
            self.assertFalse(ledger.has_position("RBACK"),
                "Confirm-fail must roll back the ownership ledger — hood must not believe "
                "it owns a position whose fill was never confirmed against the broker book. "
                "Pre-fix (no rollback call in the confirm-fail branch): has_position was True.")


# ---------------------------------------------------------------------------
# G7 — Live gate hard-raises
# ---------------------------------------------------------------------------

class GateLiveHardRaise(unittest.TestCase):
    """Gate 7: live order placement is unconditionally blocked until the human go-live
    gate (SafetyCore.is_live_enabled()) is explicitly armed. This must hold even when
    every other gate (risk, safety, idempotency) would otherwise approve the trade.

    Fail-before (verified by the auditor by reverting the fix and re-running):
    test_g7_live_mode_vetoed_when_not_armed: with the live-gate check removed from
    execute_thesis, the order proceeds to the broker leaf and result.success is True —
    AssertionError: True is not false.
    """

    def test_g7_live_mode_vetoed_when_not_armed(self):
        """RunMode.LIVE with the gate unarmed must veto with live_not_enabled, before
        any sizing, quote fetch, or risk check — even on an otherwise-clean thesis.
        """
        from src.core.executor import Executor
        from src.core.schemas import RunMode, EVThesis
        from src.core.safety_core import SafetyCore
        from src.mcp.robinhood_client import MockRobinhoodClient

        orig_live = SafetyCore.is_live_enabled
        try:
            SafetyCore.is_live_enabled = staticmethod(lambda: False)
            client = MockRobinhoodClient(starting_cash=100000.0)
            ex = Executor(client=client, run_mode=RunMode.LIVE)
            thesis = EVThesis(
                ticker="LIVE", event_type="8k",
                upside_pct=10, p_upside=0.4, downside_pct=-10, p_downside=0.3,
                expected_value_pct=1.0, prior_accuracy_on_name=0.5,
                what_informed_holders_may_know_that_we_dont="humility " * 5,
                tradeable_capacity_usd=100,
            )
            result = ex.execute_thesis(thesis, side="buy")
            self.assertFalse(result.success,
                "Live order must be vetoed when the go-live gate is not armed. "
                "Pre-fix (no live-gate check): result.success was True (order reached the broker).")
            self.assertEqual(result.veto_reason, "live_not_enabled",
                f"Expected veto reason 'live_not_enabled', got {result.veto_reason!r}.")
            self.assertIsNone(result.order_id,
                "No order_id should exist — the broker leaf must never be reached.")
        finally:
            SafetyCore.is_live_enabled = orig_live

    def test_g7_paper_mode_unaffected_by_gate(self):
        """Sanity: the live gate must not accidentally veto PAPER mode (which has no
        such requirement). Confirms G7's fix is scoped to RunMode.LIVE only.
        """
        from src.core.executor import Executor
        from src.core.schemas import RunMode, EVThesis
        from src.core.safety_core import SafetyCore
        from src.mcp.robinhood_client import MockRobinhoodClient

        orig_live = SafetyCore.is_live_enabled
        try:
            SafetyCore.is_live_enabled = staticmethod(lambda: False)
            client = MockRobinhoodClient(starting_cash=100000.0)
            ex = Executor(client=client, run_mode=RunMode.PAPER)
            thesis = EVThesis(
                ticker="PAPR", event_type="8k",
                upside_pct=10, p_upside=0.4, downside_pct=-10, p_downside=0.3,
                expected_value_pct=1.0, prior_accuracy_on_name=0.5,
                what_informed_holders_may_know_that_we_dont="humility " * 5,
                tradeable_capacity_usd=100,
            )
            result = ex.execute_thesis(thesis, side="buy")
            self.assertNotEqual(result.veto_reason, "live_not_enabled",
                "PAPER mode must not be vetoed by the live gate.")
        finally:
            SafetyCore.is_live_enabled = orig_live


# ---------------------------------------------------------------------------
# G8 — Watcher/reasoner split: idle cycles make zero LLM calls
# ---------------------------------------------------------------------------

class GateWatcherReasonerSplit(unittest.TestCase):
    """Gate 8: hood previously bled funds by invoking an LLM on every loop iteration.
    The fix is a hard architectural split: the always-on watcher (event feed + deterministic
    pre-filter) makes zero LLM calls; the LLM reasoner fires only when the watcher actually
    yields a trigger. This locks that split so a future change cannot reintroduce a per-cycle
    LLM call.

    Fail-before (verified by the auditor): with a single unconditional `llm.complete(...)`
    call inserted into run_paper's per-cycle loop body (simulating the original per-iteration
    bug), this test fails with len(llm.calls) == 5 instead of 0.
    """

    def test_g8_idle_watcher_makes_zero_llm_calls(self):
        """N idle cycles (event feed yields nothing) must result in exactly zero LLM call
        attempts — not zero spend, zero ATTEMPTS. The reasoner must never be invoked at all
        when there is nothing for the watcher to react to.
        """
        from run_paper import run_paper, FakeEventFeed
        from src.core.llm_client import FakeLLMClient
        from src.core.market_data import MockMarketData

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            llm = FakeLLMClient()
            run_paper(
                data_dir=d, logs_dir=d / "l",
                market_data=MockMarketData(price_series={}),
                event_feed=FakeEventFeed([]),  # zero triggers, every cycle, for the whole run
                llm=llm, max_cycles=5, hold_bars=0,
            )
            self.assertEqual(len(llm.calls), 0,
                f"Idle watcher must make ZERO LLM call attempts across 5 cycles with no "
                f"candidate events. Got {len(llm.calls)} calls: "
                f"{[c.model for c in llm.calls]}. Pre-fix (per-cycle LLM call bug): this was 5.")


# ---------------------------------------------------------------------------
# G9 — Hard daily spend breaker: near-zero cap blocks LLM call AND trade
# ---------------------------------------------------------------------------

class GateSpendBreaker(unittest.TestCase):
    """Gate 9: the daily spend breaker must be a hard stop, not a suggestion. With the cap
    set to effectively $0, a genuine candidate event must reach zero BILLED LLM calls and
    zero trades — degrading to no-trade (fail-safe), never failing open.

    Fail-before (verified by the auditor): with the budget check removed from
    FakeLLMClient.complete() (simulating a regression that lets a call through despite a
    refused budget), this test fails — the canned thesis materializes and the candidate
    is no longer dropped with a budget reason.
    """

    def test_g9_near_zero_cap_blocks_llm_call_and_trade(self):
        from run_paper import run_paper
        from src.core.llm_client import FakeLLMClient
        from src.core.budget import DailyBudget, BudgetConfig
        from src.core.market_data import MockMarketData
        from src.core.reaction_layer import Trigger

        class OneShotFeed:
            def __init__(self, trig):
                self.trig = trig
                self._done = False
            def next_events(self, max_n=10):
                if not self._done:
                    self._done = True
                    return [self.trig]
                return []

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Isolated near-zero-cap budget injected ONLY into the LLM client — does not touch
            # the global SafetyCore ratchet (which is down-only and shared across the test
            # process; permanently tanking it here would break every later test).
            near_zero_budget = DailyBudget(BudgetConfig(daily_usd_cap=0.0000001, log_path=d / "b.json"))
            llm = FakeLLMClient(budget=near_zero_budget)
            llm.set_canned("CAPTST", json.dumps({
                "ticker": "CAPTST", "event_type": "8k", "upside_pct": 20, "p_upside": 0.5,
                "downside_pct": -10, "p_downside": 0.2, "expected_value_pct": 8.0,
                "prior_accuracy_on_name": 0.6,
                "what_informed_holders_may_know_that_we_dont": "Sufficient humility for the cap test.",
                "tradeable_capacity_usd": 1000.0, "event_risk_flags": [], "source_filings": [],
            }))
            trig = Trigger("e-cap-001", "CAPTST", "8k", "material restructuring text " * 10, False)

            summary = run_paper(
                data_dir=d, logs_dir=d / "l", tickers=["CAPTST"], use_real_llm=False,
                market_data=MockMarketData(price_series={"CAPTST": [("t0", 10.0)]}),
                event_feed=OneShotFeed(trig), llm=llm, max_cycles=1, hold_bars=0,
            )

            self.assertEqual(near_zero_budget.call_count(), 0,
                f"Near-zero cap must block ALL billed LLM calls. Got "
                f"{near_zero_budget.call_count()} billed calls. Pre-fix (budget check removed): "
                f"this would be > 0.")
            actions = [r.get("action") for r in summary.get("results", [])]
            self.assertNotIn("opened", actions,
                f"No trade may occur when the budget is exhausted (fail-safe, not fail-open). "
                f"Got results={summary.get('results')}.")
            # Never silently drop: the candidate's budget-refusal must be recorded, not vanish.
            self.assertIn("budget_dropped", actions,
                f"A candidate dropped purely due to budget exhaustion must be recorded as "
                f"budget_dropped, not silently disappear. Got results={summary.get('results')}.")


# ---------------------------------------------------------------------------
# G10 — DynamicEDGARFeed's event_type must survive tier1's filter
# ---------------------------------------------------------------------------

class GateDynamicFeedEventType(unittest.TestCase):
    """Gate 10: found live on 2026-07-01 via hood_state.json's candidate_events_today metric
    (added the day before this bug was found) — DynamicEDGARFeed tagged every trigger with
    event_type=f"8-K items={item_label}" (e.g. "8-K items=2.02,5.02"), which NEVER matches
    ReactionLayer.allowed_event_types ("8k","spinoff","restructuring","earnings","fda"). Every
    single candidate from this feed was silently rejected at the very first tier1 check
    (event_type_not_allowed) — before triage, before any LLM call, before any Graveyard
    record — for the entire 3 days the feed had been live. 19 candidates flagged that day,
    0 LLM calls, 0 spend, 0 new graveyard rows. This drives DynamicEDGARFeed.next_events()
    end-to-end (real class, fake HTTP + fake EdgarClient/MarketData — no network) and checks
    the actual Trigger objects it yields, not a reimplementation of its logic.

    Fail-before (verified by the auditor): with the old `f"8-K items={item_label}"` format
    restored, this test fails — the yielded Trigger's event_type is rejected by tier1.
    """

    def test_g10_dynamic_feed_8k_event_type_passes_tier1(self):
        import urllib.request
        from run_paper import DynamicEDGARFeed
        from src.data.edgar import EdgarClient
        from src.core.reaction_layer import ReactionLayer
        from src.mcp.robinhood_client import Quote

        class FakeMarketData:
            def get_quote(self, ticker):
                # Passes DynamicEDGARFeed's free quote pre-filter (price/ADV/spread).
                return Quote(ticker, 9.95, 10.05, 10.00, 500000, 5_000_000.0, is_halted=False,
                             timestamp="2026-07-01T14:00:00Z")

        with tempfile.TemporaryDirectory() as td:
            edgar = EdgarClient(cache_dir=Path(td))
            # Pre-populate the ticker map so _ensure_cik_map short-circuits (no network).
            edgar._tickers_cache = {"0": {"cik_str": 1234, "ticker": "GATE10"}}

            fake_ref = None
            from src.data.edgar import FilingRef
            fake_ref = FilingRef(form="8-K", accession="0001234-26-000099",
                                  filing_date="2026-07-01", primary_doc_url="http://example.test")
            edgar.get_recent_filings = lambda cik, forms=None, limit=20: [fake_ref]
            edgar.get_filing_raw = lambda ref, ticker=None: "Item 2.02 material 8-K filing text " * 10

            feed = DynamicEDGARFeed(edgar, FakeMarketData())

            fake_efts_response = json.dumps({
                "hits": {
                    "total": {"value": 1},
                    "hits": [{"_source": {
                        "adsh": "0001234-26-000099",
                        "ciks": ["0000001234"],
                        "items": ["2.02", "5.02"],
                        "file_num": ["001-12345"],
                        "form": "8-K",
                        "file_date": "2026-07-01",
                    }}],
                },
            }).encode("utf-8")

            class FakeHTTPResponse:
                def __init__(self, data):
                    self._data = data
                def read(self):
                    return self._data
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            orig_urlopen = urllib.request.urlopen
            def fake_urlopen(req, timeout=None):
                return FakeHTTPResponse(fake_efts_response)
            urllib.request.urlopen = fake_urlopen
            try:
                events = feed.next_events(max_n=5)
            finally:
                urllib.request.urlopen = orig_urlopen

            self.assertEqual(len(events), 1, f"expected exactly 1 event, got {events}")
            trig = events[0]
            self.assertEqual(trig.ticker, "GATE10")

            reaction = ReactionLayer(llm=None, executor=None, risk=None)  # only need _tier1_filter's event_type check
            passed_type_check = trig.event_type in reaction.allowed_event_types
            self.assertTrue(passed_type_check,
                f"DynamicEDGARFeed's event_type={trig.event_type!r} must be one of "
                f"{reaction.allowed_event_types} to survive ReactionLayer's tier1 filter. "
                f"Pre-fix: event_type was '8-K items=2.02,5.02', which fails this check "
                f"100% of the time — the entire feed has been silently inert since deploy.")


# ---------------------------------------------------------------------------
# G11 — Filing text must be HTML/XBRL-stripped BEFORE truncation
# ---------------------------------------------------------------------------

class GateFilingTextTruncation(unittest.TestCase):
    """Gate 11: found live on 2026-07-06 — 100% of real candidates that day were rejected by
    Haiku triage with the identical reason "8-K header only; no substantive event content
    provided." Root cause: modern SEC filings are inline-XBRL HTML — heavy XML namespace
    declarations, an <ix:header> XBRL metadata block, and per-fact <ix:nonNumeric>/<span
    id="xdx_..."> tagging wrap the cover page BEFORE any Item disclosure text. Both event
    feeds truncated the RAW HTML to 8000 chars before sending it to the LLM, so the entire
    truncation budget was consumed by markup — the actual Item 1.01/2.02/etc text never
    appeared. Verified against a real cached filing (accession 0001213900-26-075248): raw
    51,124 chars, "Item 1.01" absent from the first 8000 raw chars, present at ~2,300 chars
    after `strip_html_to_text()` cleaning.

    Fail-before (verified by the auditor): with the feed wiring reverted to truncate raw HTML
    directly (no strip_html_to_text call), this test fails — the resulting Trigger's
    raw_filing_text (first 8000 chars) does not contain the Item text at all.
    """

    # A compact but representative inline-XBRL fixture: real-shaped namespace/header noise
    # (enough to exceed 8000 raw chars on its own) followed by the actual Item disclosure —
    # mirrors the live 2026-07-06 finding without embedding a full 51KB real filing.
    _XBRL_NOISE_BLOCK = (
        '<?xml version="1.0" encoding="ASCII"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:xs="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
        'xmlns:dei="http://xbrl.sec.gov/dei/2026" xmlns:us-gaap="http://fasb.org/us-gaap/2026">\n'
        '<head><title></title></head>\n'
        '<body>\n<div style="display: none">\n<ix:header>\n<ix:hidden>\n'
        + "".join(
            f'<ix:nonNumeric contextRef="AsOf2026-06-30" id="Fact{i:06d}" '
            f'name="dei:SomeMetadataField{i}">value_{i}</ix:nonNumeric>\n'
            for i in range(80)  # padding to realistically exceed the 8000-char raw budget
        )
        + "</ix:hidden>\n</ix:header>\n</div>\n"
        '<p style="text-align:center"><b>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</b></p>\n'
        '<p style="text-align:center"><b>FORM 8-K</b></p>\n'
        '<p style="text-align:center"><b>CURRENT REPORT</b></p>\n'
    )
    _ITEM_TEXT = (
        "<p><b>Item 1.01. Entry into a Material Definitive Agreement.</b></p>\n"
        "<p>On the date hereof, the Company entered into a securities purchase agreement "
        "for gross proceeds of $15 million, before deducting placement agent fees.</p>\n"
        "</body></html>"
    )

    def test_g11_precondition_raw_truncation_misses_item_text(self):
        """Precondition: confirm the fixture actually reproduces the bug — the raw HTML's
        first 8000 chars must NOT contain the Item disclosure (otherwise this test proves
        nothing)."""
        raw = self._XBRL_NOISE_BLOCK + self._ITEM_TEXT
        self.assertGreater(len(self._XBRL_NOISE_BLOCK), 8000,
            "fixture precondition: the noise block alone must exceed the truncation budget")
        self.assertNotIn("Item 1.01", raw[:8000],
            "fixture precondition failed: Item text must NOT be reachable by naive raw truncation")

    def test_g11_strip_html_to_text_surfaces_item_within_budget(self):
        """The actual fix: strip_html_to_text() must move the Item disclosure text within
        reach of an 8000-char truncation budget.
        """
        from src.data.edgar import strip_html_to_text
        raw = self._XBRL_NOISE_BLOCK + self._ITEM_TEXT
        cleaned = strip_html_to_text(raw)
        self.assertIn("Item 1.01", cleaned[:8000],
            f"strip_html_to_text must surface the Item disclosure within the truncation "
            f"budget. Pre-fix (raw truncation): 'Item 1.01' never appears in the first 8000 "
            f"chars. cleaned[:200]={cleaned[:200]!r}")
        self.assertIn("Material Definitive Agreement", cleaned[:8000])

    def test_g11_dynamic_feed_yields_readable_trigger_text(self):
        """Drives DynamicEDGARFeed.next_events() end-to-end (real class, fake HTTP — no
        network, no reimplementation) with the XBRL-heavy fixture as the filing content, and
        asserts the resulting Trigger.raw_filing_text (what Haiku actually receives) contains
        the Item disclosure — not just cover-page markup.
        """
        import urllib.request
        from run_paper import DynamicEDGARFeed
        from src.data.edgar import EdgarClient, FilingRef
        from src.mcp.robinhood_client import Quote

        class FakeMarketData:
            def get_quote(self, ticker):
                return Quote(ticker, 9.95, 10.05, 10.00, 500000, 5_000_000.0, is_halted=False,
                             timestamp="2026-07-06T14:00:00Z")

        with tempfile.TemporaryDirectory() as td:
            edgar = EdgarClient(cache_dir=Path(td))
            edgar._tickers_cache = {"0": {"cik_str": 5678, "ticker": "GATE11"}}
            fake_ref = FilingRef(form="8-K", accession="0005678000-26-000111",
                                  filing_date="2026-07-06", primary_doc_url="http://example.test")
            edgar.get_recent_filings = lambda cik, forms=None, limit=20: [fake_ref]
            edgar.get_filing_raw = lambda ref, ticker=None: self._XBRL_NOISE_BLOCK + self._ITEM_TEXT

            feed = DynamicEDGARFeed(edgar, FakeMarketData())

            fake_efts_response = json.dumps({
                "hits": {"total": {"value": 1}, "hits": [{"_source": {
                    "adsh": "0005678000-26-000111",
                    "ciks": ["0000005678"],
                    "items": ["1.01"],
                    "file_num": ["001-99999"],
                    "form": "8-K",
                    "file_date": "2026-07-06",
                }}]},
            }).encode("utf-8")

            class FakeHTTPResponse:
                def __init__(self, data):
                    self._data = data
                def read(self):
                    return self._data
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            orig_urlopen = urllib.request.urlopen
            urllib.request.urlopen = lambda req, timeout=None: FakeHTTPResponse(fake_efts_response)
            try:
                events = feed.next_events(max_n=5)
            finally:
                urllib.request.urlopen = orig_urlopen

            self.assertEqual(len(events), 1, f"expected exactly 1 event, got {events}")
            trig = events[0]
            self.assertIn("Item 1.01", trig.raw_filing_text,
                f"DynamicEDGARFeed must yield readable disclosure text, not markup-crowded "
                f"HTML. Pre-fix: raw_filing_text[:200]={trig.raw_filing_text[:200]!r} would "
                f"be pure XBRL/namespace noise with no Item text.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
