import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ms_durable.app import create_app
from ms_durable.config import Settings
from ms_durable.media import MAX_BODY, ImageInput
from ms_durable.runtime import Runtime

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
IMAGE = {"name": "diagram.svg", "mime": "image/svg+xml", "data": base64.b64encode(SVG).decode()}


class QueueOnly:
    models = ()
    model_error = None

    def __init__(self, store, settings):
        self.store = store

    async def start(self):
        pass

    async def close(self):
        pass

    async def enqueue(self, *args, **kwargs):
        turn = await self.store.submit(*args, **kwargs)
        await self.store.event(turn["session_id"], turn["id"], "fixture", {}, turn["id"])
        return turn


def test_image_and_timeline_api(tmp_path):
    settings = Settings(data=tmp_path, access_token="fixture")
    headers = {"Authorization": "Bearer fixture"}
    image_request = {"prompt": "", "images": [IMAGE], "request_id": "same"}
    with TestClient(create_app(settings, QueueOnly), headers=headers) as client:
        session = client.post("/api/sessions", json={"model": "mock", "handle": "@Review"})
        assert session.status_code == 200 and session.json()["handle"] == "review"
        sid = session.json()["id"]
        root = f"/api/sessions/{sid}"
        assert (
            client.post("/api/sessions", json={"model": "mock", "handle": "review"}).status_code
            == 409
        )
        sent = client.post(root + "/turns", json=image_request)
        assert sent.status_code == 200
        tid = sent.json()["id"]
        assert client.post(root + "/turns", json=image_request).json()["id"] == tid
        assert (
            client.post(
                root + "/turns", json={**image_request, "images": [{**IMAGE, "name": "other.svg"}]}
            ).status_code
            == 409
        )
        assert (
            client.post(
                root + "/turns", json={**image_request, "images": [{**IMAGE, "mime": "text/html"}]}
            ).status_code
            == 422
        )
        assert client.post(root + "/turns", content=b"x" * (MAX_BODY + 1)).status_code == 413
        client.post(root + "/turns", json={"prompt": "Another", "request_id": "other"})
        first = client.get(root + "/timeline?limit=1").json()
        assert len(first["events"]) == 1 and first["next_after"] > 0
        second = client.get(root + f"/timeline?limit=1&after={first['next_after']}").json()
        assert second["next_after"] is None and second["events"][0]["seq"] > first["next_after"]
    with TestClient(create_app(settings, QueueOnly), headers=headers) as client:
        path = root + f"/turns/{tid}/images/0"
        assert client.get(path, headers={"Authorization": ""}).status_code == 401
        assert client.get(path.replace(sid, "wrong-session")).status_code == 404
        image = client.get(path)
        assert image.content == SVG
        assert image.headers["content-disposition"] == "attachment"
        assert "sandbox" in image.headers["content-security-policy"]
        stored = client.get(root).json()["turns"][0]["images"][0]
        assert stored["name"] == IMAGE["name"] and "content" not in stored


def test_reject_svg_entities():
    svg = b'<!DOCTYPE svg [<!ENTITY x "hi">]>' + SVG
    with pytest.raises(ValueError):
        ImageInput(**{**IMAGE, "data": base64.b64encode(svg).decode()}).decode()


async def test_named_chat_is_deduplicated_and_one_hop(store, tmp_path, monkeypatch):
    runtime = Runtime(Settings(data=tmp_path), store)
    monkeypatch.setattr(runtime, "kick", lambda sid: None)
    alice = (await store.create_session("Review", "mock", "@Alice"))["id"]
    bob = (await store.create_session("Build", "mock", "bob"))["id"]

    async def send(sender, target):
        tool = next(t for t in runtime.tools(sender) if t.name == "app_send_message")
        result = await tool.handler(
            SimpleNamespace(
                arguments={
                    "session_id": target,
                    "prompt": "Check the build",
                    "request_id": "stable",
                }
            )
        )
        return json.loads(result.text_result_for_llm)

    message = await send(alice, "@Bob")
    assert (await send(alice, "@bob"))["id"] == message["id"]
    assert (await store.snapshot(bob))["turns"][0]["sender_handle"] == "alice"
    with pytest.raises(ValueError, match="another app session"):
        await send(alice, "@alice")
    runtime.active[bob] = message["id"]
    with pytest.raises(ValueError, match="cannot send"):
        await send(bob, "@alice")


