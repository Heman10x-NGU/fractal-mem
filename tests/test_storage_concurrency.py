from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from fractalmem.client import FractalClient, TransportError
from fractalmem.core.view import View
from fractalmem.daemon.ring_buffer import RingBuffer
from fractalmem.daemon.server import FractalDaemon, SocketLease
from fractalmem.storage.db import Database


@contextmanager
def running_daemon(
    directory: Path, *, capacity: int = 16384
) -> Iterator[tuple[subprocess.Popen[str], FractalClient]]:
    sock = directory / "d.sock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "fractalmem.daemon.server",
            "--db",
            str(directory / "db.sqlite"),
            "--socket",
            str(sock),
            "--capacity",
            str(capacity),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client = FractalClient("project", "session", socket_path=sock)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            try:
                if client.status()["journal_mode"] == "wal":
                    break
            except OSError:
                time.sleep(0.01)
        else:
            raise AssertionError("daemon did not become ready")
        yield process, client
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert stdout == ""


def _produce(path: str, producer: int, count: int) -> list[str]:
    client = FractalClient("project", f"worker-{producer}", socket_path=path)
    accepted: list[str] = []
    for i in range(count):
        identity = f"event-{producer}-{i}"
        receipt = client.ingest_event(
            kind="tool",
            content=f"def needle_{producer}_{i}():\n    return {i}",
            event_id=identity,
            language="python",
        )
        if receipt.accepted:
            accepted.append(identity)
        assert not receipt.durable
    return accepted


def test_ring_capacity_wraparound_and_concurrent_producers() -> None:
    queue: RingBuffer[tuple[int, int]] = RingBuffer(16384)
    for i in range(16384):
        assert queue.try_push((0, i))
    assert not queue.try_push((0, 16384))
    assert queue.pop_batch(100) == [(0, i) for i in range(100)]
    for i in range(100):
        assert queue.try_push((1, i))
    assert len(queue.pop_batch(20000)) == 16384
    assert queue.stats().rejected_full == 1
    admitted: list[tuple[int, int]] = []
    lock = threading.Lock()

    def producer(worker: int) -> None:
        successes = [(worker, i) for i in range(1000) if queue.try_push((worker, i))]
        with lock:
            admitted.extend(successes)

    with ThreadPoolExecutor(8) as executor:
        list(executor.map(producer, range(8)))
    drained = queue.pop_batch(16384)
    assert set(drained) == set(admitted)
    assert len(drained) == len(admitted)
    for worker in range(8):
        numbers = [i for w, i in drained if w == worker]
        assert numbers == sorted(numbers)
    with queue._lock:  # Deliberate contention fault injection: producers must fail open.
        start = time.perf_counter()
        assert not queue.try_push((9, 9))
        assert time.perf_counter() - start < 0.015


def test_four_process_producers_durable_flush_restart_and_search() -> None:
    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        directory = Path(raw)
        with running_daemon(directory) as (process, client):
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(4) as pool:
                results = pool.starmap(
                    _produce, [(str(client.socket_path), worker, 80) for worker in range(4)]
                )
            accepted = {identity for identities in results for identity in identities}
            assert accepted  # Performance thresholds belong in benchmarks, not shared-runner CI.
            status = client.flush(compact=True)
            assert status["messages"] >= len(accepted)
            assert status["conflicts"] == 0
            assert status["compacted"] is True
            assert os.stat(client.socket_path).st_mode & 0o777 == 0o600
            with Database(directory / "db.sqlite", read_only=True) as db:
                persisted = {
                    str(row[0]) for row in db.connection.execute("SELECT event_id FROM messages")
                }
                assert accepted <= persisted  # Timed-out receipts may also have been admitted.
                count = len(persisted)
                before = db.load_view("project").render_buffer()
                identity = sorted(accepted)[0]
                _, producer, index = identity.split("-")
                symbol = f"needle_{producer}_{index}"
                hit = db.search("project", symbol)[0]
                assert hit.symbol == symbol and hit.parent_handle
                assert db.connection.execute("PRAGMA query_only").fetchone()[0] == 1
            export = directory / "db.sqlite.views" / "project" / "view.json"
            assert View.load_atomic(export).render_buffer() == before
            process.send_signal(signal.SIGTERM)
        assert not client.socket_path.exists()
        export.write_text("{corrupt derived snapshot")
        with running_daemon(directory) as (_, client):
            assert client.status()["messages"] == count
            assert View.load_atomic(export).render_buffer() == before
            retry_client = FractalClient("project", f"worker-{producer}", socket_path=client.socket_path)
            duplicate = retry_client.ingest_event(
                kind="tool",
                content=f"def {symbol}():\n    return {index}",
                event_id=identity,
                language="python",
            )
            assert duplicate.accepted
            retried = client.flush()
            assert retried["messages"] == count
            assert retried["duplicates"] == 1
            assert retried["conflicts"] == 0
            with Database(directory / "db.sqlite", read_only=True) as db:
                assert db.load_view("project").render_buffer() == before


