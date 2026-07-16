# Hood — Real Funnel Analysis (can the Phase-4 gate ever fill?)

**Date:** 2026-07-15 · **Basis:** `data_real/decisions.ndjson` — the REAL OOS runner (real EDGAR, real Yahoo quotes, real Haiku/Sonnet reasoning). Zero LLM spent on this analysis (read-only).

## The question
The Phase-4 calibration gate needs **N ≥ 20 resolved trades across ≥ 2 regimes**. At the current rate, will it ever fill?

## The real funnel — 6 trading days (2026-07-07 → 2026-07-15), 105 distinct instruments

| Stage | Killed here | Note |
|-------|-------------|------|
| 1. Deterministic screen / budget | 3 | free filter + budget breaker |
| 2. Haiku triage | 38 | "routine / no price-moving catalyst" |
| 3. **EV engine** | **75** | biggest filter — **but see correction below: most were NOT "no edge," they were budget-starved or hit a (now-fixed) API error.** |
| 4. Adversarial auditor | 3 reached | **vetoed 3 / 3** |
| → **Entries (tradeable)** | **0** | |

**Resolved trades toward the gate: 0.** (No `outcomes` file — nothing to resolve.)

## ⚠️ CORRECTION (added 2026-07-15, from graveyard `reject_reason`) — the EV stage is budget-starved, not edge-empty
My first pass above (and a parallel read by Fable) concluded the EV engine "genuinely finds no edge." **The graveyard receipts say otherwise.** Breaking down the 75 EV-stage kills by actual `reject_reason` (data_real/graveyard.db, 95 real rows):

| reject_reason | count | what it means |
|---------------|-------|---------------|
| `budget_refused_no_thesis` | 50 | **$0.10/day breaker tripped — no thesis ever computed.** Ongoing daily (07-07→07-15; 7 on 7/14, 5 on 7/15). |
| `budget_degrade_no_thesis` | 16 | budget-degraded, no thesis |
| `Error code: 400 (temperature deprecated)` | 25 | the OLD G12 bug — **dated 07-07→07-09 ONLY, already fixed, not recurring.** Stale rows. |
| `auditor_reject` / `6 adversarial findings` | 3 | reached the auditor (below) |
| `spread_too_wide`, `reverse_stock_split` | 2 | genuine deterministic screens |

**So the dominant LIVE constriction is the budget cap, not "no alpha."** 66 of the ~92 non-auditor rejects are budget-refused/degraded — the bot runs out of its 10¢/day before it can evaluate most candidates. On days with 13–14 candidates, the cap exhausts and the remainder are refused with no reasoning at all. The 25 "no edge"-looking API errors were the already-fixed temperature bug.

**Implication for the gate:** pre-registering a "no edge → kill" rule *right now* would measure a **starved** funnel, not the real edge. The daily cap (currently `--daily-usd-cap 0.10`, protected ceiling $1.00 — raising toward it is a human operational call, NOT a safety loosening) should be raised first so the EV engine actually evaluates the full candidate flow. THEN the kill-rule window is measuring real selectivity.

## The 3 that reached the auditor — all vetoed, and the vetoes are legitimate
| Ticker | EV thesis | Auditor verdict | Was the veto substantive? |
|--------|-----------|-----------------|---------------------------|
| EQPT (7/10) | +3.44%, p_up 0.62 | 6 findings, 3 high-sev | findings text pre-dates G13 persistence (not recoverable) |
| NXTC (7/14) | **−0.25%**, p_up 0.45 | 6 findings, 3 high-sev | ✅ correct kill — negative EV; also caught truncated dilution disclosure (reverse-merger minority-holder risk) |
| SPT (7/15) | +3.2%, p_up 0.60 | 6 findings, 3 high-sev | ✅ **sharp** — "guidance 'beat' is against the company's OWN prior outlook, not consensus" — a real analytical hole |

The identical "6 findings / 3 high-sev" *count* looked like a boilerplate template, but the persisted **content is specific and correct** — genuine adversarial reasoning, not reflexive blocking.

## Honest verdict
**The gate will not fill on the current horizon — but the dominant cause is a budget throttle, not proven "no alpha." (Revised.)**
- The pipeline runs correctly, but on most days it **exhausts its $0.10 budget and refuses the majority of candidates without computing a thesis** (66 of ~92 non-auditor rejects). We are not yet observing the strategy's true edge — we're observing a starved funnel.
- The auditor IS legitimately strict: the 2 positive-EV theses it did evaluate both had real, specific flaws it caught. That selectivity is genuine.
- The **"architecture may be stronger than the alpha source"** risk (CLAUDE.md) is still live and untested — but it cannot be judged until the budget throttle is cleared so the EV engine evaluates the full candidate flow.
- The 25 "no-thesis" API errors were the already-fixed temperature bug (07-07→09), not evidence of anything.

⚠️ The existing `logs/phase4_calibration_report.json` shows **PASS, N=25** — but its `deploy_ts` is `2024-03-01` with generic Q1/Q2 regimes. **That is synthetic/fixture calibration data (plumbing), not the real OOS gate.** The real gate has N=0. Do not read the "PASS" as a live result.

## What would and would NOT help
- ❌ **Spending LLM budget on replay** — the replay harness runs over *fixture* filings with `FakeLLMClient` by default; even `--use-real-llm` reasons over fake sample filings = the exact "validates plumbing not alpha" trap. Buys no real signal.
- ❌ **Loosening the auditor / EV screens** — forbidden: these are down-only safety per the owner autonomy model. And the vetoes we can read are correct.
- ✅ **Let the real OOS runner keep accumulating** — the honest, intended path. But set expectations: at 0 entries / 6 days, N=20 is *months+* away, possibly never at current selectivity.

## Decision for the owner (bcm3000) — REVISED ordering
The correction changes the sequence. **Clear the throttle before you judge the edge:**
1. **FIRST — raise the daily budget cap** (currently `--daily-usd-cap 0.10` in `run_oos.sh`; protected ceiling is $1.00, so this is an operational tuning, NOT a safety loosening). At ~13 candidates/day being refused, the funnel needs enough budget to actually compute theses. Pick a value (e.g. $0.30–$0.50) that lets a full day's candidates through without breaching $1.00. This is the single change that unblocks real measurement.
2. **THEN pre-register the kill rule** (`/postmortem`): "if real OOS entries remain 0 after N candidate-months across ≥2 regimes *on an un-starved funnel*, declare hood's current edge unproven and pivot." Dated before the window — your money rule. Pre-registering it now against a starved funnel would measure the wrong thing.
3. **Optional — re-examine the alpha source** (universe/event breadth), strictly upstream of the safety screens, once (1) makes the funnel observable.

No safety parameters were touched. No LLM budget was spent producing this analysis.
