#!/usr/bin/env python3
"""
Phase R continuous paper runner (event-driven, 24/7 style).

Uses SINGLE code path:
- Real (or fixture) EDGAR / event source -> ReactionLayer (real guards) -> EV (real/fake llm) -> Auditor -> Risk -> Executor (paper mode with real MarketData for quotes)
- Paper fill = conservative sim AGAINST the REAL quote from MarketData.
- Open paper position tracked; after hold period, resolve using LATER real quote -> real realized_return_pct (entry/exit - modeled slip).
- Logs to Graveyard with real numbers (for Phase4 gate).
- Honors kill, budget, SafetyCore, OOS param_version from meta.

Still PAPER ONLY: run_mode=PAPER, LIVE_ENABLED=False. No real orders.

For tests: pass fakes (fake llm, mock md with exact price series, fake trigger feed).
For real run: real llm (env key), Yahoo or RH md, real EDGAR trigger source.

Run: PYTHONPATH=. python3 run_paper.py --data-dir /tmp/paper --use-real-llm (gated, needs key + net)
"""

from __future__ import annotations

import argparse
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from src.core.schemas import RunMode, EVThesis
from src.core.llm_client import get_llm_client, LLMClient
from src.core.market_data import get_market_data, MarketData, MockMarketData, compute_liquidity_aware_spread
from src.core.market_calendar import is_us_market_day
from src.core.regime import classify_regime
from src.mcp.robinhood_client import get_robinhood_client
from src.core.executor import Executor
from src.core.risk import RiskController
from src.core.reaction_layer import ReactionLayer, Trigger
from src.core.ev_engine import build_ev_thesis
from src.core.auditor import run_auditor
from src.core.storage import GraveyardDB
from src.core.counterfactual import resolve_counterfactuals
from src.core.safety_core import SafetyCore
from src.data.edgar import EdgarClient, strip_html_to_text
from src.core.budget import DailyBudget, BudgetConfig
from src.core.ownership_ledger import OwnershipLedger
import json  # for positions persist
import urllib.request  # for DynamicEDGARFeed efts calls

# ── Sleeve configuration ──────────────────────────────────────────────────────
# Mandate 1: hood sizes off its OWN sleeve budget, never the whole account NAV.
# Operator sets HOOD_SLEEVE_USD in environment. Default is intentionally low ($500)
# so a missing env var causes conservatism, not over-sizing. Raise only after the
# Phase 4 calibration gate passes and the owner explicitly sets the real allocation.
_HOOD_SLEEVE_USD = float(os.getenv("HOOD_SLEEVE_USD", "500"))

# G14: how long to wait before mark-to-marketing a VETOED +EV thesis against a real quote.
COUNTERFACTUAL_HORIZON_DAYS = int(os.getenv("HOOD_COUNTERFACTUAL_HORIZON_DAYS", "5"))


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Stdlib-only .env loader (no python-dotenv dep, per project zero-deps rule).
    Values here OVERRIDE the shell environment so hood's ANTHROPIC_API_KEY is
    controllable independently of the shared shell-wide export other bots read.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


_load_dotenv()


def load_universe(path: Optional[Path] = None, env_var: str = "HOOD_UNIVERSE") -> list[str]:
    """U-A: Load configurable ticker universe. Priority: env comma-list > file (default config/universe.txt) > small default.
    Owner edits the file or sets env. Capacity layer filters tradeable at runtime.
    """
    if os.environ.get(env_var):
        return [t.strip().upper() for t in os.environ[env_var].split(",") if t.strip()]
    p = path or Path("config/universe.txt")
    if p.exists():
        tickers = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tickers.append(line.split()[0].upper())
        if tickers:
            return tickers
    # Starter microcaps (real names likely to have filings; owner must curate for liquidity/data coverage)
    return ["HOLO", "SNTG", "KXIN", "CRKN", "LGMK"]


class PaperPositionStore:
    """U-C: Persistent store for open paper positions across restarts.
    Simple json file. Keys by event_id (or ticker for simplicity). Stores entry info + entry_time.
    On resolve, the caller writes the realized row; we remove from open.
    """
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._open = json.loads(self.path.read_text())
            except Exception:
                self._open = {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._open, indent=2, default=str))

    def get_open(self) -> dict[str, dict]:
        return dict(self._open)

    def open_position(self, event_id: str, thesis: EVThesis, entry_fill: float, entry_time: str, regime: str = "live", hold_hours: float = 4.0) -> None:
        self._open[event_id] = {
            "ticker": thesis.ticker,
            "thesis_json": thesis.to_json() if hasattr(thesis, "to_json") else str(thesis),
            "entry_fill": entry_fill,
            "entry_time": entry_time,
            "regime": regime,
            "hold_hours": hold_hours,
        }
        self._save()

    def close_position(self, event_id: str) -> Optional[dict]:
        pos = self._open.pop(event_id, None)
        if pos:
            self._save()
        return pos

    def mark_all(self, market_data: MarketData) -> None:
        """Optional: update mtm values using current quotes (for logging/monitoring)."""
        for eid, pos in self._open.items():
            try:
                q = market_data.get_quote(pos["ticker"])
                pos["last_mark"] = {"price": q.last, "ts": datetime.now(timezone.utc).isoformat()}
            except Exception as e:
                # best-effort mtm; log but do not affect trading
                print(f"[POS STORE MARK] eid={eid} mark failed: {e}")
        self._save()


class ProcessedEventStore:
    """CL-1 (cross-learning from pure_arb_bot): runner-owned persisted set of processed event_ids.
    Enables observer (raw feed) / runner (action) separation. Feeds can be stateless; runner skips
    duplicates using persisted state (survives restarts, unlike in-memory reaction one-strike or feed.seen).
    """
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._processed: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self._processed = set(data.get("processed", []))
            except Exception:
                self._processed = set()

    def _save(self) -> None:
        self.path.write_text(json.dumps({"processed": sorted(self._processed)}, indent=2))

    def get_processed(self) -> set[str]:
        return set(self._processed)

    def mark_processed(self, event_id: str) -> None:
        if event_id:
            self._processed.add(event_id)
            self._save()


class FakeEventFeed:
    """Injectable for tests (offline). Yields triggers from list."""
    def __init__(self, triggers: Sequence[Trigger]):
        self._triggers = list(triggers)
        self._idx = 0

    def next_events(self, max_n: int = 10) -> list[Trigger]:
        out = self._triggers[self._idx : self._idx + max_n]
        self._idx += len(out)
        return out


