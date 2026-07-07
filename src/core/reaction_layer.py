"""Reaction Layer (P3-A): 24/7 patient-predator monitoring (spec Sections 1,2,7,8,11).

- Event-driven (not polling) simulated feed for Phase 3 (injectable for real EDGAR push later).
- Tier-1 (free, deterministic, no LLM): filter most triggers.
- Escalate ONLY qualifying to existing Phase-2 EV Engine (Sonnet via LLMClient, with guards).
- Off-hours: pass is_offhours=True to executor (tighter spread, smaller size per spec 5/11); thesis needs regular-hours confirmation for full size (enforced by caller policy).
- One strike per trigger/event.
- Reuses all Phase2 guards (G1-7 via llm; kill/budget/safety via core).

All paper. No real capital.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
import time

import json
import re

from .ev_engine import build_ev_thesis
from .auditor import run_auditor
from .risk import RiskController
from .executor import Executor
from .llm_client import LLMClient, get_llm_client, DEFAULT_HAIKU_MODEL
from .budget import estimate_cost
from .safety_core import SafetyCore
from .schemas import EVThesis
from .capacity import estimate_tradeable_capacity, is_tradeable_at_size
from .decision_emit import (
    DEFAULT_DECISIONS_PATH,
    build_decision_record,
    emit_decision_safe,
    fetch_benchmarks,
    should_sample_screen_reject,
)
from .regime import classify_regime


_TRIAGE_SYSTEM = (
    "You are a fast triage filter for an event-driven micro/small-cap trading desk. "
    "You receive the text of a single SEC filing. Decide ONLY whether it is potentially "
    "MATERIAL enough to justify expensive downstream analysis.\n"
    "MATERIAL (pass=true): going-concern / liquidity doubt, dilution or shelf (S-3/S-1/ATM), "
    "M&A or strategic alternatives, restructuring / bankruptcy, major customer or contract win/loss, "
    "clinical or regulatory outcome, earnings guidance change, debt default / covenant breach, "
    "delisting notice, leadership change tied to a real event, or any clear price-moving development.\n"
    "NOT MATERIAL (pass=false): routine administrative filings — earnings-call scheduling, "
    "boilerplate Reg-FD reaffirmations, routine exhibit/8-K item-9.01 refilings, ordinary-course "
    "compensation housekeeping, fund/ETF NAV notices, with no price-moving content.\n"
    "Bias toward true when uncertain — missing a real event is worse than an unnecessary review. "
    'Respond ONLY with compact JSON: {"material": true|false, "reason": "<=12 words"}'
)


@dataclass
class Trigger:
    event_id: str
    ticker: str
    event_type: str
    raw_filing_text: str = ""
    is_offhours: bool = False
    # Recon 2-bar earnings reaction in PERCENT: (Close[bar after filing] / Close[bar before
    # filing] - 1) * 100, matching edge_recon/code/backtest.py event_row(react_min). Drives the
    # V2/V3 earnings-reaction vetoes. Populated ONLY by a source that can observe the COMPLETED
    # 2-bar reaction (which lands a full day after the filing — see AUDIT_LEDGER 2026-07-05
    # timing finding). None → V2/V3 stay fail-closed inert (safe), never miscalibrated against a
    # different (e.g. intraday-gap) reaction measure. Do NOT populate from a shorter live window.
    reaction_pct: Optional[float] = None
    # for sim: capacity hint etc


class ReactionLayer:
    """The monitoring/strike engine. 'Watch always; trade seldom.'"""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        executor: Optional[Executor] = None,
        risk: Optional[RiskController] = None,
        graveyard: Any = None,  # for logging if needed
        allowed_event_types: Sequence[str] = ("8k", "spinoff", "restructuring", "earnings", "fda"),
        min_capacity_usd: float = 100.0,
        use_triage: bool = True,
        decisions_path: Optional[str] = None,
        # Bug fix (2026-07-06): this defaulted to True with decisions_path defaulting to a
        # hardcoded absolute path (decision_emit.DEFAULT_DECISIONS_PATH = data/decisions.ndjson,
        # ignoring whatever data_dir the caller actually uses). Every ReactionLayer constructed
        # without explicitly overriding both params — which was every unit test, since none of
        # them knew this side effect existed — silently appended into the SAME shared file the
        # real OOS run also writes to. Verified: 60 of 76 records in that file were test
        # fixtures (tickers like GRUN/RBACK/CONF/TST with synthetic IDs), not real trading
        # decisions. Default now off; callers that want telemetry (the real run_paper() path)
        # must opt in explicitly with a path scoped to their own data_dir.
        emit_decisions: bool = False,
    ):
        self.llm = llm or get_llm_client(fake=True)
        self.executor = executor
        self.risk = risk or RiskController(10000.0)  # default sleeve for demo
        self.graveyard = graveyard
        self.allowed_event_types = set(allowed_event_types)
        self.min_capacity_usd = min_capacity_usd
        self.use_triage = use_triage  # cheap Haiku gate before the dual-Sonnet pass
        self._processed_events: set[str] = set()  # for one-strike
        self.decisions_path = decisions_path or str(DEFAULT_DECISIONS_PATH)
        self.emit_decisions = emit_decisions

    def _quote_fn(self):
        client = self.executor.client if self.executor else None
        if client is None:
            return None
        return client.get_quote

    def _benchmarks(self) -> dict[str, float]:
        qf = self._quote_fn()
        return fetch_benchmarks(qf) if qf else {}

    def _ref_price(self, ticker: str) -> Optional[float]:
        qf = self._quote_fn()
        if not qf:
            return None
        try:
            q = qf(ticker)
            last = getattr(q, "last", None)
            return float(last) if last and float(last) > 0 else None
        except Exception:
            return None

    def _emit(
        self,
        *,
        kind: str,
        trig: Trigger,
        reason: str,
        thesis: Optional[EVThesis] = None,
        auditor_report: Optional[AuditorReport] = None,
        ref_price: Optional[float] = None,
        actual: Optional[dict] = None,
        llm_calls: int = 0,
    ) -> None:
        if not self.emit_decisions:
            return
        # Fail-safe: the ENTIRE emit (regime read, benchmark fetch, record build, append) must
        # NEVER break the trading path — decisions.ndjson is telemetry, not a trading dependency.
        # (Auditor fix 2026-07-05: emit_decision_safe only guarded the append; classify_regime /
        # _benchmarks / build_decision_record could still throw into process_trigger's 9 call sites.)
        try:
            regime = classify_regime()
            rec = build_decision_record(
                kind=kind,
                instrument=trig.ticker,
                reason=reason,
                regime=regime,
                lineage={"trigger": f"edgar:{trig.event_id}"},
                mode="paper",
                thesis=thesis,
                ref_price=ref_price,
                actual=actual,
                benchmarks=self._benchmarks(),
                llm_calls=llm_calls,
            )
            emit_decision_safe(self.decisions_path, rec)
        except Exception:
            try:
                import logging
                logging.getLogger(__name__).warning(
                    "decision emit failed (non-fatal, trading continues)", exc_info=True)
            except Exception:
                pass

    def _haiku_triage(self, trig: Trigger) -> tuple[bool, str]:
        """Cheap Haiku gate (Tier-2) before the expensive dual-Sonnet EV+Auditor pass.
        Purpose: kill routine/non-material filings for ~$0.001 instead of paying ~2x Sonnet.

        Fail policy (deliberate):
        - kill / budget-refused / call error  -> NOT material (fail-CLOSED: do not spend Sonnet
          when triage can't run; the event is simply not traded, logged — never fabricated).
        - model output unparseable/ambiguous    -> MATERIAL=True (do NOT silently drop a possibly
          real event on a parse hiccup; bias to recall, the expensive layers still gate it).
        """
        if not self.use_triage:
            return True, "triage_disabled"
        user = (
            "<filing_text>\n"
            f"{(trig.raw_filing_text or '')[:6000]}\n"
            "</filing_text>\n\n"
            f"Ticker: {trig.ticker}  Event hint: {trig.event_type}\n"
            'Return ONLY JSON: {"material": true|false, "reason": "<=12 words"}'
        )
        try:
            resp = self.llm.complete(
                model=DEFAULT_HAIKU_MODEL,
                system=_TRIAGE_SYSTEM,
                user=user,
                max_tokens=120,
                temperature=0.0,
                cache_system=True,
                is_tier3=False,        # Tier-2: cheap, doesn't consume the Sonnet degrade budget
                workload="t2_triage",
            )
        except Exception as e:
            return False, f"triage_unavailable_{type(e).__name__}"
        if resp.get("error"):
            return False, f"triage_{resp['error']}"
        text = resp.get("text", "") or ""
        try:
            m = re.search(r"\{.*\}", text, re.S)
            d = json.loads(m.group(0)) if m else {}
            if "material" in d:
                return bool(d["material"]), str(d.get("reason", ""))[:60]
        except Exception:
            pass
        return True, "triage_unparsed_proceed"  # parse hiccup -> let the expensive layers decide

    def _tier1_filter(self, trig: Trigger, current_book_usd: float = 0.0, is_killed: Callable[[], bool] = lambda: False, budget_can: Callable[[float], bool] = lambda e: True) -> tuple[bool, str]:
        """Cheap deterministic gate. Returns (pass, reason). Most die here."""
        if is_killed():
            return False, "killed"
        if trig.event_type not in self.allowed_event_types:
            return False, "event_type_not_allowed"
        # A1 (Fable edge recon §2.4; AUDIT_LEDGER 2026-07-05): the FAST reaction path cannot
        # observe the calibrated 2-bar earnings reaction (it completes a full day after the
        # filing), so it cannot apply the V2/V3 pop/crash vetoes — and fast-entering earnings
        # reactions is net-negative in both directions. FAIL CLOSED: an earnings 8-K is
        # ineligible for entry unless a completed reaction (trig.reaction_pct) is supplied by a
        # deferred observe-then-enter path (A2, not yet built). No reaction data → no earnings
        # trade. Placed before budget/triage/EV so a skipped earnings 8-K costs zero LLM.
        if trig.event_type == "earnings" and getattr(trig, "reaction_pct", None) is None:
            return False, "earnings_no_reaction_fail_closed"
        if trig.ticker in [p.ticker for p in (self.executor.client.get_positions() if self.executor and self.executor.client else [])]:
            # simplistic: already have position? but for reaction rare
            pass
        # rough capacity check (would use real quote in real)
        if trig.raw_filing_text and len(trig.raw_filing_text) < 50:  # dummy for sim
            pass
        # Cost-efficiency fix: this pre-check used to estimate a full Sonnet thesis ($0.10),
        # which is ~the ENTIRE $0.10/day cap — so after one cent of spend, every subsequent
        # candidate was refused here regardless of how cheap the real next step (Haiku triage)
        # actually is. The only LLM call this gate is actually deciding whether to permit is
        # the immediate next one (Haiku triage); EV/auditor each re-check the breaker
        # independently inside llm.complete() when/if escalation happens. Estimate the real
        # next step, not the whole pipeline's worst case.
        est = estimate_cost("t2_triage", DEFAULT_HAIKU_MODEL)
        if not budget_can(est):
            return False, "budget_refused"
        # M3: real capacity filter using Phase1 capacity layer (cheap Tier-1, no LLM)
        adv = getattr(trig, 'adv', 100000)
        price = getattr(trig, 'price', 10.0)
        cap_tag = estimate_tradeable_capacity(adv, price, current_book_usd or 10000.0)
        class _T: pass
        tt = _T()
        tt.tradeable_capacity_usd = cap_tag.tradeable_capacity_usd
        if not is_tradeable_at_size(tt, current_book_usd or 10000.0):
            return False, "capacity_too_low_for_book"
        return True, "tier1_pass"

    def process_trigger(self, trig: Trigger, current_book_usd: float = 0.0, is_killed: Callable[[], bool] = lambda: False, budget_can: Callable[[float], bool] = lambda e: True) -> Optional[dict]:
        """Main entry for a trigger. Returns result dict or None if filtered/rejected."""
        if trig.event_id in self._processed_events:
            return None  # one-strike
        passed, reason = self._tier1_filter(trig, current_book_usd, is_killed, budget_can)
        if not passed:
            if should_sample_screen_reject(trig.event_id):
                self._emit(kind="screen_reject", trig=trig, reason=f"screen:{reason}")
            return {"filtered": True, "reason": reason, "event_id": trig.event_id}

        # Cheap Haiku triage BEFORE the dual-Sonnet pass — kills routine filings for ~$0.001.
        material, mreason = self._haiku_triage(trig)
        if not material:
            self._processed_events.add(trig.event_id)
            self._emit(kind="reject", trig=trig, reason=f"haiku:{mreason}", llm_calls=1)
            return {"escalated": True, "haiku_filtered": True, "reason": mreason, "event_id": trig.event_id}

        # Escalate to EV (reuses Phase2, with G guards via llm, offhours)
        thesis = build_ev_thesis(
            trig.ticker,
            trig.event_type,
            trig.raw_filing_text,
            tradeable_capacity_usd=5000.0,  # sim; real would compute
            llm=self.llm,
            graveyard=self.graveyard,
            event_risk_flags=["reaction_offhours"] if trig.is_offhours else [],
        )
        if thesis is None:
            self._processed_events.add(trig.event_id)
            self._emit(kind="reject", trig=trig, reason="ev_engine rejected", llm_calls=2)
            return {"escalated": True, "ev_rejected": True, "event_id": trig.event_id}

        # Auditor (real if llm)
        # Forward the 2-bar earnings reaction (if a source populated it) so V2/V3 can fire.
        # trig.reaction_pct is None today (no live source computes the completed 2-bar reaction
        # at reaction-time) → vetoes stay inert until that data + the timing decision land.
        det = run_auditor(thesis, recent_filings=[], llm=self.llm, recent_filings_text=trig.raw_filing_text,
                          entry_reaction_pct=trig.reaction_pct)

        if not det.overall_pass:
            self._processed_events.add(trig.event_id)
            kind = "veto" if det.deterministic.has_hard_veto() else "reject"
            self._emit(
                kind=kind,
                trig=trig,
                reason=(det.summary or "auditor rejected")[:280],
                thesis=thesis,
                ref_price=self._ref_price(trig.ticker),
                llm_calls=3,
            )
            return {"escalated": True, "auditor_rejected": True, "event_id": trig.event_id}

        # Risk + Exec (paper)
        if self.risk and self.executor:
            live_pos = self.executor.client.get_positions() if self.executor.client else []
            rc = self.risk.check(thesis, current_book=[], current_positions=live_pos)
            if rc.ok:
                ex_res = self.executor.execute_thesis(thesis, is_offhours=trig.is_offhours)
                self._processed_events.add(trig.event_id)
                if ex_res.success:
                    self._emit(
                        kind="entry",
                        trig=trig,
                        reason=f"auditor pass EV={thesis.expected_value_pct:.2f}%",
                        thesis=thesis,
                        ref_price=ex_res.avg_fill_price or self._ref_price(trig.ticker),
                        actual={
                            "filled_qty": ex_res.filled_shares,
                            "avg_price": ex_res.avg_fill_price,
                            "order_id": ex_res.order_id,
                        },
                        llm_calls=3,
                    )
                elif ex_res.veto_reason:
                    self._emit(
                        kind="veto",
                        trig=trig,
                        reason=ex_res.veto_reason[:280],
                        thesis=thesis,
                        ref_price=self._ref_price(trig.ticker),
                        llm_calls=3,
                    )
                return {
                    "escalated": True,
                    "success": bool(ex_res.success),
                    "executed": bool(ex_res.success),
                    "paper_order": ex_res.success,
                    "veto": ex_res.veto_reason,
                    "offhours": trig.is_offhours,
                    "event_id": trig.event_id,
                    "thesis": thesis,  # full object for runner record/open (was only ev number)
                    "avg_fill_price": ex_res.avg_fill_price if ex_res.success else None,
                    "thesis_ev": thesis.expected_value_pct,  # keep for observers
                    "filled_shares": ex_res.filled_shares if ex_res.success else 0.0,
                }
            else:
                self._processed_events.add(trig.event_id)
                self._emit(
                    kind="reject",
                    trig=trig,
                    reason=rc.reason[:280],
                    thesis=thesis,
                    ref_price=self._ref_price(trig.ticker),
                    llm_calls=3,
                )
                return {"escalated": True, "risk_rejected": rc.reason, "event_id": trig.event_id}

        self._processed_events.add(trig.event_id)
        self._emit(
            kind="hold",
            trig=trig,
            reason="auditor pass; no executor wired",
            thesis=thesis,
            ref_price=self._ref_price(trig.ticker),
            llm_calls=3,
        )
        return {"escalated": True, "no_executor": True, "event_id": trig.event_id}

    def process_triggers(self, triggers: Sequence[Trigger], **kwargs) -> list[dict]:
        """Batch for sim feed."""
        results = []
        for t in triggers:
            r = self.process_trigger(t, **kwargs)
            if r:
                results.append(r)
        return results
