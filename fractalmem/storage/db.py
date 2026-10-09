"""Single-owner SQLite WAL store with immutable events and transactional views."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote

from fractalmem.core.node import HMRNode, MessageKind, NodeStatus
from fractalmem.core.tree import HMRTreeEngine
from fractalmem.core.view import View
from fractalmem.protocol import Event, validate_identifier
from fractalmem.security.gitleaks import rules
from fractalmem.security.redact import Redactor
from fractalmem.storage.fts import SearchHit, index_message, search

SCHEMA_VERSION = 1


class WriterAlreadyRunningError(RuntimeError):
    """Another process owns this database's write path."""


class EventConflictError(ValueError):
    """An event identity was reused for a different payload."""


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: int
    project_id: str
    session_id: str
    seq: int
    event_id: str
    kind: str
    payload: str
    language: str | None
    taint_level: str
    created_ns: int


@dataclass(frozen=True, slots=True)
class AppendResult:
    message: StoredMessage
    duplicate: bool


@dataclass(frozen=True, slots=True)
class CompactionJob:
    project_id: str
    node_id: str
    attempts: int


def _message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=int(row["id"]),
        project_id=str(row["project_id"]),
        session_id=str(row["session_id"]),
        seq=int(row["seq"]),
        event_id=str(row["event_id"]),
        kind=str(row["kind"]),
        payload=str(row["payload"]),
        language=str(row["language"]) if row["language"] is not None else None,
        taint_level=str(row["taint_level"]),
        created_ns=int(row["created_ns"]),
    )


def _node(row: sqlite3.Row) -> HMRNode:
    return HMRNode(
        project_id=str(row["project_id"]),
        node_id=str(row["node_id"]),
        level=int(row["level"]),
        index_in_level=int(row["index_in_level"]),
        start_seq=int(row["start_seq"]),
        end_seq=int(row["end_seq"]),
        summary_text=str(row["summary_text"]),
        byte_size=int(row["byte_size"]),
        status=cast(NodeStatus, str(row["status"])),
        kind=cast(MessageKind, str(row["kind"])),
    )


