"""Ballast Engine (thin liquid conviction / factor core, 20-30% sleeve).

Phase 0: near-passive, e.g. a simple SPY or liquid high-conviction names + small factor tilt.
Only T1 work. Gives clean execution venue and keeps the book from being 100% thin names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..mcp.robinhood_client import RobinhoodClient, get_robinhood_client
from typing import TYPE_CHECKING

# for rebalance thru executor (P1-B)
from .schemas import EVThesis
if TYPE_CHECKING:
    from .executor import Executor


@dataclass
class BallastConfig:
    target_pct_of_total: float = 0.25
    rebalance_threshold: float = 0.05  # drift before touching
    liquid_names: list[str] = None  # e.g. ["SPY", "QQQ", "AAPL", "BRK.B"] or user names
    target_weights: Optional[dict[str, float]] = None  # optional factor/conviction weights (normalized); if None use equal
    # factor tilt is expressed via weights for Phase 1 (deterministic, T1, explainable)


class BallastEngine:
    def __init__(self, client: Optional[RobinhoodClient] = None, config: Optional[BallastConfig] = None):
        self.client = client or get_robinhood_client()
        self.config = config or BallastConfig(liquid_names=["SPY", "IWM"])

    def desired_allocation(self, total_equity: float) -> dict[str, float]:
        """P1-B: support target_weights for deterministic factor/conviction tilt (normalized); fallback equal."""
        names = self.config.liquid_names or ["SPY"]
        target_ballast = total_equity * self.config.target_pct_of_total
        if self.config.target_weights:
            w = self.config.target_weights
            totw = sum(w.get(n, 0.0) for n in names) or 1.0
            return {n: target_ballast * (w.get(n, 0.0) / totw) for n in names}
        per = target_ballast / len(names)
        return {n: per for n in names}

    def get_current_ballast_value(self) -> float:
        pos = {p.ticker: p.market_value for p in self.client.get_positions()}
        return sum(v for t, v in pos.items() if t in (self.config.liquid_names or []))

    def maybe_rebalance(self, total_equity: float) -> list[str]:
        """Return list of human-readable rebalance actions if drift > threshold."""
        desired = self.desired_allocation(total_equity)
        current = {p.ticker: p.market_value for p in self.client.get_positions()}
        actions: list[str] = []
        for name, d_usd in desired.items():
            have = current.get(name, 0.0)
            drift = abs(have - d_usd) / max(d_usd, 1)
            if drift > self.config.rebalance_threshold:
                actions.append(f"rebalance {name} to ~${d_usd:.0f} (currently ${have:.0f})")
        return actions

    def rebalance(self, executor: "Executor", total_equity: float, is_offhours: bool = False) -> list:
        """P1-B: actually execute rebalances *through the Executor* (so safety, caps, idemp, graveyard all apply).
        Returns list of ExecutionResults. Respects band + drift threshold. T1 only.
        """
        results = []
        desired = self.desired_allocation(total_equity)
        current = {p.ticker: p for p in self.client.get_positions()}
        for name, d_usd in desired.items():
            have = current.get(name).market_value if name in current else 0.0
            drift = abs(have - d_usd) / max(d_usd, 1)
            if drift <= self.config.rebalance_threshold:
                continue
            delta = d_usd - have
            if abs(delta) < 5:
                continue
            side = "buy" if delta > 0 else "sell"
            # dummy low-risk ballast thesis (T1, no alpha)
            t = EVThesis(
                ticker=name,
                event_type="ballast_rebalance",
                upside_pct=1.0 if side == "buy" else -0.5,
                p_upside=0.55,
                downside_pct=-2.0 if side == "buy" else 0.5,
                p_downside=0.25,
                expected_value_pct=0.3,
                prior_accuracy_on_name=0.85,
                what_informed_holders_may_know_that_we_dont="Liquid ballast name; we understand the index/factor exposure exactly.",
                tradeable_capacity_usd=abs(delta),
            )
            res = executor.execute_thesis(t, side=side, is_offhours=is_offhours)
            results.append(res)
        return results


if __name__ == "__main__":
    b = BallastEngine()
    snap = b.client.get_portfolio_snapshot() if hasattr(b.client, "get_portfolio_snapshot") else {}
    print("Ballast desired (on $10k book):", b.desired_allocation(10000))
    print("Rebalance actions stub:", b.maybe_rebalance(10000))
