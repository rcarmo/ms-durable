"""Thin async host: the Copilot SDK owns inference, tools and session recovery."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from pathlib import Path

from copilot import AgentMessageSource, CopilotClient, RuntimeConnection
from copilot.rpc import (
    MetadataContextInfoRequest,
    PermissionDecisionReject,
    SessionHistoryCompactRequest,
)
from copilot.tools import Tool, ToolBinaryResult, ToolResult

from .config import Settings
from .plans import PlanInput
from .providers import Providers
from .session_fs import SQLiteSessionFS
from .store import Store, uid

log = logging.getLogger(__name__)


def wire(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def stable_session_context(content):
    """Keep checkpoint discovery outside the changing system-prefix listing.

    The native runtime still creates checkpoints and summaries. Only its listing
    is replaced with a stable index pointer through the SDK's section API.
    """
    return re.sub(
        r"<session_context>.*?</session_context>",
        "<session_context>\nSession folder: /session-state\n"
        "Persistent artifacts: /session-state/files/\n"
        "Native compaction checkpoints, when present, are listed in "
        "/session-state/checkpoints/index.md. Read that index and relevant "
        "checkpoints to recover prior history, decisions and file changes.\n"
        "</session_context>",
        content,
        flags=re.DOTALL,
    )


class Runtime:
    def __init__(self, settings: Settings, store: Store, client=None):
        self.settings, self.store = settings, store
        self.client = client
        self.sessions = {}
        self.creating = {}
        self.workers = {}
        self.active = {}
        self.approvals = {}
        self.stopping = False
        self.models = []
        self.providers = Providers(settings.providers_file)
        self.secret_values = {settings.access_token} - {""}
        self.mcp_servers = settings.mcp_servers()
        for server in self.mcp_servers.values():
            for value in server.get("headers", {}).values():
                self.secret_values.add(value.removeprefix("Bearer "))
            for key, value in server.get("env", {}).items():
                if any(part in key.lower() for part in ("token", "secret", "password", "key")):
                    self.secret_values.add(value)
        self.model_error = None
        self.capi_available = True
        self.guard = asyncio.Lock()
        self.aborting = set()
        self.quarantined = set()
        self.abort_epochs = {}
        self.wakeups = {}

    async def start(self):
        if self.client is None:
            try:
                github = {"token": self.settings.token(), "use_logged_in_user": False}
                self.secret_values.add(github["token"])
            except (ValueError, FileNotFoundError):
                if not self.providers.connections:
                    raise
                github = {"use_logged_in_user": False}
                self.capi_available = False
            child_env = {
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PATH",
                    "LANG",
                    "LC_ALL",
                    "TMPDIR",
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                    "COPILOT_CLI_PATH",
                    "COPILOT_CLI_EXTRACT_DIR",
                }
            }
            child_home = self.settings.data / "runtime-home"
            child_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            child_env["HOME"] = str(child_home)
            self.client = CopilotClient(
                mode="coding-agent",
                runtime=RuntimeConnection.for_spawned_stdio(
                    base_directory=str(self.settings.data / "runtime"),
                    env=child_env,
                ),
                github=github,
                session_fs={
                    "initial_working_directory": str(self.settings.working_directory),
                    "session_state_path": "/session-state",
                    "conventions": "posix",
                },
            )
        await self.client.start()
        try:
            self.models = (
                [wire(m) for m in await self.client.list_models()] if self.capi_available else []
            )
        except Exception:  # noqa: BLE001 — model discovery is optional
            self.model_error = "Model discovery failed; check the server-side Copilot login."
            log.warning(self.model_error)
        self.models.extend(self.providers.catalogue())
        queued = await self.store.rows(
            "SELECT DISTINCT session_id FROM turns WHERE status='queued'"
        )
        for row in queued:
            self.kick(row["session_id"])

    async def close(self):
        self.stopping = True
        for future in list(self.approvals.values()):
            if not future.done():
                future.set_result(False)
        for sid in list(self.active):
            if sid in self.sessions:
                try:
                    await self.sessions[sid].abort()
                except Exception:  # noqa: BLE001 — attempt all shutdown cleanup
                    log.warning("Could not acknowledge session abort during shutdown")
        for task in self.workers.values():
            task.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
        # Cancelled admission can leave a harmless, in-flight native creation.
        # Let it register before detach; no cancelled worker can send a prompt.
        await asyncio.gather(*self.creating.values(), return_exceptions=True)
        # SDK 2.0.1 stop() clears its session registry before detaching, which
        # breaks sessionFs callbacks used by the final transcript flush. Explicit
        # disconnect keeps providers registered until each detach is acknowledged.
        try:
            for session in list(self.sessions.values()):
                await session.disconnect()
        finally:
            try:
                await self.client.stop()
            finally:
                await self.providers.close()

    def kick(self, sid):
        self.wakeups[sid] = self.wakeups.get(sid, 0) + 1
        if self.stopping or sid in self.aborting or sid in self.quarantined:
            return
        if sid not in self.workers or self.workers[sid].done():
            self.workers[sid] = asyncio.create_task(self.work(sid))

    async def enqueue(self, sid, prompt, request_id, kind="prompt", **options):
        if self.stopping or sid in self.aborting or sid in self.quarantined:
            raise ValueError("Session is stopping or requires recovery")
        epoch = self.abort_epochs.get(sid, 0)
        turn = await self.store.submit(sid, prompt, request_id, kind=kind, **options)
        if epoch != self.abort_epochs.get(sid, 0):
            await self.store.execute(
                "UPDATE turns SET status='cancelled' WHERE id=? AND status='queued'", (turn["id"],)
            )
            return await self.store.one("SELECT * FROM turns WHERE id=?", (turn["id"],))
        self.kick(sid)
        return turn

    async def enqueue_compaction(self, sid, instructions, request_id):
        return await self.enqueue(sid, instructions, request_id, kind="compact")

    async def metadata(self, sid):
        await self.store.session(sid)
        session = await self.session(sid)
        return {
            "session": wire(await session.rpc.metadata.snapshot()),
            "context": wire(
                await session.rpc.metadata.context_info(
                    MetadataContextInfoRequest(
                        output_token_limit=0, prompt_token_limit=0, resolve_output_token_limit=False
                    )
                )
            ),
            "attribution": wire(await session.rpc.metadata.get_context_attribution()),
        }

    def redact(self, value):
        if isinstance(value, str):
            for secret in sorted(
                self.secret_values | self.providers.secrets, key=len, reverse=True
            ):
                if secret:
                    value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    async def available_models(self):
        return self.models

    def tools(self, own_sid):
        def make(name, description, properties, required, fn):
            async def handle(invocation):
                result = await fn(invocation.arguments)
                if isinstance(result, ToolResult):
                    return result
                return ToolResult(
                    text_result_for_llm=json.dumps(self.redact(result), default=str),
                    result_type="success",
                )

            return Tool(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                handler=handle,
            )

        async def plan(args):
            change = PlanInput.model_validate(self.redact(args))
            if change.action == "read":
                if (
                    change.plan is not None
                    or change.patches is not None
                    or change.explanation is not None
                ):
                    raise ValueError("Read takes no plan, patches or explanation")
                return await self.store.plan(own_sid)
            return await self.store.change_plan(own_sid, self.active.get(own_sid), change)

        async def sessions(args):
            return await self.store.rows(
                "SELECT id,title,model,created_at,handle FROM sessions ORDER BY created_at DESC"
            )

        async def observe(args):
            info = await self.store.resolve_session(args.get("session_id", own_sid))
            sid = info["id"]
            turns = await self.store.rows(
                "SELECT * FROM turns WHERE session_id=? ORDER BY created_at DESC LIMIT 20", (sid,)
            )
            for turn in turns:
                turn["images"] = await self.store.rows(
                    "SELECT ordinal,name,mime FROM turn_images WHERE turn_id=? ORDER BY ordinal",
                    (turn["id"],),
                )
            events = await self.store.events(sid, max(0, args.get("after_seq", 0)), 101)
            return {
                "session": info,
                "turns": turns,
                "events": events[:100],
                "next_after": events[99]["seq"] if len(events) > 100 else None,
            }

        async def attachment(args):
            info = await self.store.resolve_session(args.get("session_id", own_sid))
            image = await self.store.one(
                "SELECT name,mime,content FROM turn_images JOIN turns ON turns.id=turn_images.turn_id "
                "WHERE turns.session_id=? AND turn_id=? AND ordinal=?",
                (info["id"], args["turn_id"], args["ordinal"]),
            )
            if image is None:
                raise ValueError("Unknown attachment in this session")
            name = self.redact(image["name"])
            if image["mime"] == "image/svg+xml":
                source = image["content"].decode("utf-8-sig")
                return {
                    "name": name,
                    "mime": image["mime"],
                    "source": source[:16000],
                    "truncated": len(source) > 16000,
                }
            return ToolResult(
                text_result_for_llm=json.dumps({"name": name, "mime": image["mime"]}),
                binary_results_for_llm=[
                    ToolBinaryResult(
                        type="image",
                        data=base64.b64encode(image["content"]).decode("ascii"),
                        mime_type=image["mime"],
                        description=name,
                    )
                ],
            )

        async def create(args):
            return await self.store.create_session(
                args.get("title", "Agent"),
                args.get("model") or (await self.store.session(own_sid))["model"],
                args.get("handle"),
            )

        async def send(args):
            target = await self.store.resolve_session(args["session_id"])
            if target["id"] == own_sid:
                raise ValueError(
                    "Use the normal conversation for this session; send only to another app session"
                )
            active_id = self.active.get(own_sid)
            if active_id:
                turn = await self.store.one("SELECT sender_id FROM turns WHERE id=?", (active_id,))
                if turn and turn["sender_id"]:
                    raise ValueError("An agent-delivered turn cannot send another agent message")
            prompt, request_id = args["prompt"].strip(), args["request_id"].strip()
            if not prompt or len(prompt) > 200_000 or not request_id or len(request_id) > 200:
                raise ValueError("Invalid chat message or request ID")
            return await self.enqueue(
                target["id"], prompt, f"agent:{own_sid}:{request_id}", sender_id=own_sid
            )

        async def model(args):
            # Applied when the next turn starts, using native SDK set_model.
            sid = args.get("session_id", own_sid)
            return await self.change_model(sid, args["model"], deferred=True)

        async def compact(args):
            return await self.enqueue_compaction(
                args.get("session_id", own_sid),
                args.get("instructions", ""),
                "compact:" + args["request_id"],
            )

        async def metadata(args):
            return await self.metadata(args.get("session_id", own_sid))

        string = {"type": "string"}
        plan_status = {"type": "string", "enum": ["pending", "in_progress", "completed"]}
        return [
            make(
                "app_context_status",
                "Read native token/context and compaction metadata for an app session.",
                {"session_id": string},
                [],
                metadata,
            ),
            make(
                "app_compact_session",
                "Queue native Copilot compaction at the next session boundary; stable request_id deduplicates retries.",
                {"session_id": string, "instructions": string, "request_id": string},
                ["request_id"],
                compact,
            ),
            make(
                "app_list_models",
                "List Copilot and configured Foundry/BYOK model selections.",
                {},
                [],
                lambda args: self.available_models(),
            ),
            make(
                "app_list_sessions",
                "List sessions/agents in this application database.",
                {},
                [],
                sessions,
            ),
            make(
                "app_plan",
                "Manage the current conversation’s Plan sidebar. Read the checklist, replace it with update, "
                "or apply a batch of add/update/remove patches in order. Indexes are 1-based; add without "
                "an index appends. Mark at most one step in_progress. Keep the plan current as work proceeds.",
                {
                    "action": {"type": "string", "enum": ["read", "update", "patch"]},
                    "explanation": string,
                    "plan": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["step"],
                            "properties": {"step": string, "status": plan_status},
                            "additionalProperties": False,
                        },
                    },
                    "patches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["operation"],
                            "properties": {
                                "operation": {
                                    "enum": ["add", "update", "remove"],
                                    "type": "string",
                                },
                                "index": {"type": "integer", "minimum": 1},
                                "step": string,
                                "status": plan_status,
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                ["action"],
                plan,
            ),
            make(
                "app_observe_session",
                "Inspect a session by ID or @handle; returns recent turns, attachment metadata and a page of timeline events.",
                {"session_id": string, "after_seq": {"type": "integer"}},
                [],
                observe,
            ),
            make(
                "app_read_attachment",
                "Inspect one stored chat image by turn ID and ordinal. SVG is returned as source; raster images use SDK image results.",
                {
                    "session_id": string,
                    "turn_id": string,
                    "ordinal": {"type": "integer", "minimum": 0},
                },
                ["turn_id", "ordinal"],
                attachment,
            ),
            make(
                "app_create_session",
                "Create an independent agent session in this application.",
                {"title": string, "model": string, "handle": string},
                [],
                create,
            ),
            make(
                "app_send_message",
                "Queue a one-hop message by session ID or @handle; reuse request_id for retries. Agent-delivered turns cannot send messages.",
                {"session_id": string, "prompt": string, "request_id": string},
                ["session_id", "prompt", "request_id"],
                send,
            ),
            make(
                "app_set_model",
                "Set this or another app session model for its next queued turn.",
                {"session_id": string, "model": string},
                ["model"],
                model,
            ),
        ]

    async def change_model(self, sid, model, deferred=False):
        await self.store.session(sid)
        available = {m.get("id") for m in self.models}
        if available and model not in available:
            raise ValueError("Model is not in the available Copilot model catalogue")
        if sid in self.active and not deferred:
            raise ValueError("Session is busy; change applies only at a turn boundary")
        if sid in self.sessions and sid not in self.active:
            await self.sessions[sid].set_model(model)
        await self.store.execute("UPDATE sessions SET model=? WHERE id=?", (model, sid))
        return {"session_id": sid, "model": model, "applies": "next turn"}

    async def permission(self, sid, request, invocation):
        from copilot.session import PermissionDecisionApproveOnce

        raw = self.redact(wire(request))
        if raw.get("kind") == "read":
            return PermissionDecisionApproveOnce()
        aid = uid()
        turn_id = self.active.get(sid)
        future = asyncio.get_running_loop().create_future()
        self.approvals[aid] = future
        await self.store.execute(
            "INSERT INTO approvals VALUES(?,?,?,?,?,?)",
            (aid, sid, turn_id, json.dumps(raw, default=str), "pending", time.time()),
        )
        await self.store.event(sid, turn_id, "app.approval", {"id": aid, "request": raw})
        try:
            allowed = await future
            return PermissionDecisionApproveOnce() if allowed else PermissionDecisionReject()
        finally:
            self.approvals.pop(aid, None)
            await self.store.execute(
                "UPDATE approvals SET status='expired' WHERE id=? AND status='pending'", (aid,)
            )

    async def decide(self, aid, allow):
        future = self.approvals.get(aid)
        if future is None or future.done():
            raise ValueError("Approval is no longer active")
        await self.store.execute(
            "UPDATE approvals SET status=? WHERE id=? AND status='pending'",
            ("approved" if allow else "denied", aid),
        )
        future.set_result(allow)

    async def session(self, sid):
        async with self.guard:
            if sid in self.sessions:
                return self.sessions[sid]
            if sid in self.creating:
                return await asyncio.shield(self.creating[sid])
            row = await self.store.session(sid)
            options = dict(  # noqa: C408 — mirrors SDK keyword options
                model=row["model"],
                inference=self.providers.inference(),
                github={"capi": {"enabled": self.capi_available}},
                working_directory=str(self.settings.working_directory),
                permissions={"on_request": lambda req, inv: self.permission(sid, req, inv)},
                events={"streaming": True},
                tools={"definitions": self.tools(sid)},
                skills={"enabled": True, "directories": self.settings.skill_directories},
                mcp={"servers": self.mcp_servers},
                enable_config_discovery=False,
                instructions={"skip_custom_instructions": False},
                hooks={"enable_repository_hooks": False},
                state={
                    "infinite_sessions": {"enabled": True},
                    "create_session_fs_provider": lambda session: SQLiteSessionFS(
                        self.store,
                        sid,
                        [self.settings.working_directory, *self.settings.skill_directories],
                        [
                            self.settings.auth_file,
                            self.store.path,
                            Path(str(self.store.path) + "-wal"),
                            Path(str(self.store.path) + "-shm"),
                            self.settings.data / "web-token",
                            self.settings.data / "runtime",
                            self.settings.data / "runtime-home",
                            "/workspace/.env.sh",
                            *(
                                [self.settings.providers_file]
                                if self.settings.providers_file
                                else []
                            ),
                            Path.home() / ".pi",
                            "/workspace/.pi/agent",
                            "/workspace/.piclaw",
                            *([self.settings.mcp_file] if self.settings.mcp_file else []),
                        ],
                    ),
                },
                system_message={
                    "mode": "customize",
                    "sections": {
                        name: {"action": stable_session_context}
                        for name in ["environment_context", "runtime_instructions"]
                    },
                    "content": "You are the agent in MS Durable. Use app_* tools to inspect and manage sessions in this application only. Do not access host credential files. Model changes take effect on the next turn. Tool permission denials must be respected. Discover and follow AGENTS.md and relevant skills through the native harness.",
                },
            )
            transcript_exists = await self.store.one(
                "SELECT 1 FROM sdk_files WHERE namespace=? AND path='/session-state/events.jsonl'",
                (sid,),
            )

            async def construct():
                if row["initialized"] or transcript_exists:
                    session = await self.client.resume_session(
                        sid, continue_pending_work=False, allow_transcript_recovery=False, **options
                    )
                else:
                    # Persist the native session before its first admitted prompt.
                    session = await self.client.create_session(
                        session_id=sid, name=row["title"], **options
                    )
                self.sessions[sid] = session
                await self.store.execute("UPDATE sessions SET initialized=1 WHERE id=?", (sid,))
                return session

            task = asyncio.create_task(construct())
            self.creating[sid] = task

            def settled(result):
                self.creating.pop(sid, None)
                if not result.cancelled():
                    result.exception()  # Retrieve failures even after admission was cancelled.

            task.add_done_callback(settled)
            return await asyncio.shield(task)

    async def work(self, sid):
        while not self.stopping:
            wakeup = self.wakeups.get(sid, 0)
            turn = await self.store.one(
                "SELECT * FROM turns WHERE session_id=? AND status='queued' ORDER BY created_at LIMIT 1",
                (sid,),
            )
            if not turn:
                if wakeup != self.wakeups.get(sid, 0):
                    continue
                return
            tid = turn["id"]
            queue = asyncio.Queue()
            unsubscribe = None
            consumer = None
            try:
                self.active[sid] = tid
                async with self.store.transaction() as db:
                    claimed = await db.execute(
                        "UPDATE turns SET status='running' WHERE id=? AND status='queued'", (tid,)
                    )
                    if claimed.rowcount != 1:
                        continue
                session = await self.session(sid)
                status = await self.store.one("SELECT status FROM turns WHERE id=?", (tid,))
                if status["status"] != "running":
                    continue
                await session.set_model((await self.store.session(sid))["model"])
                loop = asyncio.get_running_loop()

                def event(value, event_loop=loop, event_queue=queue):
                    event_loop.call_soon_threadsafe(event_queue.put_nowait, value)

                async def consume(event_queue=queue, turn_id=tid):
                    while True:
                        value = await event_queue.get()
                        try:
                            if value is None:
                                return
                            raw = self.redact(wire(value))
                            kind = raw.get("type", "")
                            if kind.startswith(
                                (
                                    "assistant.",
                                    "tool.execution_",
                                    "user.",
                                    "session.mcp_",
                                    "session.skills_",
                                    "skill.",
                                    "session.compaction_",
                                )
                            ) or kind in {"session.idle", "session.error"}:
                                await self.store.event(
                                    sid, turn_id, kind, raw.get("data", {}), raw.get("id")
                                )
                        finally:
                            event_queue.task_done()

                consumer = asyncio.create_task(consume())
                unsubscribe = session.on(event)
                if turn.get("kind") == "compact":
                    sending = asyncio.create_task(
                        session.rpc.history.compact(
                            SessionHistoryCompactRequest(custom_instructions=turn["prompt"] or None)
                        )
                    )
                else:
                    options = {}
                    if turn["sender_id"]:
                        options["source"] = AgentMessageSource(agent_id=turn["sender_id"])
                    prompt = turn["prompt"] or "[Image attached to the chat for display only.]"
                    sending = asyncio.create_task(
                        session.send_and_wait(
                            prompt, client_turn_id=tid, timeout=float("inf"), **options
                        )
                    )
                try:
                    done, _ = await asyncio.wait(
                        [sending, consumer], return_when=asyncio.FIRST_COMPLETED
                    )
                    if consumer in done:
                        consumer.result()  # A failed projection must stop further publication.
                        raise RuntimeError("Event projection stopped before the turn settled")
                    answer = await sending
                finally:
                    if not sending.done():
                        sending.cancel()
                        await asyncio.gather(sending, return_exceptions=True)
                # SDK owns correlation and completion. Drain callbacks before exposing done.
                await asyncio.sleep(0)
                draining = asyncio.create_task(queue.join())
                try:
                    done, _ = await asyncio.wait(
                        [draining, consumer], return_when=asyncio.FIRST_COMPLETED
                    )
                    if consumer in done:
                        consumer.result()
                    await draining
                finally:
                    if not draining.done():
                        draining.cancel()
                        await asyncio.gather(draining, return_exceptions=True)
                result = self.redact(wire(answer)) if answer is not None else {}
                text = (
                    json.dumps(result)
                    if turn.get("kind") == "compact"
                    else result.get("data", {}).get("content", "")
                )
                await self.store.execute(
                    "UPDATE turns SET status='done',answer=?,finished_at=? WHERE id=? AND status='running'",
                    (text, time.time(), tid),
                )
            except asyncio.CancelledError:
                await self.store.execute(
                    "UPDATE turns SET status='interrupted',error='Server stopped; no automatic replay.' WHERE id=? AND status='running'",
                    (tid,),
                )
                raise
            except Exception as exc:  # noqa: BLE001 — persist SDK failures
                if consumer and consumer.done() and consumer.exception():
                    self.quarantined.add(sid)
                    await session.abort()
                await self.store.execute(
                    "UPDATE turns SET status='error',error=? WHERE id=? AND status='running'",
                    (self.redact(str(exc))[:2000], tid),
                )
            finally:
                if unsubscribe:
                    unsubscribe()
                try:
                    if consumer:
                        if not consumer.done():
                            await queue.put(None)
                        await asyncio.gather(consumer, return_exceptions=True)
                finally:
                    self.active.pop(sid, None)
                await self.store.event(sid, tid, "app.turn_settled", {})

    async def abort(self, sid):
        await self.store.session(sid)
        self.abort_epochs[sid] = self.abort_epochs.get(sid, 0) + 1
        self.aborting.add(sid)
        worker = self.workers.get(sid)
        active_id = self.active.get(sid)
        # Cancel construction/admission immediately, before any await can allow
        # a newly constructed session to send a supposedly cancelled prompt.
        if worker and not worker.done():
            worker.cancel()
        try:
            await self.store.execute(
                "UPDATE turns SET status='cancelled' WHERE session_id=? AND (status IN ('queued','running') OR (id=? AND status='interrupted'))",
                (sid, active_id),
            )
            for aid, future in list(self.approvals.items()):
                row = await self.store.one("SELECT session_id FROM approvals WHERE id=?", (aid,))
                if row and row["session_id"] == sid and not future.done():
                    future.set_result(False)
            if sid in self.sessions:
                await self.sessions[sid].abort()
            if worker:
                await asyncio.gather(worker, return_exceptions=True)
        except BaseException:
            self.quarantined.add(sid)
            raise
        finally:
            self.aborting.discard(sid)
        return {"status": "cancelled"}
