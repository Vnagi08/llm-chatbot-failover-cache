"""X-Simulate-Failure header parsing and simulated provider exceptions.

Header grammar (comma separated):  <key_id|*>=<status>
    key_1=429
    key_1=429,key_2=500
    *=429
Supported statuses: 429, 500, 502, 503.

Simulated failures are scoped to a single request: the parsed plan is passed down
to the failover loop for that request only and never mutates shared key state.
When a key is marked as failing, the loop raises the same LiteLLM exception class
the provider integration would raise, so the regular failover path handles it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import litellm

SUPPORTED_STATUSES = frozenset({429, 500, 502, 503})
_ENTRY_RE = re.compile(r"^(\*|key_[1-9][0-9]*)=([0-9]{3})$")


class InvalidSimulationHeaderError(ValueError):
    pass


@dataclass(frozen=True)
class FailurePlan:
    per_key: dict[str, int] = field(default_factory=dict)
    wildcard: int | None = None

    def status_for(self, key_id: str) -> int | None:
        return self.per_key.get(key_id, self.wildcard)

    @property
    def active(self) -> bool:
        return bool(self.per_key) or self.wildcard is not None


def parse_failure_header(raw: str | None) -> FailurePlan | None:
    if raw is None or not raw.strip():
        return None
    per_key: dict[str, int] = {}
    wildcard: int | None = None
    for entry in raw.split(","):
        entry = entry.strip().replace(" ", "")
        if not entry:
            continue
        match = _ENTRY_RE.match(entry)
        if not match:
            raise InvalidSimulationHeaderError(f"invalid X-Simulate-Failure entry: {entry!r}")
        target, status_text = match.group(1), int(match.group(2))
        if status_text not in SUPPORTED_STATUSES:
            raise InvalidSimulationHeaderError(
                f"unsupported simulated status {status_text}; use one of 429, 500, 502, 503"
            )
        if target == "*":
            wildcard = status_text
        else:
            per_key[target] = status_text
    plan = FailurePlan(per_key=per_key, wildcard=wildcard)
    return plan if plan.active else None


def simulated_exception(status: int, key_id: str, model: str) -> Exception:
    provider = model.split("/", 1)[0] if "/" in model else "simulated"
    message = f"Simulated HTTP {status} for {key_id} (X-Simulate-Failure)"
    if status == 429:
        return litellm.RateLimitError(message=message, llm_provider=provider, model=model)
    if status == 502:
        return litellm.BadGatewayError(message=message, llm_provider=provider, model=model)
    if status == 503:
        return litellm.ServiceUnavailableError(message=message, llm_provider=provider, model=model)
    return litellm.InternalServerError(message=message, llm_provider=provider, model=model)
