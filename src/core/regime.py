"""Market-regime classifier — stdlib only (no external deps, per project constraint).

Labels each trade by the MARKET ENVIRONMENT at open time so the calibration gate's
multi-regime requirement is meaningful (not satisfied by an arbitrary time bucket).

Two axes, combined into one label like "riskon_calm":
  - trend: small-cap proxy (IWM) last close vs its 50-day SMA  -> riskon / riskoff
  - vol:   20-day realized vol of IWM, annualized, vs threshold -> calm / stressed

Why IWM, not SPY: the strategy's universe IS small/microcap, so small-cap risk appetite
is the relevant environment axis.

Honesty / fail-safe:
  - Returns "unknown" (never fabricated) if data is missing/insufficient or the fetch fails.
    Callers/gate should treat "unknown" as NOT a real regime (do not let it manufacture
    multi-regime coverage).
  - Computed at OPEN time and stored immutably on the trade; the resolve row must reuse the
    stored open-time regime, not re-classify at resolve.
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import date, datetime
from typing import Callable, Optional, Sequence, Union

REGIME_SYMBOL = "IWM"
_SMA_WINDOW = 50
_VOL_WINDOW = 20
_VOL_ANNUALIZED_THRESHOLD = 0.20  # 20% annualized realized vol = calm/stressed boundary
_TRADING_DAYS = 252
_UA = "hood_agent_1 regime research bcm3000@gmail.com"

UNKNOWN = "unknown"

# in-process cache: (symbol, YYYY-MM-DD) -> list[float] closes (one fetch/day, not per-trade)
_cache: dict = {}


def _fetch_closes(symbol: str, timeout: int = 15) -> list[float]:
    """Daily closes for the last ~6 months from the keyless Yahoo v8 chart endpoint."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=6mo"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    result = data.get("chart", {}).get("result", [{}])[0]
    quote = (result.get("indicators", {}).get("quote", [{}]) or [{}])[0]
    closes = quote.get("close") or []
    return [float(c) for c in closes if c is not None]


def _realized_vol_annualized(closes: Sequence[float]) -> float:
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    w = rets[-_VOL_WINDOW:]
    if len(w) < 2:
        return 0.0
    mean = sum(w) / len(w)
    var = sum((x - mean) ** 2 for x in w) / (len(w) - 1)
    return math.sqrt(var) * math.sqrt(_TRADING_DAYS)


def classify_regime(
    asof: Optional[Union[date, datetime]] = None,
    closes: Optional[Sequence[float]] = None,
    fetch_fn: Optional[Callable[[str], list[float]]] = None,
    symbol: str = REGIME_SYMBOL,
) -> str:
    """Return a market-regime label for `asof` (default today).

    `closes` (chronological daily closes) may be injected for hermetic tests; otherwise the
    daily series is fetched (and cached per calendar day). Returns UNKNOWN on any failure.
    """
    if closes is None:
        d = asof.date() if isinstance(asof, datetime) else (asof or date.today())
        key = (symbol, d.isoformat())
        if fetch_fn is None and key in _cache:
            closes = _cache[key]
        else:
            try:
                closes = (fetch_fn or _fetch_closes)(symbol)
            except Exception:
                return UNKNOWN
            if fetch_fn is None:
                _cache[key] = closes

    series = [c for c in closes if c and c > 0]
    if len(series) < _SMA_WINDOW + 1:
        return UNKNOWN

    sma = sum(series[-_SMA_WINDOW:]) / _SMA_WINDOW
    trend = "riskon" if series[-1] >= sma else "riskoff"
    vol = _realized_vol_annualized(series)
    vbucket = "stressed" if vol >= _VOL_ANNUALIZED_THRESHOLD else "calm"
    return f"{trend}_{vbucket}"
