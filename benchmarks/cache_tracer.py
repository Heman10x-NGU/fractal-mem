"""Aggregate recorded provider usage without inventing absent observations.

Input JSONL: {"provider":"openai"|"anthropic", "usage":{...},
"response_latency_ms": optional measured number, "ttft_ms": optional measured number}.
The caller is responsible for recording actual responses and observation provenance.
"""

from __future__ import annotations

import math
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

from fractalmem.compaction import Usage, parse_usage


def measured_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("measured timing must be a finite nonnegative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("measured timing must be a finite nonnegative number")
    return number


def usage_record(value: object) -> Usage:
    if not isinstance(value, dict):
        raise ValueError("usage record must be an object")
    record = cast(dict[str, object], value)
    provider = record.get("provider")
    if provider not in ("openai", "anthropic"):
        raise ValueError("provider must be openai or anthropic")
    usage = parse_usage(
        str(provider), record.get("usage"), measured_number(record.get("response_latency_ms"))
    )
    if (
        usage.input_tokens is not None
        and usage.cache_read_tokens is not None
        and usage.cache_read_tokens > usage.input_tokens
    ):
        raise ValueError("cache reads cannot exceed total input tokens")
    return replace(usage, ttft_ms=measured_number(record.get("ttft_ms")))


def aggregate_usage(records: list[Usage]) -> dict[str, object]:
    observed = [
        r for r in records if r.input_tokens is not None and r.cache_read_tokens is not None
    ]
    total = sum(r.input_tokens or 0 for r in observed)
    reads = sum(r.cache_read_tokens or 0 for r in observed)
    latencies = [r.response_latency_ms for r in records if r.response_latency_ms is not None]
    ttfts = [r.ttft_ms for r in records if r.ttft_ms is not None]
    return {
        "records": len(records),
        "cache_observed_records": len(observed),
        "cache_unmeasured_records": len(records) - len(observed),
        "observed_input_tokens": total if observed else None,
        "observed_cache_read_tokens": reads if observed else None,
        "weighted_cache_read_rate": reads / total if total else None,
        "observed_request_hit_rate": sum((r.cache_read_tokens or 0) > 0 for r in observed)
        / len(observed)
        if observed
        else None,
        "mean_response_latency_ms": sum(latencies) / len(latencies) if latencies else None,
        "mean_ttft_ms": sum(ttfts) / len(ttfts) if ttfts else None,
        "ttft_observed_records": len(ttfts),
        "providers": sorted({r.provider for r in records}),
        "measurement": "supplied_usage_records; provenance must be supplied by recorder",
        "rows": [asdict(r) for r in records],
    }


def load_usage(path: Path, *, max_records: int = 1_000_000) -> list[Usage]:
    import json

    records: list[Usage] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if len(line.encode()) > 65_536:
                raise ValueError("usage record exceeds 64 KiB")
            if not line.strip():
                continue
            if len(records) >= max_records:
                raise ValueError("usage record count exceeds limit")
            records.append(usage_record(json.loads(line)))
    return records


def load_database_usage(path: Path, project: str | None = None) -> list[Usage]:
    """Read measured normalized compactor usage without reinterpreting its totals."""
    import json

    from fractalmem.protocol import validate_identifier
    from fractalmem.storage.db import Database

    if project is not None:
        validate_identifier(project, "project_id")
    records: list[Usage] = []
    with Database(path, read_only=True) as db, db.read_snapshot():
        rows = db.connection.execute(
            "SELECT usage_json FROM compaction_runs"
            + (" WHERE project_id=?" if project else "")
            + " ORDER BY id",
            (project,) if project else (),
        )
        for row in rows:
            payload: object = json.loads(str(row[0]))
            if not isinstance(payload, list):
                raise ValueError("stored usage must be an array")
            for item in payload:
                if not isinstance(item, dict) or item.get("provider") not in (
                    "openai",
                    "anthropic",
                ):
                    raise ValueError("stored provider usage is invalid")

                usage = Usage(
                    str(item["provider"]),
                    _stored_count(item, "input_tokens"),
                    _stored_count(item, "output_tokens"),
                    _stored_count(item, "cache_read_tokens"),
                    _stored_count(item, "cache_creation_tokens"),
                    measured_number(item.get("response_latency_ms")),
                    measured_number(item.get("ttft_ms")),
                )
                if (
                    usage.input_tokens is not None
                    and usage.cache_read_tokens is not None
                    and usage.cache_read_tokens > usage.input_tokens
                ):
                    raise ValueError("cache reads exceed recorded inputs")
                records.append(usage)
                if len(records) > 1_000_000:
                    raise ValueError("stored usage exceeds record limit")
    return records


def _stored_count(record: dict[str, object], name: str) -> int | None:
    value = record.get(name)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("stored usage count is invalid")
    return value
