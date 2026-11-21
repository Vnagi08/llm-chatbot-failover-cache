"""LiteLLM client with ordered multi-key failover.

Every attempt is a genuine `litellm.acompletion` call made with one specific API key,
so the key that produced the answer is always known. Keys are tried in configured
order (key_1, key_2, ...). An attempt that fails with a failover-eligible error
(429 / quota / 5xx / timeout / connection / key auth) moves on to the next key.

Cache semantics:
  * Only the first attempt reads from the cache. A cache hit therefore always has
    key_id = None and fallback_attempts = 0.
  * Later attempts (after a failure) and every attempt of a request carrying an
    X-Simulate-Failure header use cache={"no-cache": True}: they skip the cache
    read but still store the fresh response.

Why not litellm.Router? Its cooldown bookkeeping is process-wide shared state, so a
simulated (or transient) 429 on key_1 would bench that key for later requests, and
it doesn't expose a reliable "which deployment / how many failed attempts" result
for a single call. LiteLLM still owns the provider integration, error mapping,
timeouts and caching; we own only the key ordering.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import litellm

from app.llm.cache import cache_is_enabled, flush_pending_cache_writes, is_cache_hit
from app.llm.errors import (
    AllProvidersUnavailableError,
    ProviderRequestRejectedError,
    is_failover_error,
    status_code_of,
)
from app.llm.simulation import FailurePlan, simulated_exception
from app.logging_config import redact

logger = logging.getLogger(__name__)

litellm.suppress_debug_info = True
litellm.drop_params = True


@dataclass(frozen=True)
class ApiKey:
    key_id: str
    secret: str
    model: str

    def __repr__(self) -> str:  # never leak the secret through repr()
        return f"ApiKey(key_id={self.key_id!r}, model={self.model!r})"


@dataclass(frozen=True)
class CompletionResult:
    reply: str
    model: str
    resolution_source: str  # "LIVE_INFERENCE" | "LLM_CACHE"
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int
    latency_ms: int


AcompletionFn = Callable[..., Awaitable[Any]]


class FailoverLLMClient:
    def __init__(
        self,
        api_keys: list[str],
        models: list[str],
        timeout_seconds: float,
        max_output_tokens: int | None = None,
        acompletion: AcompletionFn | None = None,
        api_base: str | None = None,
    ) -> None:
        self.keys = [
            ApiKey(key_id=f"key_{i + 1}", secret=secret, model=models[i])
            for i, secret in enumerate(api_keys)
        ]
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.api_base = api_base or None
        # Indirection so tests can route calls through litellm's mock_response.
        self._acompletion: AcompletionFn = acompletion or litellm.acompletion

    @property
    def keys_configured(self) -> int:
        return len(self.keys)

    async def complete(
        self,
        messages: list[dict[str, str]],
        failure_plan: FailurePlan | None = None,
    ) -> CompletionResult:
        started = time.perf_counter()
        failed_attempts = 0
        simulating = failure_plan is not None and failure_plan.active

        for index, key in enumerate(self.keys):
            read_cache = index == 0 and not simulating
            try:
                if failure_plan is not None:
                    status = failure_plan.status_for(key.key_id)
                    if status is not None:
                        raise simulated_exception(status, key.key_id, key.model)
                response = await self._call(key, messages, read_cache=read_cache)
            except Exception as exc:  # noqa: BLE001 - classified below
                status = status_code_of(exc)
                if is_failover_error(exc):
                    failed_attempts += 1
                    logger.warning(
                        "LLM attempt failed: key=%s model=%s status=%s error=%s -> %s",
                        key.key_id,
                        key.model,
                        status,
                        type(exc).__name__,
                        "trying next key" if index + 1 < len(self.keys) else "no keys left",
                    )
                    logger.debug("provider error detail: %s", redact(str(exc))[:500])
                    continue
                logger.error(
                    "LLM request rejected (no failover): key=%s status=%s error=%s detail=%s",
                    key.key_id,
                    status,
                    type(exc).__name__,
                    redact(str(exc))[:300],
                )
                raise ProviderRequestRejectedError(type(exc).__name__) from None

            cache_hit = is_cache_hit(response)
            if not cache_hit and cache_is_enabled():
                await flush_pending_cache_writes()
            result = self._build_result(
                response=response,
                key=key,
                messages=messages,
                cache_hit=cache_hit,
                failed_attempts=failed_attempts,
                started=started,
            )
            logger.info(
                "LLM success: source=%s key=%s model=%s fallback_attempts=%d latency_ms=%d tokens=%d/%d",
                result.resolution_source,
                result.key_id,
                result.model,
                result.fallback_attempts,
                result.latency_ms,
                result.input_tokens,
                result.output_tokens,
            )
            return result

        raise AllProvidersUnavailableError(attempts=failed_attempts)

    async def _call(self, key: ApiKey, messages: list[dict[str, str]], read_cache: bool) -> Any:
        kwargs: dict[str, Any] = {
            "model": key.model,
            "messages": messages,
            "api_key": key.secret,
            "timeout": self.timeout_seconds,
            "num_retries": 0,  # failover across keys is handled here, not by same-key retries
            "caching": cache_is_enabled(),
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.max_output_tokens:
            kwargs["max_tokens"] = self.max_output_tokens
        if cache_is_enabled() and not read_cache:
            kwargs["cache"] = {"no-cache": True}
        return await self._acompletion(**kwargs)

    def _build_result(
        self,
        response: Any,
        key: ApiKey,
        messages: list[dict[str, str]],
        cache_hit: bool,
        failed_attempts: int,
        started: float,
    ) -> CompletionResult:
        reply = (response.choices[0].message.content or "").strip()
        input_tokens, output_tokens = self._usage(response, key.model, messages, reply)
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        return CompletionResult(
            reply=reply,
            model=key.model,
            resolution_source="LLM_CACHE" if cache_hit else "LIVE_INFERENCE",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            key_id=None if cache_hit else key.key_id,
            fallback_attempts=0 if cache_hit else failed_attempts,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _usage(response: Any, model: str, messages: list[dict[str, str]], reply: str) -> tuple[int, int]:
        """Provider-reported usage for this single call; local tokenizer as fallback."""
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None) if usage is not None else None
        completion = getattr(usage, "completion_tokens", None) if usage is not None else None
        if not prompt:
            try:
                prompt = litellm.token_counter(model=model, messages=messages)
            except Exception:  # pragma: no cover
                prompt = 0
            logger.info("provider did not report prompt usage; estimated locally")
        if completion is None:
            try:
                completion = litellm.token_counter(model=model, text=reply) if reply else 0
            except Exception:  # pragma: no cover
                completion = 0
        return int(prompt), int(completion)