def test_shutdown_drains_receipted_events_and_conflicts_do_not_poison_batch() -> None:
    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        directory = Path(raw)
        with running_daemon(directory) as (process, client):
            assert client.ingest_event(kind="user", content="first", event_id="same").accepted
            assert client.ingest_event(kind="user", content="conflict", event_id="same").accepted
            assert client.ingest_event(kind="user", content="valid", event_id="other").accepted
            status = client.flush()
            assert status["messages"] == 2 and status["conflicts"] == 1
            accepted = []
            for i in range(40):
                if client.ingest_event(
                    kind="user", content=f"shutdown {i}", event_id=f"shutdown-{i}"
                ).accepted:
                    accepted.append(f"shutdown-{i}")
            process.send_signal(signal.SIGTERM)
        with Database(directory / "db.sqlite", read_only=True) as db:
            assert all(db.get_event("project", identity) is not None for identity in accepted)


def test_malformed_bounded_protocol_and_second_daemon() -> None:
    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        directory = Path(raw)
        with running_daemon(directory) as (_, client):
            for request in [
                b"not-json\n",
                b"[]\n",
                b'{"version":true,"op":"status"}\n',
                b'{"version":1,"op":"status","extra":1}\n',
                b'{"version":1,"op":"flush","compact":1}\n',
            ]:
                with socket.socket(socket.AF_UNIX) as peer:
                    peer.settimeout(1)
                    peer.connect(str(client.socket_path))
                    peer.sendall(request)
                    reply = json.loads(peer.recv(4096))
                assert reply["reason"] == "invalid_request"
            inode = os.stat(client.socket_path).st_ino
            attempt = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "fractalmem.daemon.server",
                    "--db",
                    str(directory / "other.db"),
                    "--socket",
                    str(client.socket_path),
                ],
                capture_output=True,
                timeout=10,
            )
            assert attempt.returncode == 1
            assert os.stat(client.socket_path).st_ino == inode
            assert client.status()["messages"] == 0
        file = directory / "occupied"
        file.write_text("keep")
        with pytest.raises(FileExistsError):
            SocketLease(file)
        assert file.read_text() == "keep"


def test_fail_open_unavailable_invalid_and_transport_deadline() -> None:
    client = FractalClient(socket_path="/tmp/no-fractalmem-test.sock")
    start = time.perf_counter()
    receipt = client.ingest_event(kind="user", content="hello")
    assert not receipt.accepted and receipt.reason == "unavailable"
    assert time.perf_counter() - start < 0.050  # OS scheduling slack, configured deadline is 15ms.
    assert client.ingest_event(kind="unknown", content="x").reason == "invalid_event"
    assert client.ingest_event(kind="user", content="x" * 262145).reason == "invalid_event"
    with pytest.raises(OSError):
        client.flush()
    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        path = Path(raw) / "slow.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            listener.listen(2)
            slow = FractalClient(socket_path=path)
            start = time.perf_counter()
            assert slow.ingest_event(kind="user", content="hello").reason == "timeout"
            elapsed = time.perf_counter() - start
            assert 0.010 <= elapsed < 0.050


def test_disk_writer_failure_rejects_future_admission_and_resolves_barrier() -> None:
    async def scenario(directory: Path) -> None:
        daemon = FractalDaemon(directory / "db.sqlite", directory / "d.sock")
        await daemon.start()
        try:

            def fault(db: Database) -> None:
                db.connection.execute(
                    "CREATE TRIGGER test_failure BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'disk write fault'); END"
                )

            await daemon.worker.call(fault)
            client = FractalClient("project", socket_path=directory / "d.sock")
            receipt = await asyncio.to_thread(client.ingest_event, kind="user", content="write")
            assert receipt.accepted
            with pytest.raises((TransportError, OSError)):
                await asyncio.to_thread(client.flush)
            assert daemon.worker.fatal_reason == "IntegrityError"
            later = await asyncio.to_thread(client.ingest_event, kind="user", content="later")
            assert not later.accepted and later.reason == "worker_failed"
            with Database(directory / "db.sqlite", read_only=True) as db:
                assert db.status()["messages"] == 0
        finally:
            await daemon.close()

    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        asyncio.run(scenario(Path(raw)))


