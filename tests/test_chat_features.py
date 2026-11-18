import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.app import create_app
from agent.config import Settings
from agent.media import MAX_BODY, MAX_SHARED, ImageInput
from agent.runtime import Runtime

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

    async def delete_session(self, sid):
        await self.store.delete_session(sid)

    async def enqueue(self, *args, **kwargs):
        turn = await self.store.submit(*args, **kwargs)
        await self.store.event(turn["session_id"], turn["id"], "fixture", {}, turn["id"])
        return turn


def test_session_activity_and_delete_api(tmp_path):
    settings = Settings(data=tmp_path, access_token="fixture")
    headers = {"Authorization": "Bearer fixture"}
    with TestClient(create_app(settings, QueueOnly), headers=headers) as client:
        sid = client.post("/api/sessions", json={"model": "mock"}).json()["id"]
        root = f"/api/sessions/{sid}"
        assert client.get("/api/sessions").json()[0]["activity"] == "idle"
        assert client.delete(root, headers={"Authorization": ""}).status_code == 401
        assert (
            client.post(root + "/turns", json={"prompt": "work", "request_id": "work"}).status_code
            == 200
        )
        assert client.get("/api/sessions").json()[0]["activity"] == "queued"
        assert client.delete(root).status_code == 409
        idle = client.post("/api/sessions", json={"model": "mock"}).json()["id"]
        assert client.delete(f"/api/sessions/{idle}").status_code == 204
        assert client.get(f"/api/sessions/{idle}").status_code == 404
        assert [row["id"] for row in client.get("/api/sessions").json()] == [sid]


@pytest.mark.asyncio
async def test_delete_session_removes_owned_rows_only(store):
    sid = (await store.create_session("remove", "mock"))["id"]
    other = (await store.create_session("keep", "mock"))["id"]
    image = ("diagram.svg", "image/svg+xml", "digest", SVG)
    tid = (await store.submit(sid, "", "image", images=[image]))["id"]
    await store.execute("UPDATE turns SET status='running' WHERE id=?", (tid,))
    assert {row["id"]: row["activity"] for row in await store.list_sessions()} == {
        other: "idle",
        sid: "running",
    }
    with pytest.raises(ValueError, match="running"):
        await store.delete_session(sid)
    await store.execute("UPDATE turns SET status='done' WHERE id=?", (tid,))
    await store.execute("INSERT INTO sdk_files VALUES(?,?,?,0,0)", (sid, "/fixture", b"content"))
    await store.execute("INSERT INTO session_plans VALUES(?,?)", (sid, "{}"))
    await store.execute(
        "INSERT INTO approvals VALUES(?,?,?,'{}','approved',0)", ("permit", sid, tid)
    )
    receipt = await store.submit(other, "delivered", "message", sender_id=sid)
    await store.delete_session(sid)
    for table in ("turn_images", "session_plans", "approvals", "sdk_files"):
        assert not await store.rows(f"SELECT * FROM {table}")
    assert not await store.events(sid)
    assert (await store.snapshot(other))["turns"][0]["id"] == receipt["id"]
    with pytest.raises(KeyError):
        await store.submit(sid, "gone", "request")


def test_image_and_timeline_api(tmp_path):
    settings = Settings(data=tmp_path, access_token="fixture")
    headers = {"Authorization": "Bearer fixture"}
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    raster = {"name": "dot.png", "mime": "image/png", "data": base64.b64encode(png).decode()}
    image_request = {"prompt": "", "images": [IMAGE, raster], "request_id": "same"}
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
        assert second["next_after"] and second["events"][0]["seq"] > first["next_after"]
        last = client.get(root + f"/timeline?limit=2&after={second['next_after']}").json()
        assert last["next_after"] is None
        assert [event["type"] for event in last["events"]] == ["app.turn_queued", "fixture"]
    with TestClient(create_app(settings, QueueOnly), headers=headers) as client:
        path = f"/media/{sid}/{tid}/0"
        assert client.get(path, headers={"Authorization": ""}).status_code == 401
        assert client.get(path.replace(sid, "wrong-session")).status_code == 404
        image = client.get(path)
        assert image.content == SVG
        assert image.headers["content-type"] == "image/svg+xml"
        raster_response = client.get(path[:-1] + "1")
        assert (
            raster_response.content == png
            and raster_response.headers["content-type"] == "image/png"
        )
        assert image.headers["content-disposition"] == "attachment"
        assert "sandbox" in image.headers["content-security-policy"]
        stored = client.get(root).json()["turns"][0]["images"][0]
        assert stored["name"] == IMAGE["name"] and "content" not in stored


