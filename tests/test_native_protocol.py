"""Opt-in native-runtime tests against a local synthetic Azure endpoint.

No Microsoft service is contacted and no real credential is used. The actual
Copilot executable performs routing, auth, streaming and transcript persistence.
"""

import asyncio
import json
import os
import socket

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from agent.config import Settings
from agent.runtime import Runtime

pytestmark = pytest.mark.skipif(
    os.getenv("MS_DURABLE_NATIVE_TESTS") != "1", reason="opt-in real Copilot runtime"
)


@pytest.mark.parametrize("api_version", [None, "2025-01-01-preview"])
@pytest.mark.parametrize("auth", ["api_key", "bearer", "callback"])
async def test_native_azure_routing_resume_and_prefix(
    store, tmp_path, monkeypatch, auth, api_version
):
    requests = []
    http_disconnected = asyncio.Event()
    release_stalled_response = asyncio.Event()
    endpoint = FastAPI()

    @endpoint.post("/{path:path}")
    async def inference(request: Request):
        body = await request.json()
        requests.append(
            {
                "path": request.url.path,
                "query": dict(request.query_params),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        chunk = {
            "id": "chatcmpl-local-test",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "deployment-test",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "FOUNDRY_ADAPTER_OK"},
                    "finish_reason": None,
                }
            ],
        }
        end = {
            **chunk,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "total_tokens": 1010,
                "prompt_tokens_details": {"cached_tokens": 512},
            },
        }

        async def chunks():
            yield "data: " + json.dumps(chunk) + "\n\n"
            if "PROJECTION_STALL" in json.dumps(body.get("messages", [])):
                try:
                    await release_stalled_response.wait()
                finally:
                    http_disconnected.set()
            yield "data: " + json.dumps(end) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(endpoint, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    monkeypatch.setenv("FOUNDRY_PROTOCOL_TEST_SECRET", "synthetic-not-a-real-key")
    path = tmp_path / "providers.json"
    provider = {
        "type": "azure",
        "base_url": f"http://127.0.0.1:{port}",
        "wire_api": "completions",
        "azure": {"api_version": api_version} if api_version else {},
        (
            "api_key_env" if auth == "api_key" else "bearer_token_env"
        ): "FOUNDRY_PROTOCOL_TEST_SECRET",
    }
    path.write_text(
        json.dumps(
            {
                "providers": {"foundry": provider},
                "models": [
                    {
                        "provider": "foundry",
                        "id": "test",
                        "wire_model": "deployment-test",
                        "model_id": "gpt-4.1",
                        "max_context_window_tokens": 32768,
                        "max_prompt_tokens": 30000,
                        "max_output_tokens": 2048,
                    }
                ],
            }
        )
    )
    profiles = json.loads(path.read_text())
    profiles["models"].append(
        {**profiles["models"][0], "id": "alternate", "wire_model": "deployment-alternate"}
    )
    path.write_text(json.dumps(profiles))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(data=tmp_path / "data", working_directory=workspace, providers_file=path)
    settings.data.mkdir()
    callbacks = []

    def create_runtime():
        runtime = Runtime(settings, store)
        if auth == "callback":

            async def token(args):
                callbacks.append(args)
                return "synthetic-not-a-real-key"

            connection = runtime.providers.connections[0]
            connection.pop("bearer_token", None)
            connection["bearer_token_provider"] = token
        return runtime

    runtime = create_runtime()

    # Force BYOK-only mode independently of the developer's real Pi login.
    def no_github(self):
        raise ValueError("No GitHub credential in isolated protocol test")

    monkeypatch.setattr(Settings, "token", no_github)
    sid = (await store.create_session("Foundry protocol", "foundry/test"))["id"]
    try:
        await runtime.start()
        if auth == "api_key" and api_version is None:
            created, release = asyncio.Event(), asyncio.Event()
            original_create = runtime.client.create_session

            async def held_create(**kwargs):
                session = await original_create(**kwargs)
                created.set()
                await release.wait()
                return session

            monkeypatch.setattr(runtime.client, "create_session", held_create)
            cancelled_sid = (
                await store.create_session("Cancelled native admission", "foundry/test")
            )["id"]
            try:
                cancelled = await runtime.enqueue(
                    cancelled_sid, "Never send to the inference endpoint.", "cancel-before-send"
                )
                await asyncio.wait_for(created.wait(), 30)
                await runtime.abort(cancelled_sid)
                assert not requests
                assert (await store.one("SELECT status FROM turns WHERE id=?", (cancelled["id"],)))[
                    "status"
                ] == "cancelled"
            finally:
                release.set()
                await asyncio.gather(*runtime.creating.values())
                monkeypatch.setattr(runtime.client, "create_session", original_create)
            assert not requests and cancelled_sid not in runtime.active
        for index in range(2):
            turn = await runtime.enqueue(
                sid, f"Say FOUNDRY_ADAPTER_OK. Sequence {index}. No tools.", f"first-{index}"
            )
            await asyncio.wait_for(runtime.workers[sid], 60)
            result = await store.one("SELECT * FROM turns WHERE id=?", (turn["id"],))
            assert result["status"] == "done", result["error"]
            assert result["answer"] == "FOUNDRY_ADAPTER_OK"
        assert len(requests) == 2
        if api_version:
            assert requests[0]["path"] == "/openai/deployments/deployment-test/chat/completions"
            assert requests[0]["query"]["api-version"] == api_version
        else:
            assert requests[0]["path"] == "/openai/v1/chat/completions"
            assert "api-version" not in requests[0]["query"]
        header = "api-key" if auth == "api_key" else "authorization"
        expected = (
            "synthetic-not-a-real-key" if auth == "api_key" else "Bearer synthetic-not-a-real-key"
        )
        assert requests[0]["headers"][header] == expected
        first, second = [r["body"] for r in requests]
        assert first["tools"] == second["tools"]
        assert first["messages"][0] == second["messages"][0]
        # An unchanged turn appends history rather than replacing the prefix.
        assert second["messages"][: len(first["messages"])] == first["messages"]
        await runtime.close()
        runtime = create_runtime()
        await runtime.start()
        turn = await runtime.enqueue(sid, "Say FOUNDRY_ADAPTER_OK again. No tools.", "after-reopen")
        await asyncio.wait_for(runtime.workers[sid], 60)
        result = await store.one("SELECT * FROM turns WHERE id=?", (turn["id"],))
        assert result["status"] == "done", result["error"]
        resumed = requests[-1]["body"]
        assert resumed["tools"] == first["tools"]
        assert resumed["messages"][0] == first["messages"][0]
        assert (await store.session(sid))["model"] == "foundry/test"
        compact = await runtime.enqueue_compaction(sid, "Keep the protocol marker.", "compact-test")
        await asyncio.wait_for(runtime.workers[sid], 60)
        compact = await store.one("SELECT * FROM turns WHERE id=?", (compact["id"],))
        assert compact["status"] == "done", compact["error"]
        result = json.loads(compact["answer"])
        assert result["success"] is True and result["messagesRemoved"] > 0
        assert result["summaryContent"] == "FOUNDRY_ADAPTER_OK"
        status = await runtime.metadata(sid)
        assert isinstance(
            status["attribution"], dict
        )  # Native attribution may be null until the next turn.
        assert isinstance(status["context"], dict)
        after_compact = await runtime.enqueue(
            sid, "Say FOUNDRY_ADAPTER_OK after compaction.", "after-compact"
        )
        await asyncio.wait_for(runtime.workers[sid], 60)
        assert (await store.one("SELECT status FROM turns WHERE id=?", (after_compact["id"],)))[
            "status"
        ] == "done"
        after_body = requests[-1]["body"]
        assert after_body["tools"] == first["tools"]
        import difflib

        difference = "\n".join(
            difflib.unified_diff(
                first["messages"][0]["content"].splitlines(),
                after_body["messages"][0]["content"].splitlines(),
            )
        )
        assert after_body["messages"][0] == first["messages"][0], difference
        status = await runtime.metadata(sid)
        assert status["context"]
        assert status["attribution"]["contextAttribution"]["compactions"]["count"] == 1
        # Compaction deliberately replaces the conversation suffix, not the
        # stable system/tool prefix. Cache billing is provider-owned.
        assert after_body["messages"][1:] != resumed["messages"][1:]
        await runtime.change_model(sid, "foundry/alternate", deferred=True)
        await runtime.close()
        runtime = create_runtime()
        await runtime.start()
        switched = await runtime.enqueue(
            sid, "Say FOUNDRY_ADAPTER_OK after model switch.", "after-switch"
        )
        await asyncio.wait_for(runtime.workers[sid], 60)
        assert (await store.one("SELECT status FROM turns WHERE id=?", (switched["id"],)))[
            "status"
        ] == "done"
        assert (await store.session(sid))["model"] == "foundry/alternate"
        assert requests[-1]["body"]["model"] == "deployment-alternate"
        if api_version:
            assert (
                requests[-1]["path"] == "/openai/deployments/deployment-alternate/chat/completions"
            )
        assert (await store.session(sid))["model"] == "foundry/alternate"
        if api_version:
            assert (
                requests[-1]["path"] == "/openai/deployments/deployment-alternate/chat/completions"
            )
        else:
            assert requests[-1]["body"]["model"] == "deployment-alternate"
        # Secrets are used in transport headers, never in projected events.
        events = json.dumps(await store.events(sid))
        assert "synthetic-not-a-real-key" not in events
        files = await store.rows(
            "SELECT content FROM sdk_files WHERE namespace=? AND content IS NOT NULL", (sid,)
        )
        assert all(b"synthetic-not-a-real-key" not in row["content"] for row in files)
        if auth == "api_key" and api_version is None:
            original_event = store.event

            async def failing_projection(session_id, turn_id, kind, *args, **kwargs):
                if kind == "assistant.message_delta":
                    raise OSError("Synthetic projection failure")
                return await original_event(session_id, turn_id, kind, *args, **kwargs)

            monkeypatch.setattr(store, "event", failing_projection)
            broken = await runtime.enqueue(
                sid, "PROJECTION_STALL: say the protocol marker.", "projection-failure"
            )
            await asyncio.wait_for(asyncio.gather(runtime.workers[sid], return_exceptions=True), 15)
            await asyncio.wait_for(http_disconnected.wait(), 5)
            assert (await store.one("SELECT status FROM turns WHERE id=?", (broken["id"],)))[
                "status"
            ] == "error"
            assert sid in runtime.quarantined and sid not in runtime.active
            with pytest.raises(ValueError, match="recovery"):
                await runtime.enqueue(sid, "Must not run.", "after-projection-failure")
            monkeypatch.setattr(store, "event", original_event)
        if auth == "callback":
            assert len(callbacks) >= len(requests)
            assert all(
                c["provider_name"] == "foundry" and c["session_id"] == sid for c in callbacks
            )
            await runtime.delete_session(sid)
            assert sid not in runtime.sessions
            assert not await store.rows("SELECT 1 FROM sdk_files WHERE namespace=?", (sid,))
            with pytest.raises(KeyError):
                await store.session(sid)
    finally:
        release_stalled_response.set()
        await runtime.close()
        server.should_exit = True
        await serving
