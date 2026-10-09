"""Deadline-based nonblocking UDS hook client with explicit fail-open receipts."""

from __future__ import annotations

import argparse
import errno
import json
import selectors
import socket
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from fractalmem.protocol import Event

DEFAULT_SOCKET = Path.home() / ".fractalmem" / "fractald.sock"
MAX_FRAME_BYTES = 270_336


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    accepted: bool
    durable: bool
    event_id: str
    reason: str


class TransportError(OSError):
    pass


def _request(path: Path, payload: dict[str, object], deadline: float) -> dict[str, object]:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("IPC frame exceeds limit")
    if len(str(path).encode()) > 103:
        raise ValueError("Unix socket path is too long")
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection,
        selectors.DefaultSelector() as selector,
    ):
        connection.setblocking(False)
        result = connection.connect_ex(str(path))
        if result not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN):
            raise TransportError("daemon unavailable")
        selector.register(connection, selectors.EVENT_WRITE)

        def wait(mask: int) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("IPC deadline expired")
            selector.modify(connection, mask)
            if not selector.select(remaining):
                raise TimeoutError("IPC deadline expired")

        wait(selectors.EVENT_WRITE)
        if connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
            raise TransportError("daemon connection failed")
        sent = 0
        while sent < len(data):
            try:
                count = connection.send(data[sent:])
                if count == 0:
                    raise TransportError("daemon closed while sending")
                sent += count
            except BlockingIOError:
                wait(selectors.EVENT_WRITE)
            if time.monotonic() >= deadline:
                raise TimeoutError("IPC deadline expired")
        reply = bytearray()
        while b"\n" not in reply:
            wait(selectors.EVENT_READ)
            try:
                chunk = connection.recv(8192)
            except BlockingIOError:
                continue
            if not chunk:
                raise TransportError("daemon closed before receipt")
            reply.extend(chunk)
            if len(reply) > 65_536:
                raise TransportError("daemon receipt exceeds limit")
        decoded: object = json.loads(reply.split(b"\n", 1)[0])
        if not isinstance(decoded, dict):
            raise TransportError("daemon returned an invalid receipt")
        return cast(dict[str, object], decoded)


class FractalClient:
    def __init__(
        self,
        project_id: str = "default",
        session_id: str = "default",
        *,
        socket_path: Path | str = DEFAULT_SOCKET,
        timeout_seconds: float = 0.015,
    ) -> None:
        if not 0 < timeout_seconds <= 0.015:
            raise ValueError("hook timeout must be between 0 and 15 milliseconds")
        self.project_id = project_id
        self.session_id = session_id
        self.socket_path = Path(socket_path).expanduser()
        self.timeout_seconds = timeout_seconds

    def ingest_event(
        self, *, kind: str, content: str, event_id: str | None = None, language: str | None = None
    ) -> IngestReceipt:
        deadline = time.monotonic() + self.timeout_seconds
        identity = event_id or uuid.uuid4().hex
        try:
            event = Event(self.project_id, self.session_id, identity, kind, content, language)
            reply = _request(
                self.socket_path, {"version": 1, "op": "ingest", **asdict(event)}, deadline
            )
            if (
                type(reply.get("version")) is not int
                or reply.get("version") != 1
                or type(reply.get("accepted")) is not bool
                or reply.get("event_id") != identity
            ):
                return IngestReceipt(False, False, identity, "malformed_response")
            return IngestReceipt(
                cast(bool, reply["accepted"]), False, identity, str(reply.get("reason", "unknown"))
            )
        except TimeoutError:
            return IngestReceipt(False, False, identity, "timeout")
        except (OSError, UnicodeError, json.JSONDecodeError):
            return IngestReceipt(False, False, identity, "unavailable")
        except (ValueError, TypeError):
            return IngestReceipt(False, False, identity, "invalid_event")

    def status(self, timeout_seconds: float = 2.0) -> dict[str, object]:
        return _request(
            self.socket_path, {"version": 1, "op": "status"}, time.monotonic() + timeout_seconds
        )

    def retry_failed(self, timeout_seconds: float = 2.0) -> dict[str, object]:
        return _request(
            self.socket_path,
            {"version": 1, "op": "retry_failed", "project_id": self.project_id},
            time.monotonic() + timeout_seconds,
        )

    def flush(self, *, compact: bool = False, timeout_seconds: float = 30.0) -> dict[str, object]:
        """Durable barrier for all events admitted before this request.

        Unlike the fail-open hook, this explicit durability operation waits and
        raises on transport failure. compact=True also drains current jobs.
        """
        reply = _request(
            self.socket_path,
            {"version": 1, "op": "flush", "compact": compact, "timeout_seconds": timeout_seconds},
            time.monotonic() + timeout_seconds,
        )
        if reply.get("durable") is not True:
            raise TransportError("daemon could not complete durable barrier")
        if compact and reply.get("compacted") is not True:
            raise TransportError("events are durable but compaction has failed or remains pending")
        return reply


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail-open FractalMem event hook")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--project", default="default")
    parser.add_argument("--session", default="default")
    parser.add_argument(
        "--kind", choices=("user", "unii", "tool", "echo", "work", "note"), default="user"
    )
    parser.add_argument("--language", choices=("python", "javascript", "typescript", "tsx"))
    parser.add_argument("--event-id")
    parser.add_argument(
        "--operation", choices=("ingest", "status", "flush", "retry-failed"), default="ingest"
    )
    parser.add_argument("--compact", action="store_true", help="Drain compactions during flush")
    args = parser.parse_args()
    client = FractalClient(args.project, args.session, socket_path=args.socket)
    if args.operation != "ingest":
        if args.operation == "status":
            result = client.status()
        elif args.operation == "flush":
            result = client.flush(compact=args.compact)
        else:
            result = client.retry_failed()
        print(json.dumps(result))
        return
    # Bounded stdin; never read an unbounded tool output into the hook process.
    raw = sys.stdin.buffer.read(262_145)
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        print(json.dumps({"accepted": False, "durable": False, "reason": "invalid_utf8"}))
        return
    receipt = client.ingest_event(
        kind=args.kind, content=content, event_id=args.event_id, language=args.language
    )
    print(json.dumps(asdict(receipt)))


if __name__ == "__main__":
    main()