def test_reject_svg_entities():
    svg = b'<!DOCTYPE svg [<!ENTITY x "hi">]>' + SVG
    with pytest.raises(ValueError):
        ImageInput(**{**IMAGE, "data": base64.b64encode(svg).decode()}).decode()


def test_shared_file_route_is_authenticated_and_session_scoped(tmp_path):
    settings = Settings(data=tmp_path, access_token="fixture")
    app = create_app(settings, QueueOnly)
    headers = {"Authorization": "Bearer fixture"}
    with TestClient(app, headers=headers) as client:

        async def seed():
            store = app.state.store
            sid = (await store.create_session("Files", "mock"))["id"]
            tid = (await store.submit(sid, "share", "share"))["id"]
            await store.execute("UPDATE turns SET status='running' WHERE id=?", (tid,))
            media = await store.share_file(sid, tid, "test.gif", "image/gif", "digest", b"GIF89a")
            await store.execute(
                "INSERT INTO shared_files VALUES(?,?,?,'application/octet-stream','video-digest',zeroblob(?))",
                ("video-fixture", tid, "demo.mp4", MAX_SHARED),
            )
            return sid, media

        sid, asset = client.portal.call(seed)
        url = f"/media/shared/{sid}/{asset}/file"
        assert client.get(url, headers={"Authorization": ""}).status_code == 401
        assert client.get(f"/media/shared/other/{asset}/file").status_code == 404
        assert client.get("/workspace/test.gif").status_code == 404
        response = client.get(url)
        assert response.content == b"GIF89a" and response.headers["content-type"] == "image/gif"
        assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''test.gif"
        assert client.head(url).headers["content-length"] == "6"
        etag = response.headers["etag"]
        partial = client.get(url, headers={"Range": "bytes=1-3", "If-Range": etag})
        assert (partial.status_code, partial.headers["content-range"], partial.content) == (
            206,
            "bytes 1-3/6",
            b"IF8",
        )
        assert client.get(url, headers={"Range": "bytes=-2"}).content == b"9a"
        assert client.get(url, headers={"Range": "bytes=2-"}).content == b"F89a"
        assert (
            client.get(url, headers={"Range": "bytes=6-"}).headers["content-range"] == "bytes */6"
        )
        assert client.get(url, headers={"Range": "bytes=0-1,4-5"}).status_code == 416
        assert (
            client.get(url, headers={"Range": "bytes=2-", "If-Range": '"stale"'}).status_code == 200
        )
        assert client.post(url, headers={"Authorization": ""}).status_code == 401
        assert client.post(url).status_code == 204
        assert client.get(url, headers={"Authorization": "", "Range": "bytes=3-"}).content == b"89a"
        assert (
            client.get(
                f"/media/shared/other/{asset}/file", headers={"Authorization": ""}
            ).status_code
            == 401
        )
        assert client.get(f"/api/sessions/{sid}").json()["turns"][0]["files"] == [
            {"id": asset, "name": "test.gif", "mime": "image/gif", "size": 6},
            {
                "id": "video-fixture",
                "name": "demo.mp4",
                "mime": "application/octet-stream",
                "size": MAX_SHARED,
            },
        ]
        video = f"/media/shared/{sid}/video-fixture/file"
        assert client.head(video).headers["content-length"] == str(MAX_SHARED)
        n = 300_000  # Cross a streaming chunk boundary near the end of the video.
        tail = client.get(video, headers={"Range": f"bytes={MAX_SHARED - n}-"})
        assert tail.status_code == 206 and tail.content == bytes(n)
        assert (
            tail.headers["content-range"] == f"bytes {MAX_SHARED - n}-{MAX_SHARED - 1}/{MAX_SHARED}"
        )