def test_corrupt_database_startup_releases_socket_lease() -> None:
    async def scenario(directory: Path) -> None:
        path = directory / "broken.db"
        path.write_text("not sqlite")
        daemon = FractalDaemon(path, directory / "d.sock")
        with pytest.raises(sqlite3.DatabaseError):
            await daemon.start()
        assert not (directory / "d.sock").exists()
        lease = SocketLease(directory / "d.sock")
        lease.close()

    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        asyncio.run(scenario(Path(raw)))


def test_bad_security_input_is_isolated_and_valid_neighbors_commit() -> None:
    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        with running_daemon(Path(raw)) as (_, client):
            assert client.ingest_event(kind="user", content="before").accepted
            assert client.ingest_event(kind="tool", content="[" * 66 + "0" + "]" * 66).accepted
            assert client.ingest_event(kind="user", content="after").accepted
            status = client.flush()
            assert status["messages"] == 2 and status["rejected_invalid"] == 1
            assert not client.status()["worker_failed"]
            assert client.ingest_event(kind="user", content="still healthy").accepted
            assert client.flush()["messages"] == 3


def test_restart_recovers_fully_ready_batch_without_in_memory_dirty_flags() -> None:
    from fractalmem.compaction import Compactor
    from fractalmem.protocol import Event

    async def prepare(path: Path) -> None:
        with Database(path) as db:
            db.append_events(
                [Event("project", "session", f"event-{i}", "user", "x" * 480) for i in range(270)]
            )
            assert db.load_view("project").batch_active
            while jobs := db.claim_jobs():
                for job in jobs:
                    node = db.get_node(job.project_id, job.node_id)
                    assert node is not None
                    assert node.level > 0
                    left = db.resolve_parent(node.level - 1, node.index_in_level * 2, "project")
                    right = db.resolve_parent(
                        node.level - 1, node.index_in_level * 2 + 1, "project"
                    )
                    assert left is not None and right is not None
                    ready, _ = await Compactor().build_node(
                        node, left.summary_text + "\n" + right.summary_text
                    )
                    db.complete_node(ready, resume_view=False)
            assert db.status()["pending_jobs"] == 0
            assert db.load_view("project").batch_active

    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        directory = Path(raw)
        asyncio.run(prepare(directory / "db.sqlite"))
        with running_daemon(directory) as (_, client):
            assert client.flush(compact=True)["messages"] == 270
            with Database(directory / "db.sqlite", read_only=True) as db:
                assert not db.load_view("project").batch_active
                assert db.load_view("project").byte_size <= 64000


def test_shutdown_cancels_blocked_compaction_and_drains_raw_events() -> None:
    from fractalmem.compaction import Compactor, ProviderReply

    # A deliberately blocked provider is a cancellation fault fixture; storage
    # and daemon sockets are real. Actual HTTP contracts are tested separately.
    class BlockedProvider:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def complete(self, view_buffer: str, task: str) -> ProviderReply:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario(directory: Path) -> None:
        provider = BlockedProvider()
        daemon = FractalDaemon(
            directory / "db.sqlite", directory / "d.sock", compactor=Compactor(provider)
        )
        await daemon.start()
        client = FractalClient("project", socket_path=directory / "d.sock")
        receipt = await asyncio.to_thread(
            client.ingest_event, kind="tool", content="large source " * 100
        )
        assert receipt.accepted
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        barrier = asyncio.create_task(asyncio.to_thread(client.flush, compact=True))
        await asyncio.sleep(0.02)
        await asyncio.wait_for(daemon.close(), timeout=2)
        with pytest.raises(OSError):
            await barrier
        with Database(directory / "db.sqlite", read_only=True) as db:
            assert db.status()["messages"] == 1

    with tempfile.TemporaryDirectory(prefix="fm-", dir="/tmp") as raw:
        asyncio.run(scenario(Path(raw)))
