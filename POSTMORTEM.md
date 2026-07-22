# POSTMORTEM — hood_agent_1

**Status: BURIED 2026-07-22.** Verdict: **NEVER PROVEN — retired at cost-of-proof, not disproven.**

## 1. Dates
- **Born:** 2026-07-06 (initial commit — AI-reasoning event-driven trading agent).
- **Last real work:** 2026-07-21 (Sonnet-5 handoff: auditor falsification test + counterfactual MTM).
- **Buried:** 2026-07-22.
- **Lifespan:** 16 days. **Live trades ever placed: 0.**

## 2. Thesis (one sentence)
When a microcap files an 8-K, an LLM pipeline (Haiku triage → Opus EV → Opus adversarial auditor)
can read the filing, estimate a probability-weighted edge, and trade the post-filing move before the
market fully prices it.

## 3. Verdict — NEVER PROVEN (auditor vindicated; alpha source absent)
No live test was ever run — 131/131 candidates ended in the graveyard, **zero positions taken**. The
bot's own adversarial auditor vetoed every thesis that reached it (0-for-record). The open question was
whether that auditor was **broken** (over-vetoing real edge) or **right** (the filings-only edge is
illusory). The pre-registered calibration test (`VERDICT_CALIBRATION_PREREGISTRATION_2026-07-17.md`,
threshold: mean realized return of vetoed +EV theses **>+2% ⇒ auditor miscalibrated**) resolved it.

**Receipt (2026-07-22, free resolution vs split-adjusted historicals, $0 spent):**

| ticker | veto | thesis upside | realized (veto-close → 07-21) |
|--------|------|--------------:|------------------------------:|
| SPT  | 07-15 | +10% | **−6.2%** |
| KLRS | 07-17 | +20% | **−7.8%** (intraday wick to $6.25 fully faded) |
| KRP  | 07-17 |  +5% | +0.8% |
| PSNL | 07-20 | +18% | +0.7% (stock **fell 13%** on the merger 8-K) |

**Mean −3.1%; 0 of 4 beat the +2% bar** (with a prior −9.1% sibling: 5 resolved, none >+2%). The
auditor was **right** on every one — it correctly rejected theses that all lost or fizzled. The
0-for-record is not a broken veto machine; it is a correct verdict on an **information diet too thin
to predict the move** (an 8-K stripped of deal price and buy-side context). The engineering was sound;
the alpha source was absent. Retired because the cost to reach a formal N≥20 test (a live LLM-call bug
to fix, budget override, weeks) is not justified against five straight correct vetoes.

Honest context on the census (why "0-for-10" was never the real story): of 131 graveyard rows, **89
were budget-starved** (never got a thesis) and **25 died on a `temperature`-deprecated API bug**
(2026-07-07→09) — only **15 were real auditor vetoes**, 4 of them +EV. The funnel spent most of its
life not running the experiment. That is why this is NEVER PROVEN, not DISPROVEN.

## 4. What it taught (transferred to the fleet)
- **The adversarial-auditor pattern works** — an independent Opus "red team" that must find specific,
  correct objections before capital moves. It caught real errors every time. This pattern has
  fleet-wide value and should be reused, even though this sleeve dies.
- **Counterfactual mark-to-market (G14):** resolving *rejected* theses against real prices to score a
  veto's calibration — a reusable honesty tool (used to render this very verdict).
- **Pre-registered calibration criteria** dated before data — the discipline that made this burial
  clean instead of a vibe.
- **The budget/API-bug census lesson:** "the model produced no trades" must be decomposed into "vetoed"
  vs "never ran" before you conclude anything. A starved funnel masquerades as a cautious one.
- **The signal-vs-fill / signal-vs-info-diet boundary:** a strategy can have perfect execution and
  reasoning and still have no edge because its *inputs* can't see what moves the price.

## 5. Revival condition (checkable, not a vibe)
**Revive only if hood is given an information source that carries what the filing omits** — i.e. a feed
that includes, at decision time, the **actual deal price / merger consideration** (for M&A 8-Ks) or a
**consensus-estimate bar** (for guidance 8-Ks), NOT filings text alone. Concretely: re-open only when a
backtest on ≥30 historical 8-Ks shows the auditor's vetoed +EV theses would have realized **mean >+2%**
once that richer input is supplied. Absent such a data source, **do not revive.** No amount of
prompt/architecture work on the filings-only diet is a revival trigger.

---
_Live trades: 0. LLM spend on this burial: $0. Code retained as reference (auditor + counterfactual
patterns). No further LLM spend authorized on this sleeve._
