"""LiteLLM response caching behaviour (real litellm cache, fake provider)."""

from __future__ import annotations

import asyncio

import litellm

from app.llm.client import FailoverLLMClient
from tests.conftest import KEY_1, KEY_2


def chat(client, session_id, message, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra})


def test_same_first_message_in_new_sessions_hits_cache(client, provider):
    r1 = chat(client, "s-cache-1", "What is the capital of France?")
    r2 = chat(client, "s-cache-2", "What is the capital of France?")
    assert r1.status_code == r2.status_code == 200
    m1, m2 = r1.json()["metadata"], r2.json()["metadata"]
    assert m1["resolution_source"] == "LIVE_INFERENCE"
    assert m2["resolution_source"] == "LLM_CACHE"
    assert r2.json()["reply"] == r1.json()["reply"]
    # Cached token usage preserved; cache hits have no key and no fallbacks.
    assert (m2["input_tokens"], m2["output_tokens"]) == (m1["input_tokens"], m1["output_tokens"])
    assert m2["key_id"] is None
    assert m2["fallback_attempts"] == 0
    assert m2["model"] == m1["model"]
    # Only one genuine provider call happened.
    assert len(provider.live_calls) == 1


def test_cache_hit_is_persisted_in_second_session(client):
    chat(client, "s-p1", "hello cache")
    chat(client, "s-p2", "hello cache")
    msgs = client.get("/sessions/s-p2/messages").json()["messages"]
    assert msgs[1]["metadata"]["resolution_source"] == "LLM_CACHE"


def test_different_messages_do_not_share_cache(client, provider):
    r1 = chat(client, "s-d1", "What is the capital of France?")
    r2 = chat(client, "s-d2", "What is the capital of Spain?")
    assert r2.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert r1.json()["reply"] != r2.json()["reply"]
    assert len(provider.live_calls) == 2


def test_different_system_prompt_misses_cache(client):
    chat(client, "s-sp1", "hi", system_prompt="Be brief.")
    r = chat(client, "s-sp2", "hi", system_prompt="Be verbose.")
    assert r.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_different_history_misses_cache(client):
    chat(client, "s-h1", "context A")
    chat(client, "s-h2", "context B")
    a = chat(client, "s-h1", "same follow-up")
    b = chat(client, "s-h2", "same follow-up")
    assert a.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert b.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert a.json()["reply"] != b.json()["reply"]


def test_identical_full_conversation_hits_cache(client):
    for sid in ("s-x", "s-y"):
        chat(client, sid, "turn one")
    chat(client, "s-x", "turn two")
    r = chat(client, "s-y", "turn two")
    assert r.json()["metadata"]["resolution_source"] == "LLM_CACHE"


def test_different_model_misses_cache(tmp_path, provider):
    """Cache key depends on the model name."""
    from app.llm.cache import configure_cache

    configure_cache(True, 60, 100)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "model test"}]

    async def run():
        a = FailoverLLMClient([KEY_1, KEY_2], ["gemini/gemini-3.5-flash-lite"] * 2, 10, acompletion=provider.acompletion)
        b = FailoverLLMClient([KEY_1, KEY_2], ["gemini/gemini-1.5-flash"] * 2, 10, acompletion=provider.acompletion)
        r1 = await a.complete(messages)
        r2 = await b.complete(messages)
        r3 = await a.complete(messages)
        return r1, r2, r3

    try:
        r1, r2, r3 = asyncio.run(run())
    finally:
        litellm.cache = None
    assert r1.resolution_source == "LIVE_INFERENCE"
    assert r2.resolution_source == "LIVE_INFERENCE"
    assert r3.resolution_source == "LLM_CACHE"


def test_response_from_fallback_key_is_cached(client, provider):
    provider.fail_key(KEY_1, 429)
    r1 = chat(client, "s-f1", "fallback then cache")
    assert r1.json()["metadata"]["key_id"] == "key_2"
    provider.failures.clear()
    r2 = chat(client, "s-f2", "fallback then cache")
    assert r2.json()["metadata"]["resolution_source"] == "LLM_CACHE"
    assert r2.json()["metadata"]["key_id"] is None
    assert r2.json()["metadata"]["fallback_attempts"] == 0


def test_cache_hit_even_if_key_1_currently_failing(client, provider):
    """Cache is consulted before any provider call, so a hit never counts fallbacks."""
    chat(client, "s-k1", "resilient prompt")
    provider.fail_key(KEY_1, 429)
    r = chat(client, "s-k2", "resilient prompt")
    meta = r.json()["metadata"]
    assert meta["resolution_source"] == "LLM_CACHE"
    assert meta["fallback_attempts"] == 0


def test_cache_latency_lower_than_live(client, provider):
    """Give live inference a realistic delay (applied by LiteLLM's mock only on a cache miss)."""
    provider.live_delay = 0.25
    live = chat(client, "s-lat1", "latency check")
    cached = chat(client, "s-lat2", "latency check")
    assert live.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert cached.json()["metadata"]["resolution_source"] == "LLM_CACHE"
    assert live.json()["metadata"]["latency_ms"] >= 250
    assert cached.json()["metadata"]["latency_ms"] < live.json()["metadata"]["latency_ms"]


def test_cache_disabled_never_reports_cache(client_factory):
    c = client_factory(cache_enabled=False)
    chat(c, "s-n1", "no cache please")
    r = chat(c, "s-n2", "no cache please")
    assert r.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
