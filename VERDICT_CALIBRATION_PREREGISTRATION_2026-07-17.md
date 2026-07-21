# Pre-registered verdict-calibration test — counterfactual returns on auditor vetoes

*Pre-registered 2026-07-17, before any counterfactual outcome data exists. Same discipline as
`AUDITOR_FUNNEL_TRIPWIRE_2026-07-11.md`: define the falsifiable check and the threshold BEFORE
the outcomes arrive, so the verdict can't be argued into whatever we want once the numbers land.*

## Why this test exists (what today's golden-case run did and did not prove)

Today's owner-authorized golden-case test (real ATAI/Eli Lilly merger filing, real +90%
confirmed price move, $0.0675 real spend, `AUDITOR_FALSIFICATION_REPORT_2026-07-17.md`) showed
the auditor's individual findings are substantive and specific — it caught a real downside-
modeling error, then a real missing-deal-price gap, not boilerplate.

**That is a different question from whether the verdict RULE is calibrated to expected value.**
Every trade that ever made money had real, listable risks — an adversarial model tasked with
finding fault will almost always surface at least one it rates "high." A veto rule of "any single
high-severity finding = no trade" can be near-unpassable BY CONSTRUCTION even when every finding
it produces is individually accurate. Correct findings do not prove a correct verdict. Fable
(relay) flagged this gap in the golden-case framing; this document is the fix — a test that
actually measures verdict calibration, not finding quality, using real subsequent price outcomes
instead of another hand-written thesis.

Also flagged: the golden-case thesis was hand-written by Claude, testing a *post-pop* merger-arb
entry (buying at $7.22 after the news, for the residual spread) rather than hood's own pipeline
reacting *at the filing*. Tomorrow's planned pipeline run (see Status below) removes that
confound by using hood's own EV engine output instead of a hand-written thesis.

## The pre-registered criterion

**Population:** every auditor-stage rejection with `ev_pct > 0` that gets counterfactually
resolved via `src/core/counterfactual.py` (G14, built + wired 2026-07-17) against a real quote
after its horizon passes. As of this writing: 3 queued (SPT +3.2% EV, resolves ~2026-07-20;
KLRS +5.6% EV and KRP +0.5% EV, resolve ~2026-07-22). This population grows automatically as
more +EV candidates get vetoed and age past the 5-day horizon — no manual step required.

**Trigger:** once **N ≥ 20** vetoed +EV theses have a real counterfactual resolution (matches the
same N≥20 threshold the funnel tripwire and the Phase-4 gate itself already use, for
comparability), compute the mean `realized_return_pct` across all resolved counterfactual rows
(`SELECT AVG(realized_return_pct) FROM trades WHERE outcome='counterfactual_resolved'`).

- **If mean counterfactual return is clearly positive** (a threshold of **> +2%** average, chosen
  to comfortably clear paper-trading slippage/spread noise, not just >0): the verdict rule is
  judged **miscalibrated** — the auditor is vetoing trades that would, on average, have made
  money. This triggers a written recalibration proposal (severity anchoring in the prompt,
  and/or a veto rule that weighs findings against the thesis's EV rather than any-one-kills) for
  Brooks's approval. **The auditor is never auto-loosened** — same discipline as the funnel
  tripwire.
- **If mean counterfactual return is flat-to-negative** (≤ +2%, including negative): the auditor
  is **vindicated** — its vetoes are, on average, correctly avoiding bad trades. The honest
  conclusion at that point shifts from "is the gate broken" to "is the alpha source itself weak"
  — i.e. supports re-examining the universe/sourcing (per Fable's original framing of the funnel
  tripwire's option 1), not the auditor.
- **If 2% < mean ≤ some clearly-marginal band** (this document does not pre-commit a second
  threshold — deliberately, to avoid overfitting a boundary before seeing the shape of the data):
  written up as inconclusive, with the individual per-trade breakdown, and N is left to keep
  accumulating rather than forcing a premature call.

## What this test must NOT become

- **Not** a trigger to loosen the veto automatically. Even a clearly-miscalibrated result produces
  a *proposal*, not a code change — Brooks decides, per the protected-core rule in `CLAUDE.md`.
- **Not** a one-shot decision on N=3. The 3 currently-queued resolutions inform direction but do
  not, by themselves, meet the N≥20 threshold; do not treat an early positive or negative average
  on N=3 as the verdict. Let the tripwire's own math (20-candidates-to-auditor, most of which will
  keep vetoing) continue feeding this population at its natural pace.
- **Not** a substitute for the funnel tripwire (`AUDITOR_FUNNEL_TRIPWIRE_2026-07-11.md`), which is
  still live and independently useful (it's about approval RATE; this is about vetoed-trade
  OUTCOME). Both should be read together when either fires.

## Status

- 2026-07-17: pre-registered, before any counterfactual outcome exists. Population = 0 resolved,
  3 queued (SPT ~07-20, KLRS/KRP ~07-22). N≥20 threshold not yet close — monitoring only.
- 2026-07-17 (same day, follow-up): ran the same ATAI golden case through hood's OWN pipeline
  (Haiku triage → Opus EV engine → Opus auditor, not a hand-written thesis) per Fable's critique
  that correct findings don't prove a correct verdict, and that the earlier test used a Claude-
  written thesis rather than hood's own reaction. Hood's own EV engine independently produced a
  similar thesis (EV +6.45%) and was vetoed again (2 high-sev, down from 3) — same recurring root
  cause across all 3 test runs today (2 hand-written + 1 pipeline-generated): no per-share deal
  price disclosed in the 8-K. This is informative (see `AUDITOR_FALSIFICATION_REPORT_2026-07-17.md`
  §A4) but does NOT change this document's pre-registered N≥20 threshold or criterion — it is a
  qualitative finding about WHY the auditor vetoes, not a quantitative substitute for the
  counterfactual-return test this document defines. That test still needs real resolved outcomes.
- Next check: re-read this file once the 3 queued rows resolve, to confirm the query and
  threshold logic work end-to-end on real (if still small-N) data — not to call a verdict yet.
