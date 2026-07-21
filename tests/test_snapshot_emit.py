"""Fail-before tests: hood umbrella snapshot is schema-valid, fail-safe, no fabricated capital."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# hood root on path
import sys

HOOD_ROOT = Path(__file__).resolve().parents[1]
if str(HOOD_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOD_ROOT))

from snapshot_emit import build_snapshot_dict, emit_snapshot  # noqa: E402


class TestHoodSnapshotEmit(unittest.TestCase):
    def test_schema_valid_from_real_shaped_state(self) -> None:
        from umbrella_core.snapshot import validate_snapshot

        hood_state = {
            "agent": "hood",
            "timestamp": "2026-07-17T23:11:30.054588+00:00",
            "stage": "paper",
            "live_enabled": False,
            "positions": [],
            "cash_sleeve": 500.0,
            "nav_sleeve": 500.0,
            "open_theses": [],
            "last_event_ts": None,
            "health": "budget_exhausted",
            "regime": "riskon_calm",
            "budget_remaining_usd": 0.0013,
            "llm_metrics": {
                "candidate_events_today": 29,
                "llm_calls_today": 11,
                "llm_spend_today_usd": 0.2487,
                "llm_breaker_tripped": True,
            },
        }
        d = build_snapshot_dict(hood_state=hood_state)
        errs = validate_snapshot(d)
        self.assertEqual(errs, [], f"schema errors: {errs}")
        self.assertEqual(d["identity"]["bot_id"], "hood")
        self.assertEqual(d["lifecycle"]["mode"], "paper")
        self.assertEqual(d["lifecycle"]["live_gate"], "disarmed")
        self.assertEqual(d["capital"]["own_nav"], 500.0)
        self.assertEqual(d["compute"]["llm_budget_usd"], 0.25)
        self.assertAlmostEqual(d["compute"]["llm_spend_today_usd"], 0.2487)
        self.assertTrue(d["compute"]["breaker_tripped"])

    def test_no_fabricated_capital_when_state_missing(self) -> None:
        from umbrella_core.snapshot import validate_snapshot

        d = build_snapshot_dict(hood_state={})
        errs = validate_snapshot(d)
        self.assertEqual(errs, [])
        self.assertEqual(d["capital"]["own_nav"], 0.0)
        self.assertEqual(d["capital"]["cash"], 0.0)
        warnings = d["health"]["warnings"]
        self.assertTrue(any("no capital" in w.lower() or "no hood_state" in w.lower() for w in warnings))

    def test_emit_writes_and_is_fail_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            data = t / "data_real"
            data.mkdir()
            (data / "hood_state.json").write_text(
                json.dumps(
                    {
                        "nav_sleeve": 500.0,
                        "cash_sleeve": 500.0,
                        "positions": [],
                        "live_enabled": False,
                        "health": "ok",
                        "llm_metrics": {
                            "llm_spend_today_usd": 0.01,
                            "llm_calls_today": 1,
                            "llm_breaker_tripped": False,
                        },
                        "budget_remaining_usd": 0.24,
                    }
                )
            )
            out = t / "data" / "state.json"
            ok = emit_snapshot(data_dir=data, out_path=out)
            self.assertTrue(ok)
            self.assertTrue(out.exists())
            snap = json.loads(out.read_text())
            self.assertEqual(snap["capital"]["own_nav"], 500.0)

            # fail-safe: broken validate path still returns False, no raise
            with patch("snapshot_emit.build_snapshot_dict", side_effect=RuntimeError("boom")):
                ok2 = emit_snapshot(data_dir=data, out_path=out)
            self.assertFalse(ok2)


if __name__ == "__main__":
    unittest.main()