_FILING_STALENESS_DAYS = 90   # skip filings older than this — no LLM spend on stale events
_FILING_TEXT_MAX_CHARS = 8000  # truncate before sending to LLM (~2k tokens at 4 chars/token)

class SimpleEDGARFeed:
    """Real-ish event source for runner. Polls EDGAR recent (respect rate), yields new 8k etc as triggers.
    In prod would use push/RSS. For Phase R demo, injectable + rate safe.
    """
    def __init__(self, edgar: EdgarClient, tickers: list[str], seen: set[str] = None):
        self.edgar = edgar
        self.tickers = tickers
        self.seen: set[str] = seen or set()

    @staticmethod
    def _is_stale(filing_date: str) -> bool:
        """True if filing is older than _FILING_STALENESS_DAYS. Fail-open (let through if unparseable)."""
        if not filing_date:
            return False
        try:
            age = (datetime.now() - datetime.strptime(filing_date, "%Y-%m-%d")).days
            return age > _FILING_STALENESS_DAYS
        except (ValueError, TypeError):
            return False

    def next_events(self, max_n: int = 5) -> list[Trigger]:
        events: list[Trigger] = []
        for tkr in self.tickers:
            try:
                # F3: use REAL EdgarClient API (no invented sigs)
                cik = self.edgar.get_cik_for_ticker(tkr)
                if not cik:
                    # per-ticker fail: log (do not silent [] forever)
                    print(f"[EDGAR FEED] no CIK for {tkr}; skipping ticker (not swallowed)")
                    continue
                # real sig: (cik, forms=None, limit=20) -> list[FilingRef]
                filings = self.edgar.get_recent_filings(cik, forms=["8-K", "8-K/A", "S-3", "S-1"], limit=5) or []
                for f in filings:
                    # FilingRef is dataclass: attribute access, NOT .get()
                    if not hasattr(f, "accession") or not f.accession:
                        continue
                    acc = f.accession
                    if acc in self.seen:
                        continue
                    # staleness gate: skip before fetching raw text (free check, avoids LLM spend on old events)
                    filing_date = getattr(f, "filing_date", "") or ""
                    if self._is_stale(filing_date):
                        self.seen.add(acc)  # mark so we don't re-check it next cycle
                        print(f"[EDGAR FEED] skipping stale filing {acc} ({filing_date}) for {tkr}")
                        continue
                    self.seen.add(acc)
                    # get_filing_raw expects FilingRef (not acc str)
                    raw = self.edgar.get_filing_raw(f, ticker=tkr) or ""
                    form = getattr(f, "form", "") or ""
                    # Bug fix (2026-07-06): strip inline-XBRL/HTML markup BEFORE truncating —
                    # otherwise the truncation budget is consumed by XML namespace declarations
                    # and cover-page tagging before reaching the actual Item disclosure text.
                    # See edgar.py's strip_html_to_text docstring for the live-verified finding.
                    ev = Trigger(
                        event_id=acc,
                        ticker=tkr,
                        event_type="8k" if "8-K" in str(form).upper() else "filing",
                        raw_filing_text=strip_html_to_text(raw)[:_FILING_TEXT_MAX_CHARS],
                        is_offhours=False,
                    )
                    events.append(ev)
                    if len(events) >= max_n:
                        return events
            except Exception as e:
                # F3/F5: do not silently swallow; log the specific failure (per-ticker ok to skip, but visible)
                print(f"[EDGAR FEED ERROR] ticker={tkr} reason={type(e).__name__}: {e}")
                continue  # still resilient per-ticker, but now loud
        return events


