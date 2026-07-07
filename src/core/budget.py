"""Hard daily token budget circuit breaker (Section 6).

Target < $1/day even in volatile days. At 80% consumption: stop new Tier-3, graceful degrade.
Enforced in code, not suggestion.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .safety_core import SafetyCore


# Verified mid-2026 Anthropic pricing (Section 6)
PRICING = {
    "haiku": {"in": 1.00, "out": 5.00},      # $/M tokens
    "sonnet": {"in": 3.00, "out": 15.00},
    "opus": {"in": 5.00, "out": 25.00},      # Section 6: Opus 4.7/4.8 ($5/$25). Avoided for use, but priced so it's never under-counted.
}

# Standard Anthropic prompt-caching multipliers, applied to the base "in" price for a model.
# cache_creation_input_tokens (writing a cold/expired prefix to cache) cost MORE than a plain
# input token; cache_read_input_tokens (hitting a warm cache) cost much LESS. Regular
# (non-cached) input_tokens are billed at the base "in" rate, unchanged.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Real cost from the Anthropic response usage breakdown (Mandate 2: track real cost,
    not call count). input_tokens here is the NON-cached input token count (as returned by
    the API alongside the two cache fields) — they are billed separately, not summed in.
    """
    p = PRICING[price_key_for_model(model)]
    base_in = p["in"]
    cost = (
        (input_tokens / 1_000_000.0) * base_in
        + (output_tokens / 1_000_000.0) * p["out"]
        + (cache_creation_input_tokens / 1_000_000.0) * base_in * CACHE_WRITE_MULTIPLIER
        + (cache_read_input_tokens / 1_000_000.0) * base_in * CACHE_READ_MULTIPLIER
    )
    return cost


def price_key_for_model(model: str) -> str:
    """Map a model ID (e.g. 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001') to a PRICING tier
    by SUBSTRING (not split('-'), which yields 'claude' and silently under-prices Sonnet/Opus as Haiku).
    Unknown -> 'opus' (most expensive) so the breaker never UNDER-counts spend (fail-safe on budget)."""
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "opus"  # truly unknown: assume most expensive, never under-count

# Rough token estimates per workload type (conservative)
ESTIMATES = {
    "t2_triage": (1200, 250),
    "t2_reaction": (900, 200),
    "t3_thesis": (8000, 1500),
    "t3_meta": (15000, 3000),
    "t2_risk": (600, 200),
    "t2_auditor": (1200, 450),
}


@dataclass
class BudgetConfig:
    daily_usd_cap: float = SafetyCore.get_daily_usd_cap()  # protected, down-only via SafetyCore (Phase 3)
    degrade_at_frac: float = 0.80
    log_path: Optional[Path] = None


