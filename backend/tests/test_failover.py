"""Key failover (real provider errors) and X-Simulate-Failure behaviour."""

from __future__ import annotations

import pytest

from app.llm.simulation import InvalidSimulationHeaderError, parse_failure_header
from tests.conftest import KEY_1, KEY_2


def chat(client, session_id, message, headers=None):
    return client.post("/chat", json={"session_id": session_id, "message": message}, headers=headers or {})


# --- provider-originated errors -------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_provider_error_on_key_1_falls_back_to_key_2(client, provider, status):
    provider.fail_key(KEY_1, status)
    r = chat(client, "s-fb", "hello")
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    # Genuine call made with the second key.
    assert provider.live_calls[-1].api_key == KEY_2


def test_all_keys_failing_returns_503_and_persists_nothing(client, provider):
    provider.fail_key(KEY_1, 429)
    provider.fail_key(KEY_2, 500)
    r = chat(client, "s-dead", "hello")
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert client.get("/sessions/s-dead/messages").status_code == 404


def test_failed_turn_in_existing_session_not_persisted(client, provider):
    assert chat(client, "s-keep", "first").status_code == 200
    provider.fail_key(KEY_1, 429)
    provider.fail_key(KEY_2, 429)
    assert chat(client, "s-keep", "second").status_code == 503
    msgs = client.get("/sessions/s-keep/messages").json()["messages"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["first"]
    assert len(msgs) == 2


def test_non_failover_error_does_not_try_next_key(client, provider):
    provider.fail_key(KEY_1, 400)
    r = chat(client, "s-400", "hello")
    assert r.status_code == 502
    assert "detail" in r.json()
    assert [c.api_key for c in provider.calls] == [KEY_1]
    assert client.get("/sessions/s-400/messages").status_code == 404


# --- X-Simulate-Failure ---------------------------------------------------------


@pytest.mark.parametrize("header", ["key_1=429", "key_1=500", "key_1=502", "key_1=503"])
def test_simulated_key_1_failure_uses_key_2(client, provider, header):
    r = chat(client, "s-sim", "What is 2+2?", headers={"X-Simulate-Failure": header})
    assert r.status_code == 200
    body = r.json()
    meta = body["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    # Key 1 was never sent to the provider; key 2 made a genuine call.
    assert [c.api_key for c in provider.calls] == [KEY_2]
    assert body["reply"] == provider.reply_for(provider.calls[0].messages)


def test_simulated_wildcard_returns_503_and_persists_nothing(client, provider):
    r = chat(client, "s-star", "hello", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert provider.calls == []
    assert client.get("/sessions/s-star/messages").status_code == 404


def test_simulated_both_keys_returns_503(client):
    r = chat(client, "s-both", "hello", headers={"X-Simulate-Failure": "key_1=429,key_2=500"})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}


def test_simulation_only_affects_current_request(client, provider):
    assert chat(client, "s-scope", "a", headers={"X-Simulate-Failure": "key_1=429"}).json()["metadata"]["key_id"] == "key_2"
    r = chat(client, "s-scope", "b")
    assert r.json()["metadata"]["key_id"] == "key_1"
    assert r.json()["metadata"]["fallback_attempts"] == 0


def test_simulation_bypasses_cache_read(client, provider):
    """An evaluator may replay a cached prompt with a failure header; failover must still be exercised."""
    chat(client, "s-c1", "cached prompt")
    r = chat(client, "s-c2", "cached prompt", headers={"X-Simulate-Failure": "key_1=429"})
    meta = r.json()["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["key_id"] == "key_2"
    r = chat(client, "s-c3", "cached prompt", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 503


def test_simulation_disabled_ignores_header(client_factory, provider):
    c = client_factory(enable_failure_simulation=False)
    r = chat(c, "s-off", "hello", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 200
    assert r.json()["metadata"]["key_id"] == "key_1"
    assert r.json()["metadata"]["fallback_attempts"] == 0


def test_invalid_simulation_header_rejected(client):
    for bad in ["key_1=404", "key_1", "foo=429", "key_1=abc"]:
        r = chat(client, "s-bad", "hello", headers={"X-Simulate-Failure": bad})
        assert r.status_code == 400, bad
        assert "detail" in r.json()
    assert client.get("/sessions/s-bad/messages").status_code == 404


# --- parser unit tests ----------------------------------------------------------


def test_parse_failure_header():
    plan = parse_failure_header("key_1=429, key_2=500")
    assert plan.status_for("key_1") == 429
    assert plan.status_for("key_2") == 500
    assert plan.status_for("key_3") is None
    star = parse_failure_header("*=503")
    assert star.status_for("key_1") == 503 and star.status_for("key_9") == 503
    mixed = parse_failure_header("*=429,key_2=500")
    assert mixed.status_for("key_1") == 429 and mixed.status_for("key_2") == 500
    assert parse_failure_header("") is None
    assert parse_failure_header(None) is None
    with pytest.raises(InvalidSimulationHeaderError):
        parse_failure_header("key_1=418")
