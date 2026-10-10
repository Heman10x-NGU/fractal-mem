"""Real disk/worker regressions; deterministic providers require no external model."""

from __future__ import annotations

import asyncio
import errno
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from benchmarks.cache_tracer import load_database_usage
from fractalmem.compaction import (
    Compactor,
    HTTPTextProvider,
    ProviderConfig,
    ProviderError,
    ProviderReply,
    Usage,
    observe_compaction_usage,
)
from fractalmem.core.node import HMRNode
from fractalmem.core.view import View, ViewPersistenceError
from fractalmem.daemon.ring_buffer import RingBuffer
from fractalmem.daemon.server import FractalDaemon
from fractalmem.storage.db import Database


async def ingest(daemon: FractalDaemon, identity: str, text: str) -> None:
    result = await daemon._dispatch({
        "version": 1, "op": "ingest", "project_id": "p", "session_id": "s",
        "event_id": identity, "kind": "user", "payload": text,
    })
    assert result["accepted"] is True


async def flush(daemon: FractalDaemon, *, compact: bool = False, timeout: float = 5) -> dict[str, object]:
    return await daemon._dispatch({
        "version": 1, "op": "flush", "compact": compact, "timeout_seconds": timeout,
    })


class GatedProvider:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0

    async def complete(self, view_buffer: str, task: str) -> ProviderReply:
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered.set()
        try:
            await self.release.wait()
            return ProviderReply("short summary", Usage("openai", input_tokens=10))
        finally:
            self.active -= 1


def test_compact_waiters_do_not_block_ingestion_or_exceed_concurrency(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = GatedProvider()
        daemon = FractalDaemon(tmp_path / "memory.db", compactor=Compactor(provider))
        await daemon.worker.start()  # Exercise real writer/SQLite without binding a socket.
        try:
            for i in range(20):
                await ingest(daemon, f"long-{i}", "long evidence " * 80)
            await asyncio.wait_for(provider.entered.wait(), 3)
            waiters = [asyncio.create_task(flush(daemon, compact=True)) for _ in range(3)]
            async with asyncio.timeout(3):
                while len(daemon.worker._compact_barriers) != 3:
                    await asyncio.sleep(0.001)
            await ingest(daemon, "later", "later raw event")
            plain = await asyncio.wait_for(flush(daemon), 3)
            assert plain["durable"] is True and plain["messages"] == 21
            assert not any(w.done() for w in waiters)
            provider.release.set()
            replies = await asyncio.wait_for(asyncio.gather(*waiters), 5)
            assert all(r["compacted"] is True and r["pending_jobs"] == 0 for r in replies)
            assert provider.peak <= 8
            with Database(tmp_path / "memory.db", read_only=True) as db:
                assert db.load_view("p").total_messages == 21
                assert db.status()["pending_jobs"] == 0
            assert not daemon.worker.barriers
        finally:
            await daemon.worker.close()
    asyncio.run(scenario())


def test_timed_out_compact_waiter_is_removed_and_shutdown_drains(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = GatedProvider()
        daemon = FractalDaemon(tmp_path / "memory.db", compactor=Compactor(provider))
        await daemon.worker.start()
        await ingest(daemon, "long", "source " * 200)
        await asyncio.wait_for(provider.entered.wait(), 3)
        result = await flush(daemon, compact=True, timeout=0.02)
        assert result["reason"] == "flush_timeout"
        assert not daemon.worker.barriers
        await ingest(daemon, "later", "retained on shutdown")
        await asyncio.wait_for(daemon.worker.close(), 3)
        assert not daemon.worker._compact_barriers
        assert provider.active == 0
        with Database(tmp_path / "memory.db", read_only=True) as db:
            assert db.status()["messages"] == 2
    asyncio.run(scenario())


def test_shutdown_during_job_claim_does_not_start_new_provider_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()
    original = Database.claim_jobs

    def gated_claim(db: Database, limit: int = 8):
        jobs = original(db, limit)
        if jobs:
            entered.set()
            assert release.wait(3)
        return jobs

    monkeypatch.setattr(Database, "claim_jobs", gated_claim)

    async def scenario() -> None:
        provider = GatedProvider()
        daemon = FractalDaemon(tmp_path / "memory.db", compactor=Compactor(provider))
        await daemon.worker.start()
        await ingest(daemon, "one", "source " * 200)
        assert await asyncio.to_thread(entered.wait, 3)
        closing = asyncio.create_task(daemon.worker.close())
        await asyncio.sleep(0)
        assert daemon.worker.stopping
        release.set()
        await asyncio.wait_for(closing, 3)
        assert not provider.entered.is_set()
        assert not daemon.worker._compactions
        with Database(tmp_path / "memory.db") as db:
            assert db.recover_jobs() == 1
    asyncio.run(scenario())


def test_export_failure_stops_admission_and_fails_barrier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_export(db: Database, projects=None) -> None:
        raise ViewPersistenceError("injected export durability failure")

    async def scenario() -> None:
        daemon = FractalDaemon(tmp_path / "memory.db")
        await daemon.worker.start()
        monkeypatch.setattr(Database, "export_views", fail_export)
        try:
            await ingest(daemon, "one", "source")
            result = await flush(daemon)
            assert result["durable"] is False
            assert daemon.worker.fatal_reason == "ViewPersistenceError"
            later = await daemon._dispatch({"version": 1, "op": "status"})
            assert later["reason"] == "worker_failed"
        finally:
            await daemon.worker.close()
    asyncio.run(scenario())


def test_usage_survives_failure_and_successful_retry_without_double_count(tmp_path: Path) -> None:
    class Provider:
        fail = True

        async def complete(self, view_buffer: str, task: str) -> ProviderReply:
            if "Compaction: extract" in task:
                return ProviderReply("facts", Usage("openai", input_tokens=10))
            if self.fail:
                raise ProviderError("fixture transport failure")
            return ProviderReply("summary", Usage("openai", input_tokens=20))

    async def scenario() -> None:
        provider = Provider()
        daemon = FractalDaemon(tmp_path / "memory.db", compactor=Compactor(provider))
        await daemon.worker.start()
        try:
            await ingest(daemon, "one", "source " * 200)
            failed = await flush(daemon, compact=True)
            assert failed["compacted"] is False
            assert [u.input_tokens for u in load_database_usage(tmp_path / "memory.db")] == [10]
            provider.fail = False
            await daemon._dispatch({"version": 1, "op": "retry_failed", "project_id": "p"})
            assert (await flush(daemon, compact=True))["compacted"] is True
            assert [u.input_tokens for u in load_database_usage(tmp_path / "memory.db")] == [10, 10, 20]
        finally:
            await daemon.worker.close()
    asyncio.run(scenario())


def test_exhaustion_records_every_response_and_cancellation_retains_facts(tmp_path: Path) -> None:
    class Provider:
        block = False
        entered = None

        async def complete(self, view_buffer: str, task: str) -> ProviderReply:
            if self.block and "Compaction: extract" not in task:
                assert self.entered is not None
                self.entered.set()
                await asyncio.Event().wait()
            return ProviderReply("x" * 600, Usage("openai", input_tokens=7))

    async def scenario() -> None:
        provider = Provider()
        daemon = FractalDaemon(tmp_path / "memory.db", compactor=Compactor(provider))
        await daemon.worker.start()
        try:
            await ingest(daemon, "one", "source " * 200)
            assert (await flush(daemon, compact=True))["compacted"] is False
            assert len(load_database_usage(tmp_path / "memory.db")) == 6
            provider.block = True
            provider.entered = asyncio.Event()
            await daemon._dispatch({"version": 1, "op": "retry_failed", "project_id": "p"})
            await asyncio.wait_for(provider.entered.wait(), 3)
        finally:
            await daemon.worker.close()
        assert len(load_database_usage(tmp_path / "memory.db")) == 7
    asyncio.run(scenario())


def test_rejected_http_reply_retains_usage() -> None:
    async def scenario() -> None:
        observed: list[Usage] = []

        async def record(usage: Usage) -> None:
            observed.append(usage)

        def response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"tool_calls": [{"name": "forbidden"}]}}],
                "usage": {"prompt_tokens": 25, "completion_tokens": 4},
            })

        provider = HTTPTextProvider(ProviderConfig("openai", "http://127.0.0.1/v1", "fixture"))
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        try:
            with observe_compaction_usage(record), pytest.raises(ProviderError, match="tool call"):
                await provider.complete("", "task")
            assert len(observed) == 1 and observed[0].input_tokens == 25
        finally:
            await provider.aclose()
    asyncio.run(scenario())


