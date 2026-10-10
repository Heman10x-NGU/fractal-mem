"""One owner thread commits event batches; bounded async tasks compact nodes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

from fractalmem.compaction import CompactionError, Compactor
from fractalmem.core.node import ByteBudgetExceededError, HMRNode
from fractalmem.core.summary import SummaryBudgetError
from fractalmem.daemon.ring_buffer import RingBuffer
from fractalmem.protocol import Event
from fractalmem.security.redact import RedactionError
from fractalmem.storage.db import AppendResult, CompactionJob, Database, EventConflictError

R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class EventWork:
    event: Event


@dataclass(frozen=True, slots=True)
class BarrierWork:
    future: asyncio.Future[dict[str, object]]
    compact: bool = False


WorkItem = EventWork | BarrierWork


class WriterWorker:
    def __init__(
        self,
        path: Path,
        queue: RingBuffer[WorkItem],
        *,
        compactor: Compactor | None = None,
        batch_size: int = 256,
        synchronous: str = "NORMAL",
    ) -> None:
        if not 1 <= batch_size <= 1024:
            raise ValueError("writer batch size must be 1..1024")
        self.path = path
        self.queue = queue
        self.compactor = compactor or Compactor()
        self.batch_size = batch_size
        self.synchronous = synchronous
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fractalmem-writer")
        self._db: Database | None = None
        self._task: asyncio.Task[None] | None = None
        self._compactions: set[asyncio.Task[None]] = set()
        self.wake = asyncio.Event()
        self.stopping = False
        self.fatal_reason: str | None = None
        self.committed = 0
        self.duplicates = 0
        self.conflicts = 0
        self.rejected_invalid = 0
        self.barriers: set[asyncio.Future[dict[str, object]]] = set()
        self._dirty_views: set[str] = set()
        self._completions_since_resume = 0

    def _open(self) -> None:
        self._db = Database(self.path, synchronous=self.synchronous)
        try:
            self._db.recover_jobs()
            self._db.reconcile_views()
            self._db.export_views()
        except BaseException:
            self._db.close()
            self._db = None
            raise

    def _operate(self, function: Callable[[Database], R]) -> R:
        if self._db is None:
            raise RuntimeError("writer database is not initialized")
        return function(self._db)

    async def call(self, function: Callable[[Database], R]) -> R:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._operate, function
        )

    async def start(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._open)
        except BaseException:
            self._executor.shutdown(wait=True, cancel_futures=True)
            raise
        self._task = asyncio.create_task(self._run(), name="fractalmem-writer")

    def notify(self) -> None:
        self.wake.set()

    async def _persist(self, events: list[Event]) -> None:
        if not events:
            return
        try:
            results = await self.call(lambda db: db.append_events(events))
            self.committed += sum(not result.duplicate for result in results)
            self.duplicates += sum(result.duplicate for result in results)
        except (EventConflictError, RedactionError):
            # The whole fast batch rolled back. Isolate identity conflicts so
            # one invalid producer does not poison other producers' events.
            for event in events:
                try:

                    def append_one(db: Database, current: Event = event) -> AppendResult:
                        return db.append_event(current)

                    result = await self.call(append_one)
                    self.duplicates += int(result.duplicate)
                    self.committed += int(not result.duplicate)
                except EventConflictError:
                    self.conflicts += 1
                except RedactionError:
                    self.rejected_invalid += 1
        await self.call(lambda db: db.export_views([event.project_id for event in events]))

    def _source(self, db: Database, job: CompactionJob) -> tuple[HMRNode, str, str]:
        node = db.get_node(job.project_id, job.node_id)
        if node is None:
            raise RuntimeError("compaction job references missing node")
        if node.level == 0:
            message = db.get_message(job.project_id, node.start_seq)
            if message is None:
                raise RuntimeError("leaf node references missing message")
            source = f"{message.kind}: {message.payload}"
        else:
            left = db.resolve_parent(node.level - 1, node.index_in_level * 2, job.project_id)
            right = db.resolve_parent(node.level - 1, node.index_in_level * 2 + 1, job.project_id)
            if left is None or right is None or left.status != "ready" or right.status != "ready":
                raise RuntimeError("merge job children are not ready")
            source = left.summary_text + "\n" + right.summary_text
        view = (
            db.load_view(job.project_id).render_buffer()
            if self.compactor.provider is not None
            else ""
        )
        return node, source, view

    async def _compact(self, job: CompactionJob) -> None:
        try:
            node, source, view = await self.call(lambda db: self._source(db, job))
            ready, result = await self.compactor.build_node(node, source, view)

            def finish(db: Database) -> None:
                with db.transaction():
                    db.complete_node(ready, resume_view=False)
                    db.record_compaction(
                        ready.project_id,
                        ready.node_id,
                        result.mode,
                        json.dumps([asdict(usage) for usage in result.usage]),
                    )

            await self.call(finish)
            self._dirty_views.add(ready.project_id)
            self._completions_since_resume += 1
        except (
            CompactionError,
            RedactionError,
            ByteBudgetExceededError,
            SummaryBudgetError,
        ) as error:
            reason = type(error).__name__
            await self.call(lambda db: db.fail_job(job, reason))
        finally:
            self.wake.set()

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._compactions.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.fatal_reason = type(error).__name__
                self.wake.set()

    async def _schedule(self) -> bool:
        slots = 8 - len(self._compactions)
        if slots <= 0 or self.stopping or self.fatal_reason:
            return False
        jobs = await self.call(lambda db: db.claim_jobs(slots))
        for job in jobs:
            task = asyncio.create_task(self._compact(job), name="fractalmem-compaction")
            self._compactions.add(task)
            task.add_done_callback(self._finished)
        return bool(jobs)

    async def _drain_compactions(self) -> None:
        while not self.fatal_reason:
            scheduled = await self._schedule()
            await self._resume_views(force=not self._compactions)
            if not self._compactions and not scheduled:
                return
            if self._compactions:
                await asyncio.wait(self._compactions, return_when=asyncio.FIRST_COMPLETED)

    async def _resume_views(self, *, force: bool = False) -> None:
        if not self._dirty_views or (not force and self._completions_since_resume < 16):
            return
        projects = tuple(self._dirty_views)
        self._dirty_views.clear()
        self._completions_since_resume = 0

        def resume(db: Database) -> None:
            for project in projects:
                if db.publish_ready_view(project):
                    db.export_views([project])

        await self.call(resume)

    async def _barrier(self, item: BarrierWork) -> None:
        if item.compact:
            await self._drain_compactions()
        status = await self.call(lambda db: db.status())
        if not item.future.done():
            item.future.set_result(
                {
                    "version": 1,
                    "durable": self.fatal_reason is None,
                    "compacted": status["pending_jobs"] == 0 and status["failed_jobs"] == 0,
                    "committed": self.committed,
                    "duplicates": self.duplicates,
                    "conflicts": self.conflicts,
                    "rejected_invalid": self.rejected_invalid,
                    **status,
                }
            )
        self.barriers.discard(item.future)

    async def _run(self) -> None:
        try:
            while not self.stopping or len(self.queue):
                if self.fatal_reason:
                    break
                items = self.queue.pop_batch(self.batch_size)
                pending: list[Event] = []
                for item in items:
                    if isinstance(item, EventWork):
                        pending.append(item.event)
                    else:
                        await self._persist(pending)
                        pending = []
                        await self._barrier(item)
                await self._persist(pending)
                await self._schedule()
                await self._resume_views(force=not self._compactions)
                if not items:
                    self.wake.clear()
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=0.01)
                    except TimeoutError:
                        pass  # Bounded queue polling, not a swallowed work failure.
        except Exception as error:
            self.fatal_reason = type(error).__name__
        finally:
            if self.fatal_reason:
                for future in self.barriers:
                    if not future.done():
                        future.set_result(
                            {"version": 1, "durable": False, "reason": "worker_failed"}
                        )
                self.barriers.clear()
                for item in self.queue.pop_batch(self.queue.capacity):
                    if isinstance(item, BarrierWork) and not item.future.done():
                        item.future.set_result(
                            {"version": 1, "durable": False, "reason": "worker_failed"}
                        )

    async def status(self) -> dict[str, object]:
        stored = await self.call(lambda db: db.status())
        stats = self.queue.stats()
        return {
            "version": 1,
            **stored,
            "queue_size": stats.size,
            "capacity": stats.capacity,
            "admitted": stats.admitted,
            "rejected_full": stats.rejected_full,
            "rejected_contention_lower_bound": stats.rejected_contention,
            "committed": self.committed,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "rejected_invalid": self.rejected_invalid,
            "worker_failed": self.fatal_reason is not None,
            "failure_type": self.fatal_reason,
        }

    async def close(self) -> None:
        self.stopping = True
        self.wake.set()
        for task in tuple(self._compactions):
            task.cancel()
        if self._compactions:
            await asyncio.gather(*self._compactions, return_exceptions=True)
        if self._task is not None:
            await self._task
        if self._db is not None:
            if self.fatal_reason is None:
                await self.call(lambda db: db.reconcile_views())
            await self.call(lambda db: db.close())
            self._db = None
        self._executor.shutdown(wait=True, cancel_futures=True)
