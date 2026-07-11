"""Core JSON schemas and dataclasses for the Agentic Trading System.

Per spec Section 8: every thesis is an EV distribution, not conviction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional


class EventType(str, Enum):
    SPINOFF = "spinoff"
    EARNINGS = "earnings"
    RESTRUCTURING = "restructuring"
    FDA = "fda"
    BANKRUPTCY_EXIT = "bankruptcy_exit"
    INDEX_INCLUSION = "index_inclusion"
    OTHER_CORP_ACTION = "other_corp_action"
    FILING_8K = "8k"
    FILING_10Q = "10q"
    FILING_10K = "10k"
    S1 = "s1"
    S3 = "s3"
    # etc.


class RunMode(str, Enum):
    """Phase R: single code path for paper/live. Only the submission leaf differs.
    PAPER: conservative sim fill vs REAL quote from MarketData, mark vs REAL subsequent prices.
    LIVE: real broker submission (gated by SafetyCore human_go_live + passing Phase4 calib).
    """
    PAPER = "paper"
    LIVE = "live"


@dataclass
class EVThesis:
    """Mandatory thesis schema (Section 8). Every engine must emit this exactly."""

    ticker: str
    event_type: str  # from EventType or freeform for now
    upside_pct: float
    p_upside: float
    downside_pct: float
    p_downside: float
    expected_value_pct: float  # computed = (p_up * up + p_down * down) - (1-p_up-p_down)*0 or similar
    prior_accuracy_on_name: float  # self-scored hit-rate for this name, 0-1
    what_informed_holders_may_know_that_we_dont: str  # forced humility
    tradeable_capacity_usd: float
    event_risk_flags: list[str] = field(default_factory=list)
    source_filings: list[str] = field(default_factory=list)  # EDGAR accession #s

    # Added 2026-07-10: the analyst's free-text rationale for the numbers above (not just the
    # numbers themselves). Owner asked to review WHY a thesis said what it said, not just its
    # EV/p_up/downside — this had never been captured anywhere; only the structured numeric
    # fields were persisted. Optional/defaulted so no existing caller or canned fixture breaks.
    reasoning: str = ""

    # Optional runtime fields
    thesis_id: Optional[str] = None
    timestamp: Optional[str] = None  # ISO
    capacity_tag: Optional[str] = None  # e.g. "ok_at_current_size", "too_large"
    model_used: Optional[str] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        # Basic sanity (not full validation)
        if not (0.0 <= self.p_upside <= 1.0):
            raise ValueError("p_upside must be in [0,1]")
        if not (0.0 <= self.p_downside <= 1.0):
            raise ValueError("p_downside must be in [0,1]")
        if self.p_upside + self.p_downside > 1.0 + 1e-6:
            raise ValueError("p_upside + p_downside > 1")

    def compute_ev(self) -> float:
        """Recompute expected value from the distribution. Spec: computed, not asserted."""
        # Simple model: EV = p_up * upside + p_down * downside + (1-pu-pd) * 0 (no-move)
        # Downside is negative number typically.
        no_move_p = max(0.0, 1.0 - self.p_upside - self.p_downside)
        ev = (
            self.p_upside * self.upside_pct
            + self.p_downside * self.downside_pct
            + no_move_p * 0.0
        )
        return round(ev, 4)

    def to_json(self) -> str:
        d = asdict(self)
        # ensure event_risk_flags etc are lists
        return json.dumps(d, indent=2, default=str)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EVThesis:
        # filter to known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
        clean = {k: v for k, v in d.items() if k in known}
        return cls(**clean)

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = ok). Used by Auditor.
        F2: added deterministic implausible_ev ceiling as non-LLM backstop against injection/model breakage.
        """
        errs: list[str] = []
        if abs(self.expected_value_pct - self.compute_ev()) > 1e-4:
            errs.append("expected_value_pct does not match computed_ev()")
        if len(self.what_informed_holders_may_know_that_we_dont.strip()) < 20:
            errs.append("what_informed_holders_may_know_that_we_dont must be substantive (>=20 chars)")
        if self.tradeable_capacity_usd <= 0:
            errs.append("tradeable_capacity_usd must be positive")
        if not self.ticker or len(self.ticker) > 6:
            errs.append("invalid ticker")
        # F2: conservative non-LLM sanity (ruin prevention); real +EV theses live inside these bounds
        if abs(self.expected_value_pct) > 50.0:
            errs.append("implausible_ev")
        if self.p_upside >= 0.95 and self.downside_pct >= -5.0:
            errs.append("implausible_ev_high_p_upside_low_downside")
        return errs


