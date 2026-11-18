import asyncio

from agent.config import Settings
from agent.runtime import Runtime


async def test_abort_during_submit_cancels_late_commit(store, tmp_path, monkeypatch):
    runtime = Runtime(Settings(data=tmp_path), store, client=object())
    sid = (await store.create_session("Late submit", "mock"))["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.submit

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "submit", delayed)
    pending = asyncio.create_task(runtime.enqueue(sid, "Never admitted", "late"))
    await entered.wait()
    await runtime.abort(sid)
    release.set()
    result = await pending
    assert result["status"] == "cancelled"
    assert not runtime.workers
    snapshot = await store.snapshot(sid)
    assert [event["type"] for event in snapshot["events"]] == [
        "app.turn_queued",
        "app.turn_settled",
    ]


async def test_known_secrets_are_redacted_from_nested_output(store, tmp_path):
    runtime = Runtime(
        Settings(data=tmp_path, access_token="private-test-token"), store, client=object()
    )
    assert runtime.redact({"data": ["failed: private-test-token"]}) == {
        "data": ["failed: [redacted]"]
    }
