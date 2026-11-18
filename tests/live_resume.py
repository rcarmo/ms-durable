"""Opt-in real-runtime reopen, model-switch and independent-session probe."""

import asyncio
import os
from pathlib import Path

from agent.config import Settings
from agent.runtime import Runtime
from agent.store import Store


async def main():
    root = Path(os.environ["MS_DURABLE_PROBE_DIR"]).resolve()
    settings = Settings()
    settings.data = root
    settings.working_directory = root / "workspace"
    settings.skill_directories = [str(root / "skills")]
    store = Store(root / "app.sqlite")
    await store.open()
    runtime = Runtime(settings, store)
    try:
        await runtime.start()
        sid = (await store.rows("SELECT id FROM sessions ORDER BY created_at"))[0]["id"]
        await runtime.change_model(sid, "claude-haiku-4.5")
        other = await store.create_session("Independent second agent", "claude-haiku-5.5")
        a = await runtime.enqueue(
            sid,
            "Without tools, recall the two NATIVE_ markers from our prior conversation. Reply with those two markers only.",
            "resume-probe-1",
        )
        b = await runtime.enqueue(
            other["id"], "Reply exactly SECOND_AGENT_OK. Do not use any tools.", "second-probe-1"
        )
        await asyncio.gather(*(runtime.workers[s] for s in [sid, other["id"]]))
        a = await store.one("SELECT * FROM turns WHERE id=?", (a["id"],))
        b = await store.one("SELECT * FROM turns WHERE id=?", (b["id"],))
        print("RESUME", a["status"], a["answer"], a["error"])
        print("SECOND", b["status"], b["answer"], b["error"])
        assert a["status"] == b["status"] == "done"
        assert "NATIVE_SKILL_OK" in a["answer"] and "NATIVE_AGENTS_OK" in a["answer"]
        assert "SECOND_AGENT_OK" in b["answer"]
        assert (await store.session(sid))["model"] == "claude-haiku-4.5"
    finally:
        try:
            await runtime.close()
        finally:
            await store.close()


if __name__ == "__main__":
    asyncio.run(main())
