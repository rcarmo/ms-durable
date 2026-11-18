import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.config import Settings
from agent.runtime import Runtime


class FakeSession:
    def __init__(self):
        self.handlers = []
        self.sent = []
        self.aborted = 0
        self.wait = asyncio.Event()

    async def set_model(self, model):
        self.model = model

    def on(self, callback):
        self.handlers.append(callback)
        return lambda: self.handlers.remove(callback)

    async def send_and_wait(self, prompt, **kwargs):
        self.sent.append(prompt)
        for callback in self.handlers:
            callback({"id": "user-1", "type": "user.message", "data": {"content": prompt}})
        await self.wait.wait()
        return {"type": "assistant.message", "data": {"content": "done"}}

    async def abort(self):
        self.aborted += 1
        self.wait.set()

    async def disconnect(self):
        pass


class FakeClient:
    def __init__(self, held=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.session = FakeSession()
        if not held:
            self.release.set()

    async def create_session(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return self.session

    async def delete_session(self, sid):
        pass

    async def stop(self):
        pass


async def test_context_metadata_does_not_initialize_and_redacts_native_values(store, tmp_path):
    client = FakeClient()
    runtime = Runtime(Settings(data=tmp_path, access_token="fixture-secret"), store, client)
    sid = (await store.create_session("Context", "mock"))["id"]
    assert await runtime.metadata(sid) == {"session": None, "context": None, "attribution": None}
    assert not client.entered.is_set()

    async def metadata(*args):
        return {"value": "fixture-secret"}

    client.session.rpc = SimpleNamespace(
        metadata=SimpleNamespace(
            snapshot=metadata,
            context_info=metadata,
            get_context_attribution=metadata,
        )
    )
    await runtime.session(sid)
    assert all(value == {"value": "[redacted]"} for value in (await runtime.metadata(sid)).values())
    await runtime.close()


async def test_summary_cache_is_session_history_and_model_scoped(store, tmp_path):
    client = FakeClient()
    client.session.wait.set()
    runtime = Runtime(Settings(data=tmp_path, access_token="fixture-secret"), store, client)
    runtime.models = [{"id": "gpt-5.6-luna"}]
    sid = (await store.create_session("Summary", "mock"))["id"]
    other = (await store.create_session("Other", "mock"))["id"]
    prompt = "a" * 1496 + "fixture-secret"
    for id_ in (sid, other):
        turn = await store.submit(id_, prompt, "initial")
        await store.execute("UPDATE turns SET status='done' WHERE id=?", (turn["id"],))
    assert await runtime.summarize(sid) == await runtime.summarize(sid)
    assert len(client.session.sent) == 1
    assert json.loads(client.session.sent[0])[0]["prompt"] == "a" * 1496 + "[red"
    await runtime.summarize(other)
    assert len(client.session.sent) == 2
    turn = await store.submit(sid, "New history", "new")
    await store.execute("UPDATE turns SET status='done' WHERE id=?", (turn["id"],))
    await runtime.summarize(sid)
    assert len(client.session.sent) == 3
    runtime.models = [{"id": "gpt-6-luna"}]
    assert (await runtime.summarize(sid))["model"] == "gpt-6-luna"
    assert len(client.session.sent) == 4
    await runtime.delete_session(sid)
    assert sid not in runtime.summary_cache
    await runtime.close()


async def test_queued_delegate_is_not_sent_after_parent_settles(store, tmp_path):
    client = FakeClient()
    runtime = Runtime(Settings(data=tmp_path), store, client)
    sid = (await store.create_session("Caller", "mock"))["id"]
    target = (await store.create_session("Worker", "mock", "worker"))["id"]
    await store.rename_session(target, "Worker", "worker", True)
    parent = await store.submit(sid, "Parent", "parent")
    await store.execute("UPDATE turns SET status='running' WHERE id=?", (parent["id"],))
    await store.submit(
        target,
        "Never run",
        f"delegate:{parent['id']}:batch",
        kind="delegation",
        sender_id=sid,
        outgoing=(parent["id"], "@worker", "Never run", None),
    )
    await runtime.abort(sid)
    runtime.kick(target)
    await asyncio.wait_for(runtime.workers[target], 3)
    snapshot = await store.snapshot(target)
    assert snapshot["turns"][0]["status"] == "cancelled"
    assert snapshot["events"][-1]["type"] == "app.turn_settled"
    assert not client.entered.is_set()
    await runtime.close()


async def test_model_changes_apply_only_when_worker_sends(store, tmp_path):
    client = FakeClient()
    client.session.wait.set()
    runtime = Runtime(Settings(data=tmp_path), store, client)
    sid = (await store.create_session("Model", "mock"))["id"]
    await runtime.session(sid)
    await runtime.change_model(sid, "new-model", deferred=True)
    assert not hasattr(client.session, "model")
    await runtime.enqueue(sid, "Use new model", "next")
    await asyncio.wait_for(runtime.workers[sid], 3)
    assert client.session.model == "new-model"
    assert client.session.sent == ["Use new model"]
    await runtime.close()


async def test_abort_during_construction_never_sends(store, tmp_path):
    client = FakeClient(held=True)
    runtime = Runtime(Settings(data=tmp_path), store, client)
    sid = (await store.create_session("Agent", "mock"))["id"]
    turn = await runtime.enqueue(sid, "Must not run", "stable")
    await asyncio.wait_for(client.entered.wait(), 3)
    await runtime.abort(sid)
    client.release.set()
    await asyncio.sleep(0)
    assert not client.session.sent
    assert not runtime.active
    assert (await store.one("SELECT status FROM turns WHERE id=?", (turn["id"],)))[
        "status"
    ] == "cancelled"
    await runtime.close()


async def test_projection_failure_aborts_and_quarantines(store, tmp_path, monkeypatch):
    client = FakeClient()
    runtime = Runtime(Settings(data=tmp_path), store, client)
    sid = (await store.create_session("Agent", "mock"))["id"]

    original_event = store.event

    async def fail(sid, tid, kind, *args, **kwargs):
        if kind == "user.message":
            raise OSError("projection unavailable")
        return await original_event(sid, tid, kind, *args, **kwargs)

    async def abort_failed():
        client.session.aborted += 1
        raise OSError("abort unavailable")

    monkeypatch.setattr(store, "event", fail)
    monkeypatch.setattr(client.session, "abort", abort_failed)
    first = await store.submit(sid, "Do not outlive failed publication", "stable")
    second = await store.submit(sid, "Do not send queued work after quarantine", "later")
    runtime.kick(sid)
    await asyncio.wait_for(asyncio.gather(runtime.workers[sid], return_exceptions=True), 3)
    assert client.session.aborted == 1
    assert sid in runtime.quarantined
    assert not runtime.active
    assert client.session.sent == ["Do not outlive failed publication"]
    turns = {row["id"]: row for row in (await store.snapshot(sid))["turns"]}
    assert turns[first["id"]]["status"] == "error"
    assert turns[first["id"]]["error"] == "projection unavailable"
    assert turns[second["id"]]["status"] == "queued"
    with pytest.raises(ValueError, match="recovery"):
        await runtime.enqueue(sid, "later", "new")
    await runtime.close()


async def test_approval_decision_publishes_settlement_for_other_clients(store, tmp_path):
    runtime = Runtime(Settings(data=tmp_path), store, FakeClient())
    sid = (await store.create_session("Agent", "mock"))["id"]
    approval = asyncio.create_task(runtime.request_approval(sid, {"kind": "shell"}))
    try:
        async with asyncio.timeout(3):
            while not (snapshot := await store.snapshot(sid))["approvals"]:
                await asyncio.sleep(0.01)
        (pending,) = snapshot["approvals"]
        (notification,) = snapshot["events"]
        assert notification["type"] == "app.approval"
        await runtime.decide(pending["id"], False)
        assert await approval is False
        snapshot = await store.snapshot(sid)
        assert snapshot["approvals"] == []
        assert [e["type"] for e in await store.events(sid, notification["seq"])] == ["app.approval"]
    finally:
        if not approval.done():
            approval.cancel()
            await asyncio.gather(approval, return_exceptions=True)


async def test_abort_publishes_settlement_for_queued_turns(store, tmp_path):
    runtime = Runtime(Settings(data=tmp_path), store, FakeClient())
    sid = (await store.create_session("Agent", "mock"))["id"]
    queued = await store.submit(sid, "Queued", "queued")
    assert (await runtime.abort(sid))["status"] == "cancelled"
    snapshot = await store.snapshot(sid)
    assert snapshot["turns"][0]["status"] == "cancelled"
    assert snapshot["events"][-1]["type"] == "app.turn_settled"
    assert snapshot["events"][-1]["turn_id"] == queued["id"]


async def test_enqueue_during_idle_check_does_not_lose_wakeup(store, tmp_path, monkeypatch):
    client = FakeClient()
    client.session.wait.set()
    runtime = Runtime(Settings(data=tmp_path), store, client)
    sid = (await store.create_session("Idle race", "mock"))["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.one
    held = False

    async def delayed(sql, args=()):
        nonlocal held
        result = await original(sql, args)
        if not held and "FROM turns" in sql and "status='queued'" in sql and result is None:
            held = True
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(store, "one", delayed)
    runtime.kick(sid)
    await asyncio.wait_for(entered.wait(), 3)
    turn = await runtime.enqueue(sid, "Must wake up", "idle-race")
    release.set()
    await asyncio.wait_for(runtime.workers[sid], 3)
    assert (await original("SELECT status FROM turns WHERE id=?", (turn["id"],)))[
        "status"
    ] == "done"
    assert client.session.sent == ["Must wake up"]
    await runtime.close()
