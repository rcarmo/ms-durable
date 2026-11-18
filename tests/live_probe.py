"""Opt-in real SDK integration probe. Uses only a synthetic workspace and read-only tools."""

import asyncio
import json
import os
from pathlib import Path

from agent.config import Settings
from agent.runtime import Runtime
from agent.store import Store


async def main():
    root = Path(os.environ["MS_DURABLE_PROBE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    settings = Settings(data=root, working_directory=root / "workspace")
    settings.working_directory.mkdir(exist_ok=True)
    settings.working_directory.joinpath("AGENTS.md").write_text(
        "For every final answer include the exact marker NATIVE_AGENTS_OK.\n"
    )
    skill = root / "skills" / "probe"
    skill.mkdir(parents=True, exist_ok=True)
    skill.joinpath("SKILL.md").write_text(
        "---\nname: probe\ndescription: Integration test skill for this synthetic workspace.\n---\nWhen asked to use the probe skill, read this file and include NATIVE_SKILL_OK in the answer.\n"
    )
    settings.skill_directories = [str(root / "skills")]
    store = Store(root / "app.sqlite")
    await store.open()
    runtime = Runtime(settings, store)
    try:
        await runtime.start()
        print("MODELS", [(m.get("id"), m.get("name")) for m in runtime.models], flush=True)
        if runtime.model_error:
            raise RuntimeError(runtime.model_error)
        preferred = os.getenv("MS_DURABLE_MODEL")
        if not preferred:
            preferred = next(
                (m["id"] for m in runtime.models if "mini" in m["id"] or "haiku" in m["id"]),
                runtime.models[0]["id"],
            )
        sid = (await store.create_session("Integration probe", preferred))["id"]
        turn = await runtime.enqueue(
            sid,
            "Use the probe skill. Call list_sessions and Memento memory_status. Report the memory service readiness and both required markers. Do not write files or use shell commands.",
            "probe-1",
        )
        while True:
            row = await store.one("SELECT * FROM turns WHERE id=?", (turn["id"],))
            if row["status"] not in {"queued", "running"}:
                print("RESULT", row["status"], row["answer"], row["error"], flush=True)
                break
            pending = await store.rows("SELECT * FROM approvals WHERE status='pending'")
            for item in pending:
                request = json.loads(item["request"])
                print("PERMISSION", request, flush=True)
                # Explicit probe-only grants for observation. Never approve shell/write.
                safe = any(
                    name in json.dumps(request) for name in ["memory_status", "list_sessions"]
                )
                await runtime.decide(item["id"], safe)
            await asyncio.sleep(0.1)
        events = await store.events(sid)
        print("EVENT_TYPES", sorted({e["type"] for e in events}), flush=True)
        print(
            "SDK_FILES",
            await store.rows("SELECT namespace,path,length(content) AS bytes FROM sdk_files"),
            flush=True,
        )
        if (
            row["status"] != "done"
            or "NATIVE_AGENTS_OK" not in row["answer"]
            or "NATIVE_SKILL_OK" not in row["answer"]
        ):
            raise RuntimeError("Native integration markers were not verified")
    finally:
        await runtime.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