@dataclass
class TradeDecision:
    """Output after Auditor + Risk Controller."""
    thesis: EVThesis
    approved: bool
    reject_reason: Optional[str] = None
    sized_shares: Optional[float] = None  # fractional ok
    sized_usd: Optional[float] = None
    risk_score: Optional[float] = None  # from Risk Controller ruin stress
    auditor_notes: list[str] = field(default_factory=list)


# Example of the deterministic screens output (used by Auditor part a)
@dataclass
class DeterministicScreenResult:
    ticker: str
    dilution_risk: bool  # S-3/S-1 recent
    going_concern: bool
    thin_float: bool
    low_adv: bool
    wide_spread: bool
    recent_halt: bool
    high_short_interest: bool
    earnings_pop_veto: bool = False
    earnings_crash_veto: bool = False
    raw_13d_veto: bool = False
    flags: list[str] = field(default_factory=list)

    def has_hard_veto(self) -> bool:
        """Deterministic hard-fail flags (ruin / measured-negative setups)."""
        return any(
            [
                self.dilution_risk,
                self.going_concern,
                self.recent_halt,
                self.earnings_pop_veto,
                self.earnings_crash_veto,
                self.raw_13d_veto,
            ]
        )

    def is_clean(self) -> bool:
        return not self.has_hard_veto()


@dataclass
class ExecutionSafetyResult:
    ok: bool
    reason: str
    spread_pct: Optional[float] = None
    pct_of_adv: Optional[float] = None


# The exact function from spec Section 11, hard-coded, pre-LLM.
def validate_execution_safety(
    bid: float,
    ask: float,
    avg_daily_volume: float,
    order_size_shares: float,
    is_halted: bool,
    max_allowed_spread_pct: float = 0.02,
    max_pct_of_adv: float = 0.05,
) -> ExecutionSafetyResult:
    """Hard structural veto before order construction. Returns result with reason."""
    if is_halted:
        return ExecutionSafetyResult(ok=False, reason="halted")
    if bid <= 0 or ask <= 0:
        return ExecutionSafetyResult(ok=False, reason="no_two_sided_market")
    mid = (ask + bid) / 2.0
    spread_pct = (ask - bid) / mid
    if spread_pct > max_allowed_spread_pct:
        return ExecutionSafetyResult(
            ok=False, reason=f"spread_too_wide_{spread_pct:.3f}", spread_pct=spread_pct
        )
    if avg_daily_volume <= 0 or (order_size_shares / avg_daily_volume) > max_pct_of_adv:
        pct = (order_size_shares / avg_daily_volume) if avg_daily_volume > 0 else 1.0
        return ExecutionSafetyResult(
            ok=False, reason="order_too_large_vs_liquidity", pct_of_adv=pct
        )
    return ExecutionSafetyResult(ok=True, reason="ok", spread_pct=spread_pct, pct_of_adv=order_size_shares / avg_daily_volume if avg_daily_volume > 0 else 0)


if __name__ == "__main__":
    # quick self test
    t = EVThesis(
        ticker="ABCD",
        event_type="8k",
        upside_pct=25.0,
        p_upside=0.4,
        downside_pct=-40.0,
        p_downside=0.3,
        expected_value_pct=0.0,  # will be overwritten in real use
        prior_accuracy_on_name=0.55,
        what_informed_holders_may_know_that_we_dont="Management has deep supplier relationships and may know about upcoming contract wins we cannot see from filings alone.",
        tradeable_capacity_usd=25000.0,
        event_risk_flags=["thin_float"],
        source_filings=["0001234567-24-000001"],
    )
    t.expected_value_pct = t.compute_ev()
    print(t.to_json())
    print("Validation errors:", t.validate())
    print("Safety example:", validate_execution_safety(10.0, 10.2, 500000, 10000, False))
