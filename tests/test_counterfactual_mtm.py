"""G14: counterfactual mark-to-market of vetoed +EV theses.

Fail-before evidence (captured before src/core/counterfactual.py existed):
    ModuleNotFoundError: No module named 'src.core.counterfactual'
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from src.core.schemas import EVThesis
from src.core.storage import GraveyardDB
from src.core.market_data import MockMarketData
from src.core.counterfactual import resolve_counterfactuals


def _thesis(ticker: str, ev_pct: float, ts: str) -> EVThesis:
    return EVThesis(
        ticker=ticker,
        event_type="8k",
        upside_pct=abs(ev_pct) * 3,
        p_upside=0.6,
        downside_pct=-abs(ev_pct) * 2,
        p_downside=0.25,
        expected_value_pct=ev_pct,
        prior_accuracy_on_name=0.5,
        what_informed_holders_may_know_that_we_dont="Unknown.",
        tradeable_capacity_usd=10000,
        event_risk_flags=[],
        timestamp=ts,
    )


class TestCounterfactualMTM(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graveyard = GraveyardDB(Path(self.tmp.name))

    def tearDown(self):
        self.graveyard.close()
        self.tmp.cleanup()

    def test_resolves_vetoed_positive_ev_thesis_past_horizon(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        thesis = _thesis("FOO", ev_pct=4.5, ts=old_ts)
        self.graveyard.record_rejection(
            thesis,
            "6 adversarial findings. Deterministic clean=True. High-sev=3.",
            regime="riskon_calm",
            meta={"adversarial_findings": [], "deterministic_clean": True, "deterministic_flags": [], "ref_price": 10.0},
        )

        market = MockMarketData(price_series={"FOO": [("t0", 11.5)]})
        n = resolve_counterfactuals(self.graveyard, market, horizon_days=5)

        self.assertEqual(n, 1)
        conn = self.graveyard._get_conn()
        rows = conn.execute(
            "SELECT ticker, outcome, realized_return_pct, meta FROM trades WHERE outcome='counterfactual_resolved'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "FOO")
        # bid = 11.5 * (1 - spread/2); pessimistic sell at bid*0.985, same discipline as run_paper.py real resolve
        self.assertGreater(rows[0]["realized_return_pct"], 0)  # thesis was right, auditor's veto cost money here

    def test_does_not_resolve_before_horizon(self):
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        thesis = _thesis("BAR", ev_pct=3.0, ts=recent_ts)
        self.graveyard.record_rejection(thesis, "auditor rejected", meta={"ref_price": 5.0})

        market = MockMarketData(price_series={"BAR": [("t0", 5.5)]})
        n = resolve_counterfactuals(self.graveyard, market, horizon_days=5)

        self.assertEqual(n, 0)

    def test_skips_negative_ev_rejections(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        thesis = _thesis("BAZ", ev_pct=-2.0, ts=old_ts)
        self.graveyard.record_rejection(thesis, "auditor rejected", meta={"ref_price": 5.0})

        market = MockMarketData(price_series={"BAZ": [("t0", 5.5)]})
        n = resolve_counterfactuals(self.graveyard, market, horizon_days=5)

        self.assertEqual(n, 0)

    def test_idempotent_does_not_double_resolve(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        thesis = _thesis("QUX", ev_pct=4.5, ts=old_ts)
        self.graveyard.record_rejection(thesis, "auditor rejected", meta={"ref_price": 10.0})

        market = MockMarketData(price_series={"QUX": [("t0", 11.5)]})
        first = resolve_counterfactuals(self.graveyard, market, horizon_days=5)
        second = resolve_counterfactuals(self.graveyard, market, horizon_days=5)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)  # already resolved, not re-resolved

    def test_never_mutates_original_row_append_only(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        thesis = _thesis("APP", ev_pct=4.5, ts=old_ts)
        self.graveyard.record_rejection(thesis, "auditor rejected", meta={"original": True, "ref_price": 10.0})

        conn = self.graveyard._get_conn()
        before = dict(conn.execute("SELECT * FROM trades WHERE ticker='APP'").fetchone())

        market = MockMarketData(price_series={"APP": [("t0", 11.5)]})
        resolve_counterfactuals(self.graveyard, market, horizon_days=5)

        after = dict(conn.execute("SELECT * FROM trades WHERE ticker='APP' AND outcome='rejected'").fetchone())
        self.assertEqual(before, after)  # original rejection row untouched — new row inserted instead


if __name__ == "__main__":
    unittest.main()
