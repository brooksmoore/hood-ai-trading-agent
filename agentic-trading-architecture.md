# Agentic Trading System — Architecture & Build Spec (v3, Alpha-Generation)
### For handoff to Grok Build | Target: < $1/day API spend · scale-aware · self-improving · hygienic · bug-resistant

> **v3 changelog (after multi-model adversarial review):** (1) Auditor rebuilt as a deterministic rule/screen core + a *Sonnet-tier* adversarial pass — fixes the "Haiku auditing Sonnet" rubber-stamp and correlated-failure problem. (2) Direct EDGAR filing ingestion replaces web-search for primary research. (3) Hard-coded spread/liquidity/halt execution veto added at the system layer. (4) Thesis schema reframed from "conviction" to **expected value (EV) distributions**. (5) **Graveyard DB** promoted to a first-class component. (6) Per-name ceiling set to **25%** with an **event-risk-adjusted cap nested underneath**. (7) A hard **EV-calibration gate** added before any live capital. The unresolved central risk — whether the engine generates real edge or just sophisticated narratives — is stated plainly and made the thing the calibration gate exists to test.

---

## 0. Read This First — What This System Is and Isn't

This specifies an AI agent connected to a **Robinhood Agentic Trading** account (isolated, sandboxed capital) via MCP. It is deliberately an **alpha-generation** system, not a risk-engineering one. The owner has explicitly chosen to pursue outperformance and accept the real drawdowns that come with it, within a hard ruin-prevention boundary.

**This is NOT quant trading.** Quant trading is statistical: an effect that holds across thousands of instances, sized by historical edge, where no single trade matters. This system is **AI-augmented, event-driven, discretionary** trading: a relatively small number of reasoned, situation-specific, high-conviction bets where the thesis is qualitative and an AI supplies the judgment at scale. This distinction is load-bearing — it dictates how the system is validated (process quality + attribution, NOT statistical backtest of a repeatable signal) and how it is sized (conviction-weighted, NOT variance-normalized across a large N). Building this as if it were quant would attach the wrong validation and sizing machinery and would fail. The only quant-flavored component is the small factor-core ballast (Section 3).

**Three grounding truths that still bind even in alpha mode:**

1. **The edge is reasoning depth on ignored situations, not speed or prediction.** Roughly 95% of backtested strategies fail live; transaction costs and spreads are the dominant killer of frequent-trading strategies. We do not compete on latency (lost to microwave towers before inference finishes) and we do not "pick winners." We win by doing deep, patient analysis on situations institutions can't or won't touch.

2. **The model is static; the system learns.** Claude's raw reasoning doesn't improve over time. What improves is the context around it — memory, universe, calibration, a documented track record. Self-improvement is a property of the surrounding system, human-gated, never autonomous rule-rewriting.

3. **Honest return profile.** This version will have real drawdowns — down double-digit months while SPY is up are expected and acceptable. The bet: asymmetric sizing + capacity-constrained edges + reaction speed produce enough on the winners to dominate over a multi-year horizon. The failure mode is no longer "anticlimactic mediocrity"; it is "right thesis, wrong timing, painful interim losses." This demands genuinely ring-fenced capital the owner will not pull at the bottom.

4. **The central, unresolved risk (stated plainly).** The architecture is almost certainly stronger than the alpha source. The whole system rests on one unproven claim: *that an AI can reason its way to superior microcap-event investing.* The skeptical version is real — in a thin name, the other side of your trade often knows management, talks to suppliers, and understands industry dynamics; you may not be beating retail, you may be the least-informed participant. No architecture fixes this. The only honest response is to **demand proof of calibrated edge before risking real capital** (Section 11 gate), and to accept that the engine may be producing sophisticated narratives around outcomes driven by factors it cannot observe. If the EV estimates aren't calibrated out-of-sample, the edge isn't there, and no amount of elegant plumbing saves it.

**Every major design decision below is stated with an adversarial counter-position before the final call** — the same discipline the Auditor agent applies to every trade.

---

## 1. The Alpha Thesis — Where the Edge Actually Lives

Most "AI trading edges" are already arbitraged. Every serious fund has had NLP transcript pipelines for years; reading filings fast is table stakes, not a moat. The genuinely defensible edges for a *small, scaling, retail* account are:

