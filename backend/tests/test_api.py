"""Contract tests for /health, /chat and /sessions/{id}/messages."""

from __future__ import annotations

import json
import sqlite3

from tests.conftest import KEY_1, KEY_2

METADATA_FIELDS = {
    "resolution_source",
    "model",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "key_id",
    "fallback_attempts",
}


def chat(client, session_id, message, headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})


# --- health ---------------------------------------------------------------------


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


def test_health_degraded_with_single_key(client_factory):
    c = client_factory(llm_api_keys="only-one-key")
    r = c.get("/health")
    assert r.status_code == 503
    assert r.json()["keys_configured"] == 1
    assert "detail" in r.json()


def test_health_degraded_when_cache_disabled(client_factory):
    r = client_factory(cache_enabled=False).get("/health")
    assert r.status_code == 503
    assert r.json()["cache_enabled"] is False


# --- chat -----------------------------------------------------------------------


def test_chat_success_contract(client, provider):
    r = chat(client, "s-7f3a", "What is the capital of France?")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s-7f3a"
    assert body["reply"] == provider.reply_for(provider.live_calls[0].messages)
    meta = body["metadata"]
    assert set(meta) == METADATA_FIELDS
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "gemini/gemini-3.5-flash-lite"
    assert isinstance(meta["latency_ms"], int) and meta["latency_ms"] >= 0
    assert meta["input_tokens"] > 0 and meta["output_tokens"] > 0
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0


def test_message_is_trimmed_and_system_prompt_used(client, provider):
    r = chat(client, "s1", "   hello   ", system_prompt="You are a pirate.")
    assert r.status_code == 200
    sent = provider.live_calls[0].messages
    assert sent[0] == {"role": "system", "content": "You are a pirate."}
    assert sent[-1] == {"role": "user", "content": "hello"}


def test_default_system_prompt_applied(client, provider):
    chat(client, "s1", "hi")
    assert provider.live_calls[0].messages[0] == {"role": "system", "content": "You are a helpful assistant."}


def test_system_prompt_persists_for_session(client, provider):
    chat(client, "s1", "first", system_prompt="Answer in French.")
    chat(client, "s1", "second")
    assert provider.live_calls[1].messages[0]["content"] == "Answer in French."


def test_multi_turn_context_preserved(client, provider):
    chat(client, "s-multi", "My name is Ada.")
    r = chat(client, "s-multi", "What is my name?")
    assert r.status_code == 200
    sent = provider.live_calls[-1].messages
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "My name is Ada."
    assert sent[2]["content"] == provider.reply_for(provider.live_calls[0].messages)
    assert "My name is Ada." in r.json()["reply"]


def test_sessions_are_isolated(client, provider):
    chat(client, "s-a", "Alpha secret")
    chat(client, "s-b", "Bravo question")
    sent_b = provider.live_calls[-1].messages
    assert all("Alpha secret" not in m["content"] for m in sent_b)
    hist_a = client.get("/sessions/s-a/messages").json()["messages"]
    hist_b = client.get("/sessions/s-b/messages").json()["messages"]
    assert [m["content"] for m in hist_a if m["role"] == "user"] == ["Alpha secret"]
    assert [m["content"] for m in hist_b if m["role"] == "user"] == ["Bravo question"]


def test_token_usage_is_per_request_not_cumulative(client, provider):
    m1 = chat(client, "s-tok", "one two three").json()["metadata"]
    m2 = chat(client, "s-tok", "four").json()["metadata"]
    # Each request reports exactly what the provider reported for that call.
    expected_in_2 = sum(provider.count(m["content"]) + 4 for m in provider.live_calls[1].messages)
    assert m2["input_tokens"] == expected_in_2
    assert m2["output_tokens"] == provider.count(provider.reply_for(provider.live_calls[1].messages))
    # The second prompt contains the first turn, so it is bigger - but it is not a running total.
    assert m2["input_tokens"] > m1["input_tokens"]
    assert m2["input_tokens"] != m1["input_tokens"] + expected_in_2


# --- history --------------------------------------------------------------------


def test_history_ordering_and_format(client):
    chat(client, "s-h", "first question")
    chat(client, "s-h", "second question")
    r = client.get("/sessions/s-h/messages")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s-h"
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0] == {"role": "user", "content": "first question"}
    assert msgs[2] == {"role": "user", "content": "second question"}
    meta = msgs[1]["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert isinstance(meta["input_tokens"], int) and isinstance(meta["output_tokens"], int)
    # System prompt is never part of history.
    assert all(m["role"] != "system" for m in msgs)


def test_history_unknown_session_404(client):
    r = client.get("/sessions/does-not-exist/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


def test_messages_persisted_in_sqlite(client, tmp_path):
    chat(client, "s-db", "persist me")
    conn = sqlite3.connect(tmp_path / "app.db")
    rows = conn.execute(
        "SELECT session_id, role, content, metadata, created_at FROM messages ORDER BY id"
    ).fetchall()
    conn.close()
    assert [(r[0], r[1]) for r in rows] == [("s-db", "user"), ("s-db", "assistant")]
    assert rows[0][2] == "persist me" and rows[0][3] is None
    assert json.loads(rows[1][3])["key_id"] == "key_1"
    assert all(r[4] for r in rows)


def test_history_survives_app_restart(client_factory):
    first = client_factory()
    chat(first, "s-restart", "remember me")
    first.__exit__(None, None, None)
    second = client_factory()  # same tmp database file
    msgs = second.get("/sessions/s-restart/messages").json()["messages"]
    assert msgs[0]["content"] == "remember me"
    assert len(msgs) == 2


# --- validation -----------------------------------------------------------------


def test_missing_message_422(client):
    r = client.post("/chat", json={"session_id": "s1"})
    assert r.status_code == 422
    assert "detail" in r.json()


def test_blank_message_422(client):
    assert chat(client, "s1", "   \n\t ").status_code == 422


def test_non_string_message_422(client):
    assert client.post("/chat", json={"session_id": "s1", "message": 123}).status_code == 422


def test_oversized_message_422(client):
    assert chat(client, "s1", "x" * 4001).status_code == 422


def test_max_length_message_ok(client):
    assert chat(client, "s1", "x" * 4000).status_code == 200


def test_oversized_message_counts_after_trim(client):
    assert chat(client, "s1", "  " + "x" * 4000 + "  ").status_code == 200


def test_missing_session_id_422(client):
    assert client.post("/chat", json={"message": "hi"}).status_code == 422


def test_empty_and_long_session_id_422(client):
    assert chat(client, "", "hi").status_code == 422
    assert chat(client, "s" * 129, "hi").status_code == 422
    assert chat(client, "s" * 128, "hi").status_code == 200


def test_malformed_json_422(client):
    r = client.post("/chat", content=b'{"session_id": "s1", "message": ', headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "detail" in r.json()


def test_validation_failure_not_persisted(client):
    chat(client, "s-val", "x" * 5000)
    assert client.get("/sessions/s-val/messages").status_code == 404


def test_unknown_route_json_404(client):
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_api_keys_never_in_responses(client):
    for r in (
        client.get("/health"),
        chat(client, "s-sec", "hello"),
        chat(client, "s-sec", "hello", headers={"X-Simulate-Failure": "*=429"}),
        client.get("/sessions/s-sec/messages"),
    ):
        assert KEY_1 not in r.text and KEY_2 not in r.text


def test_cors_allows_configured_origin(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-simulate-failure",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
