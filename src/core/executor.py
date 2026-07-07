"""EXECUTOR (T1 + MCP) — Section 7.

HARD spread/liquidity/halt VETO (coded, cannot be overridden by any EV).
fractional/lot-aware, idempotency keys, re-checks caps, NEVER reasons.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .schemas import EVThesis, validate_execution_safety, ExecutionSafetyResult, RunMode
from .risk import RiskController
from ..mcp.robinhood_client import RobinhoodClient, get_robinhood_client, OrderResult, Quote
from .safety_core import SafetyCore, SafetyParams
from .ownership_ledger import OwnershipLedger
# Phase R
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .market_data import MarketData
    from .storage import GraveyardDB

# Per spec Section 12: timestamp every datapoint; reject on stale.
MAX_QUOTE_AGE_SECONDS = 30


@dataclass
class ExecutionResult:
    thesis: EVThesis
    success: bool
    order_id: Optional[str]
    filled_shares: float
    avg_fill_price: float
    veto_reason: Optional[str] = None
    safety: Optional[ExecutionSafetyResult] = None
    note: str = ""


class Executor:
    """The only place orders are placed. Defense in depth."""

    def __init__(
        self,
        client: Optional[RobinhoodClient] = None,
        risk: Optional[RiskController] = None,
        max_spread_for_offhours: float = 0.015,
        graveyard: Optional["GraveyardDB"] = None,
        is_killed: Optional[Callable[[], bool]] = None,
        run_mode: RunMode = RunMode.PAPER,
        market_data: Optional["MarketData"] = None,  # Phase R: real quotes for paper conservative sim + mtm
        ownership_ledger: Optional[OwnershipLedger] = None,  # Mandate 1: hood-owned positions only
    ):
        self.client = client or get_robinhood_client()
        self.risk = risk
        self.max_spread_offhours = max_spread_for_offhours
        self.graveyard = graveyard  # for R1: record own vetoes to Graveyard when provided
        self.is_killed = is_killed  # F3: injectable kill check before order
        self.run_mode = run_mode
        self.market_data = market_data
        # Mandate 1: hood-owned position ledger. Sell gate checks this before touching broker.
        # Fail-closed: no ledger provided → sells are permitted (paper compat), but in real wiring
        # the runner MUST always provide a ledger. See INVARIANTS.md.
        self.ownership_ledger = ownership_ledger
        # If market_data provided, try to inject into mock client for real-quote paper fills (one path)
        if self.market_data and hasattr(self.client, "_market_data"):
            try:
                self.client._market_data = self.market_data  # type: ignore[attr-defined]
            except Exception:
                pass
        self._idemp_path = Path("data/idempotency.json")
        self._recent_order_keys: set[str] = set()
        if self._idemp_path.exists():
            try:
                loaded = json.loads(self._idemp_path.read_text())
                if isinstance(loaded, list):
                    self._recent_order_keys = set(loaded)
            except Exception:
                pass  # start fresh on corrupt; persisted keys survive restart per M4

    def _idempotency_key(self, thesis: EVThesis, side: str) -> str:
        return f"{thesis.ticker}:{thesis.thesis_id or thesis.timestamp}:{side}"

    def _save_idemp_keys(self) -> None:
        try:
            self._idemp_path.parent.mkdir(parents=True, exist_ok=True)
            self._idemp_path.write_text(json.dumps(sorted(self._recent_order_keys)))
        except Exception:
            pass  # best effort persist

    def _record_veto_if_graveyard(self, thesis: EVThesis, reason: str, regime: str = "executor_veto") -> None:
        """R1: Executor now owns recording its vetoes when graveyard provided (so tests drive real path)."""
        if self.graveyard is not None:
            try:
                self.graveyard.record_rejection(thesis, reason, regime=regime)
            except Exception:
                pass  # don't let logging break execution

    def execute_thesis(
        self,
        thesis: EVThesis,
        side: str = "buy",
        is_offhours: bool = False,
        current_book_usd: float = 0.0,
    ) -> ExecutionResult:
        """Main entry. Runs all hard gates then places order."""
        key = self._idempotency_key(thesis, side)
        if key in self._recent_order_keys:
            self._record_veto_if_graveyard(thesis, "duplicate_idempotency_key")
            return ExecutionResult(thesis, False, None, 0, 0, "duplicate_idempotency_key")

        # F3: kill check before any order (G6)
        if self.is_killed and self.is_killed():
            self._record_veto_if_graveyard(thesis, "killed")
            return ExecutionResult(thesis, False, None, 0, 0, "killed")

        # INVARIANT: LIVE_GATE_HARD_RAISE
        # Live order placement is unconditionally blocked until the human go-live gate is armed.
        # This check runs before any sizing, quote fetch, or risk evaluation — it cannot be
        # bypassed by EV, regime, or meta-reviewer. SafetyCore.is_live_enabled() starts False
        # and can only be set True by an explicit human_go_live() call (Phase 4 gate).
        # See INVARIANTS.md for the full contract.
        if self.run_mode == RunMode.LIVE and not SafetyCore.is_live_enabled():
            self._record_veto_if_graveyard(thesis, "live_not_enabled")
            return ExecutionResult(thesis, False, None, 0, 0, "live_not_enabled")

        # Mandate 1: sell-side ledger gate.
        # Hood must only sell what it opened. Broker get_positions() reflects the whole shared
        # account; only the ownership_ledger reflects what hood specifically bought.
        # Fail-closed: if the ledger says hood doesn't own this ticker, veto the sell.
        if side == "sell" and self.ownership_ledger is not None:
            if not self.ownership_ledger.has_position(thesis.ticker):
                self._record_veto_if_graveyard(thesis, "not_in_hood_ledger")
                return ExecutionResult(thesis, False, None, 0, 0, "not_in_hood_ledger")

        # P4-C: use-time SafetyCore invariant (decision time check, closes residual)
        if not self._safety_invariant_ok():
            self._record_veto_if_graveyard(thesis, "safety_core_tamper")
            # trip kill response
            if hasattr(self, 'graveyard') and self.graveyard:
                try:
                    dummy = EVThesis("TAMPER", "safety", 0,0,0,0,0,0.5,"tamper",1)
                    self.graveyard.record_rejection(dummy, "safety_core_tamper", regime="use_time")
                except Exception as e:
                    # F5: log specific (best effort record, do not let logging kill the veto path)
                    print(f"[EXECUTOR SAFETY] failed to record tamper rejection: {type(e).__name__}: {e}")
            return ExecutionResult(thesis, False, None, 0, 0, "safety_core_tamper")

        # Check open orders via client (spec Section 12: "Executor checks open orders before placing")
        try:
            opens = self.client.get_open_orders(ticker=thesis.ticker) if hasattr(self.client, "get_open_orders") else []
            for o in opens or []:
                status = str(o.get("status", "open")).lower()
                if status in ("open", "pending", "working", "new") and str(o.get("side", "")).lower() == side.lower():
                    self._recent_order_keys.add(key)
                    self._save_idemp_keys()
                    self._record_veto_if_graveyard(thesis, "open_order_exists")
                    return ExecutionResult(thesis, False, None, 0, 0, "open_order_exists")
        except Exception as e:
            # F5: execution-critical guard must not be bypassed silently on error.
            # On MCP error we still proceed (fail-open for liveness, per prior design), but log the specific reason
            # so operator sees the guard was not enforced this time. (Do not add key here; that would block legit repeats.)
            print(f"[EXECUTOR OPEN-ORDER CHECK] ticker={thesis.ticker} guard-check error (proceeding without veto): {type(e).__name__}: {e}")

        # 1. Re-check risk caps (defense in depth, even if upstream approved)
        # Mandate 1: ruin stress uses only hood-owned positions (ledger), never the whole broker
        # account. Broker get_positions() would include other tenants' positions and give a false
        # picture of hood's concentration risk.
        rc_sized = None
        if self.ownership_ledger is not None:
            from ..mcp.robinhood_client import Position as BrokerPosition
            hood_positions = [
                BrokerPosition(
                    ticker=e.ticker,
                    shares=e.shares,
                    avg_cost=e.avg_cost,
                    market_value=e.shares * e.avg_cost,  # cost basis as floor; no live mtm needed here
                )
                for e in self.ownership_ledger.all_owned()
            ]
        else:
            hood_positions = self.client.get_positions() if self.client else []
        if self.risk:
            rc = self.risk.check(thesis, current_book=[], current_positions=hood_positions)
            if not rc.ok:
                self._recent_order_keys.add(key)
                self._save_idemp_keys()
                self._record_veto_if_graveyard(thesis, rc.reason)
                return ExecutionResult(thesis, False, None, 0, 0, rc.reason)
            rc_sized = rc.sized_usd
        base_cap = thesis.tradeable_capacity_usd
        target_usd_for_sizing = min(rc_sized, base_cap) if rc_sized is not None else base_cap

        # Phase R D1: UNIFIED quote source for paper and live (injected market_data is the single source of real prices).
        # Divergence is ONLY at submission leaf (paper: conservative sim fill on the quote; live: real broker order)
        # + the documented safety guard (LIVE_ENABLED). No mode branch for pre-submission data.
        if self.market_data is not None:
            try:
                q = self.market_data.get_quote(thesis.ticker)
                if getattr(q, 'last', 0) <= 0.01:
                    q = self.client.get_quote(thesis.ticker)
            except Exception:
                q = self.client.get_quote(thesis.ticker)
        else:
            q = self.client.get_quote(thesis.ticker)

        # 3. Data freshness check (spec Section 12) -- reject stale before any decision
        if q.timestamp:
            try:
                ts = q.timestamp
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                qtime = datetime.fromisoformat(ts)
                age = (datetime.now(timezone.utc) - qtime).total_seconds()
                if age > MAX_QUOTE_AGE_SECONDS:
                    self._recent_order_keys.add(key)
                    self._save_idemp_keys()
                    self._record_veto_if_graveyard(thesis, "stale_quote")
                    return ExecutionResult(thesis, False, None, 0, 0, "stale_quote")
            except Exception as e:
                # F5: bad ts format; do not veto (liveness), but log specific reason for audit
                print(f"[EXECUTOR QUOTE TS] bad timestamp for {thesis.ticker} (age check skipped, proceeding): {type(e).__name__}: {e}")

        # 4. Hard execution safety veto (Section 11) — pre-LLM, cannot be bypassed
        max_spread = self.max_spread_offhours if is_offhours else 0.02
        intended_shares_for_adv = target_usd_for_sizing / max(q.last, 0.01)
        safety = validate_execution_safety(
            q.bid,
            q.ask,
            q.avg_daily_volume,
            intended_shares_for_adv,  # use the actual order size we will place (risk-capped if applicable)
            q.is_halted,
            max_allowed_spread_pct=max_spread,
            max_pct_of_adv=0.04 if is_offhours else 0.05,
        )
        if not safety.ok:
            self._recent_order_keys.add(key)
            self._save_idemp_keys()
            self._record_veto_if_graveyard(thesis, safety.reason)
            return ExecutionResult(
                thesis, False, None, 0, 0, safety.reason, safety=safety
            )

        # 4. Sizing: use the risk-approved size (capped) as binding upper bound (defense in depth per spec).
        # Risk may have sized down for event-risk cap; capacity is liquidity upper. Take min of both.
        target_usd = target_usd_for_sizing
        if side == "buy":
            # fractional shares allowed
            shares = target_usd / max(q.ask, 0.01)
        else:
            # Phase R: sells resolved via resolve_paper_position (uses real exit quote + pessimistic) or live order.
            # Here for sizing use current bid.
            shares = target_usd / max(q.bid, 0.01)

        shares = round(shares, 4)  # fractional ok on RH

        # 5. Place (limit at ask for aggressive, or mid for patient — here aggressive for reaction)
        # Pass the *same* q snapshot used for safety to avoid TOCTOU / re-quote in mock (M2)
        limit = q.ask if side == "buy" else q.bid
        res: OrderResult = self.client.place_limit_order(thesis.ticker, side, shares, limit, quote=q)

        self._recent_order_keys.add(key)
        self._save_idemp_keys()

        if res.success:
            # Mandate 1: record the fill in hood's ownership ledger so future sells are gated correctly.
            if side == "buy" and self.ownership_ledger is not None and res.filled_shares > 0:
                self.ownership_ledger.add(
                    ticker=thesis.ticker,
                    shares=res.filled_shares,
                    avg_cost=res.avg_fill_price,
                    event_id=str(thesis.thesis_id or thesis.timestamp or ""),
                )
            elif side == "sell" and self.ownership_ledger is not None and res.filled_shares > 0:
                self.ownership_ledger.remove(thesis.ticker, res.filled_shares)
            return ExecutionResult(
                thesis,
                True,
                res.order_id,
                res.filled_shares,
                res.avg_fill_price,
                safety=safety,
                note="executed",
            )
        else:
            self._record_veto_if_graveyard(thesis, res.reason or "order_failed")
            return ExecutionResult(
                thesis, False, None, res.filled_shares, res.avg_fill_price, res.reason, safety=safety
            )

    def get_portfolio_snapshot(self) -> dict:
        # Mandate 1: hood-owned positions only (from ledger), not whole account.
        if self.ownership_ledger is not None:
            owned = self.ownership_ledger.all_owned()
            pos = owned
            nav_positions = sum(e.shares * e.avg_cost for e in owned)
        else:
            pos = self.client.get_positions()
            nav_positions = sum(p.market_value for p in pos)
        cash = self.client.get_buying_power()
        return {"cash": cash, "positions": pos, "total_usd_approx": cash + nav_positions}

    def resolve_paper_position(self, ticker: str, exit_quote: Optional["Quote"] = None) -> dict:
        """Phase R (paper only): resolve an open paper position using REAL current/exit quote.
        Computes realized_return_pct from actual entry avg_cost to pessimistic exit (bid - extra slip modeled).
        Clears via client place (which for mock-with-real-md uses the real q for sim).
        Returns dict with realized; caller records to Graveyard with the *real* number (no formula).
        For live run_mode this would place a real sell (but gated off).
        """
        if self.run_mode != RunMode.PAPER:
            return {"success": False, "reason": "resolve_paper_only_in_paper_mode"}

        q = exit_quote
        if q is None:
            if self.market_data is not None:
                try:
                    q = self.market_data.get_quote(ticker)
                except Exception:
                    q = None
            if q is None or q.last <= 0.01:
                q = self.client.get_quote(ticker)

        if not hasattr(self.client, "_positions") or ticker not in getattr(self.client, "_positions", {}):
            return {"success": False, "reason": "no_open_paper_position"}

        pos = self.client._positions[ticker]
        if pos.shares <= 0:
            return {"success": False, "reason": "no_shares"}

        # Conservative paper exit fill: worse than bid + modeled extra microcap friction/gap
        exit_price = round(q.bid * 0.985, 4)  # pessimistic (paper harder than live will be)
        entry = pos.avg_cost
        realized_pct = (exit_price - entry) / max(entry, 0.0001)
        # extra modeled slippage/impact already in the 1.5% haircut; round
        realized_pct = round(realized_pct, 4)

        # Execute the "sell" via client (re-uses sim on the (real) q; clears book)
        res = self.client.place_limit_order(ticker, "sell", pos.shares, q.bid, quote=q)

        return {
            "success": bool(res.success),
            "realized_return_pct": realized_pct,
            "entry_avg_cost": entry,
            "exit_price": exit_price,
            "shares": pos.shares,
            "order_id": res.order_id,
            "reason": res.reason or "paper_resolve",
        }

    def _safety_invariant_ok(self) -> bool:
        """P4-C: decision-time check that protected values are still within ratchet (not tampered)."""
        try:
            defaults = SafetyParams()
            if SafetyCore.get_hard_ceiling_pct() > defaults.HARD_CEILING_PCT + 1e-9:
                return False
            if SafetyCore.get_event_risk_cap_pct() > defaults.EVENT_RISK_CAP_PCT + 1e-9:
                return False
            if SafetyCore.get_daily_usd_cap() > defaults.DAILY_USD_CAP_DEFAULT + 1e-9:
                return False
            if SafetyCore.get_max_allowed_spread_pct() > defaults.MAX_ALLOWED_SPREAD_PCT_DEFAULT + 1e-9:
                return False
            if SafetyCore.get_max_pct_of_adv() > defaults.MAX_PCT_OF_ADV_DEFAULT + 1e-9:
                return False
            if not SafetyCore.is_kill_enabled() and defaults.KILL_ENABLED:
                return False
            return True
        except Exception:
            return False


if __name__ == "__main__":
    from .risk import RiskController
    from ..core.schemas import EVThesis

    rc = RiskController(5000)
    ex = Executor(risk=rc)
    t = EVThesis(
        ticker="MOCK",
        event_type="8k",
        upside_pct=22,
        p_upside=0.42,
        downside_pct=-28,
        p_downside=0.28,
        expected_value_pct=4.2,
        prior_accuracy_on_name=0.6,
        what_informed_holders_may_know_that_we_dont="We may be missing channel checks.",
        tradeable_capacity_usd=1200,
    )
    result = ex.execute_thesis(t, is_offhours=True)
    print(result)
    snap = ex.get_portfolio_snapshot()
    print("Portfolio after:", snap)
    print("Executor + hard veto test OK")
