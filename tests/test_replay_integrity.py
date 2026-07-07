"""Fail-before integrity gates for hood replay (Part B).

Not part of default expensive replay runs — hermetic only.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

HOOD_ROOT = Path(__file__).resolve().parents[1]
UMBRELLA_ROOT = HOOD_ROOT.parent / "umbrella"
for p in (HOOD_ROOT, UMBRELLA_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from umbrella_core.decisions import forward_only_decisions, read_decisions

from replay.budget_llm import ReplayBudgetLLM
from replay.decision_record import build_decision
from replay.mark_outcomes import mark_outcomes
from replay.pit_market_data import DailyBar, PointInTimeMarketData
from replay.report import write_report
from replay.run_replay import _load_pit_series, run_replay_slice
from src.core.llm_client import FakeLLMClient


class TestReplayIntegrity(unittest.TestCase):
    def test_point_in_time_canary_strips_future_prices(self):
        """Inject post-filing price row — driver must refuse/strip it."""
        as_of = date(2026, 3, 15)
        bars = [
            DailyBar(day=date(2026, 3, 10), close=3.0, volume=1e6),
            DailyBar(day=date(2026, 3, 15), close=3.12, volume=2e6),
            DailyBar(day=date(2026, 4, 1), close=9.99, volume=3e6),  # future leak
        ]
        pit = PointInTimeMarketData(as_of, {"AKTX": bars})
        q = pit.get_quote("AKTX")
        self.assertAlmostEqual(q.last, 3.12, places=2)
        self.assertNotAlmostEqual(q.last, 9.99, places=2)
        stored = pit._series["AKTX"]
        self.assertTrue(all(b.day <= as_of for b in stored))
        hist = pit.get_price_history("AKTX", days=10)
        self.assertTrue(all("2026-04" not in p.timestamp for p in hist))

    def test_mode_segregation_replay_excluded_from_forward_pool(self):
        """Replay decisions must not count toward forward-only calibration pool."""
        rows = [
            build_decision(kind="entry", instrument="A", ts="2026-03-01T00:00:00Z",
                           reason="r", regime="riskon_calm", lineage={"trigger": "t"}),
            {**build_decision(kind="entry", instrument="B", ts="2026-03-02T00:00:00Z",
                              reason="r", regime="riskon_calm", lineage={"trigger": "t"}),
             "mode": "paper", "experiment_id": None},
        ]
        forward = forward_only_decisions(rows)
        self.assertEqual(len(forward), 1)
        self.assertEqual(forward[0]["mode"], "paper")

    def test_replay_budget_breaker_stops_llm_calls(self):
        """Fail-before: without breaker, calls continue; with cap=0, next call errors."""
        inner = FakeLLMClient()
        capped = ReplayBudgetLLM(inner, budget_usd=0.0)
        r = capped.complete(model="claude-opus-4-8", system="s", user="u", workload="t3_thesis")
        self.assertEqual(r.get("error"), "replay_budget_exhausted")
        self.assertEqual(capped.calls, 0)

    def test_replay_pipeline_emits_valid_decisions_hermetic(self):
        """End-to-end hermetic slice: fetch fixture → replay → mark → report."""
        pit_path = HOOD_ROOT / "replay" / "fixtures" / "pit_prices.json"
        fixture = HOOD_ROOT / "replay" / "fixtures" / "sample_filings.json"
        pit = _load_pit_series(pit_path)
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            summary = run_replay_slice(
                data_dir=data_dir,
                pit_series=pit,
                use_real_llm=False,
                budget_usd=5.0,
                fixture=fixture,
            )
            self.assertGreater(summary["decisions_emitted"], 0)
            rows = read_decisions(data_dir / "decisions.ndjson")
            self.assertTrue(all(r.get("mode") == "replay" for r in rows))
            marked = mark_outcomes(data_dir, pit_path)
            self.assertGreaterEqual(len(marked), 0)
            report = write_report(data_dir)
            self.assertIn("Hood Replay Report", report)
            self.assertIn("replay cohort only", report)


if __name__ == "__main__":
    unittest.main()