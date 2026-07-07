# Hood Agent — First-Class Safety Invariants

These two properties are first-class invariants, not incidental implementation details.
They are tested independently in `tests/test_audit_runner_gate.py` and must be preserved
across every future change. Any PR that weakens either requires explicit owner sign-off.

---

## INVARIANT 1: LIVE_GATE_HARD_RAISE

**Location:** `src/core/executor.py` → `execute_thesis()`, immediately after the kill check.

**Contract:**
- `SafetyCore.is_live_enabled()` starts `False` and can only be set `True` by an
  explicit human `human_go_live()` call (the Phase 4 calibration gate).
- Any call to `execute_thesis()` with `run_mode=RunMode.LIVE` while `is_live_enabled()`
  is `False` returns a vetoed `ExecutionResult(success=False, veto_reason="live_not_enabled")`.
- This check runs **before** any sizing, quote fetch, risk evaluation, or LLM call.
  It cannot be bypassed by EV, regime, SafetyCore loosening, or meta-reviewer.
- The real `RobinhoodClient.place_limit_order()` also raises `NotImplementedError` as a
  belt-and-suspenders guard at the broker seam itself.

**Why it exists:** Hood will eventually run fully autonomously (24/7, no per-trade approval).
The human go-live gate is the one structural check that cannot be self-overridden. Once live,
the agent's self-rewrite may make it smarter or more cautious but can never flip this gate.

**How to verify:** `GateLiveHardRaise.test_g7_live_mode_vetoed_when_not_armed` in
`tests/test_audit_runner_gate.py` (G7). Drives `Executor.execute_thesis` directly with
`run_mode=RunMode.LIVE` and `SafetyCore.is_live_enabled()` forced `False`, and asserts the
result is vetoed with `live_not_enabled` before the order reaches the broker leaf.
Fail-before verified by the auditor: with the check removed, the mock broker's
`place_limit_order` actually fires and `result.success` is `True`.

---

## INVARIANT 2: CONFIRM_AFTER_FILL_OR_UNWIND

**Location:** `run_paper.py` → `run_paper()`, in the trigger-processing loop after a
successful `process_trigger` call.

**Contract:**
- After every order submission that returns `executed=True`, hood re-inspects actual
  broker state (`client.get_positions()`) before recording the position as open.
- If the confirm check fails for any reason (position not visible, fill price missing,
  or the broker query itself raises), the submitted order is cancelled (`cancel_all()`)
  and the position is **not** recorded in `open_positions` or the `OwnershipLedger`.
- The result is logged as `{"action": "confirm_fail_unwind"}`.

**Why it exists:** Without post-submit confirmation, a partial fill or a broker-side error
could create a position in the broker book that hood's ledger doesn't know about. When hood
later tries to sell, the ledger gate (`not_in_hood_ledger`) would veto the sell, leaving an
orphaned long position with no exit path. The confirm step closes this gap before it can happen.

**How to verify:** `TestRunnerWiring.test_runner_confirm_fails_no_open_and_triggers_unwind`
in `tests/test_core.py` injects a broker that confirms empty after submit and asserts:
1. No `open_positions` record.
2. `cancel_all()` was called.
3. `confirm_fail_unwind` appears in results.

The ledger-rollback half of this invariant (Mandate 1's extension: a confirm-fail must also
roll back any optimistic ledger write the executor made on the buy) is verified separately
by `GateTenantIsolation.test_g6_confirm_fail_rolls_back_ledger` in
`tests/test_audit_runner_gate.py` (G6). Fail-before verified by the auditor: with the
ledger-rollback call removed from the confirm-fail branch in `run_paper.py`, the ledger
still reports `has_position=True` after the unwind.

Note this is NOT in the locked gate file (`test_audit_runner_gate.py`) for its core half —
only the newer ledger-rollback extension is. The base confirm/unwind mechanism predates
this session and lives in `test_core.py`.

---

## INVARIANT 3: TENANT OWNERSHIP LEDGER (Mandate 1)

**Location:** `src/core/ownership_ledger.py`, wired through `Executor` and `run_paper.py`.

**Contract:**
- Hood maintains `hood_ownership_ledger.json` — its exclusive record of positions it opened.
- **Sells are gated against this ledger**, not against broker `get_positions()` (which reflects
  the whole shared account including positions opened by other fleet agents on the same account).
- Sell veto: `not_in_hood_ledger` — if the ledger has no record of hood owning `ticker`,
  the sell is rejected regardless of what the broker reports.
- **Fail-closed:** a missing, corrupt, or unreadable ledger file returns an empty ledger.
  Empty ledger → `has_position()` returns `False` → all sells vetoed.
- **Sizing:** `RiskController` is initialized with `HOOD_SLEEVE_USD` (env var, default $500),
  not account-total NAV. Hood never reads total account value to size positions.
- Ledger is updated atomically (write-to-temp, rename) to prevent corruption on crash.

**Why it exists:** Hood shares an account with other fleet agents (e.g., truleo). Without a
per-agent ownership ledger, hood could attempt to sell positions it didn't open (another agent's
positions) or over-size based on total account NAV rather than its own allocated sleeve.
Retrofitting this is expensive (truleo is paying that cost now); hood is built tenant-aware
from day one.

**How to verify:** `GateTenantIsolation` in `tests/test_audit_runner_gate.py` (G6):
- `test_g6_foreign_position_not_sold` — injects a position into the broker mock that hood's
  ledger has no record of (simulating another tenant's position on the shared account) and
  asserts the sell is vetoed with `not_in_hood_ledger`. Fail-before verified by the auditor:
  with the sell-gate check removed, `result.success` is `True` and the mock broker actually
  sells the foreign position.
- `test_g6_confirm_fail_rolls_back_ledger` — see Invariant 2 above.

Additional unit coverage (fail-closed on corrupt file, persistence across restart, blend-on-
second-buy, partial/full remove) lives in `TestOwnershipLedger` in `tests/test_core.py`.

---

## Continuous vs. Cron (go-live requirement)

The current scheduler (launchd, 9:35 AM and 3:35 PM) is appropriate for paper accumulation.
Hood's signal is event-driven (EDGAR filings arrive at any time), so **go-live requires a
persistent always-on process**, not a cron job.

`run_paper()` supports `max_cycles=None` for always-on mode:
```bash
PYTHONPATH=. .venv/bin/python3 run_paper.py --real --feed dynamic --max-cycles 0
```
(`--max-cycles 0` maps to `None` in the CLI; the loop runs until SIGINT or the KILL file.)

The cron setup must be replaced with a supervised persistent process (e.g., `launchd` with
`KeepAlive=true`, or a process supervisor) before go-live. This is a deployment decision,
not a code change.
