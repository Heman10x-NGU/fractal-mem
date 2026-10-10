"""Disk regressions for ready-only publication and atomic V1 transition."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fractalmem.core.node import HMRNode
from fractalmem.core.tree import HMRTreeEngine, InMemoryNodeStore
from fractalmem.core.view import View
from fractalmem.protocol import Event
from fractalmem.storage.db import Database


def event(seq: int, *, long: bool = False, project: str = "p") -> Event:
    return Event(project, "s", str(seq), "user", "long " * 150 if long else f"message {seq}")


def test_out_of_order_failure_ready_status_and_exact_restart(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with Database(path) as db:
        db.append_events([event(0), event(1, long=True), event(2, long=True), event(3)])
        original = db.load_view("p").render_buffer()
        assert db.load_view("p").total_messages == 1
        assert all(n.status == "ready" for n in db.load_view("p").nodes)
        db.complete_node(HMRNode.create_leaf(2, "later summary", project_id="p"))
        assert db.load_view("p").render_buffer() == original
        hit = db.search("p", "message 3")[0]
        assert not hit.published and db.get_node("p", hit.node_id) is not None
        job = next(j for j in db.claim_jobs() if j.start_seq == 1)
        db.fail_job(job, "failed")
        assert not db.publish_ready_view("p")
        assert db.retry_failed_jobs("p") == 1
        db.complete_node(HMRNode.create_leaf(1, "first summary", project_id="p"), resume_view=False)
        status = db.status("p")
        assert status["committed_messages"] == 4
        assert status["ready_watermark_seq"] == 3
        assert status["published_coverage"] == 1
        assert status["published_watermark_seq"] == 0
    with Database(path) as db:
        db.recover_jobs()
        db.reconcile_views()
        final = db.load_view("p").render_buffer()
        assert final.startswith(original)
        assert db.load_view("p").total_messages == 4
        assert db.search("p", "message 3")[0].published
        db.export_views()
        assert (
            View.load_atomic(
                path.with_name(path.name + ".views") / "p" / "view.json"
            ).render_buffer()
            == final
        )
    with Database(path) as db:
        db.reconcile_views()
        assert db.load_view("p").render_buffer() == final


def test_bounded_chunking_and_blocked_predecessor(tmp_path: Path) -> None:
    with Database(tmp_path / "memory.db") as db:
        db.append_events([event(0, long=True), *(event(n) for n in range(1, 601))])
        assert db.load_view("p").total_messages == 0
        chunks: list[int] = []
        original = db._ready_leaf_chunk

        def observe(project_id: str, start: int) -> list[HMRNode]:
            result = original(project_id, start)
            chunks.append(len(result))
            return result

        db._ready_leaf_chunk = observe  # type: ignore[method-assign]
        assert not db.publish_ready_view("p")
        assert chunks == [256]
        db.complete_node(HMRNode.create_leaf(0, "done", project_id="p"))
        assert db.load_view("p").total_messages == 601
        assert max(chunks) <= 256
        assert len(chunks) >= 7


def test_save_view_rejects_pending_invented_nodes_and_coverage(tmp_path: Path) -> None:
    with Database(tmp_path / "memory.db") as db:
        db.append_event(event(0, long=True))
        pending = db.resolve_parent(0, 0, "p")
        assert pending is not None
        with pytest.raises(ValueError, match="durable ready"):
            db.save_view(View("p", 1, (pending,)))
        with pytest.raises(ValueError, match="durable ready"):
            db.save_view(View("p", 1, (HMRNode.create_leaf(0, "invented", project_id="p"),)))
        with pytest.raises(ValueError, match="exceeds"):
            db.save_view(
                View("p", 2, tuple(HMRNode.create_leaf(n, "x", project_id="p") for n in range(2)))
            )
        assert db.load_view("p").total_messages == 0


def test_committed_clock_changes_actual_heap_winner() -> None:
    store = InMemoryNodeStore()
    nodes = tuple(
        HMRNode.create(level, index, "x" * 100, project_id="p")
        for level, index in [(2, 0), (2, 1), (1, 4), (1, 5)]
    )
    for level, index in [(3, 0), (2, 2)]:
        store.put(HMRNode.create(level, index, "parent", project_id="p"))
    view = View("p", 12, nodes)
    for clock, expected in [(12, "0+8"), (20, "8+4")]:
        engine = HMRTreeEngine(
            "p",
            view,
            max_view_bytes=400,
            target_view_bytes=350,
            parent_resolver=store.get,
            age_clock=clock,
        )
        assert engine.sawtooth_batch_merge()
        assert len(engine.view.nodes) == 3
        assert expected in [n.handle for n in engine.view.nodes]


def make_v1(path: Path) -> dict[str, str]:
    """Real V1 column layout, with one placeholder view and one ready view."""
    snapshots: dict[str, str] = {}
    with Database(path) as db:
        for project in ("pending", "ready"):
            db.append_events(
                [event(0, project=project), event(1, long=project == "pending", project=project)]
            )
            if project == "pending":
                leaves = tuple(db.resolve_parent(0, n, project) for n in range(2))
                assert all(n is not None for n in leaves)
                view = View(project, 2, tuple(n for n in leaves if n is not None))
                snapshot = json.dumps(view.to_dict(), separators=(",", ":"))
                db.connection.execute(
                    "UPDATE views SET snapshot=? WHERE project_id=?", (snapshot, project)
                )
                pending_leaf = leaves[1]
                assert pending_leaf is not None
                db.connection.execute(
                    "INSERT INTO active_frontier VALUES(?,?,?,?)",
                    (project, 1, 1, pending_leaf.node_id),
                )
            snapshots[project] = str(
                db.connection.execute(
                    "SELECT snapshot FROM views WHERE project_id=?", (project,)
                ).fetchone()[0]
            )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE compaction_watermarks")
        connection.execute("DROP TABLE legacy_views_archive")
        connection.execute("DROP INDEX idx_tree_nodes_status")
        connection.execute("DROP INDEX idx_tree_leaves")
        for name in ("level", "start_seq", "end_seq"):
            connection.execute(f"ALTER TABLE compaction_jobs DROP COLUMN {name}")
        connection.execute("PRAGMA user_version=1")
    return snapshots


@pytest.mark.parametrize("fail_on", [1, 2])
def test_migration_failure_rolls_back_ddl_data_and_first_project(
    tmp_path: Path, fail_on: int
) -> None:
    path = tmp_path / "legacy.db"
    snapshots = make_v1(path)

    class FailingDatabase(Database):
        calls = 0

        def _migrate_project_view(self, project_id: str) -> None:
            self.calls += 1
            if self.calls == fail_on:
                raise RuntimeError("injected migration failure")
            super()._migrate_project_view(project_id)

    with pytest.raises(RuntimeError, match="injected"):
        FailingDatabase(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='legacy_views_archive'"
        ).fetchall()
        assert "level" not in [
            r[1] for r in connection.execute("PRAGMA table_info(compaction_jobs)")
        ]
        assert dict(connection.execute("SELECT project_id,snapshot FROM views")) == snapshots
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 4
    with Database(path) as db:
        assert db.status()["schema_version"] == 2
        assert db.load_view("pending").total_messages == 1
        assert db.load_view("ready").total_messages == 2
        assert (
            db.connection.execute("SELECT snapshot FROM views WHERE project_id='ready'").fetchone()[
                0
            ]
            == snapshots["ready"]
        )
        archive = db.connection.execute(
            "SELECT snapshot,reason FROM legacy_views_archive"
        ).fetchone()
        assert archive[0] == snapshots["pending"]
        assert "cache reset" in archive[1]
        assert (
            db.connection.execute(
                "SELECT count(*) FROM active_frontier WHERE project_id='pending'"
            ).fetchone()[0]
            == 1
        )
        final = db.load_view("pending").render_buffer()
    with Database(path) as db:
        assert db.load_view("pending").render_buffer() == final


def test_publication_snapshot_and_frontier_roll_back_together(tmp_path: Path) -> None:
    with Database(tmp_path / "memory.db") as db:
        db.append_event(event(0, long=True))
        db.complete_node(HMRNode.create_leaf(0, "done", project_id="p"), resume_view=False)
        before = db.load_view("p").to_dict()
        db.connection.executescript("""
            CREATE TRIGGER injected_publication_failure BEFORE INSERT ON active_frontier
            BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            db.publish_ready_view("p")
        assert db.load_view("p").to_dict() == before
        assert not db.connection.execute("SELECT * FROM active_frontier").fetchall()
        assert db.status("p")["ready_watermark_seq"] == 0
        db.connection.execute("DROP TRIGGER injected_publication_failure")
        assert db.publish_ready_view("p")
        assert db.load_view("p").total_messages == 1


