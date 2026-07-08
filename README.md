# hood — AI-Reasoning Event-Driven Trading Agent

A discretionary, LLM-driven trading agent built around a hard question: can an AI reason
its way to real trading edge on situations institutions largely ignore — small/microcap SEC
filings (8-Ks, going-concern notices, etc.) — rather than competing on speed or statistical
signal? Paper-trading only; live capital requires an explicit human sign-off gate that the
system cannot bypass on its own.

## The core idea

Most profitable trading strategies are either fast (latency arbitrage) or statistical (an edge
that holds across thousands of repeated instances). This project deliberately does neither. It
bets that a language model can do deep, patient, situation-specific analysis on small-cap SEC
filings that are too illiquid or too obscure for institutional capital to bother with — and
that the resulting judgment, sized honestly and validated out-of-sample, can be worth something.

That framing shapes every design decision below: no backtesting a repeatable statistical signal,
no chasing latency. Instead: a real EDGAR filing feed, an LLM that produces an explicit
expected-value thesis (not just a conviction score), a deterministic safety layer the model
cannot argue its way around, and a forward-only calibration gate that has to actually pass on
real, not-yet-seen outcomes before any human even considers flipping the live switch.

## Why this is a harder engineering problem than "call an LLM and trade"

- **The model must not grade its own homework.** An `Auditor` component runs deterministic,
  non-LLM screens (halt status, going-concern flags, float size, etc.) *before* any adversarial
  LLM review — the cheap, certain checks run first and can't be reasoned around by a more
  articulate model.
- **A hard-coded execution veto that EV can't override.** Spread, average-daily-volume, and
  halt-status checks live at the broker-call boundary itself, beneath the reasoning layer
  entirely. A thesis with a great narrative still can't place an order into an illiquid, halted,
  or wide-spread name.
- **The live-trading gate is a one-way, human-only switch.** `SafetyCore.is_live_enabled()`
  starts `False`; the only path to `True` is an explicit human call after a forward-only
  calibration gate passes on real accumulated evidence — not in-sample backtest performance.
  This check runs before sizing, before quotes, before any LLM call, and the broker client
  itself raises if somehow bypassed (see `INVARIANTS.md`).
- **A `Graveyard` database of every rejected/failed thesis**, not just executed trades — the
  point is to be able to ask "what did the model almost do, and why did it get vetoed" just as
  rigorously as "what did it actually do."
- **Fail-safe telemetry.** Emitting a decision record for analysis must never be able to break
  the trading path itself — a broken logging call degrading into a dropped trade would be a
  much worse bug than the logging failure itself.

## Architecture

```
src/
  agents/                 # LLM-facing thesis generation
  core/
    schemas.py             # EV-distribution thesis schema (not a bare conviction score)
    auditor.py              # deterministic screens + adversarial LLM review, in that order
    safety_core.py            # the human-only live-gate invariant
    risk.py                    # per-name cap + event-risk-adjusted nested cap
    calibration_gate.py          # forward-only, out-of-sample promotion gate
    reaction_layer.py              # entry timing / reaction-window logic
    executor.py                     # hard-coded spread/ADV/halt veto at the broker boundary
    ev_engine.py                     # expected-value sizing
    ballast.py                        # small factor-core passive sleeve
    decision_emit.py                   # fail-safe decision/telemetry logging
    mcp/robinhood_client.py             # broker client (mock + real MCP-backed)
    data/edgar.py                        # real SEC EDGAR filing feed
run_paper.py              # the real paper-trading runner (--real, --feed dynamic, etc.)
tests/                   # safety-invariant gates, replay integrity, evidence-based vetoes
agentic-trading-architecture.md   # the original design spec
INVARIANTS.md                      # the safety properties that must never regress
```

## Run it

```bash
# Tests (hermetic, no network, no credentials)
PYTHONPATH=. python3 -m unittest discover tests -v

# Fake/demo path
PYTHONPATH=. python3 run_phase0.py

# Real paper session (needs an Anthropic API key and Python 3.10+ for the real SDK)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
PYTHONPATH=. python3 run_paper.py --real --feed dynamic --market-days-only
```

## Status

Paper-only. The out-of-sample calibration clock (real filings → real Haiku triage → real
outcomes) is actively accumulating evidence; the live-trading gate stays off until it clears a
pre-registered bar on that forward evidence, not backtest performance. See `INVARIANTS.md` for
the specific safety properties this system is designed to never let a future change weaken.

**Not financial advice.**

---

*This agent is one of a small fleet of AI-built trading systems run under a read-only supervisory layer, built through a multi-model process (one model builds, a second audits independently). The process is documented in the [case study](https://github.com/brooksmoore/ai-orchestration-case-study).*
