"""Umbrella canonical snapshot for hood_agent_1 (paper). Fail-safe, schema-validated.

Maps hood's real machine state (hood_state.json + budget) onto umbrella Snapshot.
Never fabricates capital — sleeve NAV from hood_state or honest 0 + warning.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

HOOD_ROOT = Path(__file__).resolve().parent
# Canonical path the umbrella dashboard expects (not data_real — decisions stay there).
DEFAULT_STATE_JSON = HOOD_ROOT / "data" / "state.json"
DEFAULT_HOOD_STATE = HOOD_ROOT / "data_real" / "hood_state.json"
# Production OOS cap (run_oos.sh --daily-usd-cap 0.25); protected ceiling remains $1.00.
DEFAULT_LLM_BUDGET_USD = 0.25


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001
        log.warning("hood snapshot: failed to read %s: %s", path, e)
        return None


def _llm_budget_usd(budget_obj: Any = None) -> float:
    """Prefer live DailyBudget cap, then env, then the known OOS default ($0.25)."""
    if budget_obj is not None:
        cfg = getattr(budget_obj, "config", None) or getattr(budget_obj, "cfg", None)
        cap = getattr(cfg, "daily_usd_cap", None) if cfg is not None else None
        if cap is None and hasattr(budget_obj, "daily_usd_cap"):
            cap = getattr(budget_obj, "daily_usd_cap")
        if cap is not None:
            try:
                return float(cap)
            except (TypeError, ValueError):
                pass
    env = os.getenv("HOOD_DAILY_USD_CAP")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return DEFAULT_LLM_BUDGET_USD


def build_snapshot_dict(
    *,
    hood_state: dict[str, Any] | None = None,
    budget_obj: Any = None,
    cycle_at: str | None = None,
    killed: bool | None = None,
) -> dict[str, Any]:
    """Pure builder — returns a schema-shaped dict (no I/O write)."""
    from umbrella_core.emit import (
        AccountInfo,
        CapitalInfo,
        ComputeInfo,
        HealthInfo,
        IdentityInfo,
        LifecycleInfo,
        PositionInfo,
        Snapshot,
        TimingInfo,
        snapshot_to_dict,
    )

    now = cycle_at or _iso_now()
    st = hood_state or {}
    warnings: list[str] = []

    # ── capital (honest sleeve; never invent) ─────────────────────────────────
    nav = st.get("nav_sleeve")
    cash = st.get("cash_sleeve")
    positions_raw = st.get("positions") or []
    if nav is None and cash is None:
        own_nav = 0.0
        cash_f = 0.0
        invested = 0.0
        budget_alloc = 0.0
        warnings.append("screening bot — no capital book (hood_state missing sleeve fields)")
    else:
        try:
            own_nav = float(nav if nav is not None else cash or 0.0)
        except (TypeError, ValueError):
            own_nav = 0.0
            warnings.append("nav_sleeve unparseable — capital zeroed")
        try:
            cash_f = float(cash if cash is not None else own_nav)
        except (TypeError, ValueError):
            cash_f = own_nav
        invested = 0.0
        for p in positions_raw:
            try:
                invested += float(p.get("cost_basis_usd") or 0.0)
            except (TypeError, ValueError):
                pass
        budget_alloc = own_nav  # sleeve size = paper bankroll for this bot

    positions: list[Any] = []
    for p in positions_raw:
        try:
            shares = float(p.get("shares") or 0.0)
            avg = p.get("avg_cost")
            positions.append(
                PositionInfo(
                    symbol=str(p.get("ticker") or p.get("symbol") or "?"),
                    qty=shares,
                    avg_cost=float(avg) if avg is not None else None,
                    market_value=float(p.get("cost_basis_usd")) if p.get("cost_basis_usd") is not None else None,
                )
            )
        except Exception:
            continue

    # ── compute ───────────────────────────────────────────────────────────────
    metrics = st.get("llm_metrics") or {}
    llm_budget = _llm_budget_usd(budget_obj)
    spend = metrics.get("llm_spend_today_usd")
    calls = metrics.get("llm_calls_today")
    breaker = metrics.get("llm_breaker_tripped")
    remaining = st.get("budget_remaining_usd")

    if budget_obj is not None:
        try:
            spend, _frac = budget_obj.current_spend()
        except Exception:
            pass
        try:
            remaining = budget_obj.remaining_today()
        except Exception:
            pass
        try:
            calls = budget_obj.call_count()
        except Exception:
            pass
        try:
            breaker = budget_obj.breaker_tripped()
        except Exception:
            pass

    try:
        spend_f = float(spend) if spend is not None else 0.0
    except (TypeError, ValueError):
        spend_f = 0.0
        warnings.append("llm spend unknown — reported 0")
    try:
        calls_i = int(calls) if calls is not None else 0
    except (TypeError, ValueError):
        calls_i = 0
    try:
        rem_f = float(remaining) if remaining is not None else max(0.0, llm_budget - spend_f)
    except (TypeError, ValueError):
        rem_f = max(0.0, llm_budget - spend_f)
    breaker_b = bool(breaker) if breaker is not None else (spend_f >= llm_budget)

    # ── health ────────────────────────────────────────────────────────────────
    hs = str(st.get("health") or "ok")
    if killed is None:
        killed = hs == "killed" or (HOOD_ROOT / "data_real" / "KILL").exists() or (
            HOOD_ROOT / "data" / "KILL"
        ).exists()
    if killed:
        overall = "down"
    elif hs in ("budget_exhausted",) or breaker_b:
        overall = "degraded"
        if "budget" not in " ".join(warnings).lower():
            warnings.append("LLM daily budget exhausted or breaker tripped")
    elif hs not in ("ok", "", "None"):
        overall = "degraded"
        warnings.append(f"hood_state health={hs}")
    else:
        overall = "ok"

    if not st:
        overall = "degraded"
        warnings.append("no hood_state.json — snapshot built with defaults only")

    last_cycle = st.get("timestamp") or now
    live_enabled = bool(st.get("live_enabled"))
    stage = "paper-validating"
    if live_enabled:
        stage = "live"  # should not happen with gate OFF; still report honestly

    snap = Snapshot(
        schema_version="1.0",
        identity=IdentityInfo(
            bot_id="hood",
            display_name="Hood Event Agent",
            membrane="independent",  # tenancy-aware but own sleeve; umbrella membrane field
            account=AccountInfo(broker="robinhood-paper"),
            asset_classes=["equity"],
            strategy=(
                "Microcap event-driven: EDGAR filings → EV distributions → "
                "adversarial auditor → risk-capped paper execution"
            ),
        ),
        lifecycle=LifecycleInfo(
            stage=stage,  # type: ignore[arg-type]
            mode="live" if live_enabled else "paper",
            live_gate="armed" if live_enabled else "disarmed",
            killed=bool(killed),
            cadence="cron",
            expected_update_interval_sec=6 * 3600,  # market-day OOS windows
        ),
        timing=TimingInfo(
            generated_at=now,
            last_cycle_at=str(last_cycle),
            last_fill_at=st.get("last_event_ts"),
        ),
        capital=CapitalInfo(
            base_currency="USD",
            own_nav=round(own_nav, 2),
            cash=round(cash_f, 2),
            invested=round(invested, 2),
            budget_allocation=round(budget_alloc, 2),
            day_pnl=None,
            total_pnl=None,
        ),
        positions=positions,
        compute=ComputeInfo(
            llm_spend_today_usd=round(spend_f, 4),
            llm_budget_usd=round(llm_budget, 4),
            budget_remaining_usd=round(rem_f, 4),
            calls_today=calls_i,
            breaker_tripped=breaker_b,
        ),
        health=HealthInfo(
            overall=overall,  # type: ignore[arg-type]
            sources={
                "edgar": "ok" if st else "n/a",
                "llm": "degraded" if breaker_b else ("ok" if st else "n/a"),
                "ledger": "ok" if st else "n/a",
            },
            warnings=warnings,
        ),
        extra={
            "regime": st.get("regime"),
            "open_theses": st.get("open_theses") or [],
            "source_hood_state": bool(st),
            "candidate_events_today": (metrics.get("candidate_events_today")
                                       if isinstance(metrics, dict) else None),
        },
    )
    return snapshot_to_dict(snap)


def emit_snapshot(
    *,
    data_dir: Path | str | None = None,
    budget_obj: Any = None,
    out_path: Path | str | None = None,
    hood_state_path: Path | str | None = None,
) -> bool:
    """Write data/state.json. Returns True on success; NEVER raises into the cycle."""
    try:
        from umbrella_core.emit import write_snapshot_atomic
        from umbrella_core.snapshot import validate_snapshot

        ddir = Path(data_dir) if data_dir else HOOD_ROOT / "data_real"
        state_src = Path(hood_state_path) if hood_state_path else (ddir / "hood_state.json")
        if not state_src.exists() and DEFAULT_HOOD_STATE.exists():
            state_src = DEFAULT_HOOD_STATE
        hood_state = _load_json(state_src) or {}

        dest = Path(out_path) if out_path else DEFAULT_STATE_JSON
        dest.parent.mkdir(parents=True, exist_ok=True)

        d = build_snapshot_dict(hood_state=hood_state, budget_obj=budget_obj)
        errs = validate_snapshot(d)
        if errs:
            log.warning("hood snapshot INVALID, not written: %s", errs[:3])
            return False
        write_snapshot_atomic(dest, d)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("hood snapshot emit failed (non-fatal): %s", e)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = emit_snapshot()
    print("emit_snapshot:", "OK" if ok else "FAILED", "->", DEFAULT_STATE_JSON)
