"""EDGAR ingestion pipeline (Phase 1, stdlib-only per spec Section 10/14).

- Primary source filings (not web summaries).
- Ticker -> CIK via (cached) company_tickers.json .
- Recent filings list (form, accession, date, urls).
- Fetch + retain *raw* text + structured (filing types present, going-concern phrases).
- Process-once cache by accession (sqlite) so never re-read.
- Schema-validate responses; fail fast on bad shape.
- Timestamped; simple rate limit.
- Feeds Auditor deterministic screens (and future EV engine).
- No LLM here.

Fixtures for tests; one manual smoke for real (gated).
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
import xml.etree.ElementTree as ET  # for basic, though most is html/text
import re

# SEC requires descriptive UA with contact info.
DEFAULT_UA = "hood_agent_1/phase-r (bcm3000@gmail.com)"

# Simple phrases (shared with auditor for consistency)
GOING_CONCERN_PHRASES = [
    "going concern",
    "substantial doubt",
    "ability to continue as a going concern",
    "recurring losses",
    "negative cash flows from operations",
]

DILUTION_FORMS = {"S-3", "S-1", "S-3/A", "S-1/A"}

CACHE_DB = "edgar_cache.db"

# Modern SEC filings are inline-XBRL HTML: heavy XML namespace declarations, <ix:header> XBRL
# metadata, and per-fact <ix:nonNumeric>/<span id="xdx_..."> tagging wrap the actual disclosure
# prose. A naive character-count truncation on the raw HTML (as both event feeds do before
# sending text to the LLM) can consume the entire budget on markup before reaching Item 1.01 —
# confirmed live 2026-07-06: 100% of real candidates that day were rejected by Haiku triage as
# "8-K header only; no substantive event content provided," because the first 8000 raw chars
# were pure XBRL/cover-page markup on every single one (verified against real cached filings,
# e.g. accession 0001213900-26-075248: raw 51,124 chars, Item 1.01 didn't appear until deep
# past the truncation cutoff). strip_html_to_text() must run BEFORE truncation, not after.
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_IX_HEADER_RE = re.compile(r"<ix:header>.*?</ix:header>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def strip_html_to_text(raw: str) -> str:
    """Strip HTML/inline-XBRL markup to readable prose. Stdlib-only (no external deps).
    Not a full HTML parser — good enough to get substantive disclosure text within an
    LLM's truncation budget instead of being crowded out by tag/namespace noise.
    """
    if not raw:
        return ""
    import html as _html_module
    t = _SCRIPT_STYLE_RE.sub(" ", raw)
    t = _IX_HEADER_RE.sub(" ", t)  # display:none XBRL fact block; pure metadata, not disclosure
    t = _TAG_RE.sub(" ", t)
    t = _html_module.unescape(t)
    t = _INLINE_WS_RE.sub(" ", t)
    t = _BLANK_LINES_RE.sub("\n\n", t)
    return t.strip()


@dataclass
class FilingRef:
    form: str
    accession: str
    filing_date: str
    primary_doc_url: str
    raw_text: Optional[str] = None  # filled on fetch
    fetched_at: Optional[str] = None


class EdgarClient:
    """Stdlib EDGAR client with cache, rate limit, validation."""

    def __init__(self, ua: str = DEFAULT_UA, cache_dir: Optional[Path] = None, rate_limit_sec: float = 0.12):
        self.ua = ua
        self.rate_limit_sec = rate_limit_sec
        self._last_fetch = 0.0
        self.cache_dir = cache_dir or Path("data")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_db = self.cache_dir / CACHE_DB
        self._init_cache()
        self._tickers_cache: Optional[Any] = None  # in-process cache for company_tickers.json

    def _init_cache(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.cache_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS filings (
                accession TEXT PRIMARY KEY,
                form TEXT,
                filing_date TEXT,
                primary_doc_url TEXT,
                raw_text TEXT,
                fetched_at TEXT,
                ticker TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON filings(ticker)")
        conn.commit()
        conn.close()

    def _rate_limit(self) -> None:
        now = time.time()
        wait = self.rate_limit_sec - (now - self._last_fetch)
        if wait > 0:
            time.sleep(wait)
        self._last_fetch = time.time()

    def _fetch(self, url: str, timeout: int = 30) -> bytes:
        self._rate_limit()
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _validate_response(self, data: Any, kind: str) -> None:
        """Fail fast on unexpected shape (spec discipline)."""
        if kind == "tickers" and not isinstance(data, (list, dict)):
            raise ValueError(f"Unexpected company_tickers shape: {type(data)}")
        if kind == "filings" and not isinstance(data, dict):
            raise ValueError(f"Unexpected filings shape: {type(data)}")

    def get_cik_for_ticker(self, ticker: str, tickers_json_path: Optional[Path] = None) -> Optional[str]:
        """Resolve ticker to CIK using local fixture or fetch company_tickers.json (cached)."""
        ticker = ticker.upper()
        # prefer fixture for tests
        if tickers_json_path and tickers_json_path.exists():
            data = json.loads(tickers_json_path.read_text())
            self._validate_response(data, "tickers")
            for entry in data:
                if entry.get("ticker", "").upper() == ticker:
                    return str(entry.get("cik_str", "")).zfill(10)
            return None

        # live (for smoke/manual only) — cached in-process to avoid 1 fetch/ticker/cycle
        if self._tickers_cache is None:
            url = "https://www.sec.gov/files/company_tickers.json"
            raw = self._fetch(url)
            self._tickers_cache = json.loads(raw)
            self._validate_response(self._tickers_cache, "tickers")
        data = self._tickers_cache
        # store as list of dicts
        for _, entry in data.items() if isinstance(data, dict) else enumerate(data):
            if isinstance(entry, dict) and entry.get("ticker", "").upper() == ticker:
                return str(entry.get("cik_str", "")).zfill(10)
        return None

    def get_recent_filings(self, cik: str, forms: Optional[Sequence[str]] = None, limit: int = 20) -> list[FilingRef]:
        """Get recent filings for CIK (from SEC submissions JSON)."""
        cik = str(cik).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        raw = self._fetch(url)
        data = json.loads(raw)
        self._validate_response(data, "filings")

        filings_list = []
        recent = data.get("filings", {}).get("recent", {})
        forms_list = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])

        wanted = set(forms) if forms else None
        for i, form in enumerate(forms_list):
            if wanted and form not in wanted:
                continue
            acc = accessions[i] if i < len(accessions) else ""
            fd = dates[i] if i < len(dates) else ""
            pdoc = primary_docs[i] if i < len(primary_docs) else ""
            if acc:
                # construct primary url (approx; real uses specific)
                # e.g. https://www.sec.gov/Archives/edgar/data/{cik_no0}/{acc_no0}/{pdoc}
                cik_no0 = cik.lstrip("0")
                acc_no0 = acc.replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{cik_no0}/{acc_no0}/{pdoc}"
                filings_list.append(FilingRef(form=form, accession=acc, filing_date=fd, primary_doc_url=url))
            if len(filings_list) >= limit:
                break
        return filings_list

    def get_filing_raw(self, ref: FilingRef, ticker: Optional[str] = None) -> str:
        """Fetch raw text for a filing ref; cache by accession. Returns text (html/txt)."""
        if not ref.primary_doc_url:
            return ""
        # check cache
        conn = sqlite3.connect(str(self.cache_db))
        row = conn.execute("SELECT raw_text FROM filings WHERE accession=?", (ref.accession,)).fetchone()
        if row and row[0]:
            conn.close()
            ref.raw_text = row[0]
            return row[0]

        try:
            raw = self._fetch(ref.primary_doc_url)
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            text = f"ERROR_FETCH: {e}"

        ref.raw_text = text
        ref.fetched_at = datetime.now(timezone.utc).isoformat()

        # cache
        conn.execute("""
            INSERT OR REPLACE INTO filings (accession, form, filing_date, primary_doc_url, raw_text, fetched_at, ticker)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ref.accession, ref.form, ref.filing_date, ref.primary_doc_url, text, ref.fetched_at, ticker or ""))
        conn.commit()
        conn.close()
        return text

    def extract_structured(self, text: str, form: str) -> dict[str, Any]:
        """Extract presence of key filing types (already in form) and going-concern etc. Robust per spec."""
        t = (text or "").lower()
        has_going = any(p in t for p in GOING_CONCERN_PHRASES)
        is_dilution_form = form.upper() in DILUTION_FORMS
        # simple other signals
        return {
            "form": form,
            "has_going_concern": has_going,
            "is_dilution_form": is_dilution_form,
            "text_len": len(text or ""),
        }

    def ingest_for_screens(self, ticker: str, forms: Optional[Sequence[str]] = ("10-K", "10-Q", "8-K", "S-3", "S-1"),
                           tickers_fixture: Optional[Path] = None, max_filings: int = 5) -> list[dict[str, Any]]:
        """High level: return list of structured + raw for use in run_deterministic_screens.
        Uses cache; never re-fetches known accession.
        """
        cik = self.get_cik_for_ticker(ticker, tickers_fixture)
        if not cik:
            return []
        refs = self.get_recent_filings(cik, forms=forms, limit=max_filings)
        out = []
        for ref in refs:
            txt = self.get_filing_raw(ref, ticker=ticker)
            struct = self.extract_structured(txt, ref.form)
            out.append({
                "form": ref.form,
                "accession": ref.accession,
                "filing_date": ref.filing_date,
                "url": ref.primary_doc_url,
                "raw_text": txt[:200000],  # retain (truncated for memory in phase1)
                "structured": struct,
                "fetched_at": ref.fetched_at,
            })
        return out


def build_recent_filings_list_for_auditor(ingested: list[dict[str, Any]]) -> list[str]:
    """Convert ingested to the 'recent_filings' list expected by auditor (forms + signals)."""
    out = []
    for item in ingested:
        out.append(item["form"])
        if item.get("structured", {}).get("has_going_concern"):
            out.append("going concern language in " + item["form"])
        if item.get("structured", {}).get("is_dilution_form"):
            out.append(item["form"] + " filed")
    return out


# Manual smoke (not run by unittest)
if __name__ == "__main__":
    import sys
    print("EDGAR manual smoke (will hit network; SEC rate limits apply).")
    print("Usage: python -m src.data.edgar AAPL")
    tkr = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    cli = EdgarClient()
    # use no fixture -> will fetch tickers
    print("CIK for", tkr, ":", cli.get_cik_for_ticker(tkr))
    ingested = cli.ingest_for_screens(tkr, max_filings=3)
    print("Ingested count:", len(ingested))
    for i in ingested[:1]:
        print("Sample form:", i["form"], "has_going:", i["structured"]["has_going_concern"])
        print("Raw head:", (i["raw_text"] or "")[:300])
    print("Smoke done (check cache at data/edgar_cache.db)")
