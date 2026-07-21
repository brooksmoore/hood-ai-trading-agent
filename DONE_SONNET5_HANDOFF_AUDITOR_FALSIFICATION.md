# SONNET5_HANDOFF — Auditor falsification test + counterfactual mark-to-market

**To:** Sonnet 5 (fresh Claude Code session in `~/Desktop/hood_agent_1`)
**From:** Claude (adversarial auditor for this repo)
**Date:** 2026-07-17
**Status:** Phase R (real OOS paper runner live on cron). Phase-4 calibration gate at N=0 real trades. This handoff is DIAGNOSIS + one small build. It is not a license to loosen anything.

---

## Context (cold-start)

hood_agent_1 is a paper-only, event-driven microcap trading agent: EDGAR filings → Haiku triage → Opus EV thesis → adversarial LLM auditor → risk/executor. It has run real OOS since 2026-07-07. The funnel was budget-starved until 2026-07-16 (cap raised $0.10 → $0.25/day); now un-starved, ~3 candidates/day reach the auditor. **The auditor has vetoed 10 of 10 candidates in the project's entire history, including +EV theses (EQPT +3.44%, SPT +3.2%, KLRS +5.6%), and every verdict has the identical shape: exactly "6 adversarial findings," always 3–4 high-sev.** A pre-registered tripwire (`AUDITOR_FUNNEL_TRIPWIRE_2026-07-11.md`) fires a mandatory written review at 20-with-0-approvals; we are at 10/20.

The suspected mechanism (already located in code, verify it yourself): in `src/core/auditor.py`, the LLM prompt asks for "up to 5 findings" (LLMs fill quotas → always 5), `adversarial_pass` caps at `arr[:5]` then **unconditionally appends a 6th** med-severity info_asymmetry finding (hence always exactly 6), and `run_auditor` vetoes on `len(high_sev) > 0` — any single high-severity finding rejects. An adversarial model told to attack a thesis will nearly always rate ≥1 finding "high," so the gate may reject 100% by construction. If true, the Phase-4 calibration gate can never fill regardless of whether the alpha is real.

Your job: (A) confirm or refute that the veto is structural, via a golden-case falsification test; (B) build counterfactual mark-to-market of the VETOED +EV theses so we learn whether the auditor's vetoes were right — calibration data at zero capital.

## Files to read first (in order)

1. `CLAUDE.md` — project rules, protected safety core, audit-loop discipline. Binding.
2. `AUDITOR_FUNNEL_TRIPWIRE_2026-07-11.md` — the pre-registered review this work is executing early.
3. `src/core/auditor.py` — the whole file (~330 lines). The suspected structural veto: prompt near line 229 ("List up to 5 findings"), `arr[:5]` + unconditional append near lines 261–275, `hard_fail = det.has_hard_veto() or len(high_sev) > 0` near line 301.
4. `AUDIT_LEDGER.md` — entries dated 2026-07-15 through 2026-07-17 (funnel analysis, cap raise, 0-for-10 finding).
5. `data_real/graveyard.db` — table `trades`; auditor-stage rejections have `reject_reason LIKE '%adversarial%'`; full findings JSON in `meta`, analyst rationale in `raw_thesis` (G13, present for rows from 2026-07-10 onward).
6. `src/core/market_data.py` — the real Yahoo quote path you'll reuse for counterfactual pricing.
7. `run_paper.py` — how theses/positions are resolved today (`paper_open_positions.json` flow), so the counterfactual resolver mirrors it.
8. `tests/test_audit_runner_gate.py` — house test style; every new test needs captured fail-before evidence.

## The task

### Part A — Is the veto structural? (diagnosis, ~$0.05 LLM)

