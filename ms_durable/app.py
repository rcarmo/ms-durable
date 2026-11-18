"""Authenticated FastAPI transport. Copilot owns agent execution."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .runtime import Runtime
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / "static"


class SessionInput(BaseModel):
    title: str = Field(default="New session", min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)


class TurnInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    request_id: str = Field(min_length=1, max_length=200)


class ModelInput(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class CompactionInput(BaseModel):
    instructions: str = Field(default="", max_length=20000)
    request_id: str = Field(min_length=1, max_length=200)


class ApprovalInput(BaseModel):
    allow: bool


def create_app(settings: Settings | None = None, runtime_factory=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        settings.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(settings.data, 0o700)
        owner = (settings.data / "owner.lock").open("a")
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner.close()
            raise RuntimeError(
                "Another server owns this database; use one uvicorn worker"
            ) from None
        store = Store(settings.data / "app.sqlite")
        runtime = None
        try:
            await store.open()
            token_path = settings.data / "web-token"
            if not settings.access_token:
                if not token_path.exists():
                    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        stream.write(secrets.token_urlsafe(32))
                settings.access_token = token_path.read_text().strip()
            if runtime_factory:
                runtime = runtime_factory(store, settings)
            else:
                runtime = Runtime(settings, store)
            app.state.store, app.state.runtime = store, runtime
            await runtime.start()
            yield
        finally:
            try:
                if runtime:
                    await runtime.close()
            finally:
                await store.close()
                fcntl.flock(owner, fcntl.LOCK_UN)
                owner.close()

    app = FastAPI(title="MS Durable", lifespan=lifespan)

    async def auth(authorization: str = Header(default="")):
        expected = "Bearer " + settings.access_token
        if not settings.access_token or not secrets.compare_digest(authorization, expected):
            raise HTTPException(401, "Bearer token required")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health", dependencies=[Depends(auth)])
    async def health():
        return {"status": "ok"}

    @app.get("/api/config", dependencies=[Depends(auth)])
    async def config():
        runtime = app.state.runtime
        models, error = runtime.models, runtime.model_error
        skills = []
        for directory in settings.skill_directories:
            root = Path(directory)
            if root.is_dir():
                skills.extend(p.parent.name for p in root.glob("*/SKILL.md"))
        return {
            "models": models,
            "skills": sorted(set(skills)),
            "mcp_servers": sorted(settings.mcp_servers()),
            "working_directory": str(settings.working_directory),
            "auth_source": "Native Copilot login and configured BYOK profiles (server-side)",
            "error": error,
        }

    @app.get("/api/sessions", dependencies=[Depends(auth)])
    async def sessions():
        return await app.state.store.rows("SELECT * FROM sessions ORDER BY created_at DESC")

    @app.post("/api/sessions", dependencies=[Depends(auth)])
    async def new_session(value: SessionInput):
        return await app.state.store.create_session(value.title, value.model)

    @app.get("/api/sessions/{sid}", dependencies=[Depends(auth)])
    async def session(sid: str):
        try:
            return await app.state.store.snapshot(sid)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None

    @app.post("/api/sessions/{sid}/turns", dependencies=[Depends(auth)])
    async def submit(sid: str, value: TurnInput):
        try:
            return await app.state.runtime.enqueue(sid, value.prompt, value.request_id)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/sessions/{sid}/context", dependencies=[Depends(auth)])
    async def context(sid: str):
        try:
            return await app.state.runtime.metadata(sid)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None

    @app.post("/api/sessions/{sid}/compact", dependencies=[Depends(auth)])
    async def compact(sid: str, value: CompactionInput):
        try:
            return await app.state.runtime.enqueue_compaction(
                sid, value.instructions, "compact:" + value.request_id
            )
        except KeyError:
            raise HTTPException(404, "Unknown session") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/sessions/{sid}/model", dependencies=[Depends(auth)])
    async def model(sid: str, value: ModelInput):
        try:
            return await app.state.runtime.change_model(sid, value.model, deferred=True)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/sessions/{sid}/abort", dependencies=[Depends(auth)])
    async def abort(sid: str):
        try:
            await app.state.runtime.abort(sid)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None
        return {"status": "cancelled"}

    @app.post("/api/approvals/{aid}", dependencies=[Depends(auth)])
    async def approve(aid: str, value: ApprovalInput):
        try:
            await app.state.runtime.decide(aid, value.allow)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"status": "approved" if value.allow else "denied"}

    @app.get("/api/sessions/{sid}/events", dependencies=[Depends(auth)])
    async def stream(sid: str, request: Request, after: int = 0):
        try:
            await app.state.store.session(sid)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None

        async def events():
            cursor = max(after, 0)
            while not await request.is_disconnected():
                batch = await app.state.store.events(sid, cursor)
                for item in batch:
                    cursor = item["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(item)}\n\n"
                if not batch:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    return app


app = create_app()
