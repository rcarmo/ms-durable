"""Application catalogue/queue and lossless SDK event projection."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


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
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
 request TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_plans(
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn_images(
 turn_id TEXT NOT NULL REFERENCES turns(id), ordinal INTEGER NOT NULL,
 name TEXT NOT NULL, mime TEXT NOT NULL, digest TEXT NOT NULL, content BLOB NOT NULL,
 PRIMARY KEY(turn_id,ordinal));
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
        # Never re-send an ambiguous admitted prompt or replay an unknown external effect.
        await self.db.execute(
            "UPDATE turns SET status='interrupted',error='Server stopped during this turn; inspect history before continuing.' WHERE status='running'"
        )
        await self.db.execute("UPDATE approvals SET status='expired' WHERE status='pending'")

    async def close(self):
        await self.db.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                await self.db.rollback()
                raise
            else:
                await self.db.commit()

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

    async def rename_session(self, sid, title):
        async with self.transaction() as db:
            cursor = await db.execute("SELECT * FROM sessions WHERE id=?", (sid,))
            previous = await cursor.fetchone()
            if previous is None:
                raise KeyError("Unknown session")
            session = dict(previous)
            if session["title"] != title:
                await db.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))
                await db.execute(
                    "INSERT INTO events(session_id,type,data) VALUES(?,?,?)",
                    (sid, "app.session_renamed", json.dumps({"title": title})),
                )
                session["title"] = title
            return session

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

    async def submit(self, sid, prompt, request_id, kind="prompt", *, images=(), sender_id=None):
        await self.session(sid)
        async with self.transaction() as db:
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

    async def snapshot(self, sid):
        session = await self.session(sid)
        turns = await self.rows(
            "SELECT turns.*, sessions.handle AS sender_handle FROM turns LEFT JOIN sessions ON sessions.id=turns.sender_id "
            "WHERE turns.session_id=? ORDER BY turns.created_at",
            (sid,),
        )
        images = {}
        for row in await self.rows(
            "SELECT turn_id,ordinal,name,mime,digest FROM turn_images JOIN turns ON turns.id=turn_images.turn_id "
            "WHERE turns.session_id=? ORDER BY ordinal",
            (sid,),
        ):
            images.setdefault(row.pop("turn_id"), []).append(row)
        for turn in turns:
            turn["images"] = images.get(turn["id"], [])
        return {
            "session": session,
            "turns": turns,
            "events": await self.events(sid),
            "plan": await self.plan(sid),
            "approvals": await self.rows(
                "SELECT id,request,status FROM approvals WHERE session_id=? AND status='pending'",
                (sid,),
            ),
        }
