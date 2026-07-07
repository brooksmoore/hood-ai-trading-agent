"""P4-A: Deterministic EV-Calibration Gate.

Consumes Graveyard resolved paper trades (ev_pct, realized_return_pct post-slippage, regime, meta with param_version if present).

Computes structured verdict. Default FAIL. Hard guards force FAIL.

No LLM involved in pass/fail decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING
from datetime import datetime, timezone
import json

from .storage import GraveyardDB

if TYPE_CHECKING:
    from .meta_reviewer import MetaReviewer


@dataclass
class CalibrationVerdict:
    passed: bool
    reason: str
    n: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    by_bucket: list[dict[str, Any]] = field(default_factory=list)
    by_regime: list[dict[str, Any]] = field(default_factory=list)
    window: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CalibrationGate:
    """The hard gate. Pass is exceptional and must be earned with honest out-of-sample, post-slippage, multi-regime, sufficient-N evidence.
    O2 forward-only: for non-baseline explicit versions, deploy timestamp (from deploy_times or meta_reviewer) is REQUIRED to prove trades are post-deploy (forward).
    If version specified but not baseline and no deploy_ts resolvable -> CANNOT_VERIFY_FORWARD (fail closed; does not assume).
    Baseline versions (v0 / "baseline" / 0) are treated as trivially out-of-sample (pre-any-tuning; all their trades count as forward by definition, even without deploy info).
    Time-only windows (start/end without eval_param_version) require forward proof and will CANNOT_VERIFY if none.
    See compute_verdict and handoff for exact rules.
    """

    def __init__(self, min_n: Optional[int] = None, calib_tol: Optional[float] = None, min_pos: Optional[float] = None,
                 min_per_regime: Optional[int] = None):
        self.MIN_N = min_n if min_n is not None else 20
        self.CALIB_TOLERANCE = calib_tol if calib_tol is not None else 5.0
        self.MIN_POS_EV_REALIZED = min_pos if min_pos is not None else 0.5
        # Per-regime floor: a counted regime must have at least this many resolved trades, so a
        # couple of off-regime trades can't trivially satisfy the >=2-regime requirement at low N.
        self.MIN_PER_REGIME = min_per_regime if min_per_regime is not None else 5

    def compute_verdict(
        self,
        graveyard: GraveyardDB,
        *,
        eval_param_version: Optional[str] = None,
        start_ts: Optional[str] = None,
        end_ts: Optional[str] = None,
        deploy_times: Optional[dict] = None,
        meta_reviewer: Optional["MetaReviewer"] = None,
    ) -> CalibrationVerdict:
        """Main entry. O1: PASS only for explicitly specified single-param_version window (or (ver, start, end)).
        No window arg -> NO_EVAL_WINDOW_SPECIFIED (or CROSS if data spans >1 vers).
        If resolved trades for the window span >1 param_version -> CROSS_PARAM_VERSION_DATA.
        O2: deploy timestamp (via deploy_times or meta_reviewer) REQUIRED for non-baseline versions to prove post-deploy (forward-only).
        - Baseline (eval_param_version in ("v0","0","baseline")): trivially OOS (pre-any-self-edit; all trades forward, even w/o deploy_ts). Documented assumption.
        - Non-baseline + no resolvable deploy_ts (incl. time-window w/o version): CANNOT_VERIFY_FORWARD (fail closed; refuse to assume).
        - With deploy_ts: filter ts > deploy_ts; all pre -> IN_SAMPLE_ONLY; partial forward used (may drop to INSUFFICIENT_DATA).
        Time windows should be paired with a version for deploy association.
        """
        conn = graveyard._get_conn()
        # Only resolved paper trades with realized
        query = """
            SELECT ev_pct, realized_return_pct, event_type, regime, meta, timestamp
            FROM trades
            WHERE outcome LIKE '%paper%' AND realized_return_pct IS NOT NULL
            ORDER BY timestamp
        """
        rows = conn.execute(query).fetchall()

        if not rows:
            return CalibrationVerdict(
                passed=False,
                reason="INSUFFICIENT_DATA",
                n=0,
                metrics={"error": "no resolved trades"},
            )

        # Parse all with versions
        all_parsed = []
        versions_seen = set()
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r[4]) if r[4] else {}
            except Exception:
                pass
            pv = meta.get("param_version")
            if pv:
                versions_seen.add(str(pv))
            all_parsed.append({
                "ev": r[0] or 0,
                "realized": r[1] or 0,
                "event_type": r[2],
                "regime": r[3] or "unknown",
                "meta": meta,
                "ts": r[5],
                "pv": str(pv) if pv else None,
            })

        window_specified = eval_param_version is not None or start_ts is not None or end_ts is not None

        # O1: explicit window required; refuse to pool
        if not window_specified:
            if len(versions_seen) > 1:
                return CalibrationVerdict(
                    passed=False,
                    reason="CROSS_PARAM_VERSION_DATA",
                    n=len(rows),
                    metrics={"versions": list(versions_seen)},
                )
            else:
                return CalibrationVerdict(
                    passed=False,
                    reason="NO_EVAL_WINDOW_SPECIFIED",
                    n=len(rows),
                    metrics={"note": "must specify eval_param_version (or start/end window) for verdict"},
                )

        # Build resolved set for the explicit window: first time range if any, then version
        candidates = all_parsed
        if start_ts or end_ts:
            candidates = [
                c for c in candidates
                if (not start_ts or (c["ts"] or "") >= start_ts)
                and (not end_ts or (c["ts"] or "") <= end_ts)
            ]

        if eval_param_version is not None:
            ver = str(eval_param_version)
            candidates = [c for c in candidates if c["pv"] == ver]

        # Cross check on the *resolved* set (catches time window spanning self-edits)
        vers_in_resolved = {c["pv"] for c in candidates if c["pv"]}
        if len(vers_in_resolved) > 1:
            return CalibrationVerdict(
                passed=False,
                reason="CROSS_PARAM_VERSION_DATA",
                n=len(candidates),
                metrics={"versions": list(vers_in_resolved), "note": "resolved window spans multiple param versions"},
            )

        n = len(candidates)
        if n < self.MIN_N:
            return CalibrationVerdict(
                passed=False,
                reason="INSUFFICIENT_DATA",
                n=n,
                metrics={"min_required": self.MIN_N, "version": eval_param_version, "start": start_ts, "end": end_ts},
            )

        # O2: resolve deploy_ts from arg or meta_reviewer (preferred source per spec)
        deploy_ts = None
        if deploy_times:
            # normalize keys to str and common forms
            norm = {}
            for k, v in deploy_times.items():
                ks = str(k)
                norm[ks] = v
                if ks.startswith("v"):
                    norm[ks[1:]] = v
                else:
                    norm["v" + ks] = v
            key = str(eval_param_version) if eval_param_version else ""
            deploy_ts = norm.get(key) or norm.get(key.lstrip("v")) or norm.get("v" + key.lstrip("v"))
        if not deploy_ts and meta_reviewer is not None:
            try:
                deploys = meta_reviewer.get_all_deploys() or {}
                norm = {}
                for k, v in deploys.items():
                    ks = str(k)
                    norm[ks] = v
                    norm["v" + ks.lstrip("v")] = v
                    norm[ks.lstrip("v")] = v
                key = str(eval_param_version) if eval_param_version else ""
                deploy_ts = norm.get(key) or norm.get(key.lstrip("v")) or norm.get("v" + key.lstrip("v"))
            except Exception:
                deploy_ts = None

        # forward-only (post deploy) -- O2 rem#2: split baseline (trivially OOS) vs missing deploy for non-baseline (fail closed)
        # Baseline = pre-any-self-edit tuning; all trades for it are forward by definition (even if no deploy_ts recorded).
        BASELINE = ("v0", "0", "baseline")
        is_baseline = (eval_param_version is None and not (start_ts or end_ts)) or str(eval_param_version or "").lower() in BASELINE
        if is_baseline:
            # explicitly baseline (or legacy None w/o time window): treat all candidates as forward
            forward = candidates
            in_sample_cnt = 0
        elif deploy_ts:
            forward = []
            in_sample_cnt = 0
            for f in candidates:
                ts = f["ts"] or ""
                if ts > deploy_ts:
                    forward.append(f)
                else:
                    in_sample_cnt += 1
            if not forward:
                return CalibrationVerdict(
                    passed=False,
                    reason="IN_SAMPLE_ONLY",
                    n=n,
                    metrics={
                        "in_sample_count": in_sample_cnt,
                        "deploy_ts": deploy_ts,
                        "version": eval_param_version,
                        "note": "all evaluated trades predate version.deployed_at",
                    },
                )
            candidates = forward  # now only forward for downstream checks
            n = len(candidates)
            if n < self.MIN_N:
                return CalibrationVerdict(
                    passed=False,
                    reason="INSUFFICIENT_DATA",
                    n=n,
                    metrics={
                        "min_required": self.MIN_N,
                        "after_forward_filter": True,
                        "version": eval_param_version,
                        "deploy_ts": deploy_ts,
                    },
                )
        else:
            # non-baseline version (incl. explicit ver w/ no deploy info, or time-window w/o version) -- cannot prove forward-ness
            # fail closed (symmetric to O1's NO_EVAL_WINDOW_SPECIFIED); do not assume or bless in-sample data
            return CalibrationVerdict(
                passed=False,
                reason="CANNOT_VERIFY_FORWARD",
                n=n,
                metrics={
                    "version": eval_param_version,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "note": "non-baseline version (or time window without version) with no resolvable deploy timestamp; "
                            "cannot prove trades are out-of-sample / post-deploy for this version",
                },
            )

        # Regime coverage: require >=2 distinct REAL regimes (exclude "unknown" — a data-failure
        # label must never manufacture diversity), EACH with >= MIN_PER_REGIME resolved trades
        # (so a couple of off-regime trades can't trivially satisfy coverage at low N).
        from collections import Counter
        regime_counts = Counter(f["regime"] for f in candidates if f["regime"] and f["regime"] != "unknown")
        qualifying_regimes = sorted(r for r, c in regime_counts.items() if c >= self.MIN_PER_REGIME)
        if len(qualifying_regimes) < 2:
            return CalibrationVerdict(
                passed=False,
                reason="INSUFFICIENT_REGIME_COVERAGE",
                n=n,
                metrics={
                    "regime_counts": dict(regime_counts),
                    "min_per_regime": self.MIN_PER_REGIME,
                    "qualifying_regimes": qualifying_regimes,
                    "version": eval_param_version, "deploy_ts": deploy_ts,
                },
            )
        regimes = set(qualifying_regimes)  # downstream metrics reflect the qualifying regimes only

        # Check slippage modeled (paper harness should set it; guard requires evidence)
        slippage_modeled = all(f["meta"].get("slippage_modeled", False) for f in candidates)
        if not slippage_modeled:
            return CalibrationVerdict(
                passed=False,
                reason="ZEROED_OR_UNMODELED_SLIPPAGE",
                n=n,
            )

        # Buckets by predicted EV quantiles (simple 4 buckets for low-N)
        sorted_by_ev = sorted(candidates, key=lambda x: x["ev"])
        bucket_size = max(1, n // 4)
        buckets = []
        for i in range(0, n, bucket_size):
            b = sorted_by_ev[i:i+bucket_size]
            if not b:
                continue
            mean_ev = sum(x["ev"] for x in b) / len(b)
            mean_real = sum(x["realized"] for x in b) / len(b)
            buckets.append({
                "mean_predicted_ev": round(mean_ev, 4),
                "mean_realized": round(mean_real, 4),
                "n": len(b),
                "dev": round(abs(mean_ev - mean_real), 4),
            })

        mean_abs_dev = sum(b["dev"] for b in buckets) / len(buckets) if buckets else 0

        # +EV cohort: those with ev > 0 (or > some positive threshold)
        pos_ev = [f for f in candidates if f["ev"] > 0]
        if pos_ev:
            mean_real_pos = sum(f["realized"] for f in pos_ev) / len(pos_ev)
            n_pos = len(pos_ev)
        else:
            mean_real_pos = 0
            n_pos = 0

        # Skill vs luck: dispersion check
        realized_pos = [f["realized"] for f in pos_ev]
        if realized_pos:
            rng = max(realized_pos) - min(realized_pos)
            outlier_flag = (rng > abs(mean_real_pos) * 3) and n_pos < 5
        else:
            outlier_flag = False

        # Decision
        passed = True
        reason = "PASS"
        if mean_abs_dev > self.CALIB_TOLERANCE:
            passed = False
            reason = "MISCALIBRATED"
        elif mean_real_pos <= self.MIN_POS_EV_REALIZED:
            passed = False
            reason = "POS_EV_COHORT_NOT_POSITIVE_POST_SLIPPAGE"
        elif outlier_flag:
            passed = False
            reason = "OUTLIER_DOMINATED_SKILL_VS_LUCK"

        metrics = {
            "mean_abs_dev_across_buckets": round(mean_abs_dev, 4),
            "mean_realized_for_positive_ev": round(mean_real_pos, 4),
            "n_positive_ev": n_pos,
            "outlier_flag": outlier_flag,
            "n_regimes": len(regimes),
            "slippage_modeled": slippage_modeled,
        }

        # O1/O2 explicit in window + M-N near floor note
        window = {
            "n_total": n,
            "eval_param_version": eval_param_version,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "deploy_ts": deploy_ts,
            "forward_only": bool(deploy_ts),
            "regimes": list(regimes),
            "min_n_required": self.MIN_N,
        }
        if n < max(30, self.MIN_N * 2) and self.MIN_N == 20:
            window["note"] = "N near MIN_N floor (20); operator should consider higher value (e.g. 50+) for go-live decision per process honesty."

        return CalibrationVerdict(
            passed=passed,
            reason=reason,
            n=n,
            metrics=metrics,
            by_bucket=buckets,
            by_regime=[{"regime": r, "n": sum(1 for f in candidates if f["regime"] == r)} for r in regimes],
            window=window,
        )
