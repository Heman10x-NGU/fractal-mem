"""Measure real multiprocess hooks against an isolated fractald subprocess.

The configured 15 ms deadline is separate from observed OS scheduling tails.
Process and interpreter startup are excluded from each ingest_event timing.
No rejected/ambiguous receipt is silently retried; disk reconciliation records
which timed-out attempts actually persisted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from multiprocessing.synchronize import Barrier
from pathlib import Path
from typing import cast

from benchmarks.eval_harness import environment, percentiles, write_artifacts
from fractalmem.client import FractalClient
from fractalmem.storage.db import Database

_GATE: Barrier | None = None


def _initialize(gate: Barrier) -> None:
    global _GATE
    _GATE = gate


def _payload(producer: int, index: int, rng: random.Random, large: bool, large_bytes: int) -> str:
    text = (
        f"def udsNeedle_{producer}_{index}(value):\n"
        f"    return value + {rng.randrange(1_000_000)}\n"
        "# passive terminal memory evidence; preserve the exact symbol and parent handle\n"
    )
    if large:
        line = "# repeated passive disk compaction workload; preserve chronological evidence\n"
        text += line * ((large_bytes - len(text.encode())) // len(line.encode()))
    return text


def _produce(arguments: tuple[str, int, int, int, int, int]) -> dict[str, object]:
    socket_path, producer, count, seed, large_every, large_bytes = arguments
    client = FractalClient("udsbench", f"producer-{producer}", socket_path=socket_path)
    rng = random.Random(seed + producer)
    rows: list[dict[str, object]] = []
    digest = hashlib.sha256()
    if _GATE is None:
        raise RuntimeError("producer start gate was not initialized")
    _GATE.wait(timeout=30)
    for index in range(count):
        large = large_every > 0 and index % large_every == 0
        text = _payload(producer, index, rng, large, large_bytes)
        digest.update(text.encode())
        digest.update(b"\0")
        identity = f"event-{producer}-{index}"
        started = time.perf_counter_ns()
        receipt = client.ingest_event(
            kind="tool", content=text, event_id=identity, language="python"
        )
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        if receipt.durable:
            raise RuntimeError("hook receipt unexpectedly claimed durable persistence")
        rows.append(
            {
                "producer": producer,
                "event_index": index,
                "event_id": identity,
                "payload_bytes": len(text.encode()),
                "large_payload": large,
                "accepted_volatile": receipt.accepted,
                "receipt_durable": receipt.durable,
                "reason": receipt.reason,
                "ingest_event_ms": elapsed,
                "above_configured_15ms": elapsed > 15.0,
            }
        )
    return {"rows": rows, "corpus_sha256": digest.hexdigest()}


class _Daemon:
    def __init__(self, directory: Path, capacity: int) -> None:
        self.directory = directory
        self.socket_path = directory / "d.sock"
        self.db_path = directory / "memory.sqlite"
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "fractalmem.daemon.server",
                "--db",
                str(self.db_path),
                "--socket",
                str(self.socket_path),
                "--capacity",
                str(capacity),
                "--provider",
                "offline",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.client = FractalClient("udsbench", "coordinator", socket_path=self.socket_path)
        self._closed = False

    def ready(self, timeout_seconds: float = 30.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stop()
                raise RuntimeError("benchmark daemon exited before becoming ready")
            try:
                status = self.client.status(timeout_seconds=0.5)
                if status.get("journal_mode") == "wal":
                    return status
            except OSError:
                time.sleep(0.01)
        raise TimeoutError("benchmark daemon readiness timed out")

    def stop(self) -> dict[str, object]:
        if self._closed:
            return {"graceful": self.process.returncode == 0, "exit_code": self.process.returncode}
        started = time.perf_counter()
        forced = False
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = self.process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            forced = True
            self.process.kill()
            stdout, stderr = self.process.communicate(timeout=5)
        self._closed = True
        return {
            "graceful": not forced and self.process.returncode == 0,
            "forced_kill": forced,
            "exit_code": self.process.returncode,
            "shutdown_ms": (time.perf_counter() - started) * 1000,
            "socket_removed": not self.socket_path.exists(),
            "protocol_stdout_empty": stdout == "",
            "diagnostic_bytes": len(stderr.encode()),
        }


def run_uds(
    *,
    producers: int = 4,
    events_per_producer: int = 1_000,
    capacity: int = 16_384,
    seed: int = 42,
    large_every: int = 64,
    large_bytes: int = 65_536,
    restart: bool = True,
    overall_timeout_seconds: float = 600.0,
) -> dict[str, object]:
    if not 1 <= producers <= 32 or not 1 <= events_per_producer <= 100_000:
        raise ValueError("producers must be 1..32 and events_per_producer 1..100000")
    if producers * events_per_producer > 1_000_000:
        raise ValueError("benchmark is bounded to one million hook attempts")
    if not 1 <= capacity <= 1_000_000 or not 0 <= large_every <= 100_000:
        raise ValueError("invalid queue capacity or large payload interval")
    if not 1_024 <= large_bytes <= 262_144:
        raise ValueError("large payload size must be 1024..262144 bytes")
    if not 30 <= overall_timeout_seconds <= 900:
        raise ValueError("overall administrative timeout must be 30..900 seconds")
    rows: list[dict[str, object]] = []
    hashes: list[str] = []
    stages: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    failure: dict[str, object] | None = None
    initial_status: dict[str, object] | None = None
    before_flush_status: dict[str, object] | None = None
    final_status: dict[str, object] | None = None
    flush: dict[str, object] | None = None
    flush_ms: float | None = None
    traffic_elapsed: float | None = None
    total_producer_seconds: float | None = None
    producer_results_complete = False
    socket_mode: int | None = None
    wal_bytes: int | None = None
    db_bytes: int | None = None
    persisted_ids: set[str] = set()
    before_view: bytes | None = None
    pragmas: dict[str, object] = {}
    restarted_equal: bool | None = None
    restart_status: dict[str, object] | None = None
    restart_shutdown: dict[str, object] | None = None
    operation_started = time.monotonic()
    overall_deadline = operation_started + overall_timeout_seconds
    active_stage = "startup"

    def stage(name: str) -> None:
        nonlocal active_stage
        active_stage = name
        record: dict[str, object] = {
            "stage": name,
            "elapsed_seconds": time.monotonic() - operation_started,
        }
        stages.append(record)
        print(
            json.dumps({"benchmark_stage": name, "elapsed_seconds": record["elapsed_seconds"]}),
            file=sys.stderr,
            flush=True,
        )

    def remaining(limit: float) -> float:
        available = overall_deadline - time.monotonic()
        if available <= 0:
            raise TimeoutError("benchmark overall administrative deadline expired")
        return min(limit, available)

    def error_record(error: BaseException) -> dict[str, object]:
        return {
            "stage": active_stage,
            "error_type": type(error).__name__,
            "error_message": str(error)[:256],
            "elapsed_seconds": time.monotonic() - operation_started,
        }

    with tempfile.TemporaryDirectory(prefix="fm-uds-", dir="/tmp") as raw:
        directory = Path(raw)
        os.chmod(directory, 0o700)
        daemon = _Daemon(directory, capacity)
        try:
            stage("startup")
            initial_status = daemon.ready(timeout_seconds=remaining(30))
            socket_mode = os.stat(daemon.socket_path).st_mode & 0o777
            ctx = multiprocessing.get_context("spawn")
            gate = ctx.Barrier(producers + 1)
            started = time.perf_counter()
            stage("producers")
            with ctx.Pool(producers, initializer=_initialize, initargs=(gate,)) as pool:
                results = pool.map_async(
                    _produce,
                    [
                        (
                            str(daemon.socket_path),
                            producer,
                            events_per_producer,
                            seed,
                            large_every,
                            large_bytes,
                        )
                        for producer in range(producers)
                    ],
                )
                gate.wait(timeout=remaining(30))
                traffic_started = time.perf_counter()
                batches = results.get(timeout=remaining(max(120, events_per_producer * 0.02 + 30)))
                traffic_elapsed = time.perf_counter() - traffic_started
            total_producer_seconds = time.perf_counter() - started
            for batch in batches:
                rows.extend(cast(list[dict[str, object]], batch["rows"]))
                hashes.append(str(batch["corpus_sha256"]))
            producer_results_complete = True
            stage("status_before_flush")
            try:
                before_flush_status = daemon.client.status(timeout_seconds=remaining(60))
            except OSError as error:
                warnings.append(error_record(error))
            stage("flush_compact")
            flush_started = time.perf_counter()
            try:
                flush = daemon.client.flush(compact=True, timeout_seconds=remaining(180))
            finally:
                flush_ms = (time.perf_counter() - flush_started) * 1000
            stage("status_after_flush")
            try:
                final_status = daemon.client.status(timeout_seconds=remaining(60))
            except OSError as error:
                warnings.append(error_record(error))
            stage("disk_reconciliation")
            with Database(daemon.db_path, read_only=True) as db:
                persisted_ids = {
                    str(row[0])
                    for row in db.connection.execute(
                        "SELECT event_id FROM messages WHERE project_id='udsbench'"
                    )
                }
                before_view = db.load_view("udsbench").render_buffer().encode()
                pragmas = {
                    name: db.connection.execute("PRAGMA " + name).fetchone()[0]
                    for name in ("journal_mode", "synchronous", "query_only")
                }
            wal_path = daemon.db_path.with_name(daemon.db_path.name + "-wal")
            wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        except (
            ValueError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            multiprocessing.TimeoutError,
        ) as error:
            failure = error_record(error)
            # Preserve physical evidence even when an administrative request fails.
            if daemon.db_path.exists():
                try:
                    with Database(daemon.db_path, read_only=True) as db:
                        failure["disk_status_at_failure"] = dict(db.status())
                except (OSError, RuntimeError, sqlite3.Error) as inspection_error:
                    failure["disk_inspection_error_type"] = type(inspection_error).__name__
        finally:
            stage("shutdown")
            shutdown = daemon.stop()
        if daemon.db_path.exists():
            db_bytes = daemon.db_path.stat().st_size
            if failure is not None:
                try:
                    with Database(daemon.db_path, read_only=True) as db:
                        persisted_ids = {
                            str(row[0])
                            for row in db.connection.execute(
                                "SELECT event_id FROM messages WHERE project_id='udsbench'"
                            )
                        }
                except (OSError, RuntimeError, sqlite3.Error) as inspection_error:
                    failure["post_shutdown_inspection_error_type"] = type(inspection_error).__name__
        if restart and failure is None:
            second = _Daemon(directory, capacity)
            try:
                remaining(30)
                stage("restart")
                restart_status = second.ready(timeout_seconds=remaining(30))
                with Database(second.db_path, read_only=True) as reader:
                    restored = reader.load_view("udsbench").render_buffer().encode()
                    restored_count = int(
                        reader.connection.execute("SELECT count(*) FROM messages").fetchone()[0]
                    )
                restarted_equal = restored == before_view and restored_count == len(persisted_ids)
            except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
                failure = error_record(error)
            finally:
                stage("restart_shutdown")
                restart_shutdown = second.stop()
    accepted = {str(row["event_id"]) for row in rows if row["accepted_volatile"]}
    timed_out = {str(row["event_id"]) for row in rows if row["reason"] == "timeout"}
    unavailable = {str(row["event_id"]) for row in rows if row["reason"] == "unavailable"}
    attempted = {str(row["event_id"]) for row in rows}
    for row in rows:
        row["persisted_after_flush"] = (
            row["event_id"] in persisted_ids if flush is not None else None
        )
        row["persisted_at_final_observation"] = row["event_id"] in persisted_ids
    groups: dict[str, object] = {}
    for group, subset in (
        ("all", rows),
        ("accepted_volatile", [row for row in rows if row["accepted_volatile"]]),
        ("timed_out", [row for row in rows if row["reason"] == "timeout"]),
        ("unavailable", [row for row in rows if row["reason"] == "unavailable"]),
        ("large_payload", [row for row in rows if row["large_payload"]]),
    ):
        groups[group] = {
            "attempts": len(subset),
            "latency_ms": percentiles([cast(float, row["ingest_event_ms"]) for row in subset]),
            "observed_above_15ms": sum(bool(row["above_configured_15ms"]) for row in subset),
        }
    return {
        "measurement": "actual_unix_socket_multiprocess_hooks_disk_wal_and_offline_compaction",
        "environment": environment(),
        "performance_conditions": "shared development machine; background processes are not controlled or isolated",
        "observed_logical_cpu_count": os.cpu_count(),
        "run_completed": failure is None,
        "failure": failure,
        "warnings": warnings,
        "operation_stages": stages,
        "overall_elapsed_seconds": time.monotonic() - operation_started,
        "configuration": {
            "producers": producers,
            "events_per_producer": events_per_producer,
            "queue_capacity": capacity,
            "configured_client_deadline_ms": 15.0,
            "administrative_status_timeout_seconds": 60.0,
            "administrative_flush_timeout_seconds": 180.0,
            "overall_administrative_timeout_seconds": overall_timeout_seconds,
            "shutdown_cleanup_timeout_seconds": 45.0,
            "seed": seed,
            "large_every": large_every,
            "large_target_bytes": large_bytes,
            "compactor": "offline_extractive",
            "process_start_method": "spawn",
        },
        "timing_scope": "ingest_event in a running producer includes validation, JSON, connect/send/read and receipt parsing; excludes process startup and corpus generation",
        "deadline_interpretation": "15ms configured transport budget; observed wall time includes OS scheduling and is not a hard real-time guarantee",
        "producer_corpus_sha256": hashes,
        "attempted_events": len(rows) if producer_results_complete else None,
        "requested_events": producers * events_per_producer,
        "recorded_attempts": len(rows),
        "producer_results_complete": producer_results_complete,
        "accepted_volatile_events": len(accepted) if producer_results_complete else None,
        "persisted_events_after_flush": len(persisted_ids) if flush is not None else None,
        "persisted_events_at_final_observation": len(persisted_ids),
        "persistence_observation": "after_flush" if flush is not None else "after_failure_shutdown",
        "accepted_missing_from_disk": sorted(accepted - persisted_ids),
        "all_accepted_durable_after_flush": accepted <= persisted_ids
        if flush is not None
        else None,
        "unexpected_persisted_ids": sorted(persisted_ids - attempted),
        "timeout_ambiguous_attempts": len(timed_out),
        "timeout_attempts_persisted": len(timed_out & persisted_ids),
        "unavailable_attempts_persisted": len(unavailable & persisted_ids),
        "receipt_reasons": dict(Counter(str(row["reason"]) for row in rows)),
        "latency_groups": groups,
        "traffic_elapsed_seconds_excluding_pool_startup": traffic_elapsed,
        "producer_total_seconds_including_pool_startup": total_producer_seconds,
        "flush_compact_ms": flush_ms,
        "flush": flush,
        "initial_status": initial_status,
        "status_before_flush": before_flush_status,
        "status_after_flush": final_status,
        "read_only_connection_pragmas": pragmas,
        "socket_permissions": oct(socket_mode) if socket_mode is not None else None,
        "temporary_directory_permissions": "0o700",
        "wal_bytes_before_shutdown": wal_bytes,
        "db_bytes_after_shutdown": db_bytes,
        "shutdown": shutdown,
        "restart_byte_exact_and_count_equal": restarted_equal,
        "restart_status": restart_status,
        "restart_shutdown": restart_shutdown,
        "provider_cache_read_rate": None,
        "provider_ttft_ms": None,
        "rows": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producers", type=int, default=4)
    parser.add_argument("--events", type=int, default=1_000, help="attempts per producer")
    parser.add_argument("--capacity", type=int, default=16_384)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--large-every", type=int, default=64, help="0 disables large payloads")
    parser.add_argument("--large-bytes", type=int, default=65_536)
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--overall-timeout", type=float, default=600.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_uds(
            producers=args.producers,
            events_per_producer=args.events,
            capacity=args.capacity,
            seed=args.seed,
            large_every=args.large_every,
            large_bytes=args.large_bytes,
            restart=not args.no_restart,
            overall_timeout_seconds=args.overall_timeout,
        )
        report["command"] = list(argv) if argv is not None else sys.argv[1:]
        files = write_artifacts(args.output, report)
    except (
        ValueError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        multiprocessing.TimeoutError,
    ) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "artifacts": [str(file) for file in files],
                "run_completed": report["run_completed"],
                "failure": report["failure"],
                "status_before_flush": report["status_before_flush"],
                "attempted_events": report["attempted_events"],
                "accepted_volatile_events": report["accepted_volatile_events"],
                "persisted_events_after_flush": report["persisted_events_after_flush"],
                "all_accepted_durable_after_flush": report["all_accepted_durable_after_flush"],
                "timeout_attempts_persisted": report["timeout_attempts_persisted"],
                "latency_groups": report["latency_groups"],
                "shutdown": report["shutdown"],
                "restart_byte_exact_and_count_equal": report["restart_byte_exact_and_count_equal"],
            },
            allow_nan=False,
        )
    )
    successful = bool(
        report["run_completed"]
        and report["all_accepted_durable_after_flush"]
        and not report["unexpected_persisted_ids"]
        and cast(dict[str, object], report["shutdown"])["graceful"]
        and cast(dict[str, object], report["shutdown"])["protocol_stdout_empty"]
        and report["restart_byte_exact_and_count_equal"] is not False
        and (
            report["restart_shutdown"] is None
            or cast(dict[str, object], report["restart_shutdown"])["graceful"]
        )
    )
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
