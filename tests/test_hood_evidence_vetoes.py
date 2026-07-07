"""Fail-before tests for Fable edge-recon evidence vetoes (GROK_HANDOFF_HOOD_VETOES.md).

Each veto test asserts the flag fires (or correctly does not) AND that no LLM call is
made when a deterministic hard veto triggers (call-count guard — must save spend).
"""

from __future__ import annotations

import unittest

from src.core.auditor import run_auditor, run_deterministic_screens
from src.core.llm_client import FakeLLMClient
from src.core.schemas import EVThesis
from src.core.veto_params import (
    EARNINGS_POP_VETO_PCT,
    EARNINGS_CRASH_VETO_PCT,
    GOING_CONCERN_PERSIST_AB_20D_NET_N,
    GOING_CONCERN_PERSIST_AB_20D_NET_PCT,
)


def _clean_earnings_thesis(ticker: str = "ERN") -> EVThesis:
    return EVThesis(
        ticker=ticker,
        event_type="earnings",
        upside_pct=8.0,
        p_upside=0.45,
        downside_pct=-5.0,
        p_downside=0.25,
        expected_value_pct=2.35,
        prior_accuracy_on_name=0.7,
        what_informed_holders_may_know_that_we_dont=(
            "This is a well-covered name; we have no material info asymmetry beyond standard modeling."
        ),
        tradeable_capacity_usd=25000.0,
        event_risk_flags=[],
    )


