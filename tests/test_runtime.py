import asyncio

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

    async def stop(self):
        pass


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

    async def fail(*args, **kwargs):
        raise OSError("projection unavailable")

    monkeypatch.setattr(store, "event", fail)
    await runtime.enqueue(sid, "Do not outlive failed publication", "stable")
    await asyncio.wait_for(asyncio.gather(runtime.workers[sid], return_exceptions=True), 3)
    assert client.session.aborted == 1
    assert sid in runtime.quarantined
    assert not runtime.active
    with pytest.raises(ValueError, match="recovery"):
        await runtime.enqueue(sid, "later", "new")
    await runtime.close()


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
