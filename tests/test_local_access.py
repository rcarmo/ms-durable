from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when
from test_core import FakeRuntime

from agent.app import create_app
from agent.config import Settings

scenarios("../features/local-access.feature")


@pytest.fixture
def state():
    return {}


@given(parsers.parse('browser authentication is "{mode}"'))
def authentication(mode, state, monkeypatch, tmp_path):
    if mode == "local":
        monkeypatch.setenv("MS_DURABLE_AUTH_REQUIRED", "0")
    else:
        monkeypatch.delenv("MS_DURABLE_AUTH_REQUIRED", raising=False)
    state["settings"] = Settings(data=tmp_path / "data")
    with TestClient(create_app(state["settings"], FakeRuntime)):
        state["token"] = (state["settings"].data / "web-token").read_text()


def request(state, method, path, **kwargs):
    with TestClient(create_app(state["settings"], FakeRuntime)) as client:
        return client.request(method, path, **kwargs)


@when("I request the session catalogue without a token")
def anonymous_catalogue(state):
    state["response"] = request(state, "GET", "/api/sessions")


@when("I create a chat without a token")
def anonymous_create(state):
    state["response"] = request(
        state,
        "POST",
        "/api/sessions",
        json={"model": "mock"},
        headers={"Origin": "http://testserver"},
    )


@when("I request the session catalogue with an invalid token")
def invalid_token(state):
    state["response"] = request(
        state, "GET", "/api/sessions", headers={"Authorization": "Bearer invalid"}
    )


@when("a foreign website creates a chat without a token")
def foreign_create(state):
    state["response"] = request(
        state,
        "POST",
        "/api/sessions",
        json={"model": "mock"},
        headers={"Origin": "https://foreign.example"},
    )


@when("a cross-site browser requests the session catalogue")
def cross_site_catalogue(state):
    state["response"] = request(
        state, "GET", "/api/sessions", headers={"Sec-Fetch-Site": "cross-site"}
    )


@when("I restart with browser authentication required")
def require_authentication(state, monkeypatch):
    monkeypatch.delenv("MS_DURABLE_AUTH_REQUIRED", raising=False)
    state["settings"] = Settings(data=state["settings"].data)


@then(parsers.parse("the HTTP status is {status:d}"))
def status(state, status):
    assert state["response"].status_code == status


@then("the created chat appears in the session catalogue")
def created_chat(state):
    chats = request(state, "GET", "/api/sessions")
    assert chats.status_code == 200
    assert [chat["id"] for chat in chats.json()] == [state["response"].json()["id"]]


@then("the browser configuration reports token authentication disabled")
def local_configuration(state):
    response = request(state, "GET", "/api/config")
    assert response.status_code == 200
    assert response.json()["auth_required"] is False
    assert request(state, "GET", "/media/shared/missing/missing/file").status_code == 404


@then("the session catalogue is empty")
def empty_catalogue(state):
    response = request(state, "GET", "/api/sessions")
    assert response.status_code == 200
    assert response.json() == []


@then("the original browser token still grants access")
def original_token(state):
    assert (state["settings"].data / "web-token").read_text() == state["token"]
    response = request(
        state,
        "GET",
        "/api/sessions",
        headers={"Authorization": "Bearer " + state["token"]},
    )
    assert response.status_code == 200