class TestEvidenceVetoes(unittest.TestCase):
    def test_v1_going_concern_persistence_hard_fails_no_llm(self):
        """V1: existing going-concern screen hard-fails; documented base-rate constant exists."""
        self.assertLess(GOING_CONCERN_PERSIST_AB_20D_NET_PCT, 0.0)
        self.assertGreater(GOING_CONCERN_PERSIST_AB_20D_NET_N, 0)

        thesis = _clean_earnings_thesis("GCPR")
        llm = FakeLLMClient()
        rep = run_auditor(
            thesis,
            recent_filings=["10-K: substantial doubt about ability to continue as a going concern"],
            llm=llm,
        )
        self.assertTrue(rep.deterministic.going_concern)
        self.assertFalse(rep.overall_pass)
        self.assertEqual(rep.model, "skipped-det-veto")
        self.assertEqual(len(llm.calls), 0, "going-concern veto must skip adversarial LLM")

    def test_v2_earnings_pop_veto_thresholds_and_no_llm_on_veto(self):
        """V2: +6% pop vetoed, +3% not; veto path makes zero LLM calls."""
        thesis = _clean_earnings_thesis("POP6")
        llm = FakeLLMClient()
        det_veto = run_deterministic_screens(
            thesis, recent_filings=[], entry_reaction_pct=6.0, is_earnings_8k=True,
        )
        self.assertTrue(det_veto.earnings_pop_veto)
        rep_veto = run_auditor(
            thesis, recent_filings=[], llm=llm,
            entry_reaction_pct=6.0, is_earnings_8k=True,
        )
        self.assertFalse(rep_veto.overall_pass)
        self.assertEqual(len(llm.calls), 0)

        thesis_ok = _clean_earnings_thesis("POP3")
        det_ok = run_deterministic_screens(
            thesis_ok, recent_filings=[], entry_reaction_pct=3.0, is_earnings_8k=True,
        )
        self.assertFalse(det_ok.earnings_pop_veto)
        self.assertLess(3.0, EARNINGS_POP_VETO_PCT)

    def test_v3_earnings_crash_veto_thresholds_and_no_llm_on_veto(self):
        """V3: −12% crash vetoed, −5% not; veto path makes zero LLM calls."""
        thesis = _clean_earnings_thesis("CR12")
        llm = FakeLLMClient()
        det_veto = run_deterministic_screens(
            thesis, recent_filings=[], entry_reaction_pct=-12.0, is_earnings_8k=True,
        )
        self.assertTrue(det_veto.earnings_crash_veto)
        rep_veto = run_auditor(
            thesis, recent_filings=[], llm=llm,
            entry_reaction_pct=-12.0, is_earnings_8k=True,
        )
        self.assertFalse(rep_veto.overall_pass)
        self.assertEqual(len(llm.calls), 0)

        thesis_ok = _clean_earnings_thesis("CR05")
        det_ok = run_deterministic_screens(
            thesis_ok, recent_filings=[], entry_reaction_pct=-5.0, is_earnings_8k=True,
        )
        self.assertFalse(det_ok.earnings_crash_veto)
        self.assertGreater(-5.0, EARNINGS_CRASH_VETO_PCT)

    def test_v4_raw_13d_long_vetoed_no_llm(self):
        """V4: raw non-watchlist 13D long vetoed with zero LLM calls."""
        thesis = EVThesis(
            ticker="D13D",
            event_type="13d",
            upside_pct=10.0,
            p_upside=0.5,
            downside_pct=-15.0,
            p_downside=0.3,
            expected_value_pct=1.5,
            prior_accuracy_on_name=0.5,
            what_informed_holders_may_know_that_we_dont=(
                "The filer may know more about near-term liquidity than the market prices in."
            ),
            tradeable_capacity_usd=5000.0,
            event_risk_flags=[],
        )
        llm = FakeLLMClient()
        det = run_deterministic_screens(thesis, recent_filings=[], is_13d_trigger=True)
        self.assertTrue(det.raw_13d_veto)
        rep = run_auditor(thesis, recent_filings=[], llm=llm, is_13d_trigger=True)
        self.assertFalse(rep.overall_pass)
        self.assertEqual(len(llm.calls), 0)

    def test_reaction_pct_forwarded_from_reactionlayer_to_auditor(self):
        """W2 guard: ReactionLayer.process_trigger MUST forward Trigger.reaction_pct into
        run_auditor. This exercises the real wiring seam (not a direct run_auditor call), so
        V2/V3 can never silently go dormant again. Fails before the reaction_layer:188 change."""
        from unittest.mock import patch, MagicMock
        from src.core import reaction_layer as RL

        thesis = _clean_earnings_thesis("WIRE")
        spy = MagicMock(return_value=MagicMock(overall_pass=False))  # reject → stop right after audit
        rl = RL.ReactionLayer(llm=FakeLLMClient(), executor=None, risk=None, use_triage=False)
        with patch.object(RL.ReactionLayer, "_tier1_filter", return_value=(True, "pass")), \
             patch.object(RL.ReactionLayer, "_haiku_triage", return_value=(True, "material")), \
             patch.object(RL, "build_ev_thesis", return_value=thesis), \
             patch.object(RL, "run_auditor", spy):
            trig = RL.Trigger("e-wire-1", "WIRE", "earnings", "x" * 80, False, reaction_pct=6.0)
            rl.process_trigger(trig)

        self.assertTrue(spy.called, "ReactionLayer must call run_auditor")
        self.assertEqual(
            spy.call_args.kwargs.get("entry_reaction_pct"), 6.0,
            "Trigger.reaction_pct must reach run_auditor — else V2/V3 are dormant in production",
        )

    def test_a1_fast_earnings_entry_fail_closed_no_llm(self):
        """A1 (Fable recon §2.4): the fast reaction path must NOT enter an earnings 8-K with no
        completed 2-bar reaction — fail-closed at tier1, before any LLM spend. A completed
        reaction (the future deferred A2 path) must NOT be blocked by A1."""
        from src.core import reaction_layer as RL
        rl = RL.ReactionLayer(llm=FakeLLMClient(), executor=None, risk=None, use_triage=False)

        no_react = RL.Trigger("e-earn-noreact", "ERNX", "earnings", "material earnings 8-K text " * 5, False)
        passed, reason = rl._tier1_filter(no_react)
        self.assertFalse(passed, "earnings 8-K with no reaction must be rejected by the fast path")
        self.assertIn("earnings", reason)
        self.assertEqual(len(rl.llm.calls), 0, "fail-closed earnings reject must cost zero LLM")

        with_react = RL.Trigger("e-earn-react", "ERNX", "earnings", "material earnings 8-K text " * 5, False, reaction_pct=3.0)
        _, reason2 = rl._tier1_filter(with_react)
        self.assertNotIn("earnings_no_reaction", reason2,
                         "A1 must not block an earnings 8-K once a completed reaction is present")


if __name__ == "__main__":
    unittest.main()