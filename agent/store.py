"""Application catalogue/queue and lossless SDK event projection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .media import MAX_SHARED, SHARED_CHUNK

# External-content FTS stores postings, not a second copy of the timeline.
# Index final answers once, rather than indexing every streaming fragment.
TIMELINE_FTS = """
BEGIN IMMEDIATE;
CREATE VIEW timeline_event_text AS
 SELECT seq, session_id, type, substr(data,1,32768) AS body FROM events
 WHERE type NOT IN ('assistant.message_delta','assistant.reasoning_delta','assistant.message','user.message');
CREATE TABLE timeline_turn_keys (docid INTEGER PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE);
INSERT INTO timeline_turn_keys(turn_id) SELECT id FROM turns;
CREATE VIEW timeline_turn_text AS
 SELECT k.docid,t.session_id,t.prompt,t.answer,t.error
 FROM timeline_turn_keys k JOIN turns t ON t.id=k.turn_id;
CREATE VIRTUAL TABLE turn_fts USING fts5(
 session_id, prompt, answer, error, content='timeline_turn_text', content_rowid='docid',
 tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE event_fts USING fts5(
 session_id, type, body, content='timeline_event_text', content_rowid='seq',
 tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER turn_fts_insert AFTER INSERT ON turns BEGIN
 INSERT INTO timeline_turn_keys(turn_id) VALUES(new.id);
 INSERT INTO turn_fts(rowid,session_id,prompt,answer,error)
 VALUES(last_insert_rowid(),new.session_id,new.prompt,new.answer,new.error);
END;
CREATE TRIGGER turn_fts_delete AFTER DELETE ON turns BEGIN
 INSERT INTO turn_fts(turn_fts,rowid,session_id,prompt,answer,error)
 SELECT 'delete',docid,old.session_id,old.prompt,old.answer,old.error
 FROM timeline_turn_keys WHERE turn_id=old.id;
 DELETE FROM timeline_turn_keys WHERE turn_id=old.id;
END;
CREATE TRIGGER turn_fts_update AFTER UPDATE OF session_id,prompt,answer,error ON turns
 WHEN old.session_id IS NOT new.session_id OR old.prompt IS NOT new.prompt
   OR old.answer IS NOT new.answer OR old.error IS NOT new.error BEGIN
 INSERT INTO turn_fts(turn_fts,rowid,session_id,prompt,answer,error)
 SELECT 'delete',docid,old.session_id,old.prompt,old.answer,old.error
 FROM timeline_turn_keys WHERE turn_id=old.id;
 INSERT INTO turn_fts(rowid,session_id,prompt,answer,error)
 SELECT docid,new.session_id,new.prompt,new.answer,new.error
 FROM timeline_turn_keys WHERE turn_id=new.id;
END;
CREATE TRIGGER event_fts_insert AFTER INSERT ON events BEGIN
 INSERT INTO event_fts(rowid,session_id,type,body)
 SELECT seq,session_id,type,body FROM timeline_event_text WHERE seq=new.seq;
END;
CREATE TRIGGER event_fts_delete BEFORE DELETE ON events BEGIN
 INSERT INTO event_fts(event_fts,rowid,session_id,type,body)
 SELECT 'delete',seq,session_id,type,body FROM timeline_event_text WHERE seq=old.seq;
END;
INSERT INTO turn_fts(turn_fts) VALUES('rebuild');
INSERT INTO event_fts(event_fts) VALUES('rebuild');
COMMIT;
"""


def uid() -> str:
    return str(uuid.uuid4())


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
 created_at REAL NOT NULL, initialized INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS turns(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 request_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
 answer TEXT NOT NULL DEFAULT '', error TEXT, created_at REAL NOT NULL,
 UNIQUE(session_id,request_id));
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
 turn_id TEXT, sdk_id TEXT, type TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(session_id,sdk_id));
CREATE INDEX IF NOT EXISTS events_session ON events(session_id,seq);
CREATE INDEX IF NOT EXISTS chat_sent_session ON events(session_id,seq) WHERE type='app.chat_sent';
CREATE INDEX IF NOT EXISTS turns_session_recent ON turns(session_id,created_at DESC);
CREATE INDEX IF NOT EXISTS pending_turns ON turns(session_id,status) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
 request TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS link_preview_cache(
 url TEXT NOT NULL, scope TEXT NOT NULL, data TEXT NOT NULL, expires_at INTEGER NOT NULL,
 asset_id TEXT UNIQUE, mime TEXT, content BLOB,
 PRIMARY KEY(url,scope));
CREATE INDEX IF NOT EXISTS link_preview_expiry ON link_preview_cache(expires_at);
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_plans(
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn_images(
 turn_id TEXT NOT NULL REFERENCES turns(id), ordinal INTEGER NOT NULL,
 name TEXT NOT NULL, mime TEXT NOT NULL, digest TEXT NOT NULL, content BLOB NOT NULL,
 PRIMARY KEY(turn_id,ordinal));
CREATE TABLE IF NOT EXISTS shared_files(
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
 name TEXT NOT NULL, mime TEXT NOT NULL, digest TEXT NOT NULL, content BLOB NOT NULL,
 UNIQUE(turn_id,name,digest));
CREATE TABLE IF NOT EXISTS sdk_files(
 namespace TEXT NOT NULL, path TEXT NOT NULL, content BLOB,
 is_directory INTEGER NOT NULL, mtime REAL NOT NULL,
 PRIMARY KEY(namespace,path));
""")
        columns = await (await self.db.execute("PRAGMA table_info(turns)")).fetchall()
        if "kind" not in {row["name"] for row in columns}:
            await self.db.execute(
                "ALTER TABLE turns ADD COLUMN kind TEXT NOT NULL DEFAULT 'prompt'"
            )
        names = {row["name"] for row in columns}
        for name, datatype in [("sender_id", "TEXT"), ("finished_at", "REAL")]:
            if name not in names:
                await self.db.execute(f"ALTER TABLE turns ADD COLUMN {name} {datatype}")
        columns = await (await self.db.execute("PRAGMA table_info(sessions)")).fetchall()
        if "handle" not in {row["name"] for row in columns}:
            await self.db.execute("ALTER TABLE sessions ADD COLUMN handle TEXT")
        await self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS session_handle ON sessions(handle)"
        )
        if "delegation_enabled" not in {row["name"] for row in columns}:
            await self.db.execute(
                "ALTER TABLE sessions ADD COLUMN delegation_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "description" not in {row["name"] for row in columns}:
            await self.db.execute(
                "ALTER TABLE sessions ADD COLUMN description TEXT NOT NULL DEFAULT ''"
            )
        if not await self.one("SELECT 1 FROM sqlite_schema WHERE name='turn_fts'"):
            try:
                await self.db.executescript(TIMELINE_FTS)
            except BaseException:
                await self.db.rollback()
                raise
        # Never re-send an ambiguous admitted prompt or replay an unknown external effect.
        await self.db.execute(
            "UPDATE turns SET status='interrupted',error='Server stopped during this turn; inspect history before continuing.' WHERE status='running'"
        )
        await self.db.execute(
            "UPDATE turns SET status='cancelled', error='Delegating turn stopped before delivery.' WHERE kind='delegation' AND status='queued'"
        )
        await self.db.execute("UPDATE approvals SET status='expired' WHERE status='pending'")

    async def close(self):
        await self.db.close()

    @asynccontextmanager
    async def transaction(self, *, write=True):
        async with self.lock:
            try:
                await self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield self.db
                await self.db.commit()
            except BaseException:
                # BEGIN is dispatched to aiosqlite's thread even if its await is cancelled.
                await self.db.rollback()
                raise

    async def rows(self, sql, args=()):
        async with self.lock:
            cursor = await self.db.execute(sql, args)
            return [dict(row) for row in await cursor.fetchall()]

    async def one(self, sql, args=()):
        rows = await self.rows(sql, args)
        return rows[0] if rows else None

    async def execute(self, sql, args=()):
        async with self.transaction() as db:
            await db.execute(sql, args)

    async def create_session(self, title, model, handle=None):
        handle = self.normalize_handle(handle) if handle is not None else None
        sid = uid()
        try:
            await self.execute(
                "INSERT INTO sessions(id,title,model,created_at,handle) VALUES(?,?,?,?,?)",
                (sid, title, model, time.time(), handle),
            )
        except aiosqlite.IntegrityError:
            raise ValueError("Agent name already exists") from None
        return await self.session(sid)

    @staticmethod
    def normalize_description(description):
        description = " ".join(description.split())
        if len(description) > 280:
            raise ValueError("Session descriptions must be at most 280 characters")
        return description

    async def set_session_description(self, sid, description):
        description = self.normalize_description(description)
        async with self.transaction() as db:
            updated = await db.execute(
                "UPDATE sessions SET description=? WHERE id=? AND description<>?",
                (description, sid, description),
            )
            if updated.rowcount:
                await db.execute(
                    "INSERT INTO events(session_id,type,data) VALUES(?,?,?)",
                    (
                        sid,
                        "app.session_description_updated",
                        json.dumps({"description": description}),
                    ),
                )
            elif not await (
                await db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,))
            ).fetchone():
                raise KeyError("Unknown session")
        return {"id": sid, "description": description}

    async def rename_session(self, sid, title, handle, delegation_enabled=None, description=None):
        handle = self.normalize_handle(handle)
        async with self.transaction() as db:
            cursor = await db.execute("SELECT * FROM sessions WHERE id=?", (sid,))
            previous = await cursor.fetchone()
            if previous is None:
                raise KeyError("Unknown session")
            session = dict(previous)
            enabled = (
                session["delegation_enabled"]
                if delegation_enabled is None
                else int(delegation_enabled)
            )
            updated = {
                "title": title,
                "handle": handle,
                "delegation_enabled": enabled,
                "description": session["description"]
                if description is None
                else self.normalize_description(description),
            }
            if any(session[key] != value for key, value in updated.items()):
                try:
                    await db.execute(
                        "UPDATE sessions SET title=?,handle=?,delegation_enabled=?,description=? WHERE id=?",
                        (*updated.values(), sid),
                    )
                except aiosqlite.IntegrityError:
                    raise ValueError("Agent name already exists") from None
                await db.execute(
                    "INSERT INTO events(session_id,type,data) VALUES(?,?,?)",
                    (
                        sid,
                        "app.session_renamed",
                        json.dumps(updated),
                    ),
                )
                session.update(updated)
            return session

    async def cached_preview(self, url, scope):
        row = await self.one(
            "SELECT data FROM link_preview_cache WHERE url=? AND scope=? AND expires_at>?",
            (url, scope, int(time.time())),
        )
        return json.loads(row["data"]) if row else None

    async def cache_preview(self, url, scope, data, image=None):
        now = int(time.time())
        asset_id = uid() if image else None
        mime, content = image or (None, None)
        if asset_id:
            data["image"] = "/media/preview/" + asset_id
        async with self.transaction() as db:
            await db.execute("DELETE FROM link_preview_cache WHERE expires_at<=?", (now,))
            await db.execute(
                "INSERT INTO link_preview_cache VALUES(?,?,?,?,?,?,?) ON CONFLICT(url,scope) "
                "DO UPDATE SET data=excluded.data,expires_at=excluded.expires_at, "
                "asset_id=excluded.asset_id,mime=excluded.mime,content=excluded.content",
                (url, scope, json.dumps(data), now + 30 * 86400, asset_id, mime, content),
            )

    async def preview_settings(self):
        row = await self.one("SELECT value FROM app_settings WHERE key='link_previews'")
        mode = row["value"] if row else "off"
        return {"enabled": mode != "off", "any_domain": mode == "public"}

    async def set_preview_settings(self, enabled, any_domain):
        mode = ("public" if any_domain else "allowlist") if enabled else "off"
        await self.execute(
            "INSERT INTO app_settings VALUES('link_previews',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (mode,),
        )

    async def approvals_enabled(self):
        row = await self.one("SELECT value FROM app_settings WHERE key='approval_prompts'")
        return row is None or row["value"] != "off"

    async def set_approvals_enabled(self, enabled):
        await self.execute(
            "INSERT INTO app_settings VALUES('approval_prompts',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("on" if enabled else "off",),
        )

    async def list_sessions(self):
        return await self.rows("""
            SELECT sessions.*, CASE
                WHEN EXISTS (SELECT 1 FROM turns WHERE session_id=sessions.id AND status='running') THEN 'running'
                WHEN EXISTS (SELECT 1 FROM turns WHERE session_id=sessions.id AND status='queued') THEN 'queued'
                ELSE 'idle'
            END AS activity FROM sessions ORDER BY created_at DESC
        """)

    async def delete_session(self, sid):
        async with self.transaction() as db:
            if not await (await db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,))).fetchone():
                raise KeyError("Unknown session")
            if await (
                await db.execute(
                    "SELECT 1 FROM turns WHERE session_id=? AND status IN ('queued','running')",
                    (sid,),
                )
            ).fetchone():
                raise ValueError(
                    "Stop or finish queued and running work before deleting this session"
                )
            for sql in (
                "DELETE FROM turn_images WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?)",
                "DELETE FROM shared_files WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?)",
                "DELETE FROM turns WHERE session_id=?",
                "DELETE FROM events WHERE session_id=?",
                "DELETE FROM approvals WHERE session_id=?",
                "DELETE FROM session_plans WHERE session_id=?",
                "DELETE FROM sdk_files WHERE namespace=?",
                "DELETE FROM sessions WHERE id=?",
            ):
                await db.execute(sql, (sid,))

    async def session(self, sid):
        row = await self.one("SELECT * FROM sessions WHERE id=?", (sid,))
        if row is None:
            raise KeyError("Unknown session")
        return row

    @staticmethod
    def normalize_handle(handle):
        handle = handle.strip().removeprefix("@").lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", handle):
            raise ValueError(
                "Use an agent name of 1–32 letters, digits, underscores or hyphens, starting with a letter"
            )
        return handle

    async def resolve_session(self, target):
        if not target.startswith("@"):
            return await self.session(target)
        row = await self.one(
            "SELECT * FROM sessions WHERE handle=?", (self.normalize_handle(target),)
        )
        if row is None:
            raise KeyError("Unknown agent")
        return row

    async def submit(
        self, sid, prompt, request_id, kind="prompt", *, images=(), sender_id=None, outgoing=None
    ):
        async with self.transaction() as db:
            session = await (
                await db.execute("SELECT delegation_enabled FROM sessions WHERE id=?", (sid,))
            ).fetchone()
            if session is None:
                raise KeyError("Unknown session")
            if kind == "delegation":
                if not outgoing or not sender_id or sender_id == sid:
                    raise ValueError("Delegation requires a different, active caller")
                parent = outgoing[0]
                if not await (
                    await db.execute(
                        "SELECT 1 FROM turns WHERE id=? AND session_id=? AND status='running' AND sender_id IS NULL AND kind='prompt'",
                        (parent, sender_id),
                    )
                ).fetchone():
                    raise ValueError("Delegating turn is no longer active")
                existing = await (
                    await db.execute(
                        "SELECT 1 FROM turns WHERE session_id=? AND request_id=?", (sid, request_id)
                    )
                ).fetchone()
                if not existing:
                    if not session["delegation_enabled"]:
                        raise ValueError("Session has not opted in to delegation")
                    if await (
                        await db.execute(
                            "SELECT 1 FROM turns WHERE session_id=? AND status IN ('queued','running') LIMIT 1",
                            (sid,),
                        )
                    ).fetchone():
                        raise ValueError("Session is busy")
            insert = await db.execute(
                "INSERT OR IGNORE INTO turns(id,session_id,request_id,prompt,status,created_at,kind,sender_id) "
                "VALUES(?,?,?,?, 'queued',?,?,?)",
                (uid(), sid, request_id, prompt, time.time(), kind, sender_id),
            )
            cursor = await db.execute(
                "SELECT * FROM turns WHERE session_id=? AND request_id=?", (sid, request_id)
            )
            result = dict(await cursor.fetchone())
            tid = result["id"]
            if insert.rowcount:
                await db.executemany(
                    "INSERT INTO turn_images VALUES(?,?,?,?,?,?)",
                    [(tid, ordinal, *image) for ordinal, image in enumerate(images)],
                )
            cursor = await db.execute(
                "SELECT ordinal,name,mime,digest FROM turn_images WHERE turn_id=? ORDER BY ordinal",
                (tid,),
            )
            media = [dict(row) for row in await cursor.fetchall()]
            if (
                result["prompt"] != prompt
                or result["kind"] != kind
                or result["sender_id"] != sender_id
                or [(item["name"], item["mime"], item["digest"]) for item in media]
                != [image[:3] for image in images]
            ):
                raise ValueError("request_id already used for different input")
            result["images"] = media
            if insert.rowcount:
                await db.execute(
                    "INSERT INTO events(session_id,turn_id,type,data) VALUES(?,?,?,?)",
                    (
                        sid,
                        tid,
                        "app.chat_reply" if kind == "chat_reply" else "app.turn_queued",
                        "{}",
                    ),
                )
                if outgoing:
                    source_turn, destination, message, tool_call_id = outgoing
                    await db.execute(
                        "INSERT INTO events(session_id,turn_id,type,data) VALUES(?,?,?,?)",
                        (
                            sender_id,
                            source_turn,
                            "app.chat_sent",
                            json.dumps(
                                {
                                    "destination": destination,
                                    "message": message,
                                    "turn_id": tid,
                                    **({"tool_call_id": tool_call_id} if tool_call_id else {}),
                                }
                            ),
                        ),
                    )
            return result

    async def event(self, sid, tid, kind, data, sdk_id=None):
        await self.execute(
            "INSERT OR IGNORE INTO events(session_id,turn_id,sdk_id,type,data) VALUES(?,?,?,?,?)",
            (sid, tid, sdk_id, kind, json.dumps(data, default=str)),
        )

    async def events(self, sid, after=0, limit=2000):
        rows = await self.rows(
            "SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
            (sid, after, limit),
        )
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    @staticmethod
    def _plan_data(row):
        return json.loads(row["data"]) if row else {"revision": 0, "explanation": "", "items": []}

    async def plan(self, sid):
        row = await self.one("SELECT data FROM session_plans WHERE session_id=?", (sid,))
        return self._plan_data(row)

    async def change_plan(self, sid, tid, change):
        async with self.transaction() as db:
            cursor = await db.execute("SELECT data FROM session_plans WHERE session_id=?", (sid,))
            previous = self._plan_data(await cursor.fetchone())
            value = change.apply(previous)
            if (
                value["items"] == previous["items"]
                and value["explanation"] == previous["explanation"]
            ):
                return previous
            data = json.dumps(value)
            await db.execute(
                "INSERT INTO session_plans(session_id,data) VALUES(?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET data=excluded.data",
                (sid, data),
            )
            await db.execute(
                "INSERT INTO events(session_id,turn_id,type,data) VALUES(?,?,?,?)",
                (sid, tid, "app.plan_updated", data),
            )
            return value

    async def search_timeline(self, sid, query, limit=10):
        if not isinstance(query, str) or not query.strip() or len(query) > 256 or "\0" in query:
            raise ValueError("Enter search text of 1–256 characters")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Search limit must be between 1 and 20")

        # Compile literal words/phrases, never arbitrary MATCH syntax from a tool call.
        def quoted(text):
            return '"' + text.replace('"', '""') + '"'

        terms = " AND ".join(quoted(word) for word in query.split())
        scope = "session_id : " + quoted(sid) + " AND "
        async with self.lock:
            turns = await self.db.execute_fetchall(
                """
                SELECT t.id AS turn_id,t.created_at,t.status,
                    snippet(turn_fts,1,'','',' … ',20) AS prompt,
                    snippet(turn_fts,2,'','',' … ',20) AS answer,
                    snippet(turn_fts,3,'','',' … ',12) AS error
                FROM turn_fts JOIN timeline_turn_keys k ON k.docid=turn_fts.rowid
                JOIN turns t ON t.id=k.turn_id
                WHERE turn_fts MATCH ? AND t.session_id=?
                ORDER BY bm25(turn_fts,0,1.4,1,0.8), t.created_at DESC, t.id LIMIT ?
            """,
                (scope + "{prompt answer error} : (" + terms + ")", sid, limit),
            )
            events = await self.db.execute_fetchall(
                """
                SELECT e.seq,e.turn_id,e.type,snippet(event_fts,2,'','',' … ',24) AS excerpt
                FROM event_fts JOIN events e ON e.seq=event_fts.rowid
                WHERE event_fts MATCH ? AND e.session_id=?
                ORDER BY bm25(event_fts,0,1,1), e.seq DESC LIMIT ?
            """,
                (scope + "{type body} : (" + terms + ")", sid, limit),
            )

        def bounded(rows):
            return [
                {k: v[:640] if isinstance(v, str) else v for k, v in dict(row).items()}
                for row in rows
            ]

        return {"turn_matches": bounded(turns), "event_matches": bounded(events)}

    def share_file_stream(self, sid, tid, name, mime, digest, size, source):
        """Run on a worker thread, with one connection for the complete BLOB write."""
        if not 0 <= size <= MAX_SHARED:
            raise ValueError("Shared files must be at most 256 MiB")
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute(
                    "SELECT 1 FROM turns WHERE id=? AND session_id=? AND status='running'",
                    (tid, sid),
                ).fetchone():
                    raise ValueError("File sharing requires an active turn")
                row = db.execute(
                    "SELECT id FROM shared_files WHERE turn_id=? AND name=? AND digest=?",
                    (tid, name, digest),
                ).fetchone()
                if row:
                    return row[0]
                count, total = db.execute(
                    "SELECT count(*), coalesce(sum(length(content)),0) FROM shared_files WHERE turn_id=?",
                    (tid,),
                ).fetchone()
                if count >= 5 or total + size > MAX_SHARED:
                    raise ValueError("Share at most five files and 256 MiB total per turn")
                asset = uid()
                rowid = db.execute(
                    "INSERT INTO shared_files VALUES(?,?,?,?,?,zeroblob(?))",
                    (asset, tid, name, mime, digest, size),
                ).lastrowid
                # Rehash the exact bytes written. A source change after the secret
                # scan must not publish different bytes under the checked digest.
                check = hashlib.sha256()
                if size:
                    with db.blobopen("shared_files", "content", rowid) as blob:
                        remaining = size
                        while remaining:
                            chunk = source.read(min(SHARED_CHUNK, remaining))
                            if not chunk:
                                raise ValueError("The file changed while being shared")
                            remaining -= len(chunk)
                            check.update(chunk)
                            blob.write(chunk)
                if source.read(1) or check.hexdigest() != digest:
                    raise ValueError("The file changed while being shared")
                db.execute(
                    "INSERT INTO events(session_id,turn_id,type,data) VALUES(?,?,?,?)",
                    (sid, tid, "app.file_shared", "{}"),
                )
                return asset
        finally:
            db.close()

    async def snapshot(self, sid):
        async with self.transaction(write=False) as db:
            return await self._snapshot(db, sid)

    async def _snapshot(self, db, sid):
        async def rows(sql, args):
            cursor = await db.execute(sql, args)
            return [dict(row) for row in await cursor.fetchall()]

        sessions = await rows("SELECT * FROM sessions WHERE id=?", (sid,))
        if not sessions:
            raise KeyError("Unknown session")
        turns = await rows(
            "SELECT turns.*, sessions.handle AS sender_handle FROM turns LEFT JOIN sessions ON sessions.id=turns.sender_id "
            "WHERE turns.session_id=? ORDER BY turns.created_at",
            (sid,),
        )
        images = {}
        for row in await rows(
            "SELECT turn_id,ordinal,name,mime,digest FROM turn_images JOIN turns ON turns.id=turn_images.turn_id "
            "WHERE turns.session_id=? ORDER BY ordinal",
            (sid,),
        ):
            images.setdefault(row.pop("turn_id"), []).append(row)
        files = {}
        for row in await rows(
            "SELECT shared_files.id,turn_id,name,mime,length(content) AS size FROM shared_files "
            "JOIN turns ON turns.id=shared_files.turn_id WHERE turns.session_id=? ORDER BY shared_files.rowid",
            (sid,),
        ):
            files.setdefault(row.pop("turn_id"), []).append(row)
        for turn in turns:
            turn["images"] = images.get(turn["id"], [])
            turn["files"] = files.get(turn["id"], [])
        events = await rows(
            "SELECT * FROM events WHERE session_id=? ORDER BY seq LIMIT 2000", (sid,)
        )
        for event in events:
            event["data"] = json.loads(event["data"])
        plan = await rows("SELECT data FROM session_plans WHERE session_id=?", (sid,))
        return {
            "session": sessions[0],
            "turns": turns,
            "events": events,
            "sent_messages": [
                {**row, "data": json.loads(row["data"])}
                for row in await rows(
                    "SELECT seq,turn_id,type,data FROM events WHERE session_id=? AND type='app.chat_sent' ORDER BY seq",
                    (sid,),
                )
            ],
            "plan": self._plan_data(plan[0] if plan else None),
            "approvals": await rows(
                "SELECT id,request,status FROM approvals WHERE session_id=? AND status='pending'",
                (sid,),
            ),
        }