async def test_agent_file_sharing_without_approval_snapshots_bytes(store, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    runtime = Runtime(
        Settings(data=tmp_path, working_directory=workspace, access_token="secret-fixture"), store
    )
    sid = (await store.create_session("Files", "mock"))["id"]
    tid = (await store.submit(sid, "share", "share"))["id"]
    await store.execute("UPDATE turns SET status='running' WHERE id=?", (tid,))
    runtime.active[sid] = tid
    picture = workspace / "test.gif"
    gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    picture.write_bytes(gif)
    (workspace / "wide.gif").write_bytes(gif[:6] + (5000).to_bytes(2, "little") + gif[8:])
    (workspace / "linked.gif").symlink_to(picture)
    (workspace / "report.txt").write_text("Report")
    (workspace / "sensitive.txt").write_text("secret-fixture")
    tool = next(t for t in runtime.tools(sid) if t.name == "attach_file")

    async def attach(path):
        return json.loads(
            (await tool.handler(SimpleNamespace(arguments={"path": path}))).text_result_for_llm
        )

    assert tool.skip_permission is True
    assert all(not t.skip_permission for t in runtime.tools(sid) if t.name != "attach_file")
    image = await asyncio.wait_for(attach("test.gif"), 2)
    assert not (await store.snapshot(sid))["approvals"]
    assert image["name"] == "test.gif"
    with (workspace / "too-large.mp4").open("wb") as file:
        file.truncate(MAX_SHARED + 1)
    assert (await attach("test.gif"))["id"] == image["id"]
    await attach("report.txt")
    await attach("wide.gif")
    files = {f["name"]: f["mime"] for f in (await store.snapshot(sid))["turns"][0]["files"]}
    assert files == {
        "test.gif": "image/gif",
        "report.txt": "application/octet-stream",
        "wide.gif": "application/octet-stream",
    }
    for path in ("../outside", ".env", "linked.gif", "sensitive.txt", "too-large.mp4"):
        with pytest.raises(ValueError):
            await attach(path)
    picture.unlink()
    saved = await store.one("SELECT content FROM shared_files WHERE id=?", (image["id"],))
    assert saved["content"] == gif
    await store.execute("UPDATE turns SET status='done' WHERE id=?", (tid,))
    await store.delete_session(sid)
    assert not await store.rows("SELECT id FROM shared_files")


async def test_timeline_fts_backfill_updates_and_session_scope(tmp_path):
    import sqlite3

    from agent.store import Store

    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as old:
        old.executescript("""
            CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT NOT NULL,model TEXT NOT NULL,
                created_at REAL NOT NULL,initialized INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE turns(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),
                request_id TEXT NOT NULL,prompt TEXT NOT NULL,status TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',error TEXT,created_at REAL NOT NULL, UNIQUE(session_id,request_id));
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,
                turn_id TEXT,sdk_id TEXT,type TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(session_id,sdk_id));
            INSERT INTO sessions VALUES('old','Legacy','mock',1,0);
            INSERT INTO turns VALUES('turn-old','old','req','Café backup','done','old answer',NULL,1);
            INSERT INTO events(session_id,turn_id,type,data) VALUES
                ('old','turn-old','tool.execution_complete','{"result":"backup checksum"}'),
                ('old','turn-old','assistant.message_delta','{"deltaContent":"fragmentonly"}');
        """)
    old.close()
    store = Store(path)
    await store.open()
    try:
        assert (await store.search_timeline("old", "cafe backup"))["turn_matches"][0][
            "turn_id"
        ] == "turn-old"
        assert (await store.search_timeline("old", "checksum"))["event_matches"][0]["seq"] == 1
        assert not (await store.search_timeline("old", "fragmentonly"))["event_matches"]
        assert not (await store.search_timeline("old", '" OR *'))["event_matches"]
        await store.execute("UPDATE turns SET answer=? WHERE id=?", ("fresh answer", "turn-old"))
        assert not (await store.search_timeline("old", "old answer"))["turn_matches"]
        assert (await store.search_timeline("old", "fresh"))["turn_matches"]
        other = (await store.create_session("Other", "mock", "other"))["id"]
        own = await store.submit(other, "backup elsewhere", "req")
        await store.event(other, own["id"], "tool.execution_complete", {"result": "unique result"})
        await store.event(other, own["id"], "tool.execution_complete", {"result": "stable"}, "same")
        await store.event(
            other, own["id"], "tool.execution_complete", {"result": "duplicated"}, "same"
        )
        assert not (await store.search_timeline(other, "duplicated"))["event_matches"]
        runtime = Runtime(Settings(data=tmp_path), store)
        observe = next(tool for tool in runtime.tools(other) if tool.name == "observe_session")
        result = await observe.handler(SimpleNamespace(arguments={"query": "backup", "limit": 1}))
        data = json.loads(result.text_result_for_llm)
        assert [row["turn_id"] for row in data["turn_matches"]] == [own["id"]]
        assert not data["event_matches"]
        with pytest.raises(ValueError):
            await store.search_timeline(other, "backup", 21)
        await store.delete_session("old")
        await store.db.execute("VACUUM")
        for table in ("turn_fts", "event_fts"):
            await store.execute(f"INSERT INTO {table}({table},rank) VALUES('integrity-check',1)")
    finally:
        await store.close()
    await store.open()
    try:
        assert (await store.search_timeline(other, "unique"))["event_matches"]
        assert not (await store.search_timeline("old", "backup"))["turn_matches"]
    finally:
        await store.close()


async def test_named_chat_is_deduplicated_and_one_hop(store, tmp_path, monkeypatch):
    runtime = Runtime(Settings(data=tmp_path), store)
    monkeypatch.setattr(runtime, "kick", lambda sid: None)
    alice = (await store.create_session("Review", "mock", "@Alice"))["id"]
    bob = (await store.create_session("Build", "mock", "bob"))["id"]

    async def send(sender, target):
        tool = next(t for t in runtime.tools(sender) if t.name == "chat")
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

    first = json.loads((await invoke("observe_session", session_id="@review")).text_result_for_llm)
    assert len(first["events"]) == 100
    assert [a["name"] for a in first["turns"][0]["images"]] == ["diagram.svg", "test.png"]
    tail = json.loads(
        (await invoke("observe_session", after_seq=first["next_after"])).text_result_for_llm
    )
    assert len(tail["events"]) == 3 and tail["next_after"] is None
    result = await invoke("read_attachment", turn_id=turn["id"], ordinal=0)
    assert json.loads(result.text_result_for_llm)["source"].encode() == SVG
    result = await invoke("read_attachment", turn_id=turn["id"], ordinal=1)
    assert base64.b64decode(result.binary_results_for_llm[0].data) == pixels
    with pytest.raises(ValueError, match="Unknown attachment"):
        await invoke("read_attachment", session_id=other, turn_id=turn["id"], ordinal=0)


async def test_agent_plan_is_atomic_and_session_scoped(store, tmp_path):
    runtime = Runtime(Settings(data=tmp_path), store)
    sid = (await store.create_session("Review", "mock"))["id"]
    other = (await store.create_session("Other", "mock"))["id"]
    tool = next(t for t in runtime.tools(sid) if t.name == "plan")

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
    from agent.store import Store

    reopened = Store(store.path)
    await reopened.open()
    try:
        assert (await reopened.snapshot(sid))["plan"] == second
    finally:
        await reopened.close()


def test_rename_session_preserves_identity(tmp_path):
    settings = Settings(data=tmp_path, access_token="fixture")
    with TestClient(
        create_app(settings, QueueOnly), headers={"Authorization": "Bearer fixture"}
    ) as client:
        session = client.post(
            "/api/sessions", json={"model": "mock", "title": "Old", "handle": "review"}
        ).json()
        path = f"/api/sessions/{session['id']}"
        assert (
            client.post(path + "/title", json={"title": "   ", "handle": "next"}).status_code == 422
        )
        assert (
            client.post(
                "/api/sessions/missing/title", json={"title": "New", "handle": "next"}
            ).status_code
            == 404
        )
        assert client.post(path + "/title", json={"title": " New ", "handle": "@Next"}).json() == {
            **session,
            "title": "New",
            "handle": "next",
        }
        snapshot = client.get(path).json()
        assert (
            snapshot["session"]["title"] == "New"
            and snapshot["events"][0]["type"] == "app.session_renamed"
        )
        assert client.get("/vendor/markdown-it.min.js").status_code == 200


async def test_session_approval_matches_request_not_call_id(store, tmp_path):
    runtime = Runtime(Settings(data=tmp_path), store)
    sid = (await store.create_session("Review", "mock"))["id"]
    other = (await store.create_session("Other", "mock"))["id"]
    request = {"kind": "shell", "fullCommandText": "printf hello", "toolCallId": "one"}

    async def decide_once(chat, data, allow, remember=False):
        task = asyncio.create_task(runtime.permission(chat, data, None))
        try:
            async with asyncio.timeout(3):
                while not (
                    row := await store.one(
                        "SELECT id FROM approvals WHERE session_id=? AND status='pending'", (chat,)
                    )
                ):
                    await asyncio.sleep(0.01)
                await runtime.decide(row["id"], allow, remember=remember)
                return (await task).kind
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    assert await decide_once(sid, request, True, True) == "approve-once"
    repeated = {**request, "toolCallId": "two"}
    assert (
        await asyncio.wait_for(runtime.permission(sid, repeated, None), 3)
    ).kind == "approve-once"
    assert await decide_once(other, repeated, False) == "reject"
    changed = {**repeated, "fullCommandText": "printf different"}
    assert await decide_once(sid, changed, False) == "reject"
    await store.set_approvals_enabled(False)
    assert (
        await asyncio.wait_for(runtime.permission(other, changed, None), 3)
    ).kind == "approve-once"
    await store.set_approvals_enabled(True)
    assert await decide_once(other, changed, False) == "reject"


def test_preview_policy_cache_and_thumbnail_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DURABLE_PREVIEW_DOMAINS", "example.com")
    calls = []

    def metadata(url, domains, any_domain):
        calls.append((url, domains, any_domain))
        return {"title": "Example", "image": "https://example.com/image.png"}

    monkeypatch.setattr("agent.app.preview", metadata)
    monkeypatch.setattr("agent.app.thumbnail", lambda *_: ("image/png", b"fixture-bytes"))
    settings = Settings(data=tmp_path, access_token="fixture")
    headers = {"Authorization": "Bearer fixture"}
    prefs = "/api/settings/link-previews"
    url = "/api/link-preview?url=https%3A%2F%2Fexample.com%2F"
    with TestClient(create_app(settings, QueueOnly), headers=headers) as client:
        assert client.get(url).json() == {} and calls == []
        assert client.post(prefs, json={"enabled": True}).json() == {
            "enabled": True,
            "any_domain": False,
        }
        card = client.get(url).json()
        assert card["title"] == "Example" and card["image"].startswith("/media/preview/")
        assert client.get(card["image"], headers={"Authorization": ""}).status_code == 401
        assert client.get(card["image"]).content == b"fixture-bytes"
        assert calls == [("https://example.com/", {"example.com"}, False)]
    with TestClient(create_app(settings, QueueOnly), headers=headers) as client:
        assert client.get(url).json() == card and len(calls) == 1
        client.post(prefs, json={"enabled": True, "any_domain": True})
        assert client.get(card["image"]).status_code == 404
        assert client.get(url).json()["image"] != card["image"]
        client.post(prefs, json={"enabled": True})
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(tmp_path / "app.sqlite")) as db, db:
            assert db.execute(
                "SELECT min(expires_at - unixepoch()) FROM link_preview_cache"
            ).fetchone()[0] in range(2591990, 2592001)
            db.execute("UPDATE link_preview_cache SET expires_at=0")
        assert client.get(card["image"]).status_code == 404
        assert client.get(url).json()["image"] != card["image"]
        assert len(calls) == 3


def test_external_preview_rejects_nonpublic_and_unlisted_hosts(monkeypatch):
    from agent.previews import preview

    def resolve(*args, **kwargs):
        return [(None, None, None, None, ("192.168.1.1", 443))]

    monkeypatch.setattr("agent.previews.socket.getaddrinfo", resolve)
    with pytest.raises(ValueError, match="not public"):
        preview("https://example.com/", set(), True)
    with pytest.raises(ValueError, match="not enabled"):
        preview("https://example.com/", set(), False)