### PRIMARY ENGINE — Capacity-Constrained Small/Microcap Events (Option 1)
Hunt small and micro-cap names with thin analyst coverage, built around complex events: earnings, spin-offs, index inclusions, restructurings, FDA decisions, bankruptcy exits. **This is the one edge institutions structurally cannot compete away** — a $50M position would move the stock, but a small account won't. The surviving inefficiency lives precisely where arbitrage costs are highest and coverage is thinnest (well-supported in the PEAD/limits-to-arbitrage literature: drift and mispricing persist for small firms with few or no analysts and low share prices).

- *Why it's the real moat:* fishing where the big boats literally cannot follow. Free of the "everyone has NLP" problem because the names aren't worth a fund's time.
- *Honest risk:* liquidity cuts both ways — wide spreads, ugly exits on losers, higher fraud/blowup rate. This is the highest-alpha AND highest-operational-risk option. Defenses in Sections 6–7.

### REACTION LAYER — 24/7 Patient-Predator Monitoring (Option 2, layered into Option 1)
The runtime advantage, used correctly. The system monitors continuously — after-hours/overnight 8-Ks, news, developments — and does the deep read while humans sleep, then takes **one** well-reasoned position before the regular market opens. This is NOT micro-scalping (see Section 2, explicitly rejected). It is a patient predator: watches everything, strikes seldom, hard, and only on asymmetric odds. Detection is free (Tier-1 code); expensive reasoning fires only when something real triggers. Layered intelligently into the primary engine — most reaction events ARE small-cap event situations.

- *Why it works:* a structural edge built on *attention during low-participation hours*, not latency. Doesn't require beating Citadel.
- *Honest risk:* extended-hours liquidity is thin; dislocations can be illusory or reverse violently at the open; execution quality is worse off-hours. Sizing must be smaller for off-hours entries.

### THIN BALLAST — Liquid Conviction / Factor Core (small, 20–30%)
Just enough liquid, low-cost exposure (factor tilt + a few high-conviction liquid names you actually believe in) to not be naked and to give clean execution somewhere in the book. This is the *only* quant-flavored sleeve. It is deliberately a minority of capital — in alpha mode the conviction engine is the meal, not the garnish.

**Adversarial check on concentration in Option 1:** *"Thin names + concentration = one fraud wipes you out."* — Correct, and this is the genuine danger, not mere volatility. Mitigation: the 25% hard ceiling with an event-risk-adjusted cap underneath (Section 4) guarantees no single blindside is terminal; the two-part Auditor hunts both reasoning holes and hard risk flags; and capacity-awareness (Section 5) forces the universe toward liquidity as size grows. We accept volatility; we do not accept ruin.

---

## 2. Explicitly Rejected: Bloomberg-Terminal Micro-Scalping

This path is intuitive and wrong, and is documented here so it is **not reintroduced during build.** The "many small profits that stack up" model has three killers at this setup:

1. **Spreads don't scale down and are widest on our targets.** Robinhood equities are commission-free, but bid-ask spread is a real per-round-trip cost — largest exactly on the thin small-caps the primary engine targets. Frequent small profits get eaten by spread. Frequent-signal strategies that look profitable frictionlessly become unviable once realistic execution costs are included.
2. **It competes where retail is weakest.** Genuine micro-scalping is a latency contest lost to co-located servers before an LLM finishes inference. Any "quick trade" edge on public, liquid information is already arbitraged.
3. **It inverts our edge and wastes the budget.** Our advantage is *depth of reasoning on ignored situations* — slow and, at $1/day, rationed. Scalping demands shallow, fast, high-volume decisions, spending scarce intelligence where it adds least.

**The salvaged kernel:** the 24/7 *monitoring* instinct is right — captured as the Reaction Layer (Option 2). Separate monitoring (constant, free) from trading frequency (rare, high-conviction). Watch always; trade seldom.

---

## 3. Capital Allocation — Alpha-Mode Inversion

The risk-engineering version had a 60–75% ballast core with capped satellite afterthoughts. **Alpha mode inverts this:**

| Sleeve | Target range | Engine | Compute tier |
|---|---|---|---|
| Conviction (primary) | 50–70% | Option 1 small/microcap events + Option 2 reaction | T1 detect → T3 thesis |
| Liquid ballast | 20–30% | Factor tilt + liquid conviction names | mostly T1 |
| Dry powder (cash) | 10–20% | held for reaction-layer strikes | — |

