"""Test fixtures.

External LLM calls are replaced by a deterministic fake provider that still goes
through the *real* `litellm.acompletion` (via LiteLLM's `mock_response` hook). That
means LiteLLM's real cache lookup / cache write / exception types are exercised; only
the network call to the provider is replaced.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import litellm
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.cache import is_cache_hit
from app.main import create_app

KEY_1 = "test-secret-key-AAAA"
KEY_2 = "test-secret-key-BBBB"


@dataclass
class ProviderCall:
    api_key: str
    messages: list[dict[str, str]]
    outcome: str  # "live" | "cache" | "error"


@dataclass
class FakeProvider:
    """Per-key programmable fake LLM backend."""

    failures: dict[str, Callable[[], Exception]] = field(default_factory=dict)
    calls: list[ProviderCall] = field(default_factory=list)
    live_delay: float | None = None  # seconds; LiteLLM applies it only when the provider is really "called"

    def fail_key(self, api_key: str, status: int) -> None:
        def make() -> Exception:
            if status == 429:
                return litellm.RateLimitError(message="quota exceeded", llm_provider="gemini", model="m")
            if status == 503:
                return litellm.ServiceUnavailableError(message="unavailable", llm_provider="gemini", model="m")
            if status == 400:
                return litellm.BadRequestError(message="bad request", llm_provider="gemini", model="m")
            return litellm.InternalServerError(message="boom", llm_provider="gemini", model="m")

        self.failures[api_key] = make

    @staticmethod
    def reply_for(messages: list[dict[str, str]]) -> str:
        users = [m["content"] for m in messages if m["role"] == "user"]
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        earlier = " | ".join(users[:-1]) or "nothing"
        return f"[sys:{system}] You said: {users[-1]}. Earlier: {earlier}."

    @staticmethod
    def count(text: str) -> int:
        return len(text.split())

    @property
    def live_calls(self) -> list[ProviderCall]:
        return [c for c in self.calls if c.outcome == "live"]

    async def acompletion(self, **kwargs: Any) -> Any:
        api_key = kwargs["api_key"]
        messages = kwargs["messages"]
        failure = self.failures.get(api_key)
        if failure is not None:
            mock: Any = failure()
        else:
            reply = self.reply_for(messages)
            prompt_tokens = sum(self.count(m["content"]) + 4 for m in messages)
            completion_tokens = self.count(reply)
            mock = {
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "model": kwargs["model"],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        try:
            extra = {"mock_delay": self.live_delay} if self.live_delay else {}
            response = await litellm.acompletion(**kwargs, mock_response=mock, **extra)
        except Exception:
            self.calls.append(ProviderCall(api_key, messages, "error"))
            raise
        self.calls.append(ProviderCall(api_key, messages, "cache" if is_cache_hit(response) else "live"))
        return response


def make_settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_api_keys": f"{KEY_1},{KEY_2}",
        "llm_model": "gemini/gemini-3.5-flash-lite",
        "database_url": f"sqlite:///{tmp_path.as_posix()}/app.db",
        "enable_failure_simulation": True,
        "cache_enabled": True,
        "allowed_origins": "http://localhost:3000",
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client_factory(tmp_path, provider) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        app = create_app(make_settings(tmp_path, **overrides), acompletion=provider.acompletion)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)
    litellm.cache = None


@pytest.fixture
def client(client_factory) -> TestClient:
    return client_factory()
