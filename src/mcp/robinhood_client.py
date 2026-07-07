"""Robinhood Trading MCP client abstraction (Section 14).

The real MCP is brand-new; we schema-validate every response and expect bugs.
This module provides:
- A typed client interface
- A mock implementation for Phase 0-2 (paper + simulation)
- Later: real implementation that calls MCP tools via search_tool / use_tool when the server is configured.

All external calls go through here so the rest of the system never talks raw MCP.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, TYPE_CHECKING
if TYPE_CHECKING:
    from .market_data import MarketData  # for type only in mock init


@dataclass
class Quote:
    ticker: str
    bid: float
    ask: float
    last: float
    volume: float
    avg_daily_volume: float
    is_halted: bool = False
    timestamp: str = ""


@dataclass
class Position:
    ticker: str
    shares: float
    avg_cost: float
    market_value: float


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    filled_shares: float = 0.0
    avg_fill_price: float = 0.0
    reason: str = ""


class RobinhoodClient(Protocol):
    """Minimal interface the Executor and engines need."""

    def get_quote(self, ticker: str) -> Quote: ...
    def get_positions(self) -> list[Position]: ...
    def get_buying_power(self) -> float: ...
    def place_limit_order(
        self, ticker: str, side: str, shares: float, limit_price: float, time_in_force: str = "day",
        quote: Optional["Quote"] = None
    ) -> OrderResult: ...
    def get_open_orders(self, ticker: Optional[str] = None) -> list[dict]: ...
    def cancel_all(self) -> None: ...


class MockRobinhoodClient:
    """Phase 0/1/2 safe mock. Never touches real capital. Simulates realistic microcap friction.
    Phase R: can take market_data: MarketData to source REAL quotes for paper sim fills/marking (while execution stays simulated conservative).
    This keeps paper==live path: only the final submission leaf differs; quotes are real for paper too.
    """

    def __init__(self, starting_cash: float = 5000.0, seed: int = 42, market_data: Optional["MarketData"] = None):
        self._cash = starting_cash
        self._positions: dict[str, Position] = {}
        self._orders: list[dict] = []  # fill history / completed
        self._open_orders: list[dict] = []  # genuinely pending/open for M4 guard (F1 fix)
        self._rng = random.Random(seed)
        self._halt_list: set[str] = set()
        self._market_data = market_data  # Phase R: real quotes for paper mode fidelity (conservative sim on top)

    def _simulate_spread(self, last: float, adv: float) -> tuple[float, float]:
        # Wider on low ADV / low price names
        spread = 0.008 + (50000 / max(adv, 10000)) * 0.01
        spread = min(0.08, max(0.005, spread))
        bid = round(last * (1 - spread / 2), 4)
        ask = round(last * (1 + spread / 2), 4)
        return bid, ask

    def get_quote(self, ticker: str) -> Quote:
        # Phase R: if real market_data injected, use it for quote (paper fidelity: real prices, conservative fill sim here)
        if self._market_data is not None:
            try:
                q = self._market_data.get_quote(ticker)
                # ensure non-zero for downstream; if real failed, fall to sim
                if q.last > 0.01:
                    return q
            except Exception:
                pass
        # Fake but plausible microcap quote (tests / no-md case)
        last = round(1.5 + self._rng.random() * 8.5, 3)
        adv = 80000 + self._rng.random() * 400000
        if ticker in self._halt_list:
            ts = datetime.now(timezone.utc).isoformat()
            return Quote(ticker, 0, 0, last, 0, adv, is_halted=True, timestamp=ts)
        bid, ask = self._simulate_spread(last, adv)
        vol = int(adv * (0.3 + self._rng.random() * 1.2))
        ts = datetime.now(timezone.utc).isoformat()
        return Quote(ticker, bid, ask, last, vol, adv, is_halted=False, timestamp=ts)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_buying_power(self) -> float:
        return self._cash

    def place_limit_order(
        self, ticker: str, side: str, shares: float, limit_price: float, time_in_force: str = "day",
        quote: Optional["Quote"] = None
    ) -> OrderResult:
        q = quote if quote is not None else self.get_quote(ticker)
        if q.is_halted:
            return OrderResult(False, None, 0, 0, "halted")

        # Simulate realistic partial / bad fill on thin names
        mid = (q.ask + q.bid) / 2
        # If limit is way off, fail
        if side == "buy" and limit_price < q.ask * 0.995:
            return OrderResult(False, None, 0, 0, "limit_too_low_vs_ask")
        if side == "sell" and limit_price > q.bid * 1.005:
            return OrderResult(False, None, 0, 0, "limit_too_high_vs_bid")

        fill_price = mid + (self._rng.random() - 0.5) * (q.ask - q.bid) * 1.5  # slippage
        fill_shares = shares
        if q.avg_daily_volume < 150000 and shares > q.avg_daily_volume * 0.03:
            fill_shares = shares * (0.4 + self._rng.random() * 0.4)  # partial

        if side == "buy":
            cost = fill_shares * fill_price
            if cost > self._cash:
                return OrderResult(False, None, 0, 0, "insufficient_cash")
            self._cash -= cost
            if ticker in self._positions:
                p = self._positions[ticker]
                total_s = p.shares + fill_shares
                total_cost = p.avg_cost * p.shares + cost
                p.shares = total_s
                p.avg_cost = total_cost / total_s
                p.market_value = total_s * fill_price
            else:
                self._positions[ticker] = Position(ticker, fill_shares, fill_price, fill_shares * fill_price)
        else:
            if ticker not in self._positions or self._positions[ticker].shares < fill_shares:
                return OrderResult(False, None, 0, 0, "insufficient_shares")
            self._positions[ticker].shares -= fill_shares
            self._cash += fill_shares * fill_price
            if self._positions[ticker].shares < 1e-6:
                del self._positions[ticker]

        oid = f"mock-{len(self._orders)}"
        self._orders.append({"id": oid, "ticker": ticker, "side": side, "shares": fill_shares, "price": fill_price})
        # F1: on fill, clear any matching from open (sync fill model)
        self._open_orders = [o for o in self._open_orders if not (o.get("ticker") == ticker and str(o.get("side","")).lower() == side.lower())]
        return OrderResult(True, oid, fill_shares, fill_price, "filled_sim")

    def cancel_all(self) -> None:
        self._orders.clear()
        self._open_orders.clear()

    # Test helpers
    def force_halt(self, ticker: str) -> None:
        self._halt_list.add(ticker)

    def unfreeze(self, ticker: str) -> None:
        self._halt_list.discard(ticker)

    def get_open_orders(self, ticker: Optional[str] = None) -> list[dict]:
        """Return only genuinely open/pending orders (F1 fix). Fills go to _orders history only.
        Mock fills sync, so normally empty unless force_open_order used for test.
        """
        opens = self._open_orders
        if ticker is None:
            return list(opens)
        return [o for o in opens if o.get("ticker") == ticker]

    def force_open_order(self, ticker: str, side: str = "buy", shares: float = 10.0, price: float = 10.0) -> dict:
        """Test helper to simulate a genuinely pending open order (for proving the guard still works for duplicates)."""
        oid = f"open-{len(self._open_orders)}"
        o = {"id": oid, "ticker": ticker, "side": side, "shares": shares, "price": price, "status": "open"}
        self._open_orders.append(o)
        return o


class RealRobinhoodMCPClient:
    """Real read-only impl via Robinhood Trading MCP.
    Tool surface verified 2026-06-09 against live MCP (mcp__robinhood-trading__* tools).
    - Quotes:    get_equity_quotes(symbols=[...])
    - Positions: get_equity_positions(account_number=...)
    - Cash:      get_portfolio(account_number=...)
    - Orders:    get_equity_orders(account_number=..., state="new")
    - place_limit_order: always NotImplementedError (gated by LIVE_ENABLED + Phase 4 human_go_live).
    - _call: looks for use_tool injected into __main__ (works when Claude is the orchestrator);
      raises RuntimeError when not found — RobinhoodMCPMarketData catches this and falls back to Yahoo (labeled).
    - account_number: set HOOD_RH_ACCOUNT env var (default: 981398050 — the Agentic cash account).
    """
    # Verified agentic account (cash, agentic_allowed=True, nickname="Agentic"). Set via env to override.
    _DEFAULT_ACCOUNT = "981398050"

    def __init__(self):
        import os
        self._tool_prefix = "mcp__robinhood-trading__"
        self._account_number = os.getenv("HOOD_RH_ACCOUNT", self._DEFAULT_ACCOUNT)

    def _call(self, tool_name: str, **params) -> dict:
        # Full qualified name as used by the platform (mcp__robinhood-trading__<tool>).
        qname = f"{self._tool_prefix}{tool_name}"
        try:
            import sys
            use_tool_fn = None
            main_mod = sys.modules.get("__main__")
            if main_mod:
                use_tool_fn = getattr(main_mod, "use_tool", None)
            if use_tool_fn is None:
                use_tool_fn = globals().get("use_tool")
            if use_tool_fn is None:
                raise RuntimeError(
                    "No use_tool in context — RH MCP only available when Claude is the orchestrator. "
                    "Standalone run_paper.py falls back to Yahoo (labeled). Connect MCP and run via Claude."
                )
            raw = use_tool_fn(qname, **params)
            return self._validate(tool_name, raw)
        except Exception as e:
            raise RuntimeError(f"MCP {qname} failed: {e}") from e

    def _validate(self, tool: str, raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise ValueError(f"Bad MCP response for {tool}: expected dict, got {type(raw)}")
        if raw.get("error"):
            raise ValueError(f"MCP error {tool}: {raw['error']}")
        # Verified response shapes (2026-06-09 live survey):
        # get_equity_quotes -> {"data": {"results": [{"quote": {...}, "close": {...}}]}}
        # get_equity_positions -> {"data": {"positions": [...]}}
        # get_portfolio -> {"data": {"buying_power": {"buying_power": "0.0", ...}, ...}}
        # get_equity_orders -> {"data": {"orders": [...]}}
        data = raw.get("data")
        if tool == "get_equity_quotes":
            if not isinstance(data, dict) or "results" not in data:
                raise ValueError(f"get_equity_quotes: missing data.results: {raw}")
        elif tool == "get_equity_positions":
            if not isinstance(data, dict) or "positions" not in data:
                raise ValueError(f"get_equity_positions: missing data.positions: {raw}")
        elif tool == "get_portfolio":
            if not isinstance(data, dict) or "buying_power" not in data:
                raise ValueError(f"get_portfolio: missing data.buying_power: {raw}")
        return raw

    def get_quote(self, ticker: str) -> Quote:
        # Verified param: symbols=[list]; response: data.results[0].quote
        raw = self._call("get_equity_quotes", symbols=[ticker])
        results = raw["data"]["results"]
        if not results:
            raise ValueError(f"No quote result for {ticker}")
        q = results[0].get("quote", {})
        # Prefer last_non_reg if more recent (after-hours), else last_trade_price
        last_trade = float(q.get("last_trade_price") or 0.0)
        last_non_reg = float(q.get("last_non_reg_trade_price") or 0.0)
        last = last_non_reg if last_non_reg > 0 else last_trade
        bid = float(q.get("bid_price") or 0.0)
        ask = float(q.get("ask_price") or 0.0)
        # Volume: not in quote response; proxy from history or 0 (ADV computed from spread model if 0)
        vol = 0.0
        adv = 100000.0  # proxy; real ADV unavailable from this endpoint
        ts = q.get("venue_last_trade_time") or datetime.now(timezone.utc).isoformat()
        # Halted/unlisted: treat state != "active" as not normally tradeable
        state = q.get("state", "active")
        halted = (state != "active")
        # Zero bid on epoch timestamp (0001-01-01) means truly no market
        bid_ts = q.get("venue_bid_time", "")
        if "0001-01-01" in str(bid_ts):
            bid = 0.0
            ask = 0.0
        return Quote(ticker, bid, ask, last, vol, adv, halted, ts)

    def get_positions(self) -> list[Position]:
        raw = self._call("get_equity_positions", account_number=self._account_number)
        items = raw["data"].get("positions") or []
        out: list[Position] = []
        for p in items if isinstance(items, list) else []:
            if not isinstance(p, dict):
                continue
            # Verified fields: symbol, quantity, average_buy_price
            out.append(Position(
                ticker=str(p.get("symbol") or p.get("ticker") or ""),
                shares=float(p.get("quantity") or p.get("shares") or 0),
                avg_cost=float(p.get("average_buy_price") or p.get("avg_cost") or 0),
                market_value=float(p.get("market_value") or 0),
            ))
        return out

    def get_buying_power(self) -> float:
        raw = self._call("get_portfolio", account_number=self._account_number)
        bp_block = raw["data"].get("buying_power", {})
        if isinstance(bp_block, dict):
            return float(bp_block.get("buying_power") or bp_block.get("unleveraged_buying_power") or 0)
        return float(raw["data"].get("cash") or 0)

    def place_limit_order(self, ticker: str, side: str, shares: float, limit_price: float,
                          time_in_force: str = "day", quote: Optional[Quote] = None) -> OrderResult:
        raise NotImplementedError(
            "RH MCP order submission is gated by Phase 4 human_go_live + LIVE_ENABLED. "
            "Paper-only until calibration gate passes."
        )

    def get_open_orders(self, ticker: Optional[str] = None) -> list[dict]:
        try:
            kwargs: dict = {"account_number": self._account_number, "state": "new"}
            if ticker:
                kwargs["symbol"] = ticker
            raw = self._call("get_equity_orders", **kwargs)
            return raw["data"].get("orders") or []
        except Exception:
            return []

    def cancel_all(self) -> None:
        # cancel_equity_order requires a specific order_id; no bulk cancel on this MCP surface.
        # For paper mode, cancellation is handled locally (MockRobinhoodClient.cancel_all).
        # Log and pass — no orphaned live orders since place_limit_order is NotImplementedError.
        pass


def get_robinhood_client(use_mock: bool = True, **mock_kwargs) -> RobinhoodClient:
    """Factory. Phase R: pass market_data=... (real impl) to Mock for paper RUN (real quotes + conservative fill sim on them).
    Real MCP client (use_mock=False) for live reads when available; order place leaf remains human-gated (LIVE_ENABLED + Phase4 gate).
    Tests: use_mock + fake MarketData with exact price series for realized assertions.
    """
    if use_mock:
        return MockRobinhoodClient(**mock_kwargs)
    # Real Robinhood MCP (read for paper fidelity or live) - discover via search_tool/use_tool + validate.
    # Per this handoff: implement read-only (positions, buying_power, quote). place stays gated.
    # Lazy: mcp SDK (pinned in requirements) + platform use_tool for calls in this env (owner MCP must be connected for real).
    try:
        return RealRobinhoodMCPClient()
    except Exception as e:
        raise NotImplementedError(f"Real Robinhood MCP client requires connected MCP (owner step). {e}") from e


# Schema validation helper example (to be used on every real MCP response)
def validate_mcp_response(raw: dict[str, Any], expected_shape: str) -> dict[str, Any]:
    """Fail fast on unexpected shape. The product is new; we do not trust it."""
    # For Phase 0 this is a no-op stub that at least asserts keys in mock paths.
    if expected_shape == "quote" and "bid" not in raw and "last" not in raw:
        raise ValueError(f"Malformed quote response: {raw}")
    return raw


if __name__ == "__main__":
    client = get_robinhood_client(starting_cash=10000)
    q = client.get_quote("ABCD")
    print("Quote:", q)
    res = client.place_limit_order("ABCD", "buy", 120, q.ask * 0.999)
    print("Order:", res)
    print("Positions:", client.get_positions())
    print("Cash left:", client.get_buying_power())
    print("MCP client mock OK")
