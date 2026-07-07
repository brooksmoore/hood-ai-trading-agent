"""Canonical decision-event builder + fail-safe emitter for hood (paper + replay)."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .schemas import EVThesis

logger = logging.getLogger(__name__)

DEFAULT_HORIZON_DAYS = 20
REPLAY_EXPERIMENT_ID = "hood-replay-2026H1"
SCREEN_SAMPLE_RATE = 0.05

HOOD_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DECISIONS_PATH = HOOD_ROOT / "data" / "decisions.ndjson"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=HOOD_ROOT,
        ).strip()
    except Exception:
        return "unknown"


def _config_hash() -> str:
    env = os.environ.get("HOOD_CONFIG_HASH")
    if env:
        return env[:12]
    cfg = HOOD_ROOT / "config.py"
    if cfg.exists():
        return hashlib.sha256(cfg.read_bytes()).hexdigest()[:12]
    return hashlib.sha256(b"hood-default").hexdigest()[:12]


def _prompt_hash() -> str:
    return hashlib.sha256(b"ev-opus+auditor-opus").hexdigest()[:12]


def experiment_id_for_mode(mode: str) -> str | None:
    if mode == "replay":
        return REPLAY_EXPERIMENT_ID
    return os.environ.get("HOOD_EXPERIMENT_ID") or None


def prediction_from_thesis(thesis: EVThesis, *, horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict[str, Any]:
    return {
        "type": "ev",
        "value": float(thesis.expected_value_pct),
        "p_up": float(thesis.p_upside),
        "horizon_days": horizon_days,
    }


def ts_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ts_from_filing(filing_date: str) -> str:
    """ISO timestamp at filing date (no intraday — EDGAR file_date has no time)."""
    try:
        d = datetime.strptime(filing_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return d.isoformat().replace("+00:00", "Z")
    except Exception:
        return ts_now()


def fetch_benchmarks(quote_fn: Callable[[str], Any]) -> dict[str, float]:
    """SPY/IWM last from the live quote source (not independent resolver prices)."""
    out: dict[str, float] = {}
    for sym in ("SPY", "IWM"):
        try:
            q = quote_fn(sym)
            last = getattr(q, "last", None) if q is not None else None
            if last and float(last) > 0:
                out[sym] = round(float(last), 4)
        except Exception:
            continue
    return out


def should_sample_screen_reject(event_id: str) -> bool:
    """Deterministic ~5% sample for tier-1 screen-outs (A2)."""
    h = int(hashlib.sha256(event_id.encode()).hexdigest()[:8], 16)
    return (h % 10_000) < int(SCREEN_SAMPLE_RATE * 10_000)


def build_decision_record(
    *,
    kind: str,
    instrument: str,
    reason: str,
    regime: str,
    lineage: dict[str, Any],
    mode: str,
    ts: str | None = None,
    experiment_id: str | None = None,
    thesis: EVThesis | None = None,
    ref_price: float | None = None,
    actual: dict[str, Any] | None = None,
    benchmarks: dict[str, float] | None = None,
    prediction: dict[str, Any] | None = None,
    intended: dict[str, Any] | None = None,
    llm_calls: int = 0,
    llm_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Build a dict that passes umbrella_core.decisions.validate_decision."""
    ts_val = ts or ts_now()
    pred = prediction
    if pred is None and thesis is not None:
        pred = prediction_from_thesis(thesis)
    if pred is None:
        pred = {"type": "none"}

    if intended is None and ref_price is not None and kind in ("entry", "reject", "veto", "hold"):
        intended = {"side": "buy", "qty": 0, "ref_price": float(ref_price)}

    reason = (reason or "")[:280]
    lineage_out = {**lineage, "llm_calls": llm_calls, "llm_cost_usd": round(llm_cost_usd, 6)}
    decision_id = f"hood:{ts_val}:{instrument}:{kind}"

    return {
        "schema_version": "1.0",
        "decision_id": decision_id,
        "bot_id": "hood",
        "ts": ts_val,
        "kind": kind,
        "instrument": instrument,
        "intended": intended,
        "actual": actual,
        "prediction": pred,
        "reason": reason,
        "benchmarks_at_decision": benchmarks or {},
        "regime": regime,
        "lineage": lineage_out,
        "provenance": {
            "git_sha": _git_sha(),
            "config_hash": _config_hash(),
            "prompt_hash": _prompt_hash(),
        },
        "mode": mode,
        "experiment_id": experiment_id if experiment_id is not None else experiment_id_for_mode(mode),
    }


def emit_decision_safe(
    path: str | Path,
    record: dict[str, Any],
    *,
    append_fn: Optional[Callable[[str | Path, dict[str, Any]], None]] = None,
) -> bool:
    """Append one decision; never raise — trading path must continue on failure."""
    try:
        if append_fn is None:
            import sys

            umbrella_root = HOOD_ROOT.parent / "umbrella"
            if str(umbrella_root) not in sys.path:
                sys.path.insert(0, str(umbrella_root))
            from umbrella_core.decisions import append_decision_atomic

            append_fn = append_decision_atomic
        append_fn(path, record)
        return True
    except Exception as exc:
        logger.warning("decision emit failed (non-fatal): %s", exc)
        return False