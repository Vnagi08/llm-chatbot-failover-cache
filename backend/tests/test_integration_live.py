"""Real-provider integration tests.

Skipped unless LIVE_LLM_API_KEYS (comma-separated, >= 2 keys) is set, so the default
suite never spends credits. Run with:

    LIVE_LLM_API_KEYS="key1,key2" LIVE_LLM_MODEL="gemini/gemini-3.5-flash-lite" pytest -m integration
"""

from __future__ import annotations

import os

import litellm
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings

LIVE_KEYS = os.getenv("LIVE_LLM_API_KEYS", "")
LIVE_MODEL = os.getenv("LIVE_LLM_MODEL", "gemini/gemini-3.5-flash-lite")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(len([k for k in LIVE_KEYS.split(",") if k.strip()]) < 2, reason="LIVE_LLM_API_KEYS not set"),
]


@pytest.fixture
def live_client(tmp_path):
    app = create_app(make_settings(tmp_path, llm_api_keys=LIVE_KEYS, llm_model=LIVE_MODEL, log_level="INFO"))
    with TestClient(app) as c:
        yield c
    litellm.cache = None


def test_live_chat_cache_and_failover(live_client):
    prompt = "Reply with exactly one word: the capital of France."
    r1 = live_client.post("/chat", json={"session_id": "live-1", "message": prompt})
    assert r1.status_code == 200, r1.text
    m1 = r1.json()["metadata"]
    assert m1["resolution_source"] == "LIVE_INFERENCE"
    assert m1["key_id"] == "key_1"
    assert m1["input_tokens"] > 0 and m1["output_tokens"] > 0
    assert "paris" in r1.json()["reply"].lower()

    r2 = live_client.post("/chat", json={"session_id": "live-2", "message": prompt})
    m2 = r2.json()["metadata"]
    assert m2["resolution_source"] == "LLM_CACHE"
    assert r2.json()["reply"] == r1.json()["reply"]
    assert m2["latency_ms"] < m1["latency_ms"]

    r3 = live_client.post(
        "/chat",
        json={"session_id": "live-3", "message": "Reply with one word: the capital of Italy."},
        headers={"X-Simulate-Failure": "key_1=429"},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["metadata"]["key_id"] == "key_2"
    assert r3.json()["metadata"]["fallback_attempts"] == 1

    r4 = live_client.post("/chat", json={"session_id": "live-1", "message": "And what country is it in?"})
    assert r4.status_code == 200
    assert "france" in r4.json()["reply"].lower()
    assert r4.json()["metadata"]["input_tokens"] > m1["input_tokens"]
