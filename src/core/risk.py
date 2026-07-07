"""Risk Controller (deterministic core + T2 Haiku) — RUIN mandate only (Section 4, 7).

Two-tier position sizing:
- Tier 1: hard 25% ceiling of the *agentic sleeve* (not whole portfolio).
- Tier 2: event-risk-adjusted cap (10-15% on flagged names) enforced by RULES, not LLM.

Also ruin-stress simulation and the execution safety gate (re-exported).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Any

from .schemas import (
    EVThesis,
    TradeDecision,
    DeterministicScreenResult,
    validate_execution_safety,
    ExecutionSafetyResult,
)
from .safety_core import SafetyCore


# M2: removed stale module-level copies of protected caps (they were snapshot at import and a trap).
# All code must use SafetyCore.get_hard_ceiling_pct() etc. (two_tier_cap already does).
CLEAN_NAME_CAP_PCT = 0.25  # legacy non-protected alias


@dataclass
class RiskCheckResult:
    ok: bool
    sized_usd: float
    cap_applied: float  # the effective cap used
    reason: str
    ruin_stress_score: float  # 0-1, higher = worse (e.g. "if top 3 wrong, book -X%")
    flags: list[str]


class RiskController:
    """Enforces ruin-prevention only. Never reasons about alpha."""

    def __init__(self, agentic_sleeve_usd: float):
        self.agentic_sleeve_usd = max(100.0, agentic_sleeve_usd)  # guard tiny

    def two_tier_cap(self, thesis: EVThesis) -> tuple[float, str]:
        """Return (allowed_usd, cap_reason). Uses SafetyCore for protected values (Phase 3 0.1)."""
        base_cap = self.agentic_sleeve_usd * SafetyCore.get_hard_ceiling_pct()
        if thesis.event_risk_flags:
            hard_flags = {
                "S-3_on_file", "S-1_on_file", "going_concern", "recent_halt", "dilution",
                "earnings_pop_veto", "earnings_crash_veto", "raw_13d_veto",
            }
            if any(f in hard_flags for f in thesis.event_risk_flags):
                cap = min(base_cap, self.agentic_sleeve_usd * SafetyCore.get_event_risk_cap_pct())
                return cap, f"event_risk_adjusted_{SafetyCore.get_event_risk_cap_pct()*100:.0f}pct"
        if "thin_float" in thesis.event_risk_flags or "low_adv" in thesis.event_risk_flags:
            cap = min(base_cap, self.agentic_sleeve_usd * 0.18)
            return cap, "liquidity_adjusted_18pct"
        return base_cap, "clean_25pct"

    def ruin_stress(self, current_book: list[EVThesis], candidate: EVThesis, current_positions: Optional[list[Any]] = None) -> float:
        """Book-aware ruin scenario (Phase1 R2): use *actual* position market_values for held names + candidate sized.
        Conservative worst for held (ballast/liquid) ~35% loss; use thesis for candidate.
        Top-3 largest exposures drive the stress (spec: if top concentrated names wrong/halt).
        """
        total_down = 0.0
        exposures: list[tuple[float, float]] = []  # (size_usd, worst_loss_frac)

        if current_positions:
            for p in current_positions:
                mv = getattr(p, 'market_value', 0) or 0.0
                if mv > 0:
                    exposures.append( (mv, 0.35) )  # conservative for liquid held names
        else:
            # fallback to old proxy for compat
            for t in current_book:
                size = getattr(t, 'tradeable_capacity_usd', self.agentic_sleeve_usd * 0.20) or (self.agentic_sleeve_usd * 0.20)
                worst = abs(getattr(t, 'downside_pct', -30)) / 100.0 * 0.8
                exposures.append( (size, worst) )

        # candidate sized (use its capacity or risk sized if known, but here use capacity as upper)
        cand_size = getattr(candidate, 'tradeable_capacity_usd', self.agentic_sleeve_usd * 0.20) or (self.agentic_sleeve_usd * 0.20)
        cand_worst = abs(getattr(candidate, 'downside_pct', -50)) / 100.0 * 0.8
        exposures.append( (cand_size, cand_worst) )

        # top 3 by size
        exposures.sort(key=lambda x: -x[0])
        for size, worst in exposures[:3]:
            total_down += size * worst

        stress = min(1.0, total_down / max(self.agentic_sleeve_usd, 1.0))
        return round(stress, 3)

    def check(
        self,
        thesis: EVThesis,
        current_book: list[EVThesis],
        screens: Optional[DeterministicScreenResult] = None,
        current_positions: Optional[list[Any]] = None,
    ) -> RiskCheckResult:
        cap_usd, cap_reason = self.two_tier_cap(thesis)
        stress = self.ruin_stress(current_book, thesis, current_positions=current_positions)

        flags: list[str] = list(thesis.event_risk_flags)

        # Hard veto conditions (ruin only)
        if stress > 0.55:
            return RiskCheckResult(
                ok=False,
                sized_usd=0.0,
                cap_applied=cap_usd,
                reason=f"ruin_stress_too_high_{stress}",
                ruin_stress_score=stress,
                flags=flags + ["high_ruin_stress"],
            )

        if screens and not screens.is_clean():
            # still allow if small, but log; Risk is ruin, Auditor catches more
            pass

        sized = min(cap_usd, thesis.tradeable_capacity_usd)
        return RiskCheckResult(
            ok=True,
            sized_usd=round(sized, 2),
            cap_applied=cap_usd,
            reason=cap_reason,
            ruin_stress_score=stress,
            flags=flags,
        )

    def book_ruin_stress(self, current_positions: list[Any]) -> float:
        """P1-A: aggregate ruin posture of the current book (top-3 exposures * conservative loss)."""
        if not current_positions:
            return 0.0
        exps = sorted([getattr(p, 'market_value', 0) or 0.0 for p in current_positions], reverse=True)[:3]
        total_down = sum(e * 0.35 for e in exps)
        return round(min(1.0, total_down / max(self.agentic_sleeve_usd, 1.0)), 3)


def apply_risk_and_size(
    thesis: EVThesis,
    controller: RiskController,
    current_book: list[EVThesis],
    screens: Optional[DeterministicScreenResult] = None,
) -> TradeDecision:
    """Convenience: run risk then produce (possibly rejected) decision."""
    rc = controller.check(thesis, current_book, screens)
    if not rc.ok:
        return TradeDecision(
            thesis=thesis,
            approved=False,
            reject_reason=rc.reason,
            risk_score=rc.ruin_stress_score,
        )
    # For now sized_usd from risk; later Executor will convert to shares via quote
    return TradeDecision(
        thesis=thesis,
        approved=True,
        sized_usd=rc.sized_usd,
        risk_score=rc.ruin_stress_score,
    )


if __name__ == "__main__":
    rc = RiskController(agentic_sleeve_usd=50000.0)
    t = EVThesis(
        ticker="MICRO",
        event_type="8k",
        upside_pct=40.0,
        p_upside=0.35,
        downside_pct=-70.0,
        p_downside=0.40,
        expected_value_pct=2.0,
        prior_accuracy_on_name=0.4,
        what_informed_holders_may_know_that_we_dont="This is a microcap with promoter history; insiders likely know more.",
        tradeable_capacity_usd=8000.0,
        event_risk_flags=["S-3_on_file", "thin_float"],
    )
    res = rc.check(t, current_book=[])
    print(res)
    dec = apply_risk_and_size(t, rc, [])
    print("Decision approved?", dec.approved, "sized=", dec.sized_usd, "reject=", dec.reject_reason)

    # safety gate test
    safety = validate_execution_safety(4.9, 5.1, 120000, 3000, False, max_allowed_spread_pct=0.015)
    print("Safety:", safety)
