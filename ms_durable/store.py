"""Application catalogue/queue and lossless SDK event projection."""

from __future__ import annotations

import asyncio
import json
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

    async def create_session(self, title, model):
        sid = uid()
        await self.execute(
            "INSERT INTO sessions(id,title,model,created_at) VALUES(?,?,?,?)",
            (sid, title, model, time.time()),
        )
        return await self.session(sid)

    async def session(self, sid):
        row = await self.one("SELECT * FROM sessions WHERE id=?", (sid,))
        if row is None:
            raise KeyError("Unknown session")
        return row

    async def submit(self, sid, prompt, request_id, kind="prompt"):
        await self.session(sid)
        async with self.transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO turns(id,session_id,request_id,prompt,status,created_at,kind) VALUES(?,?,?,?, 'queued',?,?)",
                (uid(), sid, request_id, prompt, time.time(), kind),
            )
            cursor = await db.execute(
                "SELECT * FROM turns WHERE session_id=? AND request_id=?", (sid, request_id)
            )
            result = dict(await cursor.fetchone())
            if result["prompt"] != prompt or result["kind"] != kind:
                raise ValueError("request_id already used for different input")
            return result

    async def event(self, sid, tid, kind, data, sdk_id=None):
        await self.execute(
            "INSERT OR IGNORE INTO events(session_id,turn_id,sdk_id,type,data) VALUES(?,?,?,?,?)",
            (sid, tid, sdk_id, kind, json.dumps(data, default=str)),
        )

    async def events(self, sid, after=0):
        rows = await self.rows(
            "SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 2000",
            (sid, after),
        )
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    async def snapshot(self, sid):
        return {
            "session": await self.session(sid),
            "turns": await self.rows(
                "SELECT * FROM turns WHERE session_id=? ORDER BY created_at", (sid,)
            ),
            "events": await self.events(sid),
            "approvals": await self.rows(
                "SELECT id,request,status FROM approvals WHERE session_id=? AND status='pending'",
                (sid,),
            ),
        }
