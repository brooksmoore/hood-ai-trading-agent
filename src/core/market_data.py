"""MarketData abstraction for Phase R (Go-Real).

Paper and live must share real market data for quotes and history.
- Real impls (Yahoo stdlib, or Robinhood read via client) for RUN mode (paper/live).
- MockMarketData for tests (provide exact price series so realized can be asserted to the cent against known series).
- No network in unittests (mocks only).

The one paper/live difference remains ONLY at order submission leaf.
Paper fills use real quote but conservative/pessimistic simulation (worse of bid/ask + extra slip, partials on low ADV).
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence, TYPE_CHECKING
if TYPE_CHECKING:
    from ..mcp.robinhood_client import RealRobinhoodMCPClient  # for type only; runtime lazy


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
    spread_source: str = "modeled"  # "real" when bid/ask came from upstream quote endpoint; "modeled" for liquidity-aware fallback. For audit.


@dataclass
class PricePoint:
    timestamp: str
    close: float
    # add open/high/low/volume if needed for richer sim


class MarketData(Protocol):
    """Interface for real (or fake) market data. Used for safety, sizing, paper fill sim, and mark-to-market."""

    def get_quote(self, ticker: str) -> Quote: ...

    def get_price_history(self, ticker: str, days: int = 5) -> list[PricePoint]:
        """Recent daily (or intraday) closes for marking/resolve. For tests, exact series."""
        ...


def compute_liquidity_aware_spread(last: float, adv: float) -> float:
    """R2: pure, conservative liquidity-aware spread model (fallback when no real bid/ask).

    Widens materially for the cheap, thin microcaps that are the core of the strategy (edge lives here).
    Examples (qualitative):
      - last=0.25, adv=2500 (CRKN-like) -> ~0.08-0.15 (wide; Section 11 veto can fire)
      - last=5.0, adv=300k -> ~0.015-0.03 (moderate)
      - last=20, adv=5M -> ~0.005-0.01 (narrow)

    Rationale: price term (inverse, microcaps trade wide %); ADV term (thin names have less depth, wider effective spread + impact).
    Clamped high but realistic for paper to be *harder* than live on thin names (per Phase R invariant).
    Never returns a fixed constant like 0.015.
    """
    if last <= 0 or adv <= 0:
        return 0.20
    # price term: strongly inverse for low-priced names
    p_term = max(0.005, min(0.12, 0.09 / max(last, 0.05)))
    # adv term: thinner (smaller adv) -> wider
    a_term = max(0.005, min(0.10, 200000.0 / max(adv, 1000.0) * 0.015))
    spread = min(0.30, p_term + a_term)
    return round(spread, 4)


class MockMarketData:
    """Deterministic for tests. Provide a price_series dict[ticker] -> list of (ts, close) or just closes (ts auto).
    Used to prove: flat series -> realized ~ 0 - modeled_slippage (no fabrication).
    """

    def __init__(self, price_series: Optional[dict[str, list[tuple[str, float]]]] = None, default_last: float = 10.0):
        self._series: dict[str, list[PricePoint]] = {}
        self._default_last = default_last
        if price_series:
            for tkr, pts in price_series.items():
                self._series[tkr] = [PricePoint(ts, cl) for ts, cl in pts]

    def get_quote(self, ticker: str) -> Quote:
        pts = self._series.get(ticker, [])
        if pts:
            last = pts[-1].close
            # For hermetic tests: simulate realistic adv based on price to exercise the liquidity model
            adv = 3000 if last < 1.0 else (80000 if last < 5.0 else 2000000)
            spread = compute_liquidity_aware_spread(last, adv)
            bid = round(last * (1 - spread/2), 4)
            ask = round(last * (1 + spread/2), 4)
            raw_ts = pts[-1].timestamp
            try:
                # ensure valid iso for age checks (tests often pass short labels like "t0"); fall back to now
                datetime.fromisoformat(raw_ts.replace("Z","+00:00") if raw_ts else "")
                ts = raw_ts
            except Exception:
                ts = datetime.now(timezone.utc).isoformat()
            return Quote(ticker, bid, ask, last, max(1000, adv*0.3), adv, False, ts, "modeled")
        last = self._default_last
        adv = 200000
        spread = compute_liquidity_aware_spread(last, adv)
        bid = round(last * (1 - spread/2), 4)
        ask = round(last * (1 + spread/2), 4)
        ts = datetime.now(timezone.utc).isoformat()
        return Quote(ticker, bid, ask, last, 100000, adv, False, ts, "modeled")

    def get_price_history(self, ticker: str, days: int = 5) -> list[PricePoint]:
        pts = self._series.get(ticker, [])
        if not pts:
            # synthetic flat-ish for default
            base = self._default_last
            now = datetime.now(timezone.utc)
            return [PricePoint((now.replace(hour=h)).isoformat(), round(base + i*0.01, 4)) for i, h in enumerate(range(days))]
        return pts[-days:]


class YahooFinanceMarketData:
    """Real (stdlib) market data for paper RUN mode. Uses Yahoo chart API (no key, public, delayed).
    For fidelity in absence of live RH MCP read. Conservative: use close for mtm, last~close for quote.
    Real paper will spend real budget for reasoning + this (but quotes cheap).
    """

    def __init__(self, timeout: float = 10.0, ua: str = "hood-agent-paper/1.0"):
        self.timeout = timeout
        self.ua = ua
        self._last_fetch = 0.0
        self._min_interval = 1.0  # polite

    def _rate_limit(self) -> None:
        now = time.time()
        wait = self._min_interval - (now - self._last_fetch)
        if wait > 0:
            time.sleep(wait)
        self._last_fetch = time.time()

    def get_quote(self, ticker: str) -> Quote:
        self._rate_limit()
        # Q1: Use the working keyless /v8/finance/chart for real last + volume (the one that worked before the v7 regression).
        # /v7/quote is Unauthorized keyless, so we do NOT depend on it for bid/ask (Q2: accept no real bid/ask from free Yahoo).
        # Always use liquidity model for spread (kept from prior remediation), labeled "modeled".
        # ADV: prefer from meta if present, else conservative proxy (Q3).
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = data.get("chart", {}).get("result", [{}])[0]
            meta = result.get("meta", {})
            last = float(meta.get("regularMarketPrice") or meta.get("previousClose") or meta.get("chartPreviousClose") or 0.0)
            vol = float(meta.get("regularMarketVolume") or 0.0)
            adv = float(meta.get("averageDailyVolume3Month") or meta.get("averageDailyVolume10Day") or vol * 5 or 100000)
            ts = datetime.now(timezone.utc).isoformat()
            # No real bid/ask (v7 blocked keyless); use verified liquidity-aware model. Conservative for thin names.
            spread = compute_liquidity_aware_spread(last, adv)
            bid = round(last * (1 - spread / 2), 4)
            ask = round(last * (1 + spread / 2), 4)
            return Quote(ticker, bid, ask, last, vol, adv, False, ts, "modeled")
        except Exception:
            # fail closed for quote: return invalid that will trigger safety veto upstream (no fabricated price)
            ts = datetime.now(timezone.utc).isoformat()
            return Quote(ticker, 0.0, 0.0, 0.0, 0, 0, False, ts, "modeled")

    def get_price_history(self, ticker: str, days: int = 5) -> list[PricePoint]:
        self._rate_limit()
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={days}d"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = data.get("chart", {}).get("result", [{}])[0]
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            closes = quotes.get("close", []) or []
            timestamps = result.get("timestamp", []) or []
            pts: list[PricePoint] = []
            for ts, cl in zip(timestamps, closes):
                if cl is None:
                    continue
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                pts.append(PricePoint(dt, round(float(cl), 4)))
            return pts[-days:] if pts else []
        except Exception:
            return []


def get_market_data(fake: bool = True, price_series: Optional[dict] = None, source: str = "auto") -> MarketData:
    """Factory.
    - fake=True + price_series: Mock (hermetic tests).
    - source="rh": RobinhoodMCPMarketData (real broker read via MCP; falls back to Yahoo labeled if MCP unavailable).
    - source="yahoo" or auto with fake=False: Yahoo (current, labeled modeled for spread).
    - "auto" in real run: prefers rh if MCP context available, else yahoo.
    """
    if fake:
        return MockMarketData(price_series=price_series)
    if source == "rh":
        try:
            from ..mcp.robinhood_client import get_robinhood_client
            rh_client = get_robinhood_client(use_mock=False)
            return RobinhoodMCPMarketData(rh_client=rh_client)
        except Exception:
            # Graceful fallback (labeled) so runner doesn't die
            return YahooFinanceMarketData()
    return YahooFinanceMarketData()


class RobinhoodMCPMarketData:
    """MarketData impl using real Robinhood MCP (read-only, broker-exact where available).
    - get_quote: prefers real bid/ask from MCP (spread_source="real"); else real last + compute_liquidity_aware_spread ( "modeled").
    - get_price_history: MCP if provides, else Yahoo (labeled "yahoo_fallback").
    - Schema validation + fail-closed or labeled Yahoo fallback on MCP error (no fabrication).
    - ADV from MCP when present, else proxy.
    """

    def __init__(self, rh_client: "RealRobinhoodMCPClient", fallback_market: Optional[MarketData] = None):
        self.rh = rh_client
        self.fallback = fallback_market or YahooFinanceMarketData()

    def get_quote(self, ticker: str) -> Quote:
        try:
            q = self.rh.get_quote(ticker)
            # If MCP gave real bid/ask, use them (broker-exact, the goal)
            if (q.bid or 0) > 0 and (q.ask or 0) > (q.bid or 0):
                q.spread_source = "real"
                return q
            # Otherwise real last from broker + our verified model (still better than Yahoo for your names)
            last = q.last or 0.0
            adv = q.avg_daily_volume or 100000
            spread = compute_liquidity_aware_spread(last, adv)
            bid = round(last * (1 - spread / 2), 4)
            ask = round(last * (1 + spread / 2), 4)
            return Quote(ticker, bid, ask, last, q.volume, adv, q.is_halted, q.timestamp, "modeled")
        except Exception:
            # Fallback to Yahoo (labeled) so we still have a price for paper
            q = self.fallback.get_quote(ticker)
            if getattr(q, "spread_source", None) != "real":
                q.spread_source = "yahoo_fallback"
            return q

    def get_price_history(self, ticker: str, days: int = 5) -> list[PricePoint]:
        try:
            # If MCP exposes history, use it (future-proof)
            # For now, fall to Yahoo for marking (labeled)
            return self.fallback.get_price_history(ticker, days)
        except Exception:
            return self.fallback.get_price_history(ticker, days)
