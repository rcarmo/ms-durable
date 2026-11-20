"""Copilot's session filesystem, backed by SQLite. Workspace files stay native."""

from __future__ import annotations

import asyncio
import errno
import os
import posixpath
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from copilot.rpc import DebugCollectLogsEntryKind, SessionFSReaddirWithTypesEntry
from copilot.session_fs_provider import SessionFsFileInfo, SessionFsProvider

from .store import Store


class SQLiteSessionFS(SessionFsProvider):
    def __init__(self, store: Store, namespace: str, local_roots=(), protected=(), on_read=None):
        self.store, self.namespace = store, namespace
        self.local_roots = tuple(Path(p).resolve() for p in local_roots)
        self.protected = tuple(Path(p).resolve() for p in protected)
        self.on_read = on_read

    def local(self, path):
        # sessionFs is also used by native coding tools, not just transcripts.
        # Preserve SDK tool execution with a local-workspace storage pass-through.
        normalized = self.path(path)
        if (
            not self.local_roots
            or normalized == "/session-state"
            or normalized.startswith("/session-state/")
        ):
            return None
        resolved = Path(normalized).resolve()
        if any(resolved == p or p in resolved.parents for p in self.protected):
            raise PermissionError("Credential/service state is outside the workspace adapter")
        if not any(resolved == root or root in resolved.parents for root in self.local_roots):
            raise PermissionError("Path is outside configured workspace and skill roots")
        return Path(normalized)

    @staticmethod
    def path(path):
        if not path.startswith("/") or "\x00" in path:
            raise ValueError("Session storage requires an absolute POSIX path")
        return posixpath.normpath(path)

    async def entry(self, path):
        path = self.path(path)
        row = await self.store.one(
            "SELECT * FROM sdk_files WHERE namespace=? AND path=?", (self.namespace, path)
        )
        if row is None:
            raise FileNotFoundError(errno.ENOENT, "No such session file", path)
        return row

    async def read_file(self, path):
        if local := self.local(path):
            name = local.name.upper()
            if not self.on_read or name not in {"AGENTS.MD", "SKILL.MD"}:
                return await asyncio.to_thread(local.read_text, encoding="utf-8")
            started = time.monotonic()
            content = await asyncio.to_thread(local.read_text, encoding="utf-8")
            elapsed = (time.monotonic() - started) * 1000
            await self.on_read(name.removesuffix(".MD") + ".md", elapsed, time.time())
            return content
        row = await self.entry(path)
        if row["is_directory"]:
            raise IsADirectoryError(path)
        return bytes(row["content"]).decode("utf-8")

    async def write_file(self, path, content, mode=None):
        if local := self.local(path):
            await asyncio.to_thread(local.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(local.write_text, content, encoding="utf-8")
            return
        await self.put(path, content, append=False)

    async def append_file(self, path, content, mode=None):
        if local := self.local(path):

            def append():
                local.parent.mkdir(parents=True, exist_ok=True)
                with local.open("a", encoding="utf-8") as stream:
                    stream.write(content)

            await asyncio.to_thread(append)
            return
        await self.put(path, content, append=True)

    async def put(self, path, content, append):
        path = self.path(path)
        async with self.store.transaction() as db:
            await self.parents(db, posixpath.dirname(path))
            cur = await db.execute(
                "SELECT content,is_directory FROM sdk_files WHERE namespace=? AND path=?",
                (self.namespace, path),
            )
            old = await cur.fetchone()
            if old is not None and old["is_directory"]:
                raise IsADirectoryError(path)
            data = content.encode("utf-8")
            if append and old:
                data = bytes(old["content"]) + data
            await db.execute(
                "INSERT INTO sdk_files VALUES(?,?,?,?,?) ON CONFLICT(namespace,path) DO UPDATE SET content=excluded.content,mtime=excluded.mtime",
                (self.namespace, path, data, 0, time.time()),
            )

    async def exists(self, path):
        if local := self.local(path):
            return await asyncio.to_thread(local.exists)
        return (
            await self.store.one(
                "SELECT 1 FROM sdk_files WHERE namespace=? AND path=?",
                (self.namespace, self.path(path)),
            )
            is not None
        )

    async def stat(self, path):
        if local := self.local(path):
            info = await asyncio.to_thread(local.stat)
            return SessionFsFileInfo(
                is_file=local.is_file(),
                is_directory=local.is_dir(),
                size=info.st_size,
                mtime=datetime.fromtimestamp(info.st_mtime, UTC),
                birthtime=datetime.fromtimestamp(info.st_ctime, UTC),
            )
        row = await self.entry(path)
        return SessionFsFileInfo(
            is_file=not row["is_directory"],
            is_directory=bool(row["is_directory"]),
            size=len(row["content"] or b""),
            mtime=datetime.fromtimestamp(row["mtime"], UTC),
            birthtime=datetime.fromtimestamp(row["mtime"], UTC),
        )

    async def parents(self, db, path):
        components = ["/"]
        current = ""
        for part in path.strip("/").split("/"):
            if part:
                current += "/" + part
                components.append(current)
        for value in components:
            cur = await db.execute(
                "SELECT is_directory FROM sdk_files WHERE namespace=? AND path=?",
                (self.namespace, value),
            )
            old = await cur.fetchone()
            if old and not old["is_directory"]:
                raise NotADirectoryError(value)
            await db.execute(
                "INSERT OR IGNORE INTO sdk_files VALUES(?,?,NULL,1,?)",
                (self.namespace, value, time.time()),
            )

    async def mkdir(self, path, recursive=False, mode=None):
        if local := self.local(path):
            await asyncio.to_thread(
                local.mkdir, mode=mode or 0o755, parents=recursive, exist_ok=recursive
            )
            return
        path = self.path(path)
        async with self.store.transaction() as db:
            if not recursive and path != "/":
                cur = await db.execute(
                    "SELECT is_directory FROM sdk_files WHERE namespace=? AND path=?",
                    (self.namespace, posixpath.dirname(path)),
                )
                parent = await cur.fetchone()
                if not parent:
                    raise FileNotFoundError(errno.ENOENT, "No parent", path)
                if not parent["is_directory"]:
                    raise NotADirectoryError(path)
            await self.parents(db, path)

    async def readdir(self, path):
        if local := self.local(path):
            return await asyncio.to_thread(lambda: sorted(p.name for p in local.iterdir()))
        if not (await self.entry(path))["is_directory"]:
            raise NotADirectoryError(path)
        prefix = self.path(path).rstrip("/") + "/"
        rows = await self.store.rows(
            "SELECT path FROM sdk_files WHERE namespace=?", (self.namespace,)
        )
        return sorted(
            {
                r["path"][len(prefix) :].split("/")[0]
                for r in rows
                if r["path"].startswith(prefix) and r["path"] != prefix
            }
        )

    async def readdir_with_types(self, path):
        if local := self.local(path):
            return await asyncio.to_thread(
                lambda: [
                    SessionFSReaddirWithTypesEntry(
                        name=p.name,
                        type=DebugCollectLogsEntryKind.DIRECTORY
                        if p.is_dir()
                        else DebugCollectLogsEntryKind.FILE,
                    )
                    for p in local.iterdir()
                ]
            )
        names = await self.readdir(path)
        return [
            SessionFSReaddirWithTypesEntry(
                name=n,
                type=DebugCollectLogsEntryKind.DIRECTORY
                if (await self.entry(posixpath.join(path, n)))["is_directory"]
                else DebugCollectLogsEntryKind.FILE,
            )
            for n in names
        ]

    def check_tree_operation(self, path):
        resolved = path.resolve()
        if any(
            resolved == protected or resolved in protected.parents for protected in self.protected
        ):
            raise PermissionError("Directory operation would affect protected service state")

    async def rm(self, path, recursive=False, force=False):
        if local := self.local(path):
            self.check_tree_operation(local)
            if local in self.local_roots:
                raise PermissionError("Cannot remove a workspace root")

            def remove():
                if local.is_dir():
                    shutil.rmtree(local) if recursive else local.rmdir()
                else:
                    local.unlink(missing_ok=force)

            await asyncio.to_thread(remove)
            return
        path = self.path(path)
        async with self.store.transaction() as db:
            cur = await db.execute(
                "SELECT path,is_directory FROM sdk_files WHERE namespace=?", (self.namespace,)
            )
            rows = await cur.fetchall()
            exact = [r for r in rows if r["path"] == path]
            if not exact and not force:
                raise FileNotFoundError(errno.ENOENT, "No such session file", path)
            children = [
                r["path"]
                for r in rows
                if r["path"].startswith(path.rstrip("/") + "/") and r["path"] != path
            ]
            if children and not recursive:
                raise OSError(errno.ENOTEMPTY, "Directory not empty", path)
            await db.executemany(
                "DELETE FROM sdk_files WHERE namespace=? AND path=?",
                [(self.namespace, p) for p in children + ([path] if exact else [])],
            )

    async def rename(self, src, dest):
        local_src, local_dest = self.local(src), self.local(dest)
        if local_src or local_dest:
            if local_src:
                self.check_tree_operation(local_src)
            if local_dest:
                self.check_tree_operation(local_dest)
            if (
                not local_src
                or not local_dest
                or local_src in self.local_roots
                or local_dest in self.local_roots
            ):
                raise PermissionError(
                    "Cannot rename across workspace/session storage or move a root"
                )
            await asyncio.to_thread(os.replace, local_src, local_dest)
            return
        src, dest = self.path(src), self.path(dest)
        if src == dest:
            await self.entry(src)
            return
        if dest.startswith(src.rstrip("/") + "/"):
            raise ValueError("Cannot move a directory inside itself")
        async with self.store.transaction() as db:
            cur = await db.execute("SELECT * FROM sdk_files WHERE namespace=?", (self.namespace,))
            rows = [dict(r) for r in await cur.fetchall()]
            moved = [
                r for r in rows if r["path"] == src or r["path"].startswith(src.rstrip("/") + "/")
            ]
            if not any(r["path"] == src for r in moved):
                raise FileNotFoundError(errno.ENOENT, "No source", src)
            target_children = [r for r in rows if r["path"].startswith(dest.rstrip("/") + "/")]
            if target_children:
                raise OSError(errno.ENOTEMPTY, "Destination not empty", dest)
            source_dir = next(r["is_directory"] for r in moved if r["path"] == src)
            target = next((r for r in rows if r["path"] == dest), None)
            if target and target["is_directory"] != source_dir:
                raise ValueError("Rename cannot change file/directory type")
            await self.parents(db, posixpath.dirname(dest))
            await db.execute(
                "DELETE FROM sdk_files WHERE namespace=? AND path=?", (self.namespace, dest)
            )
            await db.executemany(
                "DELETE FROM sdk_files WHERE namespace=? AND path=?",
                [(self.namespace, r["path"]) for r in moved],
            )
            await db.executemany(
                "INSERT INTO sdk_files VALUES(?,?,?,?,?)",
                [
                    (
                        self.namespace,
                        dest + r["path"][len(src) :],
                        r["content"],
                        r["is_directory"],
                        time.time(),
                    )
                    for r in moved
                ],
            )
