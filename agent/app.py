"""Authenticated FastAPI transport. Copilot owns agent execution."""

from __future__ import annotations

import asyncio
import fcntl
import hmac
import http.client
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .media import MAX_BODY, MAX_IMAGES, SHARED_CHUNK, ImageInput, byte_range
from .previews import preview, thumbnail
from .runtime import Runtime
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / "static"


class SessionInput(BaseModel):
    title: str = Field(default="New session", min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)

    handle: str | None = Field(default=None, max_length=33)


class RenameInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    handle: str = Field(min_length=1, max_length=33)


class TurnInput(BaseModel):
    prompt: str = Field(max_length=200_000)
    request_id: str = Field(min_length=1, max_length=200)
    images: list[ImageInput] = Field(default_factory=list, max_length=MAX_IMAGES)


class ModelInput(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class CompactionInput(BaseModel):
    instructions: str = Field(default="", max_length=20000)
    request_id: str = Field(min_length=1, max_length=200)


class ApprovalInput(BaseModel):
    allow: bool
    remember: bool = False


class ApprovalSettings(BaseModel):
    enabled: bool


class PreviewSettings(BaseModel):
    enabled: bool
    any_domain: bool = False


def create_app(settings: Settings | None = None, runtime_factory=None):
    settings = settings or Settings()
    preview_slots = asyncio.Semaphore(3)
    preview_domains = {
        host.strip().lower().rstrip(".")
        for host in os.getenv("MS_DURABLE_PREVIEW_DOMAINS", "").split(",")
        if host.strip()
    }

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
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' blob: data:; frame-ancestors 'none'; base-uri 'none'"
            ),
        )
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/vendor/markdown-it.min.js", include_in_schema=False)
    async def markdown_bundle():
        return FileResponse(STATIC / "vendor" / "markdown-it.min.js", media_type="text/javascript")

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
            "link_previews": await app.state.store.preview_settings(),
            "approval_prompts": await app.state.store.approvals_enabled(),
            "summary_model": getattr(runtime, "summary_model", None),
        }

    @app.get("/api/sessions", dependencies=[Depends(auth)])
    async def sessions():
        return await app.state.store.list_sessions()

    @app.post("/api/sessions", dependencies=[Depends(auth)])
    async def new_session(value: SessionInput):
        try:
            return await app.state.store.create_session(value.title, value.model, value.handle)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete("/api/sessions/{sid}", status_code=204, dependencies=[Depends(auth)])
    async def delete_session(sid: str):
        try:
            await app.state.runtime.delete_session(sid)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(status_code=204)

    @app.post("/api/sessions/{sid}/title", dependencies=[Depends(auth)])
    async def rename_session(sid: str, value: RenameInput):
        title = value.title.strip()
        if not title:
            raise HTTPException(422, "Enter a session name")
        try:
            return await app.state.store.rename_session(sid, title, value.handle)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None

        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/sessions/{sid}/summary", dependencies=[Depends(auth)])
    async def summarize_session(sid: str):
        try:
            return await app.state.runtime.summarize(sid)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/settings/approval-prompts", dependencies=[Depends(auth)])
    async def approval_settings(value: ApprovalSettings):
        await app.state.store.set_approvals_enabled(value.enabled)
        return {"enabled": value.enabled}

    @app.get("/api/sessions/{sid}", dependencies=[Depends(auth)])
    async def session(sid: str):
        try:
            return await app.state.store.snapshot(sid)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None

    @app.post("/api/sessions/{sid}/turns", dependencies=[Depends(auth)])
    async def submit(sid: str, request: Request):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                raise HTTPException(413, "Message is too large")
            body.extend(chunk)
        try:
            value = TurnInput.model_validate_json(body)
            images = [image.decode() for image in value.images]
            if not value.prompt.strip() and not images:
                raise ValueError("Enter a message or attach an image")
        except ValueError:
            raise HTTPException(
                422, "Invalid message or image (up to three images, 1 MiB each)"
            ) from None
        try:
            options = {"images": images} if images else {}
            return await app.state.runtime.enqueue(
                sid, value.prompt.strip(), value.request_id, **options
            )
        except KeyError:
            raise HTTPException(404, "Unknown session") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/settings/link-previews", dependencies=[Depends(auth)])
    async def set_previews(value: PreviewSettings):
        await app.state.store.set_preview_settings(value.enabled, value.any_domain)
        return await app.state.store.preview_settings()

    @app.get("/api/link-preview", dependencies=[Depends(auth)])
    async def link_preview(url: str = Query(max_length=2048)):
        store = app.state.store
        prefs = await store.preview_settings()
        if not prefs["enabled"]:
            return {}
        url = url.split("#", 1)[0]
        scope = (
            "public" if prefs["any_domain"] else "allowlist:" + ",".join(sorted(preview_domains))
        )
        cached = await store.cached_preview(url, scope)
        if cached is not None:
            return cached
        if preview_slots.locked():
            return {}
        async with preview_slots:
            try:
                result = await asyncio.to_thread(preview, url, preview_domains, prefs["any_domain"])
            except (OSError, ValueError, UnicodeError, http.client.HTTPException):
                return {}
            image_url = result.pop("image", None)
            image = None
            if image_url:
                try:
                    image = await asyncio.to_thread(
                        thumbnail, image_url, preview_domains, prefs["any_domain"]
                    )
                except (OSError, ValueError, http.client.HTTPException):
                    pass  # A missing thumbnail must not discard the card metadata.
            if result or image:
                await store.cache_preview(url, scope, result, image)
            return result

    @app.get("/media/preview/{asset_id}", dependencies=[Depends(auth)])
    async def preview_media(asset_id: str):
        prefs = await app.state.store.preview_settings()
        scope = (
            "public" if prefs["any_domain"] else "allowlist:" + ",".join(sorted(preview_domains))
        )
        item = (
            await app.state.store.one(
                "SELECT mime,content FROM link_preview_cache WHERE asset_id=? AND scope=? AND expires_at > unixepoch()",
                (asset_id, scope),
            )
            if prefs["enabled"]
            else None
        )
        if item is None:
            raise HTTPException(404, "Unknown media")
        return media_response(item)

    @app.get("/media/{sid}/{tid}/{ordinal}", dependencies=[Depends(auth)])
    @app.get("/api/sessions/{sid}/turns/{tid}/images/{ordinal}", dependencies=[Depends(auth)])
    async def media(sid: str, tid: str, ordinal: int):
        item = await app.state.store.one(
            "SELECT mime,content FROM turn_images JOIN turns ON turns.id=turn_images.turn_id "
            "WHERE turns.session_id=? AND turn_id=? AND ordinal=?",
            (sid, tid, ordinal),
        )
        if item is None:
            raise HTTPException(404, "Unknown media")
        return media_response(item)

    download_key = secrets.token_bytes(32)
    shared_lookup = (
        "SELECT shared_files.rowid, name, mime, length(content), digest FROM shared_files "
        "JOIN turns ON turns.id=shared_files.turn_id WHERE turns.session_id=? AND shared_files.id=?"
    )

    def download_signature(path, expires):
        return hmac.digest(download_key, f"{path}\n{expires}".encode(), "sha256").hex()

    @app.post("/media/shared/{sid}/{asset}/file", dependencies=[Depends(auth)])
    async def authorize_download(sid: str, asset: str, request: Request):
        if not await app.state.store.one(shared_lookup, (sid, asset)):
            raise HTTPException(404, "Unknown media")
        path = request.url.path
        expires = str(int(time.time()) + 300)
        response = Response(status_code=204)
        response.set_cookie(
            "ms_shared",
            expires + "." + download_signature(path, expires),
            max_age=300,
            path=path,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @app.api_route("/media/shared/{sid}/{asset}/file", methods=["GET", "HEAD"])
    async def shared_media(sid: str, asset: str, request: Request):
        ticket = request.cookies.get("ms_shared", "")
        expires, _, signature = ticket.partition(".")
        if not (
            len(expires) <= 12
            and expires.isascii()
            and expires.isdecimal()
            and int(expires) >= int(time.time())
            and signature.isascii()
            and hmac.compare_digest(signature, download_signature(request.url.path, expires))
        ):
            await auth(request.headers.get("authorization", ""))

        # Open the BLOB in the same read snapshot as its metadata so deletion
        # cannot change a response in progress. Never load the entire BLOB.
        db = sqlite3.connect(app.state.store.path, check_same_thread=False)
        blob = None
        try:
            db.execute("BEGIN")
            item = db.execute(shared_lookup, (sid, asset)).fetchone()
            if item is None:
                raise HTTPException(404, "Unknown media")
            rowid, name, mime, size, digest = item
            etag = '"' + digest + '"'
            headers = {
                "Accept-Ranges": "bytes",
                "ETag": etag,
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe=""),
            }
            start, end, status = 0, size - 1, 200
            if (
                request.method == "GET"
                and "range" in request.headers
                and request.headers.get("if-range", etag) == etag
            ):
                try:
                    start, end = byte_range(request.headers["range"], size)
                except ValueError:
                    return Response(
                        status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
                    )
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            length = end - start + 1
            headers["Content-Length"] = str(length)
            if request.method == "HEAD" or not length:
                return Response(status_code=status, media_type=mime, headers=headers)
            blob = db.blobopen("shared_files", "content", rowid, readonly=True)
            blob.seek(start)
        finally:
            if blob is None:
                db.close()

        async def chunks():
            remaining = length
            try:
                while remaining:
                    chunk = await run_in_threadpool(blob.read, min(remaining, SHARED_CHUNK))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                blob.close()
                db.close()

        return StreamingResponse(chunks(), status_code=status, media_type=mime, headers=headers)

    def media_response(item):
        return Response(
            item["content"],
            media_type=item["mime"],
            headers={
                "Content-Disposition": "attachment",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.get("/api/sessions/{sid}/timeline", dependencies=[Depends(auth)])
    async def timeline(
        sid: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)
    ):
        try:
            await app.state.store.session(sid)
        except KeyError:
            raise HTTPException(404, "Unknown session") from None
        items = await app.state.store.events(sid, after, limit + 1)
        page = items[:limit]
        return {"events": page, "next_after": page[-1]["seq"] if len(items) > limit else None}

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
            if value.remember and not value.allow:
                raise ValueError("Only an approval can be remembered")
            await app.state.runtime.decide(aid, value.allow, remember=value.remember)
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
                    if not await app.state.store.one("SELECT 1 FROM sessions WHERE id=?", (sid,)):
                        break
                    yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    return app


app = create_app()
