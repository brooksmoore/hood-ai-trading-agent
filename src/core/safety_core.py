"""PROTECTED SAFETY CORE (spec 0.1 for Phase 3).

Immutable or down-only (safer direction only) for autonomous self-rewrite.
Self-rewrite (Meta-Reviewer) MUST call validate_safety_core_change before applying any change to these.
Changes that loosen (unsafe direction) are rejected and logged 'safety_core_violation'.
Kill switch is always protected (never disableable).

This is the ONLY structural protection on live capital once go-live is human-approved (Phase 4).
Build this FIRST, before any Meta logic that can propose changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import json


@dataclass
class SafetyParams:
    """Central protected params. Access via SafetyCore.get_xxx() or direct for read (but use getters for future)."""
    HARD_CEILING_PCT: float = 0.25          # 25% of agentic sleeve - down-only
    EVENT_RISK_CAP_PCT: float = 0.15        # 10-15% for flagged - down-only
    MAX_ALLOWED_SPREAD_PCT_DEFAULT: float = 0.02  # tighter-only (smaller)
    MAX_PCT_OF_ADV_DEFAULT: float = 0.05    # tighter-only (smaller)
    DAILY_USD_CAP_DEFAULT: float = 1.00     # down-only
    # Kill is boolean protected: once on, can't turn off via self-rewrite.
    KILL_ENABLED: bool = True
    # LIVE_ENABLED: human-only go-live switch (P4-B). Default False. Protected. Can only be set True by human+passing calib. Autonomous never.
    LIVE_ENABLED: bool = False


class SafetyCoreViolation(Exception):
    """Raised when an attempt is made to loosen a protected safety parameter (fail-closed)."""
    pass


class _GuardedParams(SafetyParams):
    def __setattr__(self, name, value):
        if name in ('HARD_CEILING_PCT', 'EVENT_RISK_CAP_PCT', 'MAX_ALLOWED_SPREAD_PCT_DEFAULT',
                    'MAX_PCT_OF_ADV_DEFAULT', 'DAILY_USD_CAP_DEFAULT', 'KILL_ENABLED', 'LIVE_ENABLED'):
            old = getattr(self, name, None)
            allowed, reason = SafetyCore.validate_safety_core_change(name, old, value)
            if not allowed:
                SafetyCore.log_violation(name, old, value, reason)
                raise SafetyCoreViolation(f"Protected param direct mutation rejected: {reason}")
        object.__setattr__(self, name, value)


class SafetyCore:
    """Singleton-like core for protected values + validation + violation logging.
    S1: fail-closed - direct mutation of protected attrs now raises SafetyCoreViolation (after validate).
    Only apply_safe_change (and the new human_restore) are the allowed mutation paths for protected.
    """

    _log_path: Optional[Path] = None  # set at init, e.g. data/safety_violations.jsonl or via storage

    @classmethod
    def init_log(cls, log_dir: Path) -> None:
        cls._log_path = log_dir / "safety_core_violations.jsonl"
        log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def get_hard_ceiling_pct(cls) -> float:
        return cls._params.HARD_CEILING_PCT

    @classmethod
    def get_event_risk_cap_pct(cls) -> float:
        return cls._params.EVENT_RISK_CAP_PCT

    @classmethod
    def get_max_allowed_spread_pct(cls, is_offhours: bool = False) -> float:
        # offhours tighter in executor, but core default is the base
        base = cls._params.MAX_ALLOWED_SPREAD_PCT_DEFAULT
        return base * 0.75 if is_offhours else base  # example tighter; actual in executor

    @classmethod
    def get_max_pct_of_adv(cls, is_offhours: bool = False) -> float:
        base = cls._params.MAX_PCT_OF_ADV_DEFAULT
        return base * 0.8 if is_offhours else base

    @classmethod
    def get_daily_usd_cap(cls) -> float:
        return cls._params.DAILY_USD_CAP_DEFAULT

    @classmethod
    def is_kill_enabled(cls) -> bool:
        return cls._params.KILL_ENABLED

    @classmethod
    def validate_safety_core_change(cls, param: str, old_value: Any, new_value: Any) -> tuple[bool, str]:
        """Return (allowed, reason). allowed=True only for same or safer (down for caps, tighter=smaller for spreads, can't disable kill).
        Self-rewrite calls this; if not allowed, DO NOT apply, log violation.
        """
        param = param.upper()
        if param == "HARD_CEILING_PCT":
            if not isinstance(new_value, (int, float)):
                return False, "invalid_type"
            if new_value > old_value:
                return False, "safety_core_violation_loosen_ceiling"
            return True, "ok_down_or_same"
        if param == "EVENT_RISK_CAP_PCT":
            if new_value > old_value:
                return False, "safety_core_violation_loosen_event_risk_cap"
            return True, "ok"
        if param in ("MAX_ALLOWED_SPREAD_PCT_DEFAULT", "MAX_ALLOWED_SPREAD_PCT"):
            # tighter means new <= old
            if new_value > old_value:
                return False, "safety_core_violation_widen_spread"
            return True, "ok_tighter_or_same"
        if param in ("MAX_PCT_OF_ADV_DEFAULT", "MAX_PCT_OF_ADV"):
            if new_value > old_value:
                return False, "safety_core_violation_increase_adv_pct"
            return True, "ok"
        if param == "DAILY_USD_CAP_DEFAULT":
            if new_value > old_value:
                return False, "safety_core_violation_raise_budget_cap"
            return True, "ok"
        if param == "KILL_ENABLED":
            if old_value is True and new_value is False:
                return False, "safety_core_violation_disable_kill"
            return True, "ok"
        if param == "LIVE_ENABLED":
            # Only human path can set True; autonomous must never. Down-only? No: from False to True is enable (risky), so guard in human_go_live.
            # Here: autonomous cannot set True.
            if new_value is True and old_value is False:
                # Will be enforced higher in human_go_live; for validate, allow only if authorized context (but validate is general)
                # For direct/autonomous: reject enable.
                return False, "safety_core_violation_enable_live"
            return True, "ok"
        # unknown param: allow for non-protected (e.g. EV prompts)
        return True, "non_protected"

    @classmethod
    def log_violation(cls, param: str, old_value: Any, new_value: Any, reason: str, evidence: Optional[dict] = None) -> None:
        """Append-only log of attempted unsafe changes. Used by Meta and for audit."""
        if cls._log_path is None:
            # fallback
            cls._log_path = Path("data/safety_core_violations.jsonl")
            cls._log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "param": param,
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
            "evidence": evidence or {},
            "outcome": "rejected"
        }
        cls._log_path.parent.mkdir(parents=True, exist_ok=True)
        with cls._log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    @classmethod
    def apply_safe_change(cls, param: str, new_value: Any, evidence: Optional[dict] = None) -> bool:
        """Attempt to apply; returns True if applied (safer or non-protected).
        For protected, checks validate; on fail, logs violation and returns False.
        Non-protected changes (e.g. prompt text) always apply (caller responsibility for other gates).
        """
        old = getattr(cls._params, param, None)
        allowed, reason = cls.validate_safety_core_change(param, old, new_value)
        if not allowed:
            cls.log_violation(param, old, new_value, reason, evidence)
            return False
        if hasattr(cls._params, param):
            object.__setattr__(cls._params, param, new_value)  # bypass guarded __setattr__ since we already validated
        # for non-protected, caller handles
        return True

    @classmethod
    def human_restore(cls, param: str, value: Any, *, authorized: bool = False, evidence: Optional[dict] = None) -> bool:
        """S2: Human-authorized restore path for protected params (bounded toward original default; NOT callable by autonomous/Meta).
        autonomous self-rewrite must NEVER call this (or pass authorized=False).
        Can restore a previous tightening (e.g. 0.15 back toward 0.25), but cannot loosen beyond the protected default.
        Returns True if applied, False/rejected (and logged) otherwise.
        """
        if not authorized:
            cls.log_violation(param, cls._get_current(param), value, "human_restore_requires_authorization", evidence)
            return False
        # Bound: new value must not exceed the original default (i.e. not loosen past baseline)
        default = getattr(SafetyParams(), param, None)
        current = cls._get_current(param)
        # For caps: restore means value <= default (but since we tightened, to restore upward but <= default)
        # For spreads/adv: restore downward <= default? Wait, defaults are the max allowed; tighter is smaller.
        # General: the 'safe' direction from current must be respected, and not exceed the immutable default in the loosening direction.
        # For simplicity: if trying to set > default for cap/budget (loosening), reject.
        if param in ("HARD_CEILING_PCT", "EVENT_RISK_CAP_PCT", "DAILY_USD_CAP_DEFAULT"):
            if value > default:
                cls.log_violation(param, current, value, "human_restore_exceeds_default", evidence)
                return False
        elif param in ("MAX_ALLOWED_SPREAD_PCT_DEFAULT", "MAX_PCT_OF_ADV_DEFAULT"):
            if value > default:  # widening beyond default not allowed even by human in this context
                cls.log_violation(param, current, value, "human_restore_widens_beyond_default", evidence)
                return False
        elif param == "KILL_ENABLED":
            if current is True and value is False:
                cls.log_violation(param, current, value, "human_restore_cannot_disable_kill", evidence)
                return False
        # Apply (human path bypasses autonomous ratchet but still bounded)
        if hasattr(cls._params, param):
            object.__setattr__(cls._params, param, value)
        cls.log_violation(param, current, value, "human_restore_applied", evidence)  # log as info, not violation, but reuse for audit
        # Note: to distinguish, could have separate log, but for now reuse with 'applied' outcome
        # Actually, change log to note
        return True

    @classmethod
    def human_go_live(cls, *, authorized: bool = False, calibration_passed: bool = False, evidence: Optional[dict] = None) -> bool:
        """P4-B: Human-only go-live enable. Requires authorized=True AND calibration_passed=True (from P4-A gate).
        Autonomous/self-rewrite can NEVER enable (even with 'authorized' flag they don't possess).
        Returns True on success (sets LIVE_ENABLED=True, logs), False otherwise (rejects, logs).
        Disable (set False) always allowed for human safety.
        """
        if not authorized:
            cls.log_violation("LIVE_ENABLED", cls.is_live_enabled(), True, "human_go_live_requires_authorization", evidence)
            return False
        if not calibration_passed:
            cls.log_violation("LIVE_ENABLED", cls.is_live_enabled(), True, "human_go_live_requires_passing_calibration", evidence)
            return False
        # Apply
        object.__setattr__(cls._params, "LIVE_ENABLED", True)
        cls.log_violation("LIVE_ENABLED", False, True, "human_go_live_enabled", evidence)  # audit log
        return True

    @classmethod
    def is_live_enabled(cls) -> bool:
        return cls._params.LIVE_ENABLED

    @classmethod
    def _get_current(cls, param: str) -> Any:
        return getattr(cls._params, param, None)


# Convenience for risk/executor to import constants via core (keeps one source)
# But since we mutate? No, use the getters in updated code.
# For backward, keep module constants but route through SafetyCore in new logic.
# Phase 3+ code should use SafetyCore.get_*

SafetyCore._params = _GuardedParams()
