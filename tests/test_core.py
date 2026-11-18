from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from ms_durable.app import create_app
from ms_durable.config import Settings
from ms_durable.runtime import Runtime
from ms_durable.session_fs import SQLiteSessionFS
from ms_durable.store import Store


@pytest.fixture
async def store(tmp_path):
    result = Store(tmp_path / "state.sqlite")
    await result.open()
    yield result
    await result.close()


async def test_queue_idempotency_conflict_and_reopen(store):
    session = await store.create_session("One", "test-model")
    first = await store.submit(session["id"], "hello", "stable-id")
    assert (await store.submit(session["id"], "hello", "stable-id"))["id"] == first["id"]
    with pytest.raises(ValueError):
        await store.submit(session["id"], "different", "stable-id")
    await store.execute("UPDATE turns SET status='running' WHERE id=?", (first["id"],))
    await store.close()
    await store.open()
    assert (await store.snapshot(session["id"]))["turns"][0]["status"] == "interrupted"


async def test_session_fs_atomic_append_rename_namespace_and_reopen(store):
    a, b = SQLiteSessionFS(store, "a"), SQLiteSessionFS(store, "b")
    await a.write_file("/state/events.jsonl", "one\n")
    await asyncio.gather(*(a.append_file("/state/events.jsonl", f"{i}\n") for i in range(30)))
    assert len((await a.read_file("/state/events.jsonl")).splitlines()) == 31
    assert not await b.exists("/state/events.jsonl")
    await a.write_file("/state/replacement", "two\n")
    await a.rename("/state/replacement", "/state/events.jsonl")
    assert await a.read_file("/state/events.jsonl") == "two\n"
    assert await a.readdir("/state") == ["events.jsonl"]
    await a.rename("/state", "/moved")
    assert await a.read_file("/moved/events.jsonl") == "two\n"
    with pytest.raises(OSError):
        await a.rm("/moved", recursive=False)
    await store.close()
    await store.open()
    assert await a.read_file("/moved/events.jsonl") == "two\n"
    await a.rm("/moved", recursive=True)
    assert not await a.exists("/moved")


async def test_events_dedupe_and_snapshot(store):
    sid = (await store.create_session("One", "model"))["id"]
    await store.event(sid, None, "assistant.message", {"content": "<script>"}, "event-1")
    await store.event(sid, None, "assistant.message", {"content": "duplicate"}, "event-1")
    events = (await store.snapshot(sid))["events"]
    assert len(events) == 1
    assert events[0]["data"]["content"] == "<script>"
    assert not await store.events(sid, events[0]["seq"])


def test_auth_and_mcp_translation(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "github-copilot": {
                    "type": "oauth",
                    "refresh": "github-login",
                    "access": "wrong-short-lived-token",
                }
            }
        )
    )
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "memory": {
                        "url": "http://localhost/mcp",
                        "auth": {"type": "bearer", "token": "secret"},
                        "lazy": True,
                    }
                }
            }
        )
    )
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    settings = Settings(auth_file=auth, mcp_file=config)
    assert settings.token() == "github-login"
    server = settings.mcp_servers()["memory"]
    assert server["headers"]["Authorization"] == "Bearer secret"
    assert server["type"] == "http"
    assert "auth" not in server and "lazy" not in server


class FakeRuntime:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.models = [{"id": "mock", "name": "Mock"}]
        self.model_error = None

    async def start(self):
        pass

    async def close(self):
        pass

    async def enqueue(self, sid, prompt, request_id):
        return await self.store.submit(sid, prompt, request_id)


def test_api_auth_queue_isolation_and_restart(tmp_path):
    settings = Settings(data=tmp_path / "data", access_token="private-test-token")
    app = create_app(settings, FakeRuntime)
    headers = {"Authorization": "Bearer private-test-token"}
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/sessions").status_code == 401
        assert client.post("/api/sessions", json={"model": "mock"}).status_code == 401
        a = client.post("/api/sessions", json={"model": "mock"}, headers=headers).json()
        b = client.post("/api/sessions", json={"model": "mock"}, headers=headers).json()
        payload = {"prompt": "hi", "request_id": "one"}
        t1 = client.post(f"/api/sessions/{a['id']}/turns", json=payload, headers=headers).json()
        t2 = client.post(f"/api/sessions/{a['id']}/turns", json=payload, headers=headers).json()
        assert t1["id"] == t2["id"]
        assert client.get(f"/api/sessions/{b['id']}", headers=headers).json()["turns"] == []
        assert client.get("/api/sessions/missing", headers=headers).status_code == 404
        assert client.get("/api/config", headers=headers).json()["models"][0]["id"] == "mock"
    with TestClient(app) as client:
        assert len(client.get("/api/sessions", headers=headers).json()) == 2
        assert (
            client.get(f"/api/sessions/{a['id']}", headers=headers).json()["turns"][0]["status"]
            == "queued"
        )


async def test_introspection_only_app_sessions_and_model_changes(store, tmp_path):
    settings = Settings(data=tmp_path)
    runtime = Runtime(settings, store)
    sid = (await store.create_session("One", "mock"))["id"]
    runtime.models = [{"id": "mock"}, {"id": "other"}]
    tools = {tool.name: tool for tool in runtime.tools(sid)}
    from copilot.tools import ToolInvocation

    inv = ToolInvocation(
        session_id=sid, tool_call_id="call-1", tool_name="list_sessions", arguments={}
    )
    result = await tools["list_sessions"].handler(inv)
    assert json.loads(result.text_result_for_llm)[0]["id"] == sid
    await runtime.change_model(sid, "other")
    assert (await store.session(sid))["model"] == "other"
    with pytest.raises(ValueError):
        await runtime.change_model(sid, "unknown")
