"""Versioned bounded UDS server. Agent hooks never open mutable SQLite."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import socket
import stat
import sys
from pathlib import Path
from typing import cast

from fractalmem.client import DEFAULT_SOCKET, MAX_FRAME_BYTES
from fractalmem.compaction import Compactor, HTTPTextProvider, ProviderConfig
from fractalmem.daemon.ring_buffer import RingBuffer
from fractalmem.daemon.worker import BarrierWork, EventWork, WorkItem, WriterWorker
from fractalmem.protocol import Event, validate_identifier


class SocketLease:
    """Never remove an active socket, symlink, or unrelated regular file."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()
        self.fd: int | None = None
        self.inode: int | None = None
        if len(str(self.path).encode()) > 103:
            raise ValueError("Unix socket path exceeds 103 bytes")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.path.parent.stat()
        if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
            raise PermissionError("socket directory must be owned by this user with mode 0700")
        self.fd = os.open(
            self.path.with_name(self.path.name + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.is_symlink():
                raise FileExistsError("refusing a socket symlink")
            if self.path.exists():
                existing = self.path.stat()
                if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                    raise FileExistsError("refusing an unrelated file at socket path")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.05)
                    try:
                        probe.connect(str(self.path))
                    except ConnectionRefusedError:
                        self.path.unlink()  # Proven stale socket under the exclusive lease.
                    else:
                        raise FileExistsError("socket already has a live server")
        except BaseException:
            self.close()
            raise

    def mark_bound(self) -> None:
        os.chmod(self.path, 0o600)
        self.inode = self.path.stat().st_ino

    def close(self) -> None:
        if self.inode is not None and self.path.exists() and not self.path.is_symlink():
            current = self.path.stat()
            if stat.S_ISSOCK(current.st_mode) and current.st_ino == self.inode:
                self.path.unlink()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _text(request: dict[str, object], field: str) -> str:
    value = request.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


class FractalDaemon:
    def __init__(
        self,
        db_path: Path | str,
        socket_path: Path | str = DEFAULT_SOCKET,
        *,
        capacity: int = 16384,
        queue_bytes: int = 64 * 1024 * 1024,
        compactor: Compactor | None = None,
        synchronous: str = "NORMAL",
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.socket_path = Path(socket_path).expanduser().absolute()
        self.queue: RingBuffer[WorkItem] = RingBuffer(
            capacity, max_bytes=queue_bytes,
            sizeof=lambda item: len(item.event.payload.encode("utf-8"))
            if isinstance(item, EventWork) else 0,
        )
        self.worker = WriterWorker(
            self.db_path, self.queue, compactor=compactor, synchronous=synchronous
        )
        self._lease: SocketLease | None = None
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self.closing = False

    async def start(self) -> None:
        self._lease = SocketLease(self.socket_path)
        try:
            await self.worker.start()
            self._server = await asyncio.start_unix_server(
                self._handle, path=self.socket_path, limit=MAX_FRAME_BYTES, backlog=128
            )
            self._lease.mark_bound()
        except BaseException:
            await self.worker.close()
            self._lease.close()
            raise

    async def _dispatch(self, request: dict[str, object]) -> dict[str, object]:
        if type(request.get("version")) is not int or request["version"] != 1:
            raise ValueError("unsupported IPC version")
        operation = _text(request, "op")
        if self.closing or self.worker.fatal_reason:
            return {
                "version": 1,
                "accepted": False,
                "durable": False,
                "reason": "worker_failed" if self.worker.fatal_reason else "shutting_down",
                "event_id": request.get("event_id"),
            }
        if operation == "status":
            if set(request) != {"version", "op"}:
                raise ValueError("unknown status fields")
            return await self.worker.status()
        if operation == "retry_failed":
            if set(request) != {"version", "op", "project_id"}:
                raise ValueError("unknown retry fields")
            project = _text(request, "project_id")
            validate_identifier(project, "project_id")
            count = await self.worker.call(lambda db: db.retry_failed_jobs(project))
            self.worker.notify()
            return {"version": 1, "requeued": count}
        if operation == "ingest":
            if set(request) - {
                "version",
                "op",
                "project_id",
                "session_id",
                "event_id",
                "kind",
                "payload",
                "language",
            }:
                raise ValueError("unknown event fields")
            language = request.get("language")
            if language is not None and not isinstance(language, str):
                raise ValueError("language must be text")
            event = Event(
                _text(request, "project_id"),
                _text(request, "session_id"),
                _text(request, "event_id"),
                _text(request, "kind"),
                _text(request, "payload"),
                language,
            )
            accepted = self.queue.try_push(EventWork(event))
            if accepted:
                self.worker.notify()
            return {
                "version": 1,
                "accepted": accepted,
                "durable": False,
                "event_id": event.event_id,
                "reason": "queued" if accepted else "queue_rejected",
            }
        if operation == "flush":
            if set(request) - {"version", "op", "compact", "timeout_seconds"}:
                raise ValueError("unknown flush fields")
            compact = request.get("compact", False)
            if type(compact) is not bool:
                raise ValueError("compact must be a boolean")
            timeout = request.get("timeout_seconds", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
                raise ValueError("flush timeout must be 0..300 seconds")
            future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
            if not self.queue.try_push(BarrierWork(future, compact)):
                return {"version": 1, "durable": False, "reason": "queue_rejected"}
            self.worker.barriers.add(future)
            self.worker.notify()
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=float(timeout))
            except TimeoutError:
                return {"version": 1, "durable": False, "reason": "flush_timeout"}
            finally:
                # An abandoned request must not retain a worker-side waiter.
                if not future.done():
                    future.cancel()
                self.worker.barriers.discard(future)
                self.worker.notify()
        raise ValueError("unknown IPC operation")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("socket handler has no task")
        if len(self._connections) >= 256:
            writer.close()
            return
        self._connections.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=0.1)
            if len(raw) > MAX_FRAME_BYTES:
                raise ValueError("frame exceeds IPC limit")
            decoded: object = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("request must be an object")
            response = await self._dispatch(cast(dict[str, object], decoded))
            writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (
            ValueError,
            UnicodeError,
            TimeoutError,
            asyncio.LimitOverrunError,
            asyncio.IncompleteReadError,
        ):
            writer.write(
                b'{"version":1,"accepted":false,"durable":false,"reason":"invalid_request"}\n'
            )
            try:
                await writer.drain()
            except (ConnectionError, BrokenPipeError):
                pass  # The peer already failed open; no event payload is logged.
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            self._connections.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    async def close(self) -> None:
        self.closing = True
        if self._server is not None:
            self._server.close()
        for task in tuple(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        await self.worker.close()
        if self._server is not None:
            await self._server.wait_closed()
        if self._lease is not None:
            self._lease.close()


async def _run(args: argparse.Namespace) -> None:
    provider: HTTPTextProvider | None = None
    if args.provider != "offline":
        if not args.base_url or not args.model:
            raise ValueError("model compaction requires --base-url and --model")
        provider = HTTPTextProvider(
            ProviderConfig(
                args.provider,
                args.base_url,
                args.model,
                api_key=os.environ.get(args.api_key_env) if args.api_key_env else None,
                airgap=args.airgap,
                airgap_file=Path.cwd() / ".fractalmem_airgap",
            )
        )
    daemon = FractalDaemon(
        args.db,
        args.socket,
        capacity=args.capacity,
        queue_bytes=args.queue_bytes,
        compactor=Compactor(provider),
        synchronous=args.synchronous,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await daemon.start()
    print('{"event":"daemon_ready"}', file=sys.stderr, flush=True)
    try:
        await stop.wait()
    finally:
        await daemon.close()
        if provider is not None:
            await provider.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="FractalMem single-writer daemon")
    parser.add_argument("--db", type=Path, default=Path.home() / ".fractalmem" / "memory.db")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--capacity", type=int, default=16384)
    parser.add_argument("--queue-bytes", type=int, default=64 * 1024 * 1024,
                        help="Maximum queued UTF-8 payload bytes (not total process RSS)")
    parser.add_argument("--synchronous", choices=("NORMAL", "FULL"), default="NORMAL")
    parser.add_argument("--provider", choices=("offline", "openai", "anthropic"), default="offline")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env")
    parser.add_argument("--airgap", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except (OSError, ValueError, RuntimeError) as error:
        print(
            json.dumps({"event": "daemon_failed", "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