async def test_model_can_inspect_timeline_and_attachments(store, tmp_path):
    runtime = Runtime(Settings(data=tmp_path), store)
    sid = (await store.create_session("Review", "mock", "review"))["id"]
    other = (await store.create_session("Other", "mock"))["id"]
    pixels = b"\x89PNG\r\n\x1a\nfixture"
    images = [
        ImageInput(**IMAGE).decode(),
        ImageInput(
            name="test.png", mime="image/png", data=base64.b64encode(pixels).decode()
        ).decode(),
    ]
    turn = await store.submit(sid, "Inspect", "stable", images=images)
    for i in range(102):
        await store.event(sid, turn["id"], "fixture", {"index": i})
    tools = {t.name: t for t in runtime.tools(sid)}

    async def invoke(name, **args):
        return await tools[name].handler(SimpleNamespace(arguments=args))

    first = json.loads(
        (await invoke("app_observe_session", session_id="@review")).text_result_for_llm
    )
    assert len(first["events"]) == 100
    assert [a["name"] for a in first["turns"][0]["images"]] == ["diagram.svg", "test.png"]
    tail = json.loads(
        (await invoke("app_observe_session", after_seq=first["next_after"])).text_result_for_llm
    )
    assert len(tail["events"]) == 2 and tail["next_after"] is None
    result = await invoke("app_read_attachment", turn_id=turn["id"], ordinal=0)
    assert json.loads(result.text_result_for_llm)["source"].encode() == SVG
    result = await invoke("app_read_attachment", turn_id=turn["id"], ordinal=1)
    assert base64.b64decode(result.binary_results_for_llm[0].data) == pixels
    with pytest.raises(ValueError, match="Unknown attachment"):
        await invoke("app_read_attachment", session_id=other, turn_id=turn["id"], ordinal=0)


async def test_agent_plan_is_atomic_and_session_scoped(store, tmp_path):
    runtime = Runtime(Settings(data=tmp_path), store)
    sid = (await store.create_session("Review", "mock"))["id"]
    other = (await store.create_session("Other", "mock"))["id"]
    tool = next(t for t in runtime.tools(sid) if t.name == "app_plan")

    async def call(**args):
        return json.loads((await tool.handler(SimpleNamespace(arguments=args))).text_result_for_llm)

    assert (await call(action="read"))["items"] == []
    first = await call(
        action="update",
        explanation="Checks",
        plan=[{"step": "Inspect", "status": "in_progress"}, {"step": "Verify"}],
    )
    assert first["revision"] == 1
    with pytest.raises(ValueError, match="at most one"):
        await call(
            action="patch", patches=[{"operation": "update", "index": 2, "status": "in_progress"}]
        )
    assert await call(action="read") == first
    second = await call(
        action="patch",
        patches=[
            {"operation": "add", "index": 2, "step": "Document"},
            {"operation": "update", "index": 1, "status": "completed"},
            {"operation": "update", "index": 2, "status": "in_progress"},
            {"operation": "remove", "index": 3},
        ],
    )
    assert second["explanation"] == "Checks"
    assert [item["step"] for item in second["items"]] == ["Inspect", "Document"]
    assert second["revision"] == 2
    assert (await store.snapshot(other))["plan"]["revision"] == 0
    assert [e["data"] for e in await store.events(sid) if e["type"] == "app.plan_updated"] == [
        first,
        second,
    ]
    from ms_durable.store import Store

    reopened = Store(store.path)
    await reopened.open()
    try:
        assert (await reopened.snapshot(sid))["plan"] == second
    finally:
        await reopened.close()