def test_usage_observers_are_task_local() -> None:
    class Provider:
        async def complete(self, view_buffer: str, task: str) -> ProviderReply:
            await asyncio.sleep(0)
            return ProviderReply("summary", Usage("openai", input_tokens=int(view_buffer)))

    async def scenario() -> None:
        compactor = Compactor(Provider())
        async def one(number: int) -> list[int | None]:
            observed: list[int | None] = []
            async def record(usage: Usage) -> None:
                observed.append(usage.input_tokens)
            with observe_compaction_usage(record):
                await compactor.compact("source " * 200, str(number))
            return observed
        assert await asyncio.gather(one(11), one(22)) == [[11, 11], [22, 22]]
    asyncio.run(scenario())


def test_ring_byte_budget_utf8_wraparound_and_concurrent_admission() -> None:
    queue: RingBuffer[str] = RingBuffer(8, max_bytes=8, sizeof=lambda s: len(s.encode()))
    assert queue.try_push("🚀") and queue.try_push("éé")
    assert queue.stats().queued_bytes == 8
    assert not queue.try_push("x")
    assert queue.stats().rejected_bytes == 1
    assert queue.pop_batch(1) == ["🚀"]
    assert queue.try_push("abcd")
    assert queue.pop_batch(8) == ["éé", "abcd"]
    assert queue.stats().queued_bytes == 0
    assert not queue.try_push("x" * 9)
    concurrent: RingBuffer[str] = RingBuffer(1000, max_bytes=100, sizeof=len)
    with ThreadPoolExecutor(8) as pool:
        accepted = list(pool.map(concurrent.try_push, ["x" * 10] * 1000))
    assert sum(accepted) <= 10
    assert concurrent.stats().queued_bytes == sum(accepted) * 10
    assert len(concurrent.pop_batch(1000)) == sum(accepted)
    assert concurrent.stats().queued_bytes == 0


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_ring_rejects_invalid_byte_limits(limit: object) -> None:
    with pytest.raises(ValueError):
        RingBuffer(8, max_bytes=limit, sizeof=len)


def test_directory_fsync_failure_surfaces_without_removing_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        pytest.skip("directory fsync is POSIX-specific")
    real_fsync = os.fsync
    import stat
    directory_fds: list[int] = []

    def fail_directory(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fds.append(fd)
            raise OSError(errno.EIO, "injected directory flush failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    view = View("p", 1, (HMRNode.create_leaf(0, "ready", project_id="p"),))
    target = tmp_path / "view.json"
    with pytest.raises(ViewPersistenceError):
        view.save_atomic(target)
    assert View.load_atomic(target).render_buffer() == view.render_buffer()
    assert not list(tmp_path.glob(".tmp_*"))
    for fd in directory_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
