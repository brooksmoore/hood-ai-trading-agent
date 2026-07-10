"""LLM client seam for Phase 2+ (G1-G7 guardrails).

- Injectable: FakeLLMClient for 100% offline tests (records calls, returns canned).
- RealAnthropicClient (optional SDK) for prod/paper smoke.
- Every call goes through budget gate (can_spend + can_start_tier3 for T3), kill check, injection defense notes, schema (caller), etc.
- Supports cache markers for static system (G2).
- Records actual usage post-call (G1).
- Batch choice explicit (for non-urgent, caller decides; here we support sync primarily for decision paths).

Pin: anthropic==0.45.2 (only new dep; see README).

Model IDs centralized (update for current Sonnet 4.6 / Haiku per spec).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

# Phase R: run-mode models are OPERATOR-CONFIGURABLE via env (no code edit, no stale pin).
# Set HOOD_SONNET_MODEL / HOOD_HAIKU_MODEL / HOOD_OPUS_MODEL in the environment to override.
# Cost-efficiency mandate (cheap-model-first): Haiku 4.5 is the high-frequency triage model;
# Opus 4.8 is reserved for the rare genuine trade decision (EV thesis + adversarial audit).
# Sonnet is kept only as a legacy fallback constant (no live call site uses it after this change).
DEFAULT_SONNET_MODEL = os.getenv("HOOD_SONNET_MODEL", "claude-3-5-sonnet-20241022")
DEFAULT_HAIKU_MODEL = os.getenv("HOOD_HAIKU_MODEL", "claude-haiku-4-5")
DEFAULT_OPUS_MODEL = os.getenv("HOOD_OPUS_MODEL", "claude-opus-4-8")

from .budget import DailyBudget, estimate_cost
from .schemas import EVThesis  # for validation in callers


class LLMClient(Protocol):
    """Minimal seam. Implementations must enforce guards.
    P3-C: added batch support (G3) for non-urgent (Meta-Reviewer); Reaction uses sync.
    """

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        cache_system: bool = True,
        is_tier3: bool = True,
        workload: str = "t3_thesis",
        use_batch: bool = False,
    ) -> dict[str, Any]:
        """Return {'text': str, 'usage': {...}, 'raw': ..., 'batch_id'?: str if submitted}"""
        ...

    def create_batch(self, requests: list[dict], description: str = "") -> str:
        """Submit batch job. Return batch_id. For urgent=Reaction use complete(use_batch=False); Meta uses batch."""
        ...

    def retrieve_batch(self, batch_id: str) -> dict[str, Any]:
        """Poll/retrieve results. Returns {'status': , 'results': list of responses or errors}"""
        ...


@dataclass
class FakeCall:
    model: str
    system: str
    user: str
    timestamp: float = field(default_factory=time.time)


class FakeLLMClient:
    """Offline fake for all tests. Canned responses by key or default.
    Records every attempted call (even if gated).
    """

    def __init__(self, budget: Optional[DailyBudget] = None, is_killed: Optional[Callable[[], bool]] = None):
        self.budget = budget
        self.is_killed = is_killed or (lambda: False)
        self.calls: list[FakeCall] = []
        self._canned: dict[str, str] = {}  # key -> response text
        self._batches: dict[str, dict] = {}
        # Mandate 4: simulate prompt-cache behavior hermetically (no network). The first call
        # with a given (cache_system=True) system text is a cache WRITE (cache_creation); any
        # later call with the byte-identical system text is a cache HIT (cache_read). This lets
        # cache-effectiveness be asserted in tests without a live API call.
        self._seen_system_prompts: set[str] = set()

    def set_canned(self, key: str, text: str) -> None:
        self._canned[key] = text

    def _get_canned(self, user: str) -> str:
        # simple heuristic: if user mentions specific, use; else default valid
        for k, v in self._canned.items():
            if k in user:
                return v
        # default: a minimal valid EV json for tests (callers can override)
        return json.dumps({
            "ticker": "TEST",
            "event_type": "8k",
            "upside_pct": 15.0,
            "p_upside": 0.4,
            "downside_pct": -20.0,
            "p_downside": 0.3,
            "expected_value_pct": 0.0,  # will be recomputed by caller
            "prior_accuracy_on_name": 0.5,
            "what_informed_holders_may_know_that_we_dont": "Informed holders likely know about unannounced contract wins or related party issues not visible in this filing.",
            "tradeable_capacity_usd": 5000.0,
            "event_risk_flags": [],
            "source_filings": ["0000000-24-000001"],
        })

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        cache_system: bool = True,
        is_tier3: bool = True,
        workload: str = "t3_thesis",
        use_batch: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(FakeCall(model=model, system=system, user=user))
        # G6: kill check (even for fake, to test)
        if self.is_killed():
            return {"text": "", "usage": {"input_tokens": 0, "output_tokens": 0}, "raw": None, "error": "killed"}

        # G1: budget gate (use estimate) - use price_key_for_model via estimate_cost (supports full real IDs like claude-sonnet-4-6)
        est = estimate_cost(workload, model)
        if self.budget and not self.budget.can_spend(est):
            return {"text": "", "usage": {"input_tokens": 0, "output_tokens": 0}, "raw": None, "error": "budget_refused"}
        if self.budget and is_tier3 and not self.budget.can_start_tier3():
            return {"text": "", "usage": {"input_tokens": 0, "output_tokens": 0}, "raw": None, "error": "budget_degrade"}

        text = self._get_canned(user)
        # simulate some tokens, with cache behavior (Mandate 4) tied to the system text
        cache_creation = 0
        cache_read = 0
        if cache_system and system:
            if system in self._seen_system_prompts:
                cache_read = 900  # warm hit: most of the static system prefix served from cache
            else:
                cache_creation = 900  # cold: writing the prefix to cache for next time
                self._seen_system_prompts.add(system)
        usage = {
            "input_tokens": 300,  # the volatile, non-cached remainder (user content)
            "output_tokens": 400,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        }
        if self.budget:
            self.budget.record_usage(
                model, usage["input_tokens"], usage["output_tokens"],
                cache_creation_input_tokens=cache_creation, cache_read_input_tokens=cache_read,
            )
        if use_batch:
            # simulate immediate for fake, but mark as batch
            bid = f"fakebatch-{len(self.calls)}"
            self._batches[bid] = {"status": "completed", "results": [{"text": text, "usage": usage}] }
            return {"text": text, "usage": usage, "raw": None, "batch_id": bid, "discounted": True}
        return {"text": text, "usage": usage, "raw": None}

    def create_batch(self, requests: list[dict], description: str = "") -> str:
        bid = f"fakebatch-{int(time.time()*1000)}"
        # simulate processing: store canned results
        results = []
        for req in requests:
            txt = self._get_canned(req.get("user", "")) if hasattr(self, '_get_canned') else "{}"
            results.append({"text": txt, "usage": {"input_tokens": 800, "output_tokens": 300}, "discounted": True})
        self._batches[bid] = {"status": "completed", "results": results, "desc": description}
        return bid

    def retrieve_batch(self, batch_id: str) -> dict[str, Any]:
        return self._batches.get(batch_id, {"status": "not_found", "results": []})


class AnthropicLLMClient:
    """Real client using anthropic SDK (pinned 0.45.2).
    Enforces all G1-G7 before/after call.
    Requires: pip install "anthropic==0.45.2"
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        budget: Optional[DailyBudget] = None,
        is_killed: Optional[Callable[[], bool]] = None,
    ):
        self.budget = budget
        self.is_killed = is_killed or (lambda: False)
        try:
            import anthropic  # noqa
            self._anthropic = anthropic
            self.client = anthropic.Anthropic(api_key=api_key)
        except Exception as e:
            self.client = None
            self._import_error = e

    def complete(
        self,
        model: str = DEFAULT_SONNET_MODEL,
        system: str = "",
        user: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.2,
        cache_system: bool = True,
        is_tier3: bool = True,
        workload: str = "t3_thesis",
        use_batch: bool = False,
    ) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError(f"anthropic SDK not available: {getattr(self, '_import_error', '')}. pip install 'anthropic==0.45.2'")

        # G6 kill
        if self.is_killed():
            return {"text": "", "usage": {"input_tokens": 0, "output_tokens": 0}, "raw": None, "error": "killed"}

        # G1 pre-call gate - pass full model so price_key_for_model inside estimate_cost picks correct tier (sonnet/haiku/opus)
        est = estimate_cost(workload, model)
        if self.budget:
            if not self.budget.can_spend(est):
                return {"text": "", "usage": {"input_tokens": 0, "output_tokens": 0}, "raw": None, "error": "budget_refused"}
            if is_tier3 and not self.budget.can_start_tier3():
                return {"text": "", "usage": {"input_tokens": 0, "output_tokens": 0}, "raw": None, "error": "budget_degrade"}

        sys_blocks: list[dict] = [{"type": "text", "text": system}]
        if cache_system:
            sys_blocks[0]["cache_control"] = {"type": "ephemeral"}  # G2

        create_kwargs: dict[str, Any] = dict(
            model=model,
            system=sys_blocks,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
        )

        try:
            try:
                resp = self.client.messages.create(**create_kwargs)
            except Exception as e:
                # Bug fix (2026-07-09): found live — every real Opus 4.8 call since the
                # 2026-07-06 filing-text fix (the first day candidates had real content to
                # reach it) failed with a 400: "`temperature` is deprecated for this model."
                # 25 EV-thesis attempts died this way over 3 days, all correctly logged as
                # no-fabrication rejections (fail-closed did its job) but for the wrong
                # reason — a parameter-compatibility bug, not a real EV/risk decision. This
                # is a single BOUNDED, parameter-correcting retry (not a blind retry of the
                # same request): only fires when the error identifies temperature as the
                # incompatible param, and only strips that one field before resubmitting once.
                msg = str(e).lower()
                if "temperature" in msg and "temperature" in create_kwargs and (
                    "deprecated" in msg or "not supported" in msg or "unsupported" in msg
                ):
                    retry_kwargs = {k: v for k, v in create_kwargs.items() if k != "temperature"}
                    resp = self.client.messages.create(**retry_kwargs)
                else:
                    raise
            text = resp.content[0].text if resp.content else ""
            # Mandate 4: read the cache fields the SDK reports (getattr-guarded — older SDK
            # responses or non-cached calls may omit them; default 0, never fabricate a hit).
            cache_creation = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            usage = {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            }
            if self.budget:
                self.budget.record_usage(
                    model, usage["input_tokens"], usage["output_tokens"],
                    cache_creation_input_tokens=cache_creation, cache_read_input_tokens=cache_read,
                )
            return {"text": text, "usage": usage, "raw": resp}
        except Exception as e:
            # G7: bounded, no blind retry here; caller decides
            return {"text": "", "usage": {"input_tokens": 0, "output_tokens": 0}, "raw": None, "error": str(e)}

    def create_batch(self, requests: list[dict], description: str = "") -> str:
        """Phase R: real Batch for non-urgent (Meta weekly). Uses SDK beta.
        requests: list of {"user": "..."} or full params. Returns batch_id.
        Gated: real calls only in smoke/runner (not unittests).
        """
        if not self.client:
            return "batch_no_client"
        try:
            # anthropic>=0.45 supports beta batches for cost savings on bulk
            batch_requests = []
            for i, r in enumerate(requests):
                user_text = r.get("user") if isinstance(r, dict) else str(r)
                br = {
                    "custom_id": f"{description or 'batch'}-{i}",
                    "params": {
                        "model": self.model or DEFAULT_SONNET_MODEL,
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": user_text}],
                    },
                }
                batch_requests.append(br)
            created = self.client.beta.messages.batches.create(requests=batch_requests)
            return getattr(created, "id", str(created))
        except Exception as e:
            # Do not fabricate; surface for caller (meta will fallback or log)
            return f"batch_error:{type(e).__name__}:{e}"

    def retrieve_batch(self, batch_id: str) -> dict[str, Any]:
        """Poll/retrieve real batch results. Returns {"results": [{"text":...}, ...]} or error."""
        if not self.client or not batch_id or batch_id.startswith("batch_error"):
            return {"results": [], "error": "no_client_or_bad_id"}
        try:
            batch = self.client.beta.messages.batches.retrieve(batch_id)
            # results may be in files or direct; simplify for our use (small meta batches)
            results = []
            if hasattr(batch, "results") and batch.results:
                for res in batch.results:
                    if getattr(res, "result", None) and getattr(res.result, "message", None):
                        txt = res.result.message.content[0].text if res.result.message.content else ""
                        results.append({"text": txt})
            elif hasattr(batch, "request_counts"):
                # still processing
                return {"results": [], "status": getattr(batch, "processing_status", "in_progress")}
            return {"results": results, "status": getattr(batch, "processing_status", "complete")}
        except Exception as e:
            return {"results": [], "error": str(e)}


def get_llm_client(
    fake: bool = True,
    budget: Optional[DailyBudget] = None,
    is_killed: Optional[Callable[[], bool]] = None,
    api_key: Optional[str] = None,
) -> LLMClient:
    """Factory.
    Tests / hermetic: fake=True (FakeLLMClient, no net).
    Paper/Live RUN mode (phase R+): fake=False -> real Anthropic (env ANTHROPIC_API_KEY), current models, batch, cache, budget record.
    Real spend happens; budget breaker must stop at cap.
    """
    if fake:
        return FakeLLMClient(budget=budget, is_killed=is_killed)
    return AnthropicLLMClient(api_key=api_key, budget=budget, is_killed=is_killed)