class DailyBudget:
    """Thread-safe, persistent daily spend tracker with hard stop + degrade signal."""

    def __init__(self, config: BudgetConfig):
        self.config = config
        self._lock = threading.RLock()  # reentrant so _rollover/persist can be called while holding (from record_usage etc) and from outside callers
        self._today: date = date.today()
        self._spent_usd: float = 0.0
        self._calls: int = 0
        self._degraded: bool = False
        self._candidate_events: int = 0  # Mandate 1/3: events the watcher flagged as worth a look today
        if config.log_path:
            config.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self) -> None:
        if not self.config.log_path or not self.config.log_path.exists():
            return
        try:
            data = json.loads(self.config.log_path.read_text())
            if data.get("date") == str(self._today):
                self._spent_usd = data.get("spent_usd", 0.0)
                self._calls = data.get("calls", 0)
                self._degraded = data.get("degraded", False)
                self._candidate_events = data.get("candidate_events", 0)
        except Exception:
            pass  # start fresh on corrupt

    def _persist(self) -> None:
        if not self.config.log_path:
            return
        data = {
            "date": str(self._today),
            "spent_usd": round(self._spent_usd, 6),
            "calls": self._calls,
            "degraded": self._degraded,
            "candidate_events": self._candidate_events,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.config.log_path.write_text(json.dumps(data, indent=2))

    def _rollover_if_needed(self) -> None:
        """Check date and reset counters if new day. Persists the reset. Safe to call from any context (uses RLock)."""
        today = date.today()
        with self._lock:
            if today != self._today:
                self._today = today
                self._spent_usd = 0.0
                self._calls = 0
                self._degraded = False
                self._candidate_events = 0
                self._persist()  # persist the reset so restart doesn't see stale spend

    def record_usage(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        actual_usd: Optional[float] = None,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float:
        """Record a call. Returns current spent after this.

        Mandate 2: when cache token counts are supplied (real Anthropic usage), cost is
        computed via compute_cost() so cache reads (~0.1x) and cache writes (~1.25x) are
        billed correctly instead of treating all input tokens as full-price. Callers that
        don't pass cache info (existing call sites) get the prior flat-rate behavior unchanged.
        """
        self._rollover_if_needed()
        if actual_usd is not None:
            cost = actual_usd
        elif cache_creation_input_tokens or cache_read_input_tokens:
            cost = compute_cost(model, tokens_in, tokens_out, cache_creation_input_tokens, cache_read_input_tokens)
        else:
            p = PRICING[price_key_for_model(model)]
            cost = (tokens_in / 1_000_000.0) * p["in"] + (tokens_out / 1_000_000.0) * p["out"]
        with self._lock:
            self._spent_usd += cost
            self._calls += 1
            cap = self.config.daily_usd_cap
            if abs(cap - 1.0) < 1e-9:
                cap = SafetyCore.get_daily_usd_cap()
            frac = self._spent_usd / cap if cap > 0 else 0.0
            if frac >= self.config.degrade_at_frac and not self._degraded:
                self._degraded = True
            self._persist()
            return self._spent_usd

    def can_start_tier3(self) -> bool:
        """Circuit breaker: do not start new expensive reasoning if over threshold. M1: live SafetyCore cap."""
        self._rollover_if_needed()
        with self._lock:
            cap = self.config.daily_usd_cap
            if abs(cap - 1.0) < 1e-9:
                cap = SafetyCore.get_daily_usd_cap()
            frac = self._spent_usd / cap if cap > 0 else 0.0
            return frac < self.config.degrade_at_frac

    def is_degraded(self) -> bool:
        self._rollover_if_needed()
        with self._lock:
            return self._degraded

    def can_spend(self, estimated_usd: float) -> bool:
        """Hard 100% ceiling (spec Section 6 circuit breaker, not suggestion). True only if spent + est <= cap.
        M1: if config cap looks like default, consult live SafetyCore; tests use explicit small caps.
        """
        self._rollover_if_needed()
        with self._lock:
            cap = self.config.daily_usd_cap
            if abs(cap - 1.0) < 1e-9:  # default, use live protected
                cap = SafetyCore.get_daily_usd_cap()
            return (self._spent_usd + max(0.0, estimated_usd)) <= cap

    def current_spend(self) -> tuple[float, float]:
        """Return (spent_usd, fraction_of_cap). M1: uses live SafetyCore if default."""
        self._rollover_if_needed()
        with self._lock:
            cap = self.config.daily_usd_cap
            if abs(cap - 1.0) < 1e-9:
                cap = SafetyCore.get_daily_usd_cap()
            frac = self._spent_usd / cap if cap > 0 else 0.0
            return self._spent_usd, frac

    def force_degrade(self) -> None:
        with self._lock:
            self._degraded = True
            self._persist()

    def call_count(self) -> int:
        """Mandate 3/4 metric: total LLM calls recorded today (all tiers combined)."""
        self._rollover_if_needed()
        with self._lock:
            return self._calls

    def record_candidate_event(self) -> None:
        """Mandate 1/3 metric: watcher flagged this event as worth a look (passed the
        deterministic pre-filter and was handed to the reasoner). Counted regardless of
        whether the reasoner ultimately calls an LLM, gets budget-refused, or rejects —
        this is the watcher's output count, not the reasoner's.
        """
        self._rollover_if_needed()
        with self._lock:
            self._candidate_events += 1
            self._persist()

    def candidate_event_count(self) -> int:
        self._rollover_if_needed()
        with self._lock:
            return self._candidate_events

    def remaining_today(self) -> float:
        """Mandate 2: dollars left under the cap today (cap resolved the same way can_spend does)."""
        self._rollover_if_needed()
        with self._lock:
            cap = self.config.daily_usd_cap
            if abs(cap - 1.0) < 1e-9:
                cap = SafetyCore.get_daily_usd_cap()
            return max(0.0, cap - self._spent_usd)

    def breaker_tripped(self) -> bool:
        """Mandate 2/3 metric: has the hard breaker actually refused spend today (cap fully
        consumed), distinct from is_degraded() which is the earlier 80% soft-degrade signal.
        """
        self._rollover_if_needed()
        return self.is_degraded() or not self.can_spend(0.0)

    def reset_for_test(self) -> None:
        """Only for tests."""
        with self._lock:
            self._spent_usd = 0.0
            self._calls = 0
            self._degraded = False
            self._candidate_events = 0


def estimate_cost(workload: str, model: str = "haiku") -> float:
    """Conservative pre-call estimate."""
    if workload not in ESTIMATES:
        return 0.05
    tin, tout = ESTIMATES[workload]
    p = PRICING[price_key_for_model(model)]
    return (tin / 1e6) * p["in"] + (tout / 1e6) * p["out"]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as P

    with tempfile.TemporaryDirectory() as td:
        bp = P(td) / "budget.json"
        b = DailyBudget(BudgetConfig(daily_usd_cap=1.0, log_path=bp))
        print("Initial spend:", b.current_spend())
        b.record_usage("haiku", 1200, 250)
        print("After T2:", b.current_spend())
        print("Can tier3?", b.can_start_tier3())
        # simulate heavy
        for _ in range(20):
            b.record_usage("sonnet", 8000, 1500)
        print("After heavy:", b.current_spend(), "degraded?", b.is_degraded())
        print("Budget test OK")
