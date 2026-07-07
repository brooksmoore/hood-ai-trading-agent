"""Hood ownership ledger — the exclusive record of positions hood itself opened.

MANDATE: Hood must never assume it owns the whole account. This ledger is the sole
authority for what hood can sell. Broker get_positions() reflects the whole shared
account; only this ledger reflects what hood specifically opened.

Fail-closed by design: if the ledger file is missing or corrupt, all_owned() returns
[] and sells are vetoed. A missing ledger is treated the same as "hood owns nothing."
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


LEDGER_FILENAME = "hood_ownership_ledger.json"


@dataclass
class LedgerEntry:
    ticker: str
    shares: float
    avg_cost: float
    event_id: str
    entry_time: str


class OwnershipLedger:
    """Persisted record of every position hood has opened on the shared account.

    Backed by a JSON file. All writes are atomic (write-to-temp then rename).
    Thread-safe. Fail-closed: corrupt/missing file → empty ledger → no sells.
    """

    def __init__(self, data_dir: Path):
        self._path = data_dir / LEDGER_FILENAME
        self._lock = threading.Lock()
        self._entries: dict[str, LedgerEntry] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                for rec in raw if isinstance(raw, list) else []:
                    e = LedgerEntry(**rec)
                    if e.shares > 1e-6:
                        self._entries[e.ticker] = e
        except Exception:
            # Corrupt / unreadable → fail closed (empty ledger)
            self._entries = {}

    def _save(self) -> None:
        try:
            payload = [asdict(e) for e in self._entries.values()]
            tmp = self._path.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as exc:
            print(f"[LEDGER] save failed (continuing; positions may not persist): {exc}")

    # ── public API ────────────────────────────────────────────────────────────

    def add(self, ticker: str, shares: float, avg_cost: float,
            event_id: str, entry_time: Optional[str] = None) -> None:
        """Record a buy fill into the ledger."""
        if shares <= 0 or avg_cost <= 0:
            return
        ts = entry_time or datetime.now(timezone.utc).isoformat()
        with self._lock:
            if ticker in self._entries:
                existing = self._entries[ticker]
                total_shares = existing.shares + shares
                blended_cost = (existing.avg_cost * existing.shares + avg_cost * shares) / total_shares
                self._entries[ticker] = LedgerEntry(ticker, total_shares, round(blended_cost, 6), event_id, ts)
            else:
                self._entries[ticker] = LedgerEntry(ticker, shares, avg_cost, event_id, ts)
            self._save()

    def remove(self, ticker: str, shares: float) -> bool:
        """Reduce or clear a position after a sell fill. Returns False if hood doesn't own enough."""
        with self._lock:
            if ticker not in self._entries:
                return False
            entry = self._entries[ticker]
            if shares > entry.shares + 1e-6:
                return False
            remaining = entry.shares - shares
            if remaining < 1e-6:
                del self._entries[ticker]
            else:
                self._entries[ticker] = LedgerEntry(
                    ticker, round(remaining, 6), entry.avg_cost,
                    entry.event_id, entry.entry_time,
                )
            self._save()
            return True

    def get(self, ticker: str) -> Optional[LedgerEntry]:
        """Return hood's ledger entry for ticker, or None if hood doesn't own it."""
        with self._lock:
            return self._entries.get(ticker)

    def has_position(self, ticker: str) -> bool:
        with self._lock:
            e = self._entries.get(ticker)
            return e is not None and e.shares > 1e-6

    def all_owned(self) -> list[LedgerEntry]:
        """All positions hood currently owns, by ledger record."""
        with self._lock:
            return [e for e in self._entries.values() if e.shares > 1e-6]

    def hood_cost_basis_usd(self) -> float:
        """Sum of cost basis for all open hood positions (lower bound on NAV)."""
        with self._lock:
            return sum(e.shares * e.avg_cost for e in self._entries.values() if e.shares > 1e-6)
