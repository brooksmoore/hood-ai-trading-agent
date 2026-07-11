# Pre-registered tripwire — the adversarial auditor's approval funnel

*Pre-registered 2026-07-11, before the data accumulates. Same discipline the rest of the fleet
uses: define the falsifiable check BEFORE the outcomes arrive, so nothing can quietly become
unfalsifiable. This is not a code change to the auditor's strictness — it is a monitor that
forces a review IF a specific pattern appears.*

## The risk this closes

hood's mandate is genuinely well-designed: EV distributions (not conviction scores), a two-part
adversarial auditor, deterministic screens before any LLM spend. But an adversarial auditor that
rejects **everything** is indistinguishable, on the dashboard, from a correctly-cautious one —
and it can never be wrong, because it never approves a trade whose outcome could disprove it.

Empirical fact as of 2026-07-11: in this project's entire history, exactly **two** real
candidates have ever reached the auditor (AKTX, EQPT) — **both rejected**. That is far too small
a sample to conclude anything. But it names a real failure mode: if this stays 0-for-N as N
grows, the Phase-4 calibration gate (which needs N≥20 *resolved* trades to calibrate against)
can never be reached, and "the auditor is protecting us" becomes unfalsifiable — the exact trap
the whole EV-calibration philosophy exists to avoid.

## The tripwire (pre-committed)

Track a rolling counter of **candidates that reach the adversarial auditor** (i.e. cleared triage
+ EV engine, so a real trade decision was actually on the table) and how many the auditor
**approved**.

- **Trigger:** if **20 candidates reach the auditor and 0 are approved**, that fires the tripwire.
- **On fire — a mandatory review, NOT an automatic loosening:** the auditor's own reject reasons
  for those 20 get read as a batch, and one of two conclusions is reached and written down:
  1. **The rejections are individually correct** — each was a genuinely bad EV bet — in which
     case the finding is that hood's *universe/sourcing* is surfacing only bad candidates, and
     the fix is upstream (the event feed / screens), not the auditor. This is a real, useful
     finding.
  2. **The auditor is systematically miscalibrated** — rejecting on boilerplate risk flags that
     don't actually predict bad outcomes — in which case its thresholds get a calibration review
     against whatever resolved evidence exists. Any change to auditor strictness is itself
     pre-registered and fail-before tested, per this repo's standing rule (an auditor-owned gate
     test that can FAIL against the pre-change code).

## What this tripwire must NOT become

- **Not** an excuse to loosen the auditor to "get some trades through." A 0-for-20 auditor might
  be *correct* — the tripwire forces the question, it does not presume the answer. Loosening a
  safety veto to manufacture activity is the opposite of what this fleet stands for.
- **Not** a silent auto-adjustment. It fires a written review by Brooks + the auditor (Claude),
  never an autonomous change to the protected safety core.

## Status

- 2026-07-11: pre-registered. Counter to be surfaced (candidates-reaching-auditor and
  approvals) in `hood_state.json` / the OOS metrics so the 20-candidate threshold is observable.
  Current standing: 2 reached, 0 approved — well under the trigger, monitoring only.