1. **Static confirmation.** Read `auditor.py` and confirm/refute the three-part mechanism above. Also check `data_real/graveyard.db`: distribution of finding-count and high-sev-count across all 10 auditor-stage rejections (query `meta`). If the count is ever ≠ 6, note it.
2. **Severity-inflation probe (offline, $0).** Read the prompt text: does it give the model ANY calibration for what "high" severity means, or any instruction that "high" should be reserved for thesis-killing flaws? (Current suspicion: no — severity is unanchored, so the model rates freely.)
3. **Golden-case falsification test (real LLM, budget-gated).** Construct 2 golden theses where hindsight says the trade was clearly good — use real historical filings (e.g., a small-cap 8-K with an unambiguous positive event that the stock followed through on; pick from real EDGAR history, not fabricated numbers — the no-fabrication rule applies to test inputs too, label them clearly as historical replay). Run `run_auditor` with the real LLM (env key in `.env`, loaded by `run_paper.py`'s `_load_dotenv`; respect the $0.25/day cap — this costs ~2 Opus calls). Record: finding count, high-sev count, verdict. **If the auditor vetoes hindsight-obviously-good setups with the same 6-findings/3-high shape, the structural hypothesis is confirmed.**
4. **Write the verdict** in a new `AUDITOR_FALSIFICATION_REPORT_2026-07-17.md`: confirmed / refuted / mixed, with the evidence. If confirmed, include a **proposal** (not an applied change) for a calibrated verdict rule — e.g., severity anchoring in the prompt, or a veto rule that weighs high-sev findings against the thesis's stated edge instead of any-one-kills. DO NOT APPLY IT (see Constraints).

### Part B — Counterfactual mark-to-market of vetoed +EV theses (small build)

5. **Write a failing test first.** New test file `tests/test_counterfactual_mtm.py`: drives a new module `src/core/counterfactual.py` end-to-end with a `MockMarketData` price series — asserts that a vetoed thesis with a stored ref price and a later real quote produces a `counterfactual_return_pct` row persisted to graveyard (`meta` update or a new `counterfactual` column — prefer `meta`, it's hood-owned unconstrained JSON). Capture the failure output BEFORE building (house rule: a test that cannot fail is a bug).
6. **Build `src/core/counterfactual.py`** (stdlib only): reads auditor-stage rejections from graveyard with `ev_pct > 0`, entry = ref price from `raw_thesis`, exit = real quote at horizon (mirror the hold-period logic `run_paper.py` uses for real positions; same conservative slippage model, pessimistic direction). Writes `counterfactual_{return_pct, entry, exit, resolved_ts}` into that row's `meta`. Idempotent (never re-resolves a resolved row). All failures logged, none silent — no bare `except: continue` (house rule: silent excepts on money-adjacent paths are bugs).
7. **Wire it into `run_paper.py`'s cycle** behind the same cadence as position resolution (runs free — quotes only, no LLM). The integration must be exercised by a test that calls the REAL entry point (not a reimplementation) — extend `tests/test_audit_runner_gate.py` pattern.
8. **Backfill:** run it once for real against EQPT (vetoed 2026-07-10), SPT (2026-07-15), KLRS (2026-07-17) and any other `ev_pct > 0` auditor rejections. Report the counterfactual returns in the Part A report: did the auditor's vetoes save money or cost money?

## Constraints (binding)

- **PAPER ONLY. No real orders, ever.** `LIVE_ENABLED` stays False; `robinhood_client.py` NotImplementedError stays.
- **DO NOT loosen the auditor veto, severity rule, or any screen — not even one character — in this session.** The tripwire doc pre-registers: "the safety veto is never weakened to manufacture activity." Any change to the verdict rule is a written proposal for Brooks to approve; he is a non-engineer, so state the proposal in plain English with the numbers.
- Protected core untouched: 25% ceiling, event-risk caps, `validate_execution_safety`, $1 budget ceiling, kill switch, no-fabrication. Deterministic screens in `auditor.py` are verified-good — extend only, never rewrite.
- **Zero new dependencies** (stdlib + sqlite3 + the sanctioned `anthropic==0.45.2` only). **No network in the unittest suite** — fixtures + `FakeLLMClient`/`MockMarketData` only; the golden-case LLM run is a gated manual script, not a test.
- LLM spend this session ≤ $0.10 (2 golden-case audits). Daily cap is $0.25 — check `data_real/` budget state before spending.
- Never print or echo the contents of `.env`.
- All 122 existing tests must stay green: `PYTHONPATH=. python3 -m unittest discover -v` (python3 = system 3.8).

## Definition of done

- [ ] `AUDITOR_FALSIFICATION_REPORT_2026-07-17.md` exists with: static mechanism verdict, severity-anchoring finding, golden-case results (2 cases, real LLM, finding/high-sev counts, verdicts), counterfactual returns for all vetoed +EV theses, and a plain-English proposal (if warranted) that Brooks can approve or reject.
- [ ] `src/core/counterfactual.py` + tests exist; new tests have captured fail-before evidence pasted into the report; full suite green (122 + new).
- [ ] Graveyard rows for EQPT/SPT/KLRS (+ any other `ev_pct>0` auditor rejections) carry `counterfactual_*` fields in `meta`.
- [ ] No diff to the auditor verdict rule, deterministic screens, or any protected parameter.
- [ ] `STATUS.md` header + Recent movement updated; dated entry appended to `AUDIT_LEDGER.md`.

## Report back

Write results to `~/Desktop/umbrella/inbox/SONNET5_TO_CLAUDE_auditor_falsification_RESULTS.md` (summary + pointer to the report file), then rename this file with a `DONE_` prefix. Claude (the repo's adversarial auditor) will independently verify: re-run the suite, re-drive the counterfactual path, and check that no veto logic changed. Expect that audit — leave the fail-before evidence and the golden-case raw outputs where they can be re-checked.