Conviction is the majority of capital. Dry powder is a *feature*, not idle drag — it's ammunition for the patient predator to strike when overnight dislocations appear.

**Adversarial check:** *"50–70% in thin-coverage microcaps is reckless."* — It would be without the 25% per-name ceiling, the event-risk-adjusted cap, capacity-awareness, and the Auditor. With them, it's aggressive-but-survivable. The owner has explicitly chosen alpha over preservation; this allocation honors that choice while the ruin-prevention boundary keeps a bad single bet from ending the game.

---

## 4. Position Sizing — Two-Tier Ceiling (Ruin Prevention)

Three independent model reviews flagged the original 33% as venture-style concentration, correctly noting that for microcaps **fraud, halted trading, surprise secondary offerings, and delisting are normal events, not tail events** — a single position can gap to -70% overnight. The owner reviewed this and chose to lower the ceiling while keeping genuine aggression. Result is a **two-tier cap**:

**Tier 1 — Absolute hard ceiling: 25% of the agentic sleeve at peak.** No position ever exceeds this regardless of how clean it looks. Four names could be the entire conviction sleeve — still genuinely concentrated, but no single blindside is terminal.

**Tier 2 — Event-risk-adjusted cap (nested underneath, enforced deterministically by the Risk Controller, NOT by LLM judgment):** the *actual* allowed size for a given name is the lower of 25% and a risk-adjusted figure driven by hard, observable flags:
- Going-concern language / recent dilution / active S-1 or S-3 on file → cap ~10–15%
- Thin float / low average daily volume / wide spread → cap scaled down by liquidity
- Recent halt history / pending binary event (FDA, bankruptcy ruling) → cap reduced
- Only clean, liquid, lower-event-risk conviction names may approach the full 25%

This honors aggression *where it is survivable* and reins it in *precisely where the blindside risk is real* — a smarter cap than a single blanket number.

Everything else that *suppresses* returns is still loosened or cut: tight volatility targeting (cut), hard satellite caps (replaced by the two-tier ceiling), forced diversification (replaced by EV-weighting). The Risk Controller's mandate is **"prevent ruin only."**

**Adversarial check:** *"25% is still aggressive; reviewers suggested 10–20%."* — True, and the event-risk-adjusted Tier-2 cap is what closes that gap: the dangerous names *are* held to 10–15%, while only the cleanest approach 25%. A flat 15% would needlessly suppress size on a liquid, low-event-risk thesis; the two-tier structure targets the cap to the actual risk. The owner explicitly accepts the residual aggression.

---

## 5. Scale-Awareness — From Fractional Shares to a Real Book

The system starts very small (fractional shares) and must scale much larger without silently breaking. **A strategy that works at $500 can be impossible at $50k** — the position would move a thin stock, or the liquidity simply isn't there.

