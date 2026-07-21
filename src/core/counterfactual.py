"""G14: counterfactual mark-to-market of auditor-vetoed +EV theses.

The auditor exists to reject bad theses. Nobody was checking whether its vetoes were
RIGHT. This resolves vetoed +EV theses against later real quotes so the Phase-4
calibration question ("architecture > alpha?") can be answered with evidence, at
zero capital and zero extra LLM spend (quotes only, no reasoning calls).

Design constraints (see storage.py docstring — Graveyard is append-only by design):
this NEVER updates an existing row. Each resolution is a new row, linked back to the
original via meta.counterfactual_of_id, so the historical audit trail of "what the
auditor decided and why" is never mutated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .schemas import EVThesis
from .storage import GraveyardDB
from .market_data import MarketData


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _ref_price_from_decisions_ndjson(decisions_path, ticker: str, ts: str, tolerance_seconds: float = 120.0) -> Optional[float]:
    """Fallback for rows recorded before 2026-07-17 (meta had no ref_price yet): the
    real ref_price at decision time still exists in decisions.ndjson's intended.ref_price,
    keyed by ticker + nearest timestamp. record_rejection's thesis.timestamp and _emit's
    decision ts are set at slightly different points in the same request (seconds apart,
    plus 'Z' vs '+00:00' formatting), so this matches by ticker + closest timestamp within
    a tolerance window, not exact equality. Best-effort; returns None (never fabricates)
    if no candidate is within tolerance.
    """
    if decisions_path is None:
        return None
    target = _parse_ts(ts)
    if target is None:
        return None
    best_price: Optional[float] = None
    best_delta = tolerance_seconds
    try:
        import json as _json
        from pathlib import Path as _Path
        p = _Path(decisions_path)
        if not p.exists():
            return None
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            d = _json.loads(line)
            if d.get("instrument") != ticker:
                continue
            cand_ts = _parse_ts(d.get("ts", ""))
            if cand_ts is None:
                continue
            delta = abs((cand_ts - target).total_seconds())
            if delta <= best_delta:
                rp = (d.get("intended") or {}).get("ref_price")
                if rp and rp > 0:
                    best_price = float(rp)
                    best_delta = delta
    except Exception as e:
        print(f"[counterfactual] WARN decisions.ndjson fallback lookup failed for {ticker}@{ts}: {type(e).__name__}: {e}")
        return None
    return best_price


def resolve_counterfactuals(
    graveyard: GraveyardDB,
    market: MarketData,
    horizon_days: int = 5,
    now: Optional[datetime] = None,
    decisions_ndjson_path=None,
) -> int:
    """Resolve any auditor-vetoed, positive-EV thesis whose horizon has passed.

    Returns the number of newly-resolved rows. Idempotent: a thesis_id that already
    has a counterfactual_resolved row is skipped. Failures are logged per-row and
    never silent (this touches money-adjacent audit data — no bare except: continue).

    decisions_ndjson_path: optional fallback source for ref_price on rows recorded
    before 2026-07-17 (when meta.ref_price did not yet exist).
    """
    now = now or datetime.now(timezone.utc)
    conn = graveyard._get_conn()

    already_resolved = {
        row["thesis_id"]
        for row in conn.execute(
            "SELECT thesis_id FROM trades WHERE outcome = 'counterfactual_resolved'"
        ).fetchall()
    }

    candidates = conn.execute(
        """
        SELECT id, ticker, ev_pct, timestamp, thesis_id, raw_thesis, meta, regime
        FROM trades
        WHERE outcome = 'rejected' AND ev_pct > 0
        ORDER BY timestamp
        """
    ).fetchall()

    resolved_count = 0
    for row in candidates:
        thesis_id = row["thesis_id"]
        if thesis_id in already_resolved:
            continue

        decided_at = _parse_ts(row["timestamp"])
        if decided_at is None:
            print(f"[counterfactual] WARN skipping row {row['id']} ({row['ticker']}): unparseable timestamp {row['timestamp']!r}")
            continue
        if (now - decided_at).days < horizon_days:
            continue

        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except json.JSONDecodeError:
            print(f"[counterfactual] WARN skipping row {row['id']} ({row['ticker']}): unparseable meta JSON")
            continue

        ref_price = meta.get("ref_price")
        if not ref_price or ref_price <= 0:
            ref_price = _ref_price_from_decisions_ndjson(decisions_ndjson_path, row["ticker"], row["timestamp"])
        if not ref_price or ref_price <= 0:
            print(f"[counterfactual] WARN skipping row {row['id']} ({row['ticker']}): no ref_price in meta or decisions.ndjson fallback")
            continue

        try:
            quote = market.get_quote(row["ticker"])
        except Exception as e:
            print(f"[counterfactual] WARN quote fetch failed for {row['ticker']} (row {row['id']}): {type(e).__name__}: {e}")
            continue

        # Mirror run_paper.py's real paper-resolve discipline exactly: pessimistic
        # sell at bid with extra slip (never optimistic — paper must be harder than live).
        exit_price = round(quote.bid * 0.985, 4)
        realized_return_pct = round((exit_price - ref_price) / max(ref_price, 0.0001) * 100, 4)

        try:
            thesis = EVThesis.from_dict(json.loads(row["raw_thesis"]))
        except Exception as e:
            print(f"[counterfactual] WARN could not reload thesis for row {row['id']} ({row['ticker']}): {type(e).__name__}: {e}")
            continue

        graveyard.record_trade(
            thesis=thesis,
            outcome="counterfactual_resolved",
            realized_return_pct=realized_return_pct,
            regime=row["regime"] or "unknown",
            meta={
                "counterfactual_of_id": row["id"],
                "entry_ref_price": ref_price,
                "exit_price": exit_price,
                "resolved_ts": now.isoformat(),
                "horizon_days": horizon_days,
                "original_ev_pct": row["ev_pct"],
                "auditor_was_right": realized_return_pct <= 0,
            },
        )
        resolved_count += 1

    return resolved_count