class DynamicEDGARFeed:
    """Discovery-first event feed. Scans EDGAR's current-filings API for ALL 8-K/S-3 filed
    today across the entire public-company universe, then applies free pre-filters (price,
    ADV, spread via Yahoo) before fetching filing text. Replaces the static 38-name list
    with live daily discovery — ~5–20 qualifying events/day at ~$0.013–$0.052 Haiku spend,
    leaving headroom for 1–2 Sonnet EV evaluations within the $0.10/day cap.

    Pre-filter stack (all free, no LLM):
      1. Skip registered investment companies (file_num starts "814-")
      2. Skip pure-exhibit filings (items == {9.01} only)
      3. Skip CIKs that don't resolve to a listed ticker
      4. Skip if already in seen (dedup against ProcessedEventStore)
      5. Yahoo quote: price $2–$20, ADV >$1M, modeled spread <3%

    Qualifying events → fetch raw filing text → yield Trigger (same contract as SimpleEDGARFeed).
    """
    # EDGAR efts API breaks when forms with '/' (8-K/A, S-3/A) are combined in one query.
    # 8-K + S-3 covers >95% of material events; amendments rarely add new signal.
    _FORMS = "8-K,S-3"
    _PRICE_MIN = 2.0
    _PRICE_MAX = 20.0
    _ADV_MIN = 1_000_000.0
    _SPREAD_MAX = 0.03   # mirrors Section 11 veto threshold
    _PAGE_SIZE = 100
    _MAX_PAGES = 4       # up to 400 raw filings; typical trading day ~80–120

    def __init__(self, edgar: EdgarClient, market_data: MarketData, seen: set[str] = None):
        self.edgar = edgar
        self.market_data = market_data
        self.seen: set[str] = seen or set()
        self._cik_to_ticker: dict[str, str] = {}   # zero-padded CIK -> ticker
        self._cik_map_loaded = False
        self._cached_raw: list[dict] = []           # today's raw filings, fetched once
        self._cached_for_dates: list[str] = []      # which dates the cache covers

    def _ensure_cik_map(self) -> None:
        if self._cik_map_loaded:
            return
        # Reuse edgar's in-process cache when available (avoid double-fetching company_tickers.json)
        if self.edgar._tickers_cache is None:
            try:
                url = "https://www.sec.gov/files/company_tickers.json"
                self.edgar._rate_limit()
                req = urllib.request.Request(url, headers={"User-Agent": self.edgar.ua})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.edgar._tickers_cache = json.loads(resp.read())
            except Exception as e:
                print(f"[DYNAMIC FEED] ticker map load failed: {e}")
                self._cik_map_loaded = True
                return
        for _, entry in (self.edgar._tickers_cache.items()
                         if isinstance(self.edgar._tickers_cache, dict)
                         else enumerate(self.edgar._tickers_cache)):
            if isinstance(entry, dict):
                cik = str(entry.get("cik_str", "")).zfill(10)
                ticker = str(entry.get("ticker", "")).upper().strip()
                if cik and ticker:
                    self._cik_to_ticker[cik] = ticker
        self._cik_map_loaded = True
        print(f"[DYNAMIC FEED] CIK→ticker map loaded: {len(self._cik_to_ticker)} entries")

    def _fetch_todays_filings(self, date_str: str) -> list[dict]:
        """Fetch all matching filings for date_str from EDGAR efts. Returns list of _source dicts."""
        results: list[dict] = []
        for page in range(self._MAX_PAGES):
            url = (
                f"https://efts.sec.gov/LATEST/search-index"
                f"?q=&forms={self._FORMS}&dateRange=custom"
                f"&startdt={date_str}&enddt={date_str}"
                f"&from={page * self._PAGE_SIZE}&size={self._PAGE_SIZE}"
            )
            try:
                self.edgar._rate_limit()
                req = urllib.request.Request(url, headers={"User-Agent": self.edgar.ua})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read())
            except Exception as e:
                print(f"[DYNAMIC FEED] efts page {page} error: {e}")
                break
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break
            results.extend(h["_source"] for h in hits if "_source" in h)
            total = data.get("hits", {}).get("total", {}).get("value", 0)
            if len(results) >= total:
                break
        return results

    def _passes_quote_filter(self, ticker: str) -> tuple[bool, str]:
        """Free pre-filter: real price/ADV/spread check via Yahoo. No LLM, no cost."""
        try:
            q = self.market_data.get_quote(ticker)
            if not q or q.last <= 0:
                return False, "no_price"
            if q.last < self._PRICE_MIN or q.last > self._PRICE_MAX:
                return False, f"price_{q.last:.2f}"
            if q.avg_daily_volume < self._ADV_MIN:
                return False, f"adv_{q.avg_daily_volume:.0f}"
            # prefer real spread when available (spread_source="real"); else modeled
            if q.ask > q.bid > 0:
                spread = (q.ask - q.bid) / max(q.last, 0.01)
            else:
                spread = compute_liquidity_aware_spread(q.last, q.avg_daily_volume)
            if spread > self._SPREAD_MAX:
                return False, f"spread_{spread:.3f}"
            return True, "ok"
        except Exception as e:
            return False, f"quote_err_{type(e).__name__}"

    def next_events(self, max_n: int = 20) -> list[Trigger]:
        self._ensure_cik_map()
        # Scan last 2 calendar days: EDGAR indexes filings progressively through the morning,
        # so the 9:35 AM run would miss pre-open filings still being indexed. Yesterday catches
        # anything late; dedup (self.seen + ProcessedEventStore) handles re-processing safely.
        # Cache: fetch efts ONCE per date pair — runner calls next_events() on every cycle but
        # the filing list for a given date doesn't change between cycles.
        from datetime import timedelta
        today = datetime.now().date()
        dates = [today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")]
        if dates != self._cached_for_dates:
            raw: list[dict] = []
            for d in dates:
                raw.extend(self._fetch_todays_filings(d))
            # Only cache on a successful non-empty fetch. If the efts API failed and returned
            # nothing, leave _cached_for_dates unset so the next cycle retries the fetch.
            if raw:
                self._cached_raw = raw
                self._cached_for_dates = dates
                print(f"[DYNAMIC FEED] {len(raw)} raw filings for {dates}, scanning...")
        raw = self._cached_raw

        events: list[Trigger] = []
        skip = {"dup": 0, "inv_co": 0, "exhibit_only": 0, "no_ticker": 0, "quote": 0, "fetch_err": 0}

        from src.data.edgar import FilingRef  # local to avoid circular at module level

        for src in raw:
            accession = src.get("adsh", "")
            if not accession or accession in self.seen:
                skip["dup"] += 1
                continue

            # Free filter 1: registered investment company
            if any(str(fn).startswith("814-") for fn in src.get("file_num", [])):
                self.seen.add(accession)
                skip["inv_co"] += 1
                continue

            # Free filter 2: pure-exhibit filing (9.01 with no other items = no material content)
            items = set(src.get("items", []))
            if items and items <= {"9.01"}:
                self.seen.add(accession)
                skip["exhibit_only"] += 1
                continue

            # Resolve CIK → ticker
            ciks = src.get("ciks", [])
            cik = str(ciks[0]).zfill(10) if ciks else ""
            ticker = self._cik_to_ticker.get(cik, "")
            if not ticker:
                self.seen.add(accession)
                skip["no_ticker"] += 1
                continue

            # Dedup against caller-supplied seen set (ProcessedEventStore)
            if accession in self.seen:
                skip["dup"] += 1
                continue

            # Free filter 3: Yahoo quote — price, ADV, spread
            passes, reason = self._passes_quote_filter(ticker)
            if not passes:
                self.seen.add(accession)
                skip["quote"] += 1
                continue

            # Passed all free filters — fetch filing text via existing EdgarClient machinery.
            # get_recent_filings resolves the primary doc URL; match by accession.
            self.seen.add(accession)
            filing_date = src.get("file_date", today)
            form = src.get("form", "8-K")
            raw_text = ""
            try:
                refs = self.edgar.get_recent_filings(
                    cik,
                    forms=["8-K", "8-K/A", "S-3", "S-3/A"],
                    limit=10,
                )
                ref = next((r for r in refs if r.accession == accession), None)
                if ref:
                    raw_text = self.edgar.get_filing_raw(ref, ticker=ticker) or ""
                else:
                    # accession not in first 10 — construct best-effort URL from index
                    cik_no0 = cik.lstrip("0") or "0"
                    acc_no0 = accession.replace("-", "")
                    idx_url = (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{cik_no0}/{acc_no0}/{accession}-index.htm"
                    )
                    ref = FilingRef(form=form, accession=accession,
                                   filing_date=filing_date, primary_doc_url=idx_url)
                    raw_text = self.edgar.get_filing_raw(ref, ticker=ticker) or ""
            except Exception as e:
                print(f"[DYNAMIC FEED] fetch error {ticker}/{accession}: {type(e).__name__}: {e}")
                skip["fetch_err"] += 1
                continue

            # Bug fix (2026-07-01): this used to tag event_type as f"8-K items={item_label}"
            # (e.g. "8-K items=2.02,5.02"), which NEVER matches ReactionLayer's
            # allowed_event_types ("8k","spinoff","restructuring","earnings","fda") — every
            # single trigger from this feed was silently rejected at tier1's very first check
            # (event_type_not_allowed), before triage, before any LLM call, before any
            # Graveyard record. Confirmed via hood_state.json's candidate_events_today metric:
            # 19 candidates flagged, 0 LLM calls, 0 spend, 0 new graveyard rows since this feed
            # went live. Matches SimpleEDGARFeed's existing (working) convention instead.
            # Bug fix (2026-07-06): strip inline-XBRL/HTML markup BEFORE truncating. Verified
            # live: 100% of real candidates today were rejected by Haiku triage as "8-K header
            # only; no substantive event content provided" because the raw HTML's first 8000
            # chars were pure XML namespace declarations + XBRL cover-page tagging on every
            # single one (e.g. accession 0001213900-26-075248: raw 51,124 chars; the actual
            # "Item 1.01" text didn't appear until deep past the truncation cutoff). See
            # edgar.py's strip_html_to_text() docstring.
            ev = Trigger(
                event_id=accession,
                ticker=ticker,
                event_type="8k" if "8-K" in form.upper() else "filing",
                raw_filing_text=strip_html_to_text(raw_text)[:_FILING_TEXT_MAX_CHARS],
                is_offhours=False,
            )
            events.append(ev)
            if len(events) >= max_n:
                break

        print(f"[DYNAMIC FEED] skipped={skip} → {len(events)} events passed to triage")
        return events


def _make_paper_executor(
    data_dir: Path,
    market_data: MarketData,
    graveyard: GraveyardDB,
    llm: LLMClient,
    client=None,
    sleeve_usd: float = _HOOD_SLEEVE_USD,
) -> Executor:
    """Supports injectable client for hermetic tests of confirm paths (CL-1 remaining).
    If client provided, use it (e.g. FakeBroker that can return [] from get_positions post-fill).

    Mandate 1: sleeve_usd is hood's allocated capital, NOT the whole account NAV.
    RiskController sizes off this sleeve so position limits are relative to hood's
    own budget, not whatever else is in the shared account.
    """
    if client is None:
        client = get_robinhood_client(use_mock=True, market_data=market_data, starting_cash=sleeve_usd)
    risk = RiskController(sleeve_usd)
    ledger = OwnershipLedger(data_dir)
    ex = Executor(
        client=client,
        risk=risk,
        graveyard=graveyard,
        run_mode=RunMode.PAPER,
        market_data=market_data,
        is_killed=lambda: (data_dir / "KILL").exists(),
        ownership_ledger=ledger,
    )
    return ex


def _write_state_snapshot(
    data_dir: Path,
    ex: Executor,
    budget: "DailyBudget",
    regime: str,
    results: list[dict],
) -> None:
    """Mandate 3 (prior session): write hood_state.json — the machine-readable read surface
    for fleet dashboards. Extended this session (cost-efficiency mandate) with LLM economics:
    candidate_events_today, llm_calls_today, llm_spend_today_usd, llm_breaker_tripped — so the
    watcher/reasoner split and the spend breaker are externally observable, not just internally
    enforced.

    Fields are the uniform contract shared across all fleet agents:
      agent, timestamp, stage, live_enabled, positions, cash_sleeve, nav_sleeve,
      open_theses, last_event_ts, health, regime, budget_remaining_usd, llm_metrics.
    """
    owned = ex.ownership_ledger.all_owned() if ex.ownership_ledger else []
    positions = [
        {"ticker": e.ticker, "shares": e.shares, "avg_cost": e.avg_cost,
         "cost_basis_usd": round(e.shares * e.avg_cost, 2), "entry_time": e.entry_time}
        for e in owned
    ]
    cost_basis_total = sum(p["cost_basis_usd"] for p in positions)
    cash_sleeve = ex.risk.agentic_sleeve_usd - cost_basis_total if ex.risk else 0.0
    nav_sleeve = ex.risk.agentic_sleeve_usd if ex.risk else 0.0  # cost basis proxy (no live mtm in paper)

    last_event_ts = None
    open_theses = []
    for r in reversed(results):
        if r.get("action") in ("opened", "resolved") and last_event_ts is None:
            last_event_ts = datetime.now(timezone.utc).isoformat()
        if r.get("action") == "opened":
            open_theses.append({"event": r.get("event"), "entry": r.get("entry")})

    # Bug fix (this session): the prior version read budget._remaining_today / _spent_today,
    # neither of which exist on DailyBudget — both branches silently fell through to None via
    # the try/except, so budget_remaining_usd has always been null. Use the real public API.
    spent_today, _frac = budget.current_spend()
    budget_remaining = budget.remaining_today()
    llm_breaker_tripped = budget.breaker_tripped()

    health = "ok"
    if (data_dir / "KILL").exists():
        health = "killed"
    elif llm_breaker_tripped:
        health = "budget_exhausted"

    snapshot = {
        "agent": "hood",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": "paper",
        "live_enabled": SafetyCore.is_live_enabled(),
        "positions": positions,
        "cash_sleeve": round(cash_sleeve, 2),
        "nav_sleeve": round(nav_sleeve, 2),
        "open_theses": open_theses,
        "last_event_ts": last_event_ts,
        "health": health,
        "regime": regime,
        "budget_remaining_usd": round(budget_remaining, 4),
        "llm_metrics": {
            "candidate_events_today": budget.candidate_event_count(),
            "llm_calls_today": budget.call_count(),
            "llm_spend_today_usd": round(spent_today, 4),
            "llm_breaker_tripped": llm_breaker_tripped,
        },
    }
    out_path = data_dir / "hood_state.json"
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    tmp.replace(out_path)


def run_paper(
    data_dir: Path = Path("data"),
    logs_dir: Path = Path("logs"),
    tickers: Optional[Sequence[str]] = None,  # U-A: if None, load from config/universe.txt or env
    use_real_llm: bool = False,
    market_data: Optional[MarketData] = None,
    event_feed: Any = None,  # Fake or SimpleEDGARFeed
    max_cycles: Optional[int] = 5,  # None = run until killed (always-on mode for go-live)
    hold_bars: int = 2,  # "bars" to hold before resolve (for demo; real would use time or signal)
    llm: Optional[LLMClient] = None,  # for tests: inject fake
    positions_path: Optional[Path] = None,  # for persist
    source: str = "auto",  # rh | yahoo | fake | auto (for get_market_data)
    client=None,  # injectable for CL-1 confirm-fail tests (FakeBroker)
    daily_usd_cap: Optional[float] = None,  # if set, tighten the protected cap (down-only via SafetyCore)
    respect_market_calendar: bool = False,  # if True, skip all work on non-trading days (no EDGAR, no LLM)
    market_day_fn: Optional[Callable[[], bool]] = None,  # injectable clock for tests; default = is_us_market_day
    regime_fn: Optional[Callable[[], str]] = None,  # injectable market-regime label; default classify_regime on real runs
) -> dict[str, Any]:
    """Main continuous paper runner loop. Single path, real data where possible.
    Returns summary (for tests).
    U-B/U-C: supports real wiring + persistent positions.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    SafetyCore.init_log(logs_dir)

    # Operator may tighten the daily spend cap. This rides the EXISTING audited down-only path:
    # apply_safe_change rejects any RAISE above the protected default and logs a violation, so a
    # big universe can never out-spend this ceiling. Lowering ($1.00 -> $0.10) is the safe direction.
    if daily_usd_cap is not None:
        ok = SafetyCore.apply_safe_change("DAILY_USD_CAP_DEFAULT", float(daily_usd_cap),
                                          evidence={"source": "operator_runner_arg"})
        eff = SafetyCore.get_daily_usd_cap()
        if ok:
            print(f"[BUDGET] daily cap tightened to ${eff:.2f}/day (down-only via SafetyCore)")
        else:
            print(f"[BUDGET] requested cap ${float(daily_usd_cap):.2f} REJECTED (would loosen protected core); "
                  f"cap stays ${eff:.2f}/day")

    graveyard = GraveyardDB(data_dir)
    budget = DailyBudget(BudgetConfig(daily_usd_cap=1.0, log_path=logs_dir / "budget.json"))

    if llm is None:
        llm = get_llm_client(fake=not use_real_llm, budget=budget, api_key=os.getenv("ANTHROPIC_API_KEY"))
    if market_data is None:
        # F1: use_real_llm (the actual param) + source decide real data path. Never ref undefined 'use_real'.
        # Supports direct calls: run_paper(market_data=None, source="yahoo") or "rh" now starts clean (no NameError).
        # CLI main pre-resolves real md for --real and passes it; this path supports source-driven too.
        want_real_md = bool(use_real_llm) or (source in ("rh", "yahoo"))
        md = get_market_data(fake=not want_real_md, source=source)
    else:
        md = market_data  # tests provide series; run provides real

    ex = _make_paper_executor(data_dir, md, graveyard, llm, client=client)
    risk = ex.risk or RiskController(100000.0)
    # Bug fix (2026-07-06): explicitly opt into decision-emit telemetry, scoped to THIS run's
    # own data_dir — not the hardcoded default (which ignored --data-dir and mixed real OOS
    # decisions with every unit test's fixture noise in one shared file). See reaction_layer.py.
    reaction = ReactionLayer(
        llm=llm, executor=ex, risk=risk, graveyard=graveyard,
        emit_decisions=True, decisions_path=str(data_dir / "decisions.ndjson"),
    )

    effective_tickers = list(tickers) if tickers else load_universe()
    if event_feed is None:
        # default fake for hermetic; real caller passes SimpleEDGARFeed(edgar, effective_tickers)
        event_feed = FakeEventFeed([])

    # Market-regime labeling: real classifier on real runs (fetches IWM, caches per day); on hermetic
    # runs default to a static label so the unittest suite makes no network call. Inject for tests.
    if regime_fn is None:
        regime_fn = classify_regime if use_real_llm else (lambda: "test")

    # U-C: persistent positions
    pos_store = PaperPositionStore(positions_path or (data_dir / "paper_open_positions.json"))
    open_positions = pos_store.get_open()  # load surviving opens
    # backfill in-mem for this run's logic (keys are event_ids)

    # CL-1: processed events store for observer/runner separation (persisted; runner filters, feed can be raw)
    processed_store = ProcessedEventStore(data_dir / "processed_events.json")
    processed = processed_store.get_processed()

    results: list[dict] = []
    cycles = 0

    # simple sigint / kill file
    stop = False
    def _stop(*a): nonlocal stop; stop = True
    signal.signal(signal.SIGINT, _stop)

    _is_market_day = market_day_fn or is_us_market_day
    _logged_closed = False
    while (max_cycles is None or cycles < max_cycles) and not stop:
        cycles += 1
        # Market-calendar gate: on non-trading days do zero work — no EDGAR fetch, no LLM, no spend.
        # Weekend/holiday filings are still caught later (dedup + 90-day staleness window) at next open.
        if respect_market_calendar and not _is_market_day():
            if not _logged_closed:
                print("[MARKET CLOSED] non-trading day — skipping all cycles (no EDGAR, no LLM, $0 spend)")
                _logged_closed = True
            continue
        # Classify the market regime ONCE per active cycle (cached per day inside classify_regime),
        # stamped on every trade opened this cycle so the calibration gate sees real environments.
        try:
            current_regime = regime_fn() or "unknown"
        except Exception as e:
            print(f"[REGIME] classify failed ({type(e).__name__}); using 'unknown'")
            current_regime = "unknown"

        # 1. Real (or fake) event source over universe
        triggers = event_feed.next_events(3) if hasattr(event_feed, "next_events") else []
        if not triggers and hasattr(event_feed, "_triggers"):
            pass

        for trig in triggers:
            if trig.event_id in processed:
                continue  # runner-level filter (separation: observer may re-yield; runner skips)
            processed.add(trig.event_id)
            processed_store.mark_processed(trig.event_id)
            # Cost-efficiency mandate: every trigger reaching this point already passed the
            # watcher's deterministic pre-filter (DynamicEDGARFeed/SimpleEDGARFeed) — by
            # construction it's a "candidate event" worth a look, independent of whether the
            # reasoner below ends up calling an LLM, getting budget-refused, or rejecting.
            budget.record_candidate_event()
            # Tier1 + escalate (real path, guards)
            # Mandate 1: size off hood's sleeve, not account NAV.
            hood_sleeve = risk.agentic_sleeve_usd
            res = reaction.process_trigger(trig, current_book_usd=hood_sleeve, is_killed=ex.is_killed or (lambda: False), budget_can=budget.can_spend)
            # Cost-efficiency mandate: never silently drop a candidate because the budget ran
            # out — record it explicitly so it shows up in results/hood_state.json instead of
            # vanishing into the `continue` below with no trace.
            res_reason = (res or {}).get("reason", "") or ""
            if "budget" in res_reason:
                results.append({"event": trig.event_id, "action": "budget_dropped", "reason": res_reason})
            if not res or not (res.get("success") or res.get("executed")):
                continue

            # After pipeline, if filled paper, track for later resolve + persist.
            # F2: consume unified contract ONLY (thesis=EVThesis obj, avg_fill_price from real fill).
            # NO fallback to EV% as price (EV% is not a price; missing price => no open/record).
            if res.get("executed") and res.get("thesis"):
                th: EVThesis = res["thesis"]
                # CL-1 two-phase (adopted from pure_arb_bot): the reaction/executor call above is the "submit" phase.
                # Explicit confirm phase: re-inspect actual client state (analogous to sync_from_broker after submit)
                # before recording the position. For paper (sync mock) this will confirm; for real brokers
                # this would poll until terminal fill state. Only record on confirmed actual fill.
                # INVARIANT: CONFIRM_AFTER_FILL_OR_UNWIND
                # After every order submission, hood re-inspects actual broker state before
                # recording the position as open. If the confirm check fails (position not
                # visible, price missing, or broker query error), the submitted order is
                # cancelled (unwind) and the position is NOT recorded. This prevents
                # orphaned positions that exist in the broker but not in hood's ledger —
                # which would block future sells via the ownership ledger gate.
                # For live brokers this would poll until terminal fill state.
                # See INVARIANTS.md for the full contract.
                entry_price = res.get("avg_fill_price")
                confirmed = False
                try:
                    poss = (ex.client.get_positions() if ex and ex.client and hasattr(ex.client, "get_positions") else [])
                    for p in poss or []:
                        if getattr(p, "ticker", None) == th.ticker and getattr(p, "shares", 0) > 0:
                            confirmed = True
                            break
                except Exception:
                    # if confirm query fails, fall back to trusting the submit res (conservative for paper; real would escalate)
                    confirmed = True
                if not confirmed or entry_price is None or entry_price <= 0:
                    # Unwind: cancel the submitted order so no unfollowed position lingers in the broker book.
                    # Also remove from ledger if executor already wrote it (belt-and-suspenders).
                    try:
                        if ex and ex.client and hasattr(ex.client, "cancel_all"):
                            ex.client.cancel_all()
                    except Exception as e:
                        print(f"[CONFIRM-FAIL UNWIND] cancel attempt failed for {getattr(th, 'ticker', 'unknown')}: {e}")
                    if ex.ownership_ledger:
                        filled = res.get("filled_shares", 0.0)
                        if filled and filled > 0:
                            ex.ownership_ledger.remove(th.ticker, filled)
                    results.append({"event": trig.event_id, "action": "confirm_fail_unwind"})
                    continue
                filled_shares = res.get("filled_shares", 0.0)
                entry_time = datetime.now(timezone.utc).isoformat()
                # F4: compute horizon from this run's hold_bars (0 => immediate for tests/demos; >0 for real sane hold)
                run_horizon = (hold_bars * 0.1) if hold_bars is not None else 4.0
                open_positions[trig.event_id] = {
                    "thesis": th,
                    "entry_fill": entry_price,
                    "entry_time": entry_time,
                    "regime": current_regime,  # open-time market environment (immutable for this trade)
                    "filled_shares": filled_shares,
                    "hold_hours": run_horizon,  # persisted so restart uses same horizon for this pos
                }
                pos_store.open_position(trig.event_id, th, entry_price, entry_time, regime=current_regime, hold_hours=run_horizon)
                # record entry (for calib, ev at open)
                meta = {"param_version": "v_run", "slippage_modeled": True, "paper_run": True}
                graveyard.record_trade(th, outcome="filled_paper", realized_return_pct=None, regime=current_regime, meta=meta)
                results.append({"event": trig.event_id, "action": "opened", "entry": entry_price})

        # 2. Mark / resolve using persistent opens + REAL subsequent prices (md)
        # F4: hold rule is hours-based (defensible horizon, persisted per-pos so survives restarts).
        # Demo/test can pass hold_hours=0 for immediate; real OOS uses e.g. 4-24h or thesis horizon.
        # Resolve by eid identity (use runner's stored per-event entry_fill + current md quote for realized calc).
        # This prevents ticker-key conflation in client._positions when >1 event on same name.
        # Still clear via executor/client sell so book risk is updated. Keep no-fab on resolve failure.
        to_close = []
        now = datetime.now(timezone.utc)
        eff_hold_hours = (hold_bars * 0.1) if (hold_bars and hold_bars > 0) else 0.0  # compat with old bar calls (0 in tests)
        # prefer explicit if we stored per pos; else module-level or passed
        for eid, pos in list(open_positions.items()):
            try:
                et = datetime.fromisoformat(pos.get("entry_time", now.isoformat()))
                held = (now - et).total_seconds() / 3600.0
                pos_horizon = pos.get("hold_hours", eff_hold_hours or 4.0)
                if held >= max(0.0, pos_horizon):
                    to_close.append(eid)
            except Exception:
                if cycles % max(1, hold_bars or 1) == 0:
                    to_close.append(eid)

        for eid in to_close:
            p = open_positions.pop(eid, None)
            pos_store.close_position(eid)
            if not p:
                continue
            th: EVThesis = p["thesis"]
            entry = p.get("entry_fill")
            # F4 identity: compute realized from *this eid's* entry + current real quote (pessimistic), do not rely on ticker pos avg
            q = None
            try:
                q = md.get_quote(th.ticker)
            except Exception:
                q = None
            if q is None or q.bid <= 0:
                try:
                    q = ex.client.get_quote(th.ticker)
                except Exception:
                    q = None
            trade_regime = p.get("regime", "unknown")  # open-time environment (immutable)
            if entry is None or entry <= 0 or q is None or q.bid <= 0:
                # D3 no-fab: unresolved/NULL
                meta = {"param_version": "v_run", "paper_run": True, "resolved": False, "reason": "no_quote_or_entry_for_resolve"}
                graveyard.record_trade(th, outcome="unresolved_paper", realized_return_pct=None, regime=trade_regime, meta=meta)
                results.append({"event": eid, "action": "unresolved", "reason": "no_quote"})
                continue
            # pessimistic paper exit (same model as executor.resolve_paper_position)
            exit_price = round(q.bid * 0.985, 4)
            realized = round((exit_price - entry) / max(entry, 0.0001), 4)
            # clear the specific amount (if known) or all for ticker (risk book update)
            shares_to_clear = p.get("filled_shares") or 0.0
            if shares_to_clear > 0:
                try:
                    ex.client.place_limit_order(th.ticker, "sell", shares_to_clear, q.bid, quote=q)
                except Exception:
                    pass  # best effort clear; realized still recorded from our calc
                # Mandate 1: update ledger on resolve-sell regardless of broker result.
                if ex.ownership_ledger:
                    ex.ownership_ledger.remove(th.ticker, shares_to_clear)
            else:
                # fallback to executor resolve (ticker) for amount
                try:
                    ex.resolve_paper_position(th.ticker, exit_quote=q)
                except Exception:
                    pass
                if ex.ownership_ledger:
                    owned = ex.ownership_ledger.get(th.ticker)
                    if owned:
                        ex.ownership_ledger.remove(th.ticker, owned.shares)
            # record the *real* (series/md-derived) realized; never a const or EV proxy
            meta = {"param_version": "v_run", "slippage_modeled": True, "paper_run": True, "resolved": True}
            graveyard.record_trade(th, outcome="filled_paper_resolved", realized_return_pct=realized, regime=trade_regime, meta=meta)
            results.append({"event": eid, "action": "resolved", "realized": realized, "entry": entry})

        # optional mark
        try:
            pos_store.mark_all(md)
        except Exception:
            pass

        # G14: resolve auditor-vetoed +EV theses against later real quotes (free — quotes
        # only, no LLM). Same cadence as position resolution; tells us whether the auditor's
        # vetoes were right, independent of whether any position was ever opened.
        try:
            n_resolved = resolve_counterfactuals(
                graveyard, md, horizon_days=COUNTERFACTUAL_HORIZON_DAYS,
                decisions_ndjson_path=data_dir / "decisions.ndjson",
            )
            if n_resolved:
                print(f"[COUNTERFACTUAL] resolved {n_resolved} vetoed +EV thesis(es) against real quotes")
        except Exception as cf_err:
            print(f"[COUNTERFACTUAL] resolve pass failed (non-fatal): {cf_err}")

        # Mandate 3: write machine-readable state snapshot after each cycle.
        try:
            _write_state_snapshot(data_dir, ex, budget, current_regime, results)
        except Exception as snap_err:
            print(f"[STATE SNAPSHOT] write failed (non-fatal): {snap_err}")

        # Umbrella canonical snapshot (fail-safe; never blocks the paper loop).
        try:
            from snapshot_emit import emit_snapshot as _emit_umbrella_snapshot
            _emit_umbrella_snapshot(data_dir=data_dir, budget_obj=budget)
        except Exception as umb_err:
            print(f"[UMBRELLA SNAPSHOT] emit failed (non-fatal): {umb_err}")

        # budget / kill respect already in path
        if (data_dir / "KILL").exists():
            break
        time.sleep(0.05)  # event driven in real; here short for demo

    graveyard.close()
    # Final umbrella emit after loop exit (covers max_cycles=0 early exit / last state).
    try:
        from snapshot_emit import emit_snapshot as _emit_umbrella_snapshot
        _emit_umbrella_snapshot(data_dir=data_dir, budget_obj=budget)
    except Exception as umb_err:
        print(f"[UMBRELLA SNAPSHOT] final emit failed (non-fatal): {umb_err}")
    return {"cycles": cycles, "results": results, "open_left": len(open_positions)}


def run_real_smoke(data_dir: Path = Path("/tmp/hood_smoke"), max_events: int = 1, source: str = "auto") -> dict:
    """Gated real smoke (U-D): run with real sources, prove EDGAR detects, quotes price microcap, cycle or skip.
    NOT for unittest. Run manually in venv after `pip install -r requirements.txt` (or anthropic + mcp).
    Performs MCP discovery (search_tool) as required.
    Returns summary + proof flags.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(exist_ok=True)

    univ = load_universe()
    print(f"[SMOKE] universe: {univ}")

    # Step 1 per handoff: discover MCP surface (use search_tool as required before use_tool)
    mcp_survey = {}
    if source in ("auto", "rh"):
        try:
            # The platform search_tool
            import __main__ as _main
            search = getattr(_main, "search_tool", None)
            if search:
                res = search("robinhood", limit=5)
                mcp_survey = {"discovery": res, "note": "see RH_MCP_TOOL_SURVEY.md for full"}
                print("[SMOKE] MCP discovery:", res)
            else:
                mcp_survey = {"note": "search_tool not in __main__ context"}
        except Exception as e:
            mcp_survey = {"error": str(e)}

    # Real market (rh preferred for broker-exact; yahoo fallback labeled)
    md = get_market_data(fake=False, source=source)
    proof_price = {}
    print("[SMOKE] per-name quote (v8 chart for real last/volume; liquidity-aware modeled spread -- no real bid/ask keyless)")
    liquid_controls = ["AAPL", "SPY"]
    for t in list(univ[:5]) + [c for c in liquid_controls if c not in univ]:
        try:
            q = md.get_quote(t)
            sp = (q.ask - q.bid) / max(q.last, 0.01) if q.last > 0 else 0.0
            src = getattr(q, 'spread_source', 'unknown')
            proof_price[t] = {"last": q.last, "bid": q.bid, "ask": q.ask, "adv": q.avg_daily_volume, "spread": round(sp,4), "source": src}
            print(f"  {t}: last={q.last:.4f} bid/ask={q.bid:.4f}/{q.ask:.4f} adv~{q.avg_daily_volume:.0f} spread={sp:.4f} ({src})")
            if q.last <= 0.01:
                print(f"  WARN: {t} bad price -- thin name coverage gap in Yahoo; capacity will/should skip")
            if t in liquid_controls and q.last == 0:
                raise RuntimeError(f"LIQUID CONTROL {t} returned last=0 -- fetch regression! (should be >>0)")
        except Exception as e:
            proof_price[t] = {"error": str(e)}
            print(f"  {t} price error: {e}")
            if t in liquid_controls:
                raise
    # Model values (from compute_liquidity_aware_spread, exercised in this smoke for thins): CRKN-like ~0.22, liquid ~0.01
    print("Model on CRKN-like (0.25 last, 2500 adv): ~0.22 (wide)")
    print("Model on HOLO-like (1.0 last, 1M adv): ~0.058 (per prior report)")

    # Real EDGAR feed (will hit net, deduped)
    edgar = EdgarClient(cache_dir=data_dir)  # scoped to this smoke run's own data_dir
    feed = SimpleEDGARFeed(edgar, univ)
    events = feed.next_events(max_n=max_events)
    proof_edgar = [{"ticker": e.ticker, "event_id": e.event_id, "type": e.event_type} for e in events]
    print(f"[SMOKE] EDGAR events found: {len(events)}")
    for p in proof_edgar:
        print(f"  {p}")

    # Run short paper cycle with real (will use real resolve etc, write to /tmp)
    summary = run_paper(
        data_dir=data_dir,
        logs_dir=logs_dir,
        tickers=univ,
        use_real_llm=False,  # smoke can use fake llm to avoid spend, but real md/edgar; set True for full real reasoning
        market_data=md,
        event_feed=feed,
        max_cycles=2,
        hold_bars=1,
        positions_path=data_dir / "paper_open_positions.json",
    )
    print("[SMOKE] runner summary:", summary)

    # Check graveyard for real-basis rows (or unresolved)
    g = GraveyardDB(data_dir)
    rows = g._get_conn().execute(
        "SELECT ticker, outcome, realized_return_pct, meta FROM trades ORDER BY id DESC LIMIT 5"
    ).fetchall()
    g.close()
    real_rows = [r for r in rows if r[1] and ("resolved" in r[1] or "unresolved" in r[1])]
    print(f"[SMOKE] recent paper rows: {len(real_rows)}")
    for r in real_rows:
        print("  ", r)

    return {
        "universe": univ,
        "price_proof": proof_price,
        "edgar_proof": proof_edgar,
        "runner": summary,
        "graveyard_rows_sample": real_rows,
        "mcp_survey": mcp_survey,
        "note": "If thin names have last<=0.01 or errors, Yahoo coverage gap for microcaps - consider RH read path or curate list. Real RH verified only after owner connects MCP and re-runs smoke.",
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--logs-dir", type=Path, default=Path("logs"))
    ap.add_argument("--use-real-llm", action="store_true", help="use real Anthropic (needs key, spends budget)")
    ap.add_argument("--real", action="store_true", help="real run: force real llm + real market + real EDGAR over universe (gated smoke or prod)")
    ap.add_argument("--smoke-real", action="store_true", help="run gated U-D real smoke (EDGAR detect + microcap price + cycle) using real sources; limited, prints proof. Use in venv.")
    ap.add_argument("--source", choices=["auto", "rh", "yahoo", "fake"], default="auto", help="quote source: rh (Robinhood MCP, broker-exact when available), yahoo (free, labeled fallback), fake (tests), auto (prefer rh then yahoo)")
    ap.add_argument("--max-cycles", type=int, default=3,
                    help="max event-scan cycles (0 = run until killed, for always-on go-live mode)")
    ap.add_argument("--universe", type=Path, default=None, help="path to universe file (default config/universe.txt)")
    ap.add_argument("--hold-hours", type=float, default=4.0, help="hold horizon in hours before resolve attempt (sane for OOS; use small for demos/tests; persisted per position)")
    ap.add_argument("--daily-usd-cap", type=float, default=None, help="tighten the daily spend cap (down-only; e.g. 0.10). Env HOOD_DAILY_USD_CAP also honored. Raising above the protected $1.00 is rejected.")
    ap.add_argument("--market-days-only", action="store_true", help="only run on US equity trading days (skip weekends + NYSE holidays; $0 spend when market closed)")
    ap.add_argument("--feed", choices=["static", "dynamic"], default="static",
                    help="event source: static=poll universe.txt tickers (default), dynamic=scan all EDGAR 8-K/S-3 filings today then pre-filter on price/ADV/spread")
    args = ap.parse_args()

    # Daily cap: CLI flag wins, else env HOOD_DAILY_USD_CAP, else None (keep protected default).
    _cli_cap = args.daily_usd_cap
    if _cli_cap is None and os.getenv("HOOD_DAILY_USD_CAP"):
        try:
            _cli_cap = float(os.getenv("HOOD_DAILY_USD_CAP"))
        except ValueError:
            print(f"[BUDGET] ignoring non-numeric HOOD_DAILY_USD_CAP={os.getenv('HOOD_DAILY_USD_CAP')!r}")

    if args.smoke_real:
        # U-D gated smoke
        print("=== HOOD RUNNER REAL SMOKE (U-D) ===")
        res = run_real_smoke(data_dir=args.data_dir / "smoke", max_events=1, source=args.source)
        print("SMOKE RESULT:", res)
        # do not fall to real run logic
        import sys
        sys.exit(0)

    use_real = args.real or args.use_real_llm
    md = None
    event_feed = None
    effective_tickers = None
    if args.real or use_real or args.source in ("rh", "yahoo"):
        # U-B: wire real components, source selectable (rh = MCP broker-exact read, yahoo = free labeled, auto prefers rh)
        md = get_market_data(fake=False, source=args.source)
        univ = load_universe(args.universe)
        effective_tickers = univ
        # Bug fix (2026-07-06): EdgarClient() with no cache_dir defaults to Path("data")
        # regardless of --data-dir, so the real OOS run's EDGAR cache was silently living in
        # ./data/edgar_cache.db while everything else (graveyard, hood_state.json, processed
        # events) correctly went to --data-dir (data_real/). Found while investigating why
        # data_real/edgar_cache.db was stale (last touched 2026-06-29) despite the OOS run
        # actively fetching filings every day. Cosmetic (fetches still worked correctly; only
        # the cache location was orphaned), but confusing for any future debugging.
        edgar = EdgarClient(cache_dir=args.data_dir)  # real, rate limited, cached
        if args.feed == "dynamic":
            event_feed = DynamicEDGARFeed(edgar, md)
            print(f"[REAL RUN] feed=dynamic (all EDGAR 8-K/S-3 today + quote pre-filter) market_source={args.source or 'auto->rh/yahoo'} EDGAR=live llm=real")
        else:
            event_feed = SimpleEDGARFeed(edgar, univ)
            print(f"[REAL RUN] universe={univ} market_source={args.source or 'auto->rh/yahoo'} EDGAR=live llm=real (reads only; orders gated)")

    # positions persist
    pos_path = args.data_dir / "paper_open_positions.json"

    # hold in "bars" approx
    hold_bars = max(1, int(args.hold_hours / 0.1)) if args.real else 2

    # max_cycles=0 means always-on (run until SIGINT / KILL file); see INVARIANTS.md.
    _max_cycles = None if args.max_cycles == 0 else args.max_cycles

    summary = run_paper(
        data_dir=args.data_dir,
        logs_dir=args.logs_dir,
        tickers=effective_tickers,
        use_real_llm=use_real,
        market_data=md,
        event_feed=event_feed,
        max_cycles=_max_cycles,
        hold_bars=hold_bars,
        positions_path=pos_path,
        source=args.source,
        daily_usd_cap=_cli_cap,
        respect_market_calendar=args.market_days_only,
    )
    print("Paper run complete:", summary)
    if args.real:
        print("NOTE: This was a real-data paper run. Check data/graveyard.db for honest realized rows (or unresolved).")