class Database:
    """One connection per owner thread. Readers are physically read-only.

    The writer lock is held before mutable SQLite is opened. Library users can
    construct this repository for controlled ingestion; agent hooks use UDS.
    WAL NORMAL protects against process crashes but may lose a recent commit on
    power failure. Choose synchronous='FULL' for stronger disk durability.
    """

    def __init__(
        self, path: Path | str, *, read_only: bool = False, synchronous: str = "NORMAL"
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.read_only = read_only
        self._lock_fd: int | None = None
        self._savepoint = 0
        self.redactor = Redactor()
        # Compile the immutable catalog before the daemon advertises readiness;
        # cold rule compilation must not stall its first socket receipts.
        rules()
        self.connection: sqlite3.Connection
        if synchronous not in ("NORMAL", "FULL"):
            raise ValueError("synchronous must be NORMAL or FULL")
        try:
            if read_only:
                uri = "file:" + quote(str(self.path), safe="/") + "?mode=ro"
                self.connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
                self.connection.execute("PRAGMA query_only=ON")
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self.path.with_name(self.path.name + ".writer.lock")
                self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise WriterAlreadyRunningError("another writer owns this database") from error
                file_fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(file_fd)
                os.chmod(self.path, 0o600)
                self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute(f"PRAGMA synchronous={synchronous}")
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION or (read_only and version != SCHEMA_VERSION):
                raise RuntimeError("unsupported database schema version")
            if not read_only and version == 0:
                schema = Path(__file__).with_name("schema.sql").read_text()
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + schema
                    + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
                )
        except BaseException:
            if hasattr(self, "connection"):
                self.connection.close()
            self._release_lock()
            raise

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def close(self) -> None:
        self.connection.close()
        self._release_lock()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.read_only:
            raise PermissionError("read-only repository cannot start a write transaction")
        nested = self.connection.in_transaction
        self._savepoint += 1
        name = f"fm_{self._savepoint}"
        self.connection.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if nested:
                self.connection.execute(f"ROLLBACK TO {name}")
                self.connection.execute(f"RELEASE {name}")
            else:
                self.connection.rollback()
            raise
        else:
            self.connection.execute(f"RELEASE {name}") if nested else self.connection.commit()

    def get_message(self, project_id: str, seq: int) -> StoredMessage | None:
        row = self.connection.execute(
            "SELECT * FROM messages WHERE project_id=? AND seq=?", (project_id, seq)
        ).fetchone()
        return _message(row) if row is not None else None

    def get_event(self, project_id: str, event_id: str) -> StoredMessage | None:
        row = self.connection.execute(
            "SELECT * FROM messages WHERE project_id=? AND event_id=?", (project_id, event_id)
        ).fetchone()
        return _message(row) if row is not None else None

    def get_node(self, project_id: str, node_id: str) -> HMRNode | None:
        row = self.connection.execute(
            "SELECT * FROM tree_nodes WHERE project_id=? AND node_id=?", (project_id, node_id)
        ).fetchone()
        return _node(row) if row is not None else None

    def resolve_parent(self, level: int, index_in_level: int, project_id: str) -> HMRNode | None:
        row = self.connection.execute(
            "SELECT * FROM tree_nodes WHERE project_id=? AND level=? AND index_in_level=?",
            (project_id, level, index_in_level),
        ).fetchone()
        return _node(row) if row is not None else None

    def _insert_node(self, node: HMRNode) -> None:
        self.connection.execute(
            "INSERT INTO tree_nodes(project_id,node_id,level,index_in_level,start_seq,end_seq,summary_text,byte_size,status,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                node.project_id,
                node.node_id,
                node.level,
                node.index_in_level,
                node.start_seq,
                node.end_seq,
                node.summary_text,
                node.byte_size,
                node.status,
                node.kind,
            ),
        )

    def load_view(self, project_id: str) -> View:
        row = self.connection.execute(
            "SELECT snapshot FROM views WHERE project_id=?", (project_id,)
        ).fetchone()
        return View.from_dict(json.loads(str(row["snapshot"]))) if row else View.empty(project_id)

    def export_views(self, projects: Sequence[str] | None = None) -> None:
        """Publish recoverable atomic snapshots; SQLite remains authoritative.

        Called outside the commit transaction, so a crash between commit and
        export is repaired from SQLite at daemon startup.
        """
        if self.connection.in_transaction:
            raise RuntimeError("view exports require a committed transaction")
        names = (
            projects
            if projects is not None
            else [str(row[0]) for row in self.connection.execute("SELECT project_id FROM projects")]
        )
        root = self.path.with_name(self.path.name + ".views")
        for project in dict.fromkeys(names):
            validate_identifier(project, "project_id")
            directory = root / project
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.load_view(project).save_atomic(directory / "view.json")

    def save_view(self, view: View) -> None:
        with self.transaction():
            count = int(
                self.connection.execute(
                    "SELECT next_seq FROM projects WHERE project_id=?", (view.project_id,)
                ).fetchone()[0]
            )
            if count != view.total_messages:
                raise ValueError("view does not cover the committed project log")
            self.connection.execute(
                "INSERT INTO views(project_id,generation,snapshot) VALUES(?,1,?) ON CONFLICT(project_id) DO UPDATE SET generation=generation+1,snapshot=excluded.snapshot",
                (
                    view.project_id,
                    json.dumps(view.to_dict(), ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self.connection.execute(
                "DELETE FROM active_frontier WHERE project_id=?", (view.project_id,)
            )
            self.connection.executemany(
                "INSERT INTO active_frontier(project_id,start_seq,end_seq,node_id) VALUES(?,?,?,?)",
                [(view.project_id, n.start_seq, n.end_seq, n.node_id) for n in view.nodes],
            )

    def _schedule_parent(self, node: HMRNode) -> None:
        other = self.resolve_parent(node.level, node.index_in_level ^ 1, node.project_id)
        if node.status != "ready" or other is None or other.status != "ready":
            return
        level, index = node.level + 1, node.index_in_level // 2
        if self.resolve_parent(level, index, node.project_id) is not None:
            return
        parent = HMRNode.create_pending(level, index, node.project_id)
        self._insert_node(parent)
        self.connection.execute(
            "INSERT INTO compaction_jobs(project_id,node_id,status) VALUES(?,?,'pending')",
            (node.project_id, parent.node_id),
        )

    def append_event(self, event: Event) -> AppendResult:
        return self.append_events([event])[0]

    def append_events(self, events: Sequence[Event]) -> list[AppendResult]:
        results: list[AppendResult] = []
        with self.transaction():
            engines: dict[str, HMRTreeEngine] = {}
            leaves: dict[str, list[HMRNode]] = {}
            for event in events:
                clean = self.redactor.redact(event.payload)
                existing = self.get_event(event.project_id, event.event_id)
                if existing is not None:
                    if (
                        existing.session_id,
                        existing.kind,
                        existing.payload,
                        existing.language,
                    ) != (event.session_id, event.kind, clean, event.language):
                        raise EventConflictError("event_id reused for different event content")
                    results.append(AppendResult(existing, True))
                    continue
                self.connection.execute(
                    "INSERT OR IGNORE INTO projects(project_id) VALUES(?)", (event.project_id,)
                )
                seq = int(
                    self.connection.execute(
                        "SELECT next_seq FROM projects WHERE project_id=?", (event.project_id,)
                    ).fetchone()[0]
                )
                cursor = self.connection.execute(
                    "INSERT INTO messages(project_id,session_id,seq,event_id,kind,payload,language,byte_size,created_ns) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        event.project_id,
                        event.session_id,
                        seq,
                        event.event_id,
                        event.kind,
                        clean,
                        event.language,
                        len(clean.encode("utf-8")),
                        time.time_ns(),
                    ),
                )
                row_id = cursor.lastrowid
                if row_id is None:
                    raise RuntimeError("message insert returned no identity")
                self.connection.execute(
                    "UPDATE projects SET next_seq=next_seq+1 WHERE project_id=?",
                    (event.project_id,),
                )
                index_message(self.connection, row_id, event.project_id, seq, clean, event.language)
                leaf_text = f"{event.kind}: {clean}"
                if len(leaf_text.encode("utf-8")) <= 512:
                    leaf = HMRNode.create_leaf(
                        seq, leaf_text, kind=cast(MessageKind, event.kind), project_id=event.project_id
                    )
                else:
                    leaf = HMRNode.create_pending(0, seq, event.project_id)
                self._insert_node(leaf)
                if leaf.status == "pending":
                    self.connection.execute(
                        "INSERT INTO compaction_jobs(project_id,node_id,status) VALUES(?,?,'pending')",
                        (event.project_id, leaf.node_id),
                    )
                else:
                    self._schedule_parent(leaf)
                if event.project_id not in engines:
                    cache: dict[tuple[int, int, str], HMRNode | None] = {}

                    def resolve_cached(
                        level: int,
                        index: int,
                        project: str,
                        nodes: dict[tuple[int, int, str], HMRNode | None] = cache,
                    ) -> HMRNode | None:
                        key = (level, index, project)
                        if key not in nodes:
                            nodes[key] = self.resolve_parent(level, index, project)
                        return nodes[key]

                    engines[event.project_id] = HMRTreeEngine(
                        project_id=event.project_id,
                        view=self.load_view(event.project_id),
                        parent_resolver=resolve_cached,
                    )
                leaves.setdefault(event.project_id, []).append(leaf)
                message = self.get_message(event.project_id, seq)
                if message is None:
                    raise RuntimeError("inserted message not visible in transaction")
                results.append(AppendResult(message, False))
            for project, engine in engines.items():
                engine.append_leaf_nodes(leaves[project])
                self.save_view(engine.view)
        return results

    def claim_jobs(self, limit: int = 8) -> list[CompactionJob]:
        if not 1 <= limit <= 8:
            raise ValueError("compaction claim limit must be 1..8")
        with self.transaction():
            rows = self.connection.execute(
                "SELECT * FROM compaction_jobs WHERE status='pending' ORDER BY ordinal LIMIT ?",
                (limit,),
            ).fetchall()
            jobs = [
                CompactionJob(str(row["project_id"]), str(row["node_id"]), int(row["attempts"]) + 1)
                for row in rows
            ]
            self.connection.executemany(
                "UPDATE compaction_jobs SET status='running',attempts=attempts+1 WHERE project_id=? AND node_id=?",
                [(j.project_id, j.node_id) for j in jobs],
            )
        return jobs

    def recover_jobs(self) -> int:
        with self.transaction():
            cursor = self.connection.execute(
                "UPDATE compaction_jobs SET status='pending' WHERE status='running'"
            )
        return cursor.rowcount

    def retry_failed_jobs(self, project_id: str) -> int:
        """Explicit operator retry after fixing provider or security configuration."""
        validate_identifier(project_id, "project_id")
        with self.transaction():
            self.connection.execute(
                "UPDATE tree_nodes SET status='pending' WHERE project_id=? AND status='failed'",
                (project_id,),
            )
            cursor = self.connection.execute(
                "UPDATE compaction_jobs SET status='pending',error=NULL WHERE project_id=? AND status='failed'",
                (project_id,),
            )
        return cursor.rowcount

    def record_compaction(self, project_id: str, node_id: str, mode: str, usage_json: str) -> None:
        self.connection.execute(
            "INSERT INTO compaction_runs(project_id,node_id,mode,usage_json,created_ns) VALUES(?,?,?,?,?)",
            (project_id, node_id, mode, usage_json, time.time_ns()),
        )

    def complete_node(self, node: HMRNode, *, resume_view: bool = True) -> bool:
        """Complete a summary; return whether the frozen view changed."""
        if node.status != "ready":
            raise ValueError("completion requires a ready node")
        clean = self.redactor.redact(node.summary_text)
        if clean != node.summary_text:
            node = HMRNode.create(
                node.level, node.index_in_level, clean, project_id=node.project_id
            )
        with self.transaction():
            existing = self.get_node(node.project_id, node.node_id)
            if existing is None:
                raise ValueError("cannot complete an unknown node")
            if existing.status == "ready":
                if existing.summary_text != node.summary_text:
                    raise EventConflictError("ready node content cannot change")
                return False
            self.connection.execute(
                "UPDATE tree_nodes SET summary_text=?,byte_size=?,status='ready',kind=? WHERE project_id=? AND node_id=?",
                (node.summary_text, node.byte_size, node.kind, node.project_id, node.node_id),
            )
            self.connection.execute(
                "UPDATE compaction_jobs SET status='ready',error=NULL WHERE project_id=? AND node_id=?",
                (node.project_id, node.node_id),
            )
            self._schedule_parent(node)
            if resume_view:
                return self.resume_view_batch(node.project_id)
        return False

    def resume_view_batch(self, project_id: str) -> bool:
        with self.transaction():
            active = self.connection.execute(
                "SELECT json_extract(snapshot,'$.batch_active') FROM views WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if active is not None and active[0]:
                view = self.load_view(project_id)
                cache: dict[tuple[int, int, str], HMRNode | None] = {}

                def resolve(level: int, index: int, project: str) -> HMRNode | None:
                    key = (level, index, project)
                    if key not in cache:
                        cache[key] = self.resolve_parent(level, index, project)
                    return cache[key]

                engine = HMRTreeEngine(project_id=project_id, view=view, parent_resolver=resolve)
                # Resume an already-triggered batch; no prefix changes during ordinary appends.
                engine.resume_batch()
                if engine.view != view:
                    self.save_view(engine.view)
                    return True
        return False

    def fail_job(self, job: CompactionJob, reason: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE compaction_jobs SET status='failed',error=? WHERE project_id=? AND node_id=?",
                (self.redactor.redact(reason)[:512], job.project_id, job.node_id),
            )
            self.connection.execute(
                "UPDATE tree_nodes SET status='failed' WHERE project_id=? AND node_id=? AND status!='ready'",
                (job.project_id, job.node_id),
            )

    def search(self, project_id: str, query: str, limit: int = 10) -> list[SearchHit]:
        validate_identifier(project_id, "project_id")
        with self.read_snapshot():
            return search(self.connection, project_id, query, limit)

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        if self.connection.in_transaction:
            yield
            return
        self.connection.execute("BEGIN")
        try:
            yield
        finally:
            self.connection.rollback()

    def status(self, project_id: str | None = None) -> dict[str, int | str]:
        if project_id is not None:
            validate_identifier(project_id, "project_id")
        params = (project_id,) if project_id is not None else ()
        scoped = " WHERE project_id=?" if project_id is not None else ""
        jobs_scope = " AND project_id=?" if project_id is not None else ""
        return {
            "journal_mode": str(self.connection.execute("PRAGMA journal_mode").fetchone()[0]),
            "schema_version": int(self.connection.execute("PRAGMA user_version").fetchone()[0]),
            "messages": int(
                self.connection.execute(
                    "SELECT count(*) FROM messages" + scoped, params
                ).fetchone()[0]
            ),
            "pending_jobs": int(
                self.connection.execute(
                    "SELECT count(*) FROM compaction_jobs WHERE status IN ('pending','running')"
                    + jobs_scope,
                    params,
                ).fetchone()[0]
            ),
            "failed_jobs": int(
                self.connection.execute(
                    "SELECT count(*) FROM compaction_jobs WHERE status='failed'" + jobs_scope,
                    params,
                ).fetchone()[0]
            ),
        }
