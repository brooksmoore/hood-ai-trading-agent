"""Append-only persistent log + Graveyard DB (first-class per spec Section 9).

Every rejected/failed/won trade goes here for Meta-Reviewer mining of failure patterns.
Uses SQLite for queryability, zero external deps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .schemas import EVThesis, TradeDecision


DB_FILENAME = "graveyard.db"
LOG_FILENAME = "decision_log.jsonl"


@dataclass
class LogEntry:
    """Structured log entry for every decision point (EV estimate, sizing, regime, outcome, etc)."""

    timestamp: str
    ticker: str
    event_type: str
    ev_pct: float
    sized_usd: Optional[float]
    regime: str  # e.g. "pre_market", "earnings", "reaction"
    capacity_usd: float
    event_risk_flags: list[str]
    outcome: Optional[str] = None  # "filled", "rejected", "win", "loss", "halted", etc.
    realized_return_pct: Optional[float] = None
    reject_reason: Optional[str] = None
    thesis_id: Optional[str] = None
    raw_thesis: Optional[dict[str, Any]] = None
    meta: dict[str, Any] = None  # extra context, model, etc.


class PersistentLog:
    """Append-only JSONL log. Simple, human-readable, durable."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / LOG_FILENAME
        self._lock = threading.Lock()

    def append(self, entry: LogEntry) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry), default=str) + "\n")

    def tail(self, n: int = 50) -> list[LogEntry]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()[-n:]
        out: list[LogEntry] = []
        for ln in lines:
            if ln.strip():
                d = json.loads(ln)
                out.append(LogEntry(**d))
        return out


class GraveyardDB:
    """First-class Graveyard: queryable store of every rejected, failed, and successful trade.

    Schema designed for Meta-Reviewer to mine "what stories fool us?"
    Per spec: append-only by design (insert via record_* only). No UPDATE/DELETE in Phase 0;
    a DB trigger could enforce but comment suffices to prevent accidental mutation.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / DB_FILENAME
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                event_type TEXT NOT NULL,
                ev_pct REAL NOT NULL,
                sized_usd REAL,
                regime TEXT,
                capacity_usd REAL,
                event_risk_flags TEXT,  -- JSON array
                outcome TEXT,
                realized_return_pct REAL,
                reject_reason TEXT,
                thesis_id TEXT,
                raw_thesis TEXT,  -- full JSON
                auditor_passed INTEGER,
                risk_score REAL,
                meta TEXT,  -- JSON
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(outcome)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_ev ON trades(ev_pct)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS graveyard_patterns (
                id INTEGER PRIMARY KEY,
                pattern TEXT NOT NULL UNIQUE,
                first_seen TEXT,
                count INTEGER DEFAULT 1,
                notes TEXT
            )
            """
        )
        conn.commit()

    def record_trade(
        self,
        thesis: EVThesis,
        decision: Optional[TradeDecision] = None,
        outcome: Optional[str] = None,
        realized_return_pct: Optional[float] = None,
        regime: str = "unknown",
        meta: Optional[dict[str, Any]] = None,
    ) -> int:
        """Insert a trade record. Returns row id."""
        conn = self._get_conn()
        ts = thesis.timestamp or datetime.now(timezone.utc).isoformat()
        flags_json = json.dumps(thesis.event_risk_flags)
        raw_json = thesis.to_json()
        sized = decision.sized_usd if decision else None
        reject = decision.reject_reason if decision else None
        auditor = 1 if (decision and decision.approved) else 0
        risk = decision.risk_score if decision else None

        with self._lock:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    timestamp, ticker, event_type, ev_pct, sized_usd, regime,
                    capacity_usd, event_risk_flags, outcome, realized_return_pct,
                    reject_reason, thesis_id, raw_thesis, auditor_passed, risk_score, meta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    thesis.ticker,
                    thesis.event_type,
                    thesis.expected_value_pct,
                    sized,
                    regime,
                    thesis.tradeable_capacity_usd,
                    flags_json,
                    outcome,
                    realized_return_pct,
                    reject,
                    thesis.thesis_id,
                    raw_json,
                    auditor,
                    risk,
                    json.dumps(meta or {}),
                ),
            )
            conn.commit()
            return cur.lastrowid

    def record_rejection(
        self, thesis: EVThesis, reason: str, regime: str = "unknown", meta: Optional[dict] = None
    ) -> int:
        """Convenience for Auditor/Risk vetoes."""
        dec = TradeDecision(thesis=thesis, approved=False, reject_reason=reason)
        return self.record_trade(
            thesis=thesis, decision=dec, outcome="rejected", regime=regime, meta=meta
        )

    def query_failures(
        self, min_ev: float = 5.0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Example query for Meta: high-EV that still failed."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE ev_pct >= ? AND (outcome LIKE 'loss%' OR outcome='halted' OR realized_return_pct < 0)
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (min_ev, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_fool_patterns(self, limit: int = 50) -> list[dict[str, Any]]:
        """For Meta-Reviewer: recurring reject/failure reasons."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT reject_reason, COUNT(*) as cnt, AVG(ev_pct) as avg_ev
            FROM trades
            WHERE reject_reason IS NOT NULL OR outcome IN ('loss', 'halted')
            GROUP BY reject_reason
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# Helper to bootstrap a log entry from thesis + decision
def make_log_entry(
    thesis: EVThesis,
    decision: Optional[TradeDecision] = None,
    outcome: Optional[str] = None,
    realized: Optional[float] = None,
    regime: str = "unknown",
    meta: Optional[dict] = None,
) -> LogEntry:
    return LogEntry(
        timestamp=thesis.timestamp or datetime.now(timezone.utc).isoformat(),
        ticker=thesis.ticker,
        event_type=thesis.event_type,
        ev_pct=thesis.expected_value_pct,
        sized_usd=decision.sized_usd if decision else None,
        regime=regime,
        capacity_usd=thesis.tradeable_capacity_usd,
        event_risk_flags=thesis.event_risk_flags,
        outcome=outcome,
        realized_return_pct=realized,
        reject_reason=decision.reject_reason if decision else None,
        thesis_id=thesis.thesis_id,
        raw_thesis=json.loads(thesis.to_json()),
        meta=meta or {},
    )


if __name__ == "__main__":
    # smoke test
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        g = GraveyardDB(d)
        log = PersistentLog(d)
        t = EVThesis(
            ticker="TEST",
            event_type="8k",
            upside_pct=15,
            p_upside=0.5,
            downside_pct=-25,
            p_downside=0.25,
            expected_value_pct=3.75,
            prior_accuracy_on_name=0.6,
            what_informed_holders_may_know_that_we_dont="Lots of informed holders here.",
            tradeable_capacity_usd=10000,
            event_risk_flags=["thin_float"],
        )
        g.record_rejection(t, "deterministic_screen: thin_float + low_adv")
        print("Graveyard row count:", g._get_conn().execute("SELECT COUNT(*) FROM trades").fetchone()[0])
        print("Failures sample:", len(g.query_failures()))
        g.close()
        print("Graveyard DB test passed.")
