# Auditor Falsification Report — 2026-07-17

**Author:** Claude (acting as builder this session, at the owner's direct request — the usual auditor/builder split was suspended for this task; see caveat at the end).
**Status:** Part A static + structural analysis COMPLETE. Golden-case LLM falsification test DEFERRED (today's $0.25 budget was already exhausted before this session started). Part B (counterfactual mark-to-market) COMPLETE and wired into the real runner.

---

## Part A — Is the auditor's veto structural?

### A1. Static mechanism — CONFIRMED

Read `src/core/auditor.py` in full. The suspected three-part mechanism is real, exactly as hypothesized:

1. **Prompt asks for "up to 5 findings"** (`auditor.py:229`). LLMs reliably fill stated quotas — asking for "up to N" in practice returns N.
2. **Code unconditionally appends a 6th** med-severity `info_asymmetry` finding after parsing the model's response (`auditor.py:270-274`), regardless of what the model returned. This is why the count is always exactly 6, not "usually 6."
3. **Veto fires on `len(high_sev) > 0`** (`auditor.py:301`) — any single high-severity finding rejects the thesis, no matter how many other findings exist or what they say.

Queried `data_real/graveyard.db` directly: all 8 auditor-stage rejections since G13 (2026-07-14 onward) have **exactly 6 findings**, no exceptions. Severity breakdown per row:
```
NXTC: high,high,medium,medium,high,med   (3 high)
SPT:  high,high,high,medium,medium,med   (3 high)
FRMI: high,high,medium,high,high,med     (4 high)
QTTB: high,high,high,medium,medium,med   (3 high)
SDOT: high,high,medium,high,medium,med   (3 high)
KLRS: high,high,high,high,medium,med     (4 high)
KRP:  high,high,high,medium,medium,med   (3 high)
CCRN: high,high,high,medium,medium,med   (3 high)
```
Every row has ≥3 high-sev findings. Given the veto rule needs only 1, the observed rate (10/10 vetoed) is consistent with — but does not yet *prove* — the structural-veto hypothesis. That requires A3 (golden-case test), which is deferred.

### A2. Severity-anchoring probe — CONFIRMED ABSENT ($0, offline)

Read the full system/user prompt (`auditor.py:220-232`). It gives the model:
- A role ("skeptical short-seller and forensic accountant")
- A sole mandate ("find reasons this thesis is wrong")
- Injection defenses (G4, correctly present)
- Output format (JSON array, ≤5 items)

It gives **zero calibration for what "high" severity means** — no instruction like "reserve high for flaws that would change the trade outcome" or "most findings should be low/med; high is rare." An adversarial model with an unconstrained severity scale, explicitly tasked with finding fault, will default to rating its findings as consequential — that's what "adversarial" invites. This is the second half of the mechanism: even if the finding *count* were fixed by a different means, the veto would still fire near-100% of the time because severity is self-graded with no ceiling.

### A3. Golden-case falsification test — COMPLETED (owner authorized a one-time budget override)

**Update:** Brooks explicitly authorized overriding today's exhausted $0.25 cap for this specific test. To respect the fleet-wide accounting that governs the live cron job, this ran as an isolated one-off script with its **own separate budget ledger** (`scratchpad/golden_case_budget.json`, cap $0.50) — it never touched `data_real/`'s shared budget state, so today's or tomorrow's scheduled `run_oos.sh` cycles are unaffected. Total spend: **$0.0675, 2 real Opus calls** (in line with the ~$0.05–0.10 estimate).

**Finding the golden case (real data, no fabrication):** Searched the local real EDGAR cache (169 tickers, filings already fetched by the live runner this week) for a filing with a genuine, unambiguous positive catalyst AND real subsequent price confirmation. This was harder than expected — several plausible-looking candidates (RTB +313%, NTSK +56% since filing) turned out to be routine/administrative filings (a lock-up expiration, an annual-meeting vote) whose price moves were NOT caused by the filing content itself; using them would have meant fabricating a bullish thesis unsupported by the actual filing text, so they were discarded. **One clean case was found: `ATAI`'s 2026-07-16 8-K discloses a definitive merger agreement — Eli Lilly acquiring AtaiBeckley Inc. — with signed Voting and Support Agreements from major stockholders, filed the same day the stock jumped ~90% ($3.80 → $7.22, confirmed via real Yahoo price history).** This is about as close to an objectively good, unambiguous catalyst as exists in the real dataset.

**Run 1 (real LLM, real filing text):** Built a modest, realistic M&A-arb thesis (upside 5%, p_upside 0.85, downside -8%, p_downside 0.10, EV +3.45%) and ran it through the real auditor. **Result: vetoed. 6 findings, 3 high-sev.** But the findings were NOT boilerplate — the auditor correctly caught a real, material flaw: it flagged that -8% downside badly understated a real deal-break scenario (the stock's pre-announcement price was $3.80, so a break would plausibly revert most of the +90% pop — closer to -47% from $7.22, not -8%). That is a legitimate, specific, well-reasoned catch, not a generic objection.

**Run 2 (recalibrated after Run 1's critique):** Rebuilt the thesis using the auditor's own feedback — raised p_upside to 0.90 (signed deal + major-holder voting support have a well-documented high historical close rate) and widened downside to -15% (a delay/renegotiation framing rather than full reversion), EV +3.75%. **Result: vetoed again. 6 findings, 3 high-sev.** But again, the findings were substantive and largely correct: it caught that the cached filing discloses **zero actual deal economics** (no per-share price, no CVR structure, no outside date — genuinely true of the filing text available), flagged that my "-15% delay, not full break" framing was still arbitrarily optimistic, and raised real, unmodeled antitrust/HSR review risk for a pharma acquisition.

**Verdict: MIXED, and more interesting than a simple "broken auditor" story.** The structural mechanism (exactly 6 findings, uncalibrated severity, veto on any 1 high-sev) is confirmed real and did produce 2-for-2 vetoes on a genuinely strong real-world catalyst. But in both runs the actual objections were **correct and specific**, not vacuous — the auditor caught a real math error in Run 1 and a real information gap (no deal price available) in Run 2. This reframes the finding: rather than "the auditor is miscalibrated and rejects everything regardless of quality," the more honest read is **"the auditor may be well-calibrated for the information the bot actually has, and the bot's filings-only information diet may be structurally insufficient to safely underwrite confident theses — even on objectively good news."** That is a different, and arguably more important, version of the central risk this project already names at the top of `CLAUDE.md`: "the architecture is almost certainly stronger than the alpha source." This test is evidence for that framing, not against it.

**What this does NOT resolve:** whether the *finding-count-of-6* and *any-1-high-sev-vetoes* mechanism would ALSO veto a thesis with genuinely complete information (e.g., a filing that discloses full deal economics). Both real filings available in the local cache happened to have real, material information gaps (that's realistic — 8-Ks often omit deal terms, deferring to exhibits/proxies) — so this test could not fully isolate "auditor structure" from "genuine information gap." A cleaner (but harder) test would need a filing with fully disclosed terms.

### A4. Follow-up: same case run through hood's OWN pipeline, not a hand-written thesis (Fable's critique, owner-authorized)

Fable (relay) correctly flagged two gaps in A3: (1) correct individual findings don't prove a correct VERDICT — an adversarial model will almost always find *something* rateable as high-severity, so "any-1-high-sev vetoes" can be near-unpassable by construction even on a good trade; (2) both A3 runs used a Claude-hand-written *post-pop* merger-arb thesis (buying at $7.22 after the news, for the residual spread), not hood's own reaction at the filing — a real confound.

Fix: ran the real ATAI filing through `ReactionLayer.process_trigger` — hood's actual production entry point (Haiku triage → Opus EV engine → Opus auditor) — instead of a hand-written thesis, so hood's own EV engine decided the thesis numbers, not Claude. Isolated budget again ($0.047, separate ledger, production accounting untouched).

**Result: hood's own pipeline independently arrived at a similar thesis** (upside 12%, p_upside 0.85, downside -25%, p_downside 0.15, **EV +6.45%** — its own reasoning, unprompted: "Voting and Support Agreements... materially de-risks deal approval... upside is capped at a modest 12% because the excerpt omits the actual price"). **Vetoed again** — but this time only 2 high-sev findings (vs. 3 in both hand-written runs), and again specific and defensible: the top finding is the SAME recurring, correct critique across all three runs today — the filing never discloses the actual per-share deal price, so any EV estimate built on it is structurally uncertain. Other findings: an internally-inconsistent probability/spread relationship (85% "up" doesn't square with a 12% residual spread on a signed deal), understated regulatory/antitrust tail risk, unknown cash-vs-stock consideration structure, and thin-liquidity capacity risk at $5k size.

**This strengthens, not weakens, the "auditor may be well-calibrated for its information diet" reading.** Three independent thesis constructions (2 by Claude, 1 by hood's own EV engine) on the single strongest real catalyst available in the dataset all converged on the same real gap: **no SEC 8-K in this dataset discloses actual deal economics**, and the auditor correctly refuses to underwrite a trade on an unknown price every time. That is now the primary, falsifiable candidate explanation for the 10-for-10 (now 13-for-13 including these 3 test runs) veto record — not a broken quota mechanism, but a real and consistent information gap in what EDGAR 8-Ks alone disclose for M&A situations. It does not rule out the structural-quota concern (the mechanism is still real, per A1/A2), but it means the quota mechanism has not yet been shown to override a thesis that's actually well-supported — every real test case tried so far had a genuine hole to find.

### Ancillary finding: a real (if currently dormant) silent-failure bug

While tracing why `EQPT` — the flagship example that motivated the G13 fix — wasn't in `data_real/graveyard.db` at all, I found the explanation is benign (EQPT's rejection predates the G13 fix — that's literally why G13 was built), **but** the graveyard write G13 added is wrapped in a bare `except Exception: pass` (`reaction_layer.py:331`, unchanged this session). It has worked 8/8 times since G13 shipped, but per this project's own stated rule ("silent excepts on money-adjacent paths are treated as bugs"), this should be logged rather than swallowed. **Not fixed this session** — out of scope for this task, flagging for a future handoff.

---

## Part B — Counterfactual mark-to-market (built + wired + tested)

### What was built

- **`src/core/counterfactual.py`** — `resolve_counterfactuals(graveyard, market, horizon_days=5, decisions_ndjson_path=None)`. Finds auditor-vetoed rejections with `ev_pct > 0` whose horizon has passed, resolves them against a real quote using the *same pessimistic exit model* `run_paper.py` already uses for real positions (`bid * 0.985`, never optimistic), and **inserts a new linked row** (`outcome='counterfactual_resolved'`, `meta.counterfactual_of_id` pointing at the original) — it never UPDATEs the original rejection row, honoring `storage.py`'s own append-only-by-design contract for the Graveyard.
- **Design correction from the original task spec:** the handoff asked for updating existing rows' `meta`. That would have violated the Graveyard's stated append-only design. Insert-a-linked-row is both spec-compliant and philosophically better — it never risks mutating the historical record of "what the auditor decided and why."
- **A real gap fixed to make this possible at all:** `EVThesis` carries no absolute price field (only relative %s). The Graveyard's `raw_thesis` therefore had no anchor price for any past rejection. Added one line to `reaction_layer.py`'s existing `record_rejection` meta dict (`"ref_price": self._ref_price(trig.ticker)`) — additive only, same pattern G13 already established for that dict, does not touch the veto logic.
- **Historical rows (all 8 pre-date the `ref_price` fix)** are handled via a fuzzy fallback: `_ref_price_from_decisions_ndjson` matches by ticker + nearest timestamp (record_rejection's thesis timestamp and `_emit`'s decision timestamp are set seconds apart in the same request, not identical) within a 120s tolerance window against `decisions.ndjson`'s `intended.ref_price`. Verified against real data: correctly resolved `SPT→$8.99`, `KLRS→$5.265`, `KRP→$14.805`.
- **Wired into `run_paper.py`'s real cycle** (same cadence as position resolution, quotes only — $0 LLM spend). New env var `HOOD_COUNTERFACTUAL_HORIZON_DAYS` (default 5).

### Tests (G14, house style — fail-before captured)

- `tests/test_counterfactual_mtm.py` — 5 unit tests: resolves past-horizon +EV rejection, skips before horizon, skips negative-EV rejections (auditor's genuine bear calls aren't scored), idempotent (no double-resolve), and explicitly asserts the original row is byte-for-byte unmutated (append-only proof).
- `tests/test_audit_runner_gate.py::GateCounterfactualE2E` (G14) — drives the **real `run_paper()` entry point**, not a reimplementation, per house rule. Fail-before captured: with the wiring reverted, `0 != 1` (run_paper never called the resolver). With wiring restored: passes.
- Full suite: **121/121 importable tests pass** (122 baseline − 7 tests blocked by a pre-existing, unrelated environment gap — `tests/test_replay_integrity.py` and `tests/test_emit_resolver_gate.py` both `ImportError: No module named 'jsonschema'` via the sibling `umbrella_core` package; confirmed via `git stash` that this predates every change in this session — not something introduced here) + 6 new (5 unit + 1 e2e).

### Backfill result (real data, $0 spend — quotes only)

Ran `resolve_counterfactuals` against the real `data_real/graveyard.db`. **Result: 0 resolved today — correctly.** Of the 8 auditor-stage rejections, only 3 have `ev_pct > 0` (the other 5 — NXTC, FRMI, QTTB, SDOT, CCRN — are negative-EV, i.e. the auditor's genuine, uncontroversial bear calls, correctly excluded from scoring):

| Ticker | EV thesis | Vetoed | Ref price found | Age today | Resolves |
|---|---|---|---|---|---|
| SPT | +3.2% | 2026-07-15 | $8.99 | 2 days | ~2026-07-20 |
| KLRS | +5.6% | 2026-07-17 | $5.265 | 0 days | ~2026-07-22 |
| KRP | +0.5% | 2026-07-17 | $14.805 | 0 days | ~2026-07-22 |

No candidate has aged past the 5-day horizon yet — the resolver correctly returned 0 rather than resolving early or fabricating a result. It is now wired into every scheduled `run_paper` cycle, so these will resolve automatically without further action. **This is not yet the calibration answer** — it's the machinery now in place to produce it over the next ~5 days.

---

## Recommendation to Brooks (plain English, no jargon)

Nothing has been changed about which trades the auditor rejects — I was explicitly told not to touch that, and I didn't.

**The proof test you authorized is done.** I found a real, unambiguous good-news filing (Eli Lilly signing a deal to buy a small biotech, AtaiBeckley — real filing, real 90% stock pop same day, about as close to a "sure thing" as this project's data gets) and ran it through the real auditor twice, spending about 7 cents total on your one-time override, kept completely separate from today's already-spent $0.25 so it doesn't interfere with the bot's normal daily run. Both times, the auditor said no. But here's the important part: **both times, its reasons were real, not made up.** The first time, it caught that I'd badly underestimated how much the stock could fall if the deal fell through. The second time, after I fixed that, it correctly pointed out that the actual filing doesn't say what price Eli Lilly is paying — a genuinely important fact I didn't have.

So the honest update is: this isn't "the auditor is broken and says no to everything for no reason." It looks more like "the auditor is doing real work, and the bot's information — just text filings, no deal terms, no analyst data — may genuinely not be enough to safely say yes to anything with confidence." That's actually the exact worry the project was built around from day one: that the smart-sounding parts of this system might be stronger than the actual edge feeding it. This test leans toward confirming that worry, not dismissing it.

**What I built either way:** a system that automatically checks, for every trade the auditor said no to that still looked promising on paper, whether it *would have* made money — using real stock prices, at no extra cost. Three of those checks are now queued and will report back around July 20th and 22nd.

**Next step:** no more spending needed for now. Between this test and the queued real checks, you'll have enough evidence in about a week to decide whether the auditor needs recalibrating, or whether the harder truth is that this strategy needs a better information source than SEC filing text alone.

---

## Process caveat (read this)

This handoff was written for a fresh, independent session so a second party could verify the work. Instead, I (Claude, the same session that wrote the handoff and normally plays auditor for this repo) executed it directly at your request. I've tried to hold myself to the same discipline — fail-before evidence captured, real entry points driven, no veto logic touched — but I could not independently audit myself the way the project's process normally requires. If you want the standard level of confidence before trusting this, the cheapest check is: run the full test suite yourself (`PYTHONPATH=. python3 -m unittest tests.test_core tests.test_audit_runner_gate tests.test_hood_evidence_vetoes tests.test_counterfactual_mtm -v`) and skim `git diff` for anything touching `auditor.py`'s veto logic (there should be none — only `reaction_layer.py`'s meta dict changed, one added line).

**Budget override note:** across the golden-case test (A3, $0.0675) and the pipeline follow-up (A4, $0.047), total real spend against your one-time authorization is **$0.1145**. Both ran in isolated scripts with their own separate budget ledgers (`/private/tmp/.../scratchpad/`, cap $0.50 each) specifically so neither would touch or corrupt `data_real/`'s shared production budget state that the live cron job (`run_oos.sh`) reads — today's $0.25 operational cap and tomorrow's fresh reset are both unaffected. All scripts are in the session scratchpad, not the repo, so there's nothing to clean up in the codebase. Also see `VERDICT_CALIBRATION_PREREGISTRATION_2026-07-17.md` for the pre-registered follow-on test (real counterfactual returns, N≥20) that will settle the question these hand-run tests could only partially address.
