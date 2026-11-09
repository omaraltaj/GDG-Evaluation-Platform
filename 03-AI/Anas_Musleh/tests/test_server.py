from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, server
from app.errors import BackendError

KEY = "test-internal-key"
HEADERS = {"Authorization": "Bearer user-token", "X-Internal-Key": KEY}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "CHATBOT_INTERNAL_KEY", KEY)
    monkeypatch.setattr(config, "BACKEND_MODE", "real")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(server, "get_client", lambda: object())
    monkeypatch.setattr(server, "ToolExecutor", lambda c, token, today: SimpleNamespace(token=token))
    return TestClient(server.app)


def use_assistant(monkeypatch, ask):
    monkeypatch.setattr(server, "Assistant", lambda executor: SimpleNamespace(ask=lambda q: ask(executor, q)))


def post(client, headers=HEADERS):
    return client.post("/chat", json={"question": "كم نقاطي؟"}, headers=headers)


def test_answers_using_the_callers_token(client, monkeypatch):
    use_assistant(monkeypatch, lambda ex, q: SimpleNamespace(answer=f"{ex.token}:{q}"))
    res = post(client)
    assert res.status_code == 200
    assert res.json() == {"answer": "user-token:كم نقاطي؟"}


def test_rejects_callers_without_the_internal_key(client):
    assert post(client, {"Authorization": "Bearer x", "X-Internal-Key": "wrong"}).status_code == 403


def test_requires_a_user_token(client):
    assert post(client, {"X-Internal-Key": KEY}).status_code == 401


def test_refuses_to_serve_mock_data(client, monkeypatch):
    monkeypatch.setattr(config, "BACKEND_MODE", "mock")
    res = post(client)
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "CHATBOT_NOT_CONFIGURED"


def test_ai_provider_failure_is_a_clean_503(client, monkeypatch):
    def fail(ex, q):
        raise RuntimeError("secret provider detail")
    use_assistant(monkeypatch, fail)
    res = post(client)
    assert res.status_code == 503
    assert "secret" not in res.text


def test_expired_user_token_stays_401(client, monkeypatch):
    def fail(ex, q):
        raise BackendError("UNAUTHENTICATED", "Authentication is required", 401)
    use_assistant(monkeypatch, fail)
    assert post(client).status_code == 401