**Capacity-Awareness Layer (mandatory):**
- Every candidate thesis is tagged with an estimated *tradeable capacity* at current portfolio size (function of the name's average daily volume and spread).
- As capital scales, the eligible universe **shifts automatically from the thinnest names toward more liquid ones.** The thinnest-microcap edge is a *small-capital* edge; the system must know it is outgrowing it.
- The system **flags when a thesis is untradeable at current size** and routes that capital to the next-best tradeable idea or to ballast, rather than forcing a position that moves the market against itself.
- Fractional-share logic at the low end; lot/liquidity-aware sizing at the high end. Same code path, size-parameterized.

**Adversarial check:** *"This adds complexity early when the account is tiny."* — The capacity *check* is cheap (Tier-1 math on volume/spread). Building it in from day one costs little and prevents the most predictable scaling failure: a strategy that quietly stops working as the account grows and nobody notices until a position can't be exited. Non-negotiable.

---

## 6. Cost Model — Proving < $1/Day Holds Even in Alpha Mode

### Verified pricing (Anthropic API, mid-2026)

| Model | Input ($/Mtok) | Output ($/Mtok) | Role |
|---|---|---|---|
| Haiku 4.5 | $1.00 | $5.00 | Tier-2 triage, classification, risk check, audit |
| Sonnet 4.6 | $3.00 | $15.00 | Tier-3 thesis construction, weekly meta-review |
| Opus 4.7/4.8 | $5.00 | $25.00 | Avoid for now (budget tabled by owner) |

**Levers (all verified):** prompt caching = cache reads at 0.1× input (90% off), writes at 1.25–2×, breaks even after ~2 reuses; Batch API = flat 50% off, async within 24h; model routing = Haiku 3× cheaper than Sonnet; stacking batch + caching ≈ 95% off eligible workloads.

**Why alpha mode is actually CHEAPER per day than the broad version:** concentration means *fewer positions, fewer theses, fewer Tier-3 calls* — but each is deeper. We spend the intelligence budget concentrated, not spread. This is the key insight that lets aggression and the budget coexist.

### Worked daily budget (active day)

| Workload | Tier | Model | Calls/day | Tokens (in/out) | Cost |
|---|---|---|---|---|---|
| Continuous 24/7 monitoring | 1 | code | thousands | 0 | $0.00 |
| Event/news triage (is this real?) | 2 | Haiku (batch) | ~30 | 1,200 / 250 | ~$0.04 |
| Reaction-layer trigger eval | 2 | Haiku | ~15 | 900 / 200 | ~$0.02 |
| Deep thesis (high-conviction only) | 3 | Sonnet (cached sys) | ~3 | 8,000 / 1,500 | ~$0.10 |
| Capacity check per candidate | 1 | code | ~20 | 0 | $0.00 |
| Risk (ruin) check per trade | 2 | Haiku | ~8 | 600 / 200 | ~$0.02 |
| Auditor challenge per trade | 2 | Haiku | ~5 | 1,200 / 450 | ~$0.03 |
| Weekly meta-review (amortized) | 3 | Sonnet (batch) | ~1 | 15,000 / 3,000 | ~$0.04 |
| **Total** | | | | | **~$0.25/day** |

**Circuit breaker:** a hard daily token budget enforced in code. At 80% consumption the system stops initiating new Tier-3 reasoning and falls back to Tier-1 rules + existing theses. The budget is a circuit breaker, not a suggestion. Even a tripled volatile day stays under ~$0.80.

---

## 7. Agent Topology — One Job Per Agent

Strict separation of concerns = hygiene AND cost control (minimal context = minimal tokens). Stateless agents; state lives in storage.

```
┌──────────────────────────────────────────────────────────────┐
│ HUMAN — weekly review · strategy/rule changes only ·            │
│         approves Meta proposals · KILL SWITCH                   │
└────────────────────────────┬───────────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────────┐
│ RISK CONTROLLER (deterministic core + T2 Haiku) — RUIN mandate   │
│ 25% hard ceiling · event-risk-adjusted cap (rule-based) ·        │
│ ruin scenarios · halt/dilution/going-concern flags               │
│ HARD VETO. Reads positions + filings flags, never news.          │
└────────────────────────────┬───────────────────────────────────┘
                             │
        ┌────────────────────┼─────────────────────┐
        │                    │                      │
┌───────▼────────┐  ┌─────────▼─────────┐  ┌─────────▼─────────┐
│ EV ENGINE      │  │ REACTION LAYER    │  │ BALLAST ENGINE    │
│ (primary)      │  │ (24/7 patient-    │  │ (T1 factor/liquid)│
│ small/microcap │  │ predator monitor) │  │                   │
│ events:        │  │ T1 detect →       │  │                   │
│ T1 scan +      │  │ T3 deep read of   │  │                   │
│ EDGAR ingest → │  │ raw filing →      │  │                   │
│ T3 EV estimate │  │ EV estimate       │  │                   │
│ + CAPACITY tag │  │ + CAPACITY tag    │  │                   │
└───────┬────────┘  └─────────┬─────────┘  └─────────┬─────────┘
        └────────────────────┼──────────────────────┘
                             │ candidates (JSON: EV dist + capacity)
                  ┌───────────▼───────────────────────┐
                  │ AUDITOR (two-part, stateless)       │
                  │ (a) DETERMINISTIC SCREENS (T1 code):│
                  │     dilution/S-3·float·halt·short   │
                  │     interest·going-concern·spread   │
                  │ (b) Sonnet-tier ADVERSARIAL pass:   │
                  │     bull+bear·EV challenge·holes    │
                  │ Different prompt/seed from EV Engine │
                  └───────────┬───────────────────────┘
                             │ survivors
                  ┌───────────▼───────────┐
                  │ EXECUTOR (T1 + MCP)    │
                  │ HARD spread/liquidity/ │
                  │ halt VETO (coded) ·    │
                  │ fractional/lot-aware · │
                  │ idempotency keys ·     │
                  │ re-checks caps · never │
                  │ reasons                │
                  └───────────┬───────────┘
                             │
                  ┌───────────▼───────────┐      ┌──────────────────┐
                  │ PERSISTENT LOG         │─────▶│ GRAVEYARD DB      │
                  │ EV·sizing·regime·      │      │ every rejected/   │
                  │ capacity·outcome       │      │ failed/won trade ·│
                  └───────────┬───────────┘      │ queryable for     │
                             │ weekly, batch     │ failure patterns  │
                  ┌───────────▼───────────┐      └────────┬─────────┘
                  │ META-REVIEWER (T3      │◀──────────────┘
                  │ Sonnet, batch)         │ mines Graveyard:
                  │ EV CALIBRATION check · │ "what stories fool us?"
                  │ proposes rule changes  │ → HUMAN
                  └────────────────────────┘
```

**Adversarial check:** *"Six agents is over-engineered for a tiny account."* — A single fat agent carries huge context per call (high cost), mixes concerns (a research hallucination directly causes an order), and yields unstructured logs that can't drive learning. The decomposition pays for itself in cost, safety, and debuggability. Built in phases (Section 10) so complexity is staged.

---

## 8. The Intelligence Layer — EV, Not Conviction

The word "conviction" is psychological; **expected value is mathematical**, and the system is built around it. Every candidate trade must be expressed as a distribution, not a confidence score. This makes the Auditor's job concrete and the Meta-Reviewer's skill-vs-luck separation tractable. Concentration is what *funds* real intelligence per position under the budget.

**The mandatory thesis schema (every engine emits this):**
```json
{
  "ticker": "...",
  "event_type": "spinoff | earnings | restructuring | ...",
  "upside_pct": 0.0, "p_upside": 0.0,
  "downside_pct": 0.0, "p_downside": 0.0,
  "expected_value_pct": 0.0,          // computed, not asserted
  "prior_accuracy_on_name": 0.0,      // self-scored hit-rate
  "what_informed_holders_may_know_that_we_dont": "...",  // forced humility field
  "tradeable_capacity_usd": 0,
  "event_risk_flags": ["S-3_on_file", "thin_float", ...],
  "source_filings": ["EDGAR accession #s"]
}
```
The `what_informed_holders_may_know` field is deliberate: it forces the engine to confront Section 0's central risk on every trade — that the marginal holder of a thin name often has real information advantages (knows management, suppliers, industry). A thesis that can't articulate what it might be missing is itself a red flag.

- **EV Engine builds a forward model per name.** Records each EV estimate, then scores its own priors against realized outcomes and weights future estimates by its own calibration on that name.
- **Reaction Layer does deep off-hours reads of raw EDGAR filings** (not fast ones, not web summaries) — the edge is *quality of interpretation during low-attention hours*, e.g. parsing a 2am 8-K's real implications before the open.
- **Risk Controller runs adversarial ruin-scenario simulation:** "what breaks this book if the three highest-EV theses are wrong, or if the most concentrated name halts/dilutes?" Outputs a ruin-stress score.
- **Auditor is rebuilt as two parts to fix the "weaker-model-checks-stronger-model" fallacy:**
  - *(a) Deterministic screens (Tier-1 code, free, no LLM):* dilution/recent S-1/S-3 on file, going-concern language, float, average daily volume, spread, short interest, halt history. These catch the *blindsides that matter most* — exactly the ones an LLM auditor rubber-stamps — without depending on reasoning at all.
  - *(b) Sonnet-tier adversarial pass* (not Haiku): runs bull and bear simultaneously, challenges the EV distribution, hunts logical holes — using a **different prompt and seed** from the EV Engine to reduce correlated failure (same-model-family blind spots). It is structurally tasked only with finding reasons NOT to trade.

**Adversarial check:** *"Forward models = overfitting machines; and an LLM auditor of similar capability shares the same blind spots."* — Both real. The deterministic screen core addresses the second (rule-based checks can't share LLM hallucinations); the human-gated learning loop (Section 9) with multi-regime evidence and distrust of recency addresses the first.

---

## 9. Self-Improvement Loop — Real, Human-Gated

```
Trade executes (or is rejected)
 → Log to PERSISTENT LOG + GRAVEYARD DB:
     {EV_estimate, realized_outcome, sizing, regime_tag, capacity,
      event_risk_flags, reject_reason if rejected}
 → Weekly Meta-Reviewer (Sonnet, batch):
     • EV CALIBRATION check: did +EV-rated trades actually realize +EV
       out-of-sample, after modeled slippage? (the core edge test)
     • mines GRAVEYARD: "what kinds of stories consistently fool this system?"
     • separates skill from luck / regime
 → Proposes SPECIFIC rule changes WITH multi-regime evidence
 → HUMAN approves / rejects (short, evidence-backed, accept/reject)
 → Versioned, reversible deployment of prompt/parameter changes
```

**The Graveyard DB is a first-class component, not a log.** Every rejected, failed, and successful trade is stored and queryable. The most durable future edge may not be finding winners — it may be discovering the recurring *story patterns* that fool the EV Engine (a certain restructuring narrative, a class of guidance language, a promoter profile). The Meta-Reviewer mines it explicitly for these patterns and proposes screens to catch them.

What compounds: EV calibration on the universe (prune names/event-types the engine misjudges), prompt quality, ruin-risk calibration, regime detection from *your* book, capacity calibration as you scale, and the Graveyard's growing catalogue of failure patterns.

**Guardrails against the back-door overfitting failure:**
- No autonomous rule changes, ever. Human gate is the safety mechanism.
- Changes require evidence across **multiple regimes** — "didn't work last quarter" is explicitly insufficient (that's how you de-risk right before the bounce).
- All versions reversible.
- Meta-Reviewer must separate skill from luck — reports process quality, not just P&L. (Critical here because low-N discretionary trading is noisy; a right thesis can lose and a wrong one can win.)

**Adversarial check:** *"A weekly human gate defeats autonomy / the human becomes the bottleneck."* — Autonomy is in *execution* (trades 24/7 within mandate). The gate is only on *changing the mandate* — exactly how a desk works. The bottleneck risk is real (rubber-stamping or neglect); mitigation: the Meta-Reviewer must present changes as short, evidence-backed, accept/reject decisions, not open-ended reports, so review stays low-effort and honest.

---

## 10. Data Ingestion — Raw Filings, Not Web Search

Microcap event trading lives and dies on raw, instantaneous, primary-source data. Generic web search returns delayed secondary summaries, introduces noise, and will miss the structural text changes in a late-night 8-K or S-3 that *are* the signal. **The research pipeline ingests primary sources directly:**

- **SEC EDGAR full-text search + filing feeds** as the primary source. Parse the actual 8-K / 10-Q / 10-K / S-1 / S-3 / 13D/G text, not search snippets. This is also where dilution, going-concern, halt, and ownership-change signals live — feeding both the EV Engine and the Auditor's deterministic screens.
- **Earnings calendar + corporate-action feeds** for event detection (Tier-1 triggers).
- **Real-time quote/volume/spread** via the Robinhood MCP for capacity tagging and the execution-safety gate.
- Web search is demoted to *context enrichment only* (industry background), never the primary signal, and never the source of record for an event.

This is cheaper and cleaner than web search, and it directly closes the "reading delayed summaries" failure mode three reviewers flagged.

**Adversarial check:** *"EDGAR parsing of messy microcap filings is hard and error-prone."* — True; microcap filings are often poorly formatted. Mitigation: filings are parsed to structured fields where possible and the raw text is always retained and passed to the Tier-3 read; the deterministic screens key off presence/absence of specific filing *types* (S-3 filed = dilution risk) which is robust even when prose parsing is imperfect.

---

## 11. Execution-Safety Gate — Hard-Coded, Pre-LLM

A toxic overnight spread on a thin name can erase the entire modeled edge before the regular market opens. This guardrail lives at the **system layer, independent of any LLM reasoning** — the Executor cannot place an order that fails it, regardless of how high the EV estimate is.

```python
def validate_execution_safety(
    bid: float,
    ask: float,
    avg_daily_volume: float,
    order_size_shares: float,
    is_halted: bool,
    max_allowed_spread_pct: float = 0.02,   # 2% default; tighter for larger size
    max_pct_of_adv: float = 0.05,           # never >5% of avg daily volume
) -> tuple[bool, str]:
    """Hard structural veto before order construction. Returns (ok, reason)."""
    if is_halted:
        return (False, "halted")
    if bid <= 0 or ask <= 0:
        return (False, "no_two_sided_market")
    mid = (ask + bid) / 2.0
    spread_pct = (ask - bid) / mid
    if spread_pct > max_allowed_spread_pct:
        return (False, f"spread_too_wide_{spread_pct:.3f}")
    if avg_daily_volume <= 0 or (order_size_shares / avg_daily_volume) > max_pct_of_adv:
        return (False, "order_too_large_vs_liquidity")
    return (True, "ok")
```

Off-hours entries use a *tighter* `max_allowed_spread_pct` and smaller size; full size requires regular-hours confirmation of the thesis. This complements (does not replace) the capacity-awareness layer and the event-risk-adjusted cap.

---

## 12. Bug-Resistance & Failure Modes

| Failure mode | Defense |
|---|---|
| Hallucinated thesis → bad order | Two-part Auditor (deterministic screens + Sonnet-tier adversarial pass) must clear every trade; JSON schema validation rejects malformed EV distributions |
| Microcap fraud / surprise dilution / halt | 25% ceiling + event-risk-adjusted cap (10–15% on flagged names); deterministic Auditor screens (S-3/going-concern/halt history); ruin-stress score |
| "Weaker model rubber-stamps stronger" | Auditor core is rule-based (no LLM); LLM pass is Sonnet-tier with different prompt/seed to break correlated failure |
| Reading delayed/secondary data | Direct EDGAR ingestion of primary filings; web search demoted to context only |
| Toxic overnight spread eats edge | Hard-coded `validate_execution_safety` veto (Section 11), pre-LLM, cannot be overridden by EV |
| Runaway API spend | Hard daily token budget in code; graceful degradation at 80% |
| Strategy silently outgrows its liquidity | Capacity-Awareness layer; untradeable-at-size flag (Section 5) |
| Thin-name bad fill / ugly exit | Capacity + spread + ADV check before entry; smaller size off-hours |
| Duplicate / double orders | Idempotency keys; Executor checks open orders before placing |
| MCP auth failure / new-product bugs | Catch, halt new orders, alert human, do NOT blind-retry; schema-validate all MCP responses (product is new, expect bugs) |
| Stale data → trading old prices | Timestamp every datapoint; reject decisions on data past freshness threshold |
| Position-size violation | Risk Controller hard veto BEFORE Executor; Executor re-checks independently (defense in depth) |
| Overnight dislocation reverses at open | Smaller off-hours sizing; thesis must survive to regular-hours confirmation for full size |
| Regime shift mid-strategy | Regime detector can force de-risk; EV engine pauses new entries on extreme-vol signal |
| Silent agent failure | Heartbeat + structured logging; erroring agent halts its branch and alerts |
| Back-door overfit | Human gate + multi-regime evidence requirement + Graveyard pattern review |
| **Engine has no real edge (the central risk)** | **EV-calibration gate (Section 13) before live capital — if +EV trades don't realize +EV out-of-sample after slippage, do not deploy** |

**Mandatory validation before ANY live capital** (adapted for discretionary, not quant): since you can't statistically backtest a low-N discretionary strategy, validate on **process, attribution, and EV calibration** — paper-trade the full workflow with *realistic slippage modeling* (microcap paper fills must assume bad fills, partial fills, overnight gaps), score whether theses were sound and EV estimates were calibrated (separating skill from luck), confirm plumbing/risk/kill-switch fire correctly, THEN tiny live, THEN scale only on demonstrated calibration. Out-of-sample paper operation is non-negotiable; do not scale on a handful of lucky wins.

---

## 13. Phased Rollout — De-Risk the Build Itself

- **Phase 0 — Plumbing (no alpha).** MCP connection, append-only logging + Graveyard DB, kill switch, daily-budget circuit breaker, Tier-1 monitoring loop, capacity-check math, and the hard-coded `validate_execution_safety` gate. Run near-passive ballast with fractional shares. Prove orders, logging, risk triggers, and kill switch on a system that can barely lose.
- **Phase 1 — Risk + Ballast + Capacity + EDGAR.** Add Risk Controller (ruin mandate + two-tier cap), ballast factor sleeve, full capacity-awareness layer, and the EDGAR ingestion pipeline. Validate process pipeline end to end.
- **Phase 2 — EV Engine + two-part Auditor.** Add the small/microcap event engine emitting EV distributions + the deterministic-screen-plus-Sonnet Auditor on every trade. Paper-trade with realistic slippage.
- **Phase 3 — Reaction Layer + Meta loop.** Layer in 24/7 patient-predator monitoring and the weekly human-gated Meta-Reviewer mining the Graveyard. Now the system learns.
- **Phase 4 — THE EV-CALIBRATION GATE (hard gate, not a phase).** Before any meaningful live capital: demonstrate over an extended out-of-sample paper period that trades the engine rated +EV *actually realized +EV after modeled slippage.* If calibration fails, the alpha source is unproven — do NOT deploy capital, regardless of how good the architecture is. This gate exists specifically to test Section 0's central risk.
- **Phase 5 — Scale.** Only after the calibration gate passes and months of clean operation, scale capital — watching the capacity layer shift the universe toward liquidity as size grows.

---

## 14. Tech Stack (for Grok Build)

- **Reasoning:** Sonnet 4.6 for Tier-3 (EV Engine + Auditor adversarial pass + Meta-Reviewer), Haiku 4.5 for cheap Tier-2 triage. Opus tabled (budget). 
- **Orchestration:** stateful graph (LangGraph-style) for checkpoints + human-in-the-loop gates, or a lean custom orchestrator if minimizing dependencies.
- **Data:** SEC EDGAR full-text search + filing feeds (primary), earnings/corporate-action calendar, Robinhood MCP real-time quotes/volume/spread. Web search = context enrichment only.
- **MCP:** Robinhood Trading MCP for portfolio data + execution. Note it is a brand-new product — schema-validate every response and expect early bugs.
- **Storage:** append-only decision log + Graveyard DB (queryable) + key-value/vector store for processed filings (process once, retrieve many — never re-read a filing).
- **Cost controls:** prompt caching on all static context; Batch API for overnight + weekly workloads; hard token-budget circuit breaker in code.
- **Discipline:** everything stateless + JSON-schema'd per agent; event-driven (subscribe to MCP push + calendar), never polling; hard-coded execution-safety veto independent of any LLM.

---

## 15. One-Paragraph Build Brief

Build a phased, scale-aware, multi-agent **alpha-generation** trading system on Robinhood Agentic Trading, framed honestly as AI-augmented event-driven (NOT quant). The primary EV engine hunts capacity-constrained small/microcap event situations — the one edge institutions can't arbitrage away — reading raw EDGAR filings directly (not web summaries) and expressing every trade as an expected-value distribution rather than a "conviction" score, with a 24/7 patient-predator reaction layer that monitors constantly (free, Tier-1 code) but trades seldom and only on asymmetric, deeply-reasoned setups. Concentration funds real intelligence per position and keeps spend reliably under $1/day (~$0.25 typical) via tiering, caching, batching, and a hard budget circuit breaker. A thin liquid ballast (20–30%) gives clean execution; dry powder is ammunition. Position sizing uses a 25% hard ceiling with an event-risk-adjusted cap nested underneath (flagged names held to 10–15% by deterministic rule); the Risk Controller's mandate is "prevent ruin only." The Auditor is two-part — deterministic non-LLM screens (dilution/halt/going-concern/float) plus a Sonnet-tier adversarial pass with a different prompt/seed — fixing the "weaker-model-rubber-stamps-stronger" fallacy and correlated failure. A hard-coded execution-safety veto blocks toxic spreads before any order. A mandatory capacity layer shifts the universe toward liquidity as capital scales (fractional shares → real book). A first-class Graveyard DB plus a human-gated weekly meta-loop let the *system* improve via multi-regime evidence and explicit hunting for story-patterns that fool the engine. Micro-scalping is explicitly rejected. **The central risk is that the architecture is stronger than the alpha source: before live capital, a hard EV-calibration gate must show that +EV-rated trades actually realize +EV out-of-sample after slippage — if not, do not deploy.** Expect real drawdowns; the wager is asymmetric winners over a multi-year horizon on genuinely ring-fenced capital. Not financial advice.
