from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.uds_benchmark import main, run_uds


def test_physical_multiprocess_benchmark_compacts_flushes_and_restarts() -> None:
    result = run_uds(producers=2, events_per_producer=12, large_every=4, large_bytes=65_536)
    assert result["attempted_events"] == 24
    assert result["accepted_volatile_events"] > 0
    assert result["all_accepted_durable_after_flush"] is True
    assert result["accepted_missing_from_disk"] == []
    assert result["unexpected_persisted_ids"] == []
    assert result["socket_permissions"] == "0o600"
    assert result["temporary_directory_permissions"] == "0o700"
    assert result["db_bytes_after_shutdown"] > 0
    assert result["restart_byte_exact_and_count_equal"] is True
    assert result["shutdown"]["graceful"] is True
    assert result["shutdown"]["socket_removed"] is True
    assert result["shutdown"]["protocol_stdout_empty"] is True
    assert result["flush"]["durable"] is True
    assert result["flush"]["compacted"] is True
    assert result["status_after_flush"]["pending_jobs"] == 0
    assert result["status_after_flush"]["failed_jobs"] == 0
    assert result["read_only_connection_pragmas"]["query_only"] == 1
    assert result["latency_groups"]["large_payload"]["attempts"] == 6
    assert result["provider_cache_read_rate"] is None
    for row in result["rows"]:
        assert row["receipt_durable"] is False
        assert row["ingest_event_ms"] >= 0
        assert row["persisted_after_flush"] or not row["accepted_volatile"]
    # No timing percentile is asserted as an OS-independent guarantee.


def test_physical_benchmark_cli_generates_json_and_csv(tmp_path: Path) -> None:
    path = tmp_path / "uds.json"
    assert (
        main(
            [
                "--producers",
                "1",
                "--events",
                "3",
                "--large-every",
                "0",
                "--no-restart",
                "--output",
                str(path),
            ]
        )
        == 0
    )
    result = json.loads(path.read_text())
    assert result["attempted_events"] == 3
    assert result["restart_byte_exact_and_count_equal"] is None
    assert result["configuration"]["configured_client_deadline_ms"] == 15.0
    assert (
        result["latency_groups"]["timed_out"]["latency_ms"]["p99"] is None
        or result["timeout_ambiguous_attempts"] > 0
    )
    assert path.with_suffix(".csv").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"producers": 0},
        {"events_per_producer": 0},
        {"capacity": 0},
        {"large_every": -1},
        {"large_bytes": 512},
        {"producers": 32, "events_per_producer": 100_000},
    ],
)
def test_invalid_benchmark_bounds(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        run_uds(**kwargs)


def test_failed_administrative_transport_retains_physical_failure_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fractalmem.client import FractalClient

    actual_flush = FractalClient.flush

    def expired_flush(
        self: FractalClient, *, compact: bool = False, timeout_seconds: float = 30.0
    ) -> dict[str, object]:
        # Physical deadline fault injection: actual socket client and daemon,
        # with an expired administrative deadline, not a fabricated reply.
        return actual_flush(self, compact=compact, timeout_seconds=0.000000001)

    monkeypatch.setattr(FractalClient, "flush", expired_flush)
    output = tmp_path / "failed.json"
    assert (
        main(["--producers", "1", "--events", "4", "--large-every", "0", "--output", str(output)])
        == 1
    )
    report = json.loads(output.read_text())
    assert report["run_completed"] is False
    assert report["failure"]["stage"] == "flush_compact"
    assert report["failure"]["error_type"] == "TimeoutError"
    assert report["failure"]["disk_status_at_failure"]["journal_mode"] == "wal"
    assert report["status_before_flush"]["queue_size"] >= 0
    assert report["all_accepted_durable_after_flush"] is None
    assert report["persisted_events_after_flush"] is None
    assert report["persistence_observation"] == "after_failure_shutdown"
    assert report["recorded_attempts"] == 4
    assert report["shutdown"]["graceful"] is True
    assert output.with_suffix(".csv").exists()
