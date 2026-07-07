"""Build #6 integrity gates: hood paper emit fail-safe + schema round-trip."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HOOD_ROOT = Path(__file__).resolve().parents[1]
UMBRELLA_ROOT = HOOD_ROOT.parent / "umbrella"
for p in (HOOD_ROOT, UMBRELLA_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from umbrella_core.decisions import validate_decision

from src.core.decision_emit import build_decision_record, emit_decision_safe
from src.core.reaction_layer import ReactionLayer, Trigger
from src.core.schemas import EVThesis
from src.core.llm_client import FakeLLMClient


def _sample_thesis() -> EVThesis:
    return EVThesis(
        "AKTX",
        "8k",
        28.0,
        0.42,
        -45.0,
        0.35,
        2.5,
        0.48,
        "Management may know supplier terms we do not.",
        4200.0,
        event_risk_flags=["thin_float"],
        source_filings=["0001234567-26-000042"],
    )


class TestEmitResolverGate(unittest.TestCase):
    def test_schema_round_trip_build_decision_record(self):
        """Emitted hood records must validate against the auditor-owned schema."""
        thesis = _sample_thesis()
        rec = build_decision_record(
            kind="entry",
            instrument=thesis.ticker,
            reason="auditor pass",
            regime="riskon_calm",
            lineage={"trigger": "edgar:test-acc"},
            mode="paper",
            thesis=thesis,
            ref_price=3.12,
            benchmarks={"SPY": 623.1, "IWM": 231.44},
        )
        errors = validate_decision(rec)
        self.assertEqual(errors, [], f"expected clean validation: {errors}")

    def test_emit_fail_safe_broken_append_does_not_break_process_trigger(self):
        """A3 fail-safe: append failure must not alter process_trigger's trading result."""
        from src.core import reaction_layer as RL

        thesis = _sample_thesis()
        with tempfile.TemporaryDirectory() as td:
            decisions_path = Path(td) / "decisions.ndjson"

            def _boom(_path, _obj):
                raise OSError("simulated append failure")

            rl = ReactionLayer(
                llm=FakeLLMClient(),
                executor=None,
                risk=None,
                use_triage=False,
                decisions_path=str(decisions_path),
                emit_decisions=True,  # this test IS the emit path; default flipped off 2026-07-06
            )
            with patch.object(RL.ReactionLayer, "_tier1_filter", return_value=(True, "pass")), \
                 patch.object(RL.ReactionLayer, "_haiku_triage", return_value=(True, "material")), \
                 patch.object(RL, "build_ev_thesis", return_value=thesis), \
                 patch.object(RL, "run_auditor", return_value=MagicMock(overall_pass=False, summary="reject", deterministic=MagicMock(has_hard_veto=lambda: False))), \
                 patch.object(RL, "emit_decision_safe", side_effect=lambda path, rec, **kw: emit_decision_safe(path, rec, append_fn=_boom)):
                trig = Trigger("e-fail-safe", "AKTX", "8k", "x" * 80, False)
                res = rl.process_trigger(trig)

        self.assertIsNotNone(res)
        self.assertTrue(res.get("auditor_rejected"))
        self.assertEqual(res.get("event_id"), "e-fail-safe")

    def test_emit_fail_safe_broken_record_build_does_not_break_process_trigger(self):
        """A3 fail-safe (auditor 2026-07-05): a throw in the record-BUILD path (regime read /
        benchmark fetch / build_decision_record) — NOT just the append — must not break trading.
        This is the gap the append-only fail-safe test missed; fails before the whole-emit guard."""
        from src.core import reaction_layer as RL

        thesis = _sample_thesis()
        with tempfile.TemporaryDirectory() as td:
            decisions_path = Path(td) / "decisions.ndjson"
            rl = ReactionLayer(
                llm=FakeLLMClient(), executor=None, risk=None,
                use_triage=False, decisions_path=str(decisions_path),
                emit_decisions=True,  # this test IS the emit path; default flipped off 2026-07-06
            )
            with patch.object(RL.ReactionLayer, "_tier1_filter", return_value=(True, "pass")), \
                 patch.object(RL.ReactionLayer, "_haiku_triage", return_value=(True, "material")), \
                 patch.object(RL, "build_ev_thesis", return_value=thesis), \
                 patch.object(RL, "run_auditor", return_value=MagicMock(overall_pass=False, summary="reject", deterministic=MagicMock(has_hard_veto=lambda: False))), \
                 patch.object(RL, "classify_regime", side_effect=RuntimeError("regime source down")):
                trig = Trigger("e-fail-safe-build", "AKTX", "8k", "x" * 80, False)
                res = rl.process_trigger(trig)

        self.assertIsNotNone(res)
        self.assertTrue(res.get("auditor_rejected"), "trading path must complete despite emit build failure")
        self.assertEqual(res.get("event_id"), "e-fail-safe-build")


if __name__ == "__main__":
    unittest.main()