def test_missing_leaf_stops_publication_and_ready_parent_keeps_bytes(tmp_path: Path) -> None:
    with Database(tmp_path / "memory.db") as db:
        db.append_events([event(0), event(1)])
        original = db.load_view("p").render_buffer()
        db.complete_node(HMRNode.create(1, 0, "merged", project_id="p"))
        assert db.load_view("p").render_buffer() == original
        db.append_events([event(2, long=True), event(3, long=True), event(4)])
        missing = db.resolve_parent(0, 2, "p")
        assert missing is not None
        db.connection.execute("DELETE FROM compaction_jobs WHERE node_id=?", (missing.node_id,))
        db.connection.execute("DELETE FROM tree_nodes WHERE node_id=?", (missing.node_id,))
        db.complete_node(HMRNode.create_leaf(3, "later", project_id="p"))
        assert db.load_view("p").render_buffer() == original
        assert db.load_view("p").total_messages == 2


@pytest.mark.parametrize("batch", [False, True])
def test_explicit_age_clock_rejects_append_before_mutation(batch: bool) -> None:
    engine = HMRTreeEngine("p", age_clock=0)
    leaf = HMRNode.create_leaf(0, "ready", project_id="p")
    with pytest.raises(ValueError, match="age clock"):
        if batch:
            engine.append_leaf_nodes([leaf])
        else:
            engine.append_leaf_node(leaf)
    assert engine.view == View.empty("p")
