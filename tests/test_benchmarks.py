from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from benchmarks.cache_tracer import aggregate_usage, load_usage, usage_record
from benchmarks.eval_harness import (
    load_dataset,
    main,
    parse_locomo,
    parse_longmemeval,
    percentiles,
    run_dataset,
    run_disk,
    run_sawtooth,
    write_artifacts,
)
from fractalmem.storage.db import Database


def locomo_fixture() -> list[dict[str, object]]:
    return [
        {
            "sample_id": "conversation-1",
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_1_date_time": "8 May 2023",
                "session_1": [
                    {
                        "speaker": "Alice",
                        "dia_id": "D1:1",
                        "text": "Alice uses the terminalMemoryNeedle algorithm.",
                    },
                    {"speaker": "Bob", "dia_id": "D1:2", "text": "Bob grows tomatoes."},
                ],
            },
            "qa": [
                {
                    "question": "What terminalMemoryNeedle algorithm does Alice use?",
                    "answer": "GOLD_LABEL_SHOULD_NOT_ENTER_CONTEXT",
                    "evidence": ["D1:1"],
                    "category": 1,
                },
                {
                    "question": "Did Alice own a rocket?",
                    "evidence": ["D1:1"],
                    "adversarial_answer": "BAD_LABEL_SHOULD_NOT_ENTER_CONTEXT",
                    "category": 5,
                },
            ],
            "observation": {"forbidden": "DERIVED_LABEL_SHOULD_NOT_ENTER_CONTEXT"},
        }
    ]


def long_fixture() -> list[dict[str, object]]:
    return [
        {
            "question_id": "question-1",
            "question_type": "single-session-user",
            "question": "What terminalMemoryNeedle algorithm does Alice use?",
            "question_date": "2023/05/09",
            "answer": "GOLD_LABEL_SHOULD_NOT_ENTER_CONTEXT",
            "haystack_session_ids": ["sess-original"],
            "haystack_dates": ["2023/05/08"],
            "haystack_sessions": [
                [
                    {
                        "role": "user",
                        "content": "Alice uses terminalMemoryNeedle.",
                        "has_answer": True,
                    },
                    {"role": "assistant", "content": "Understood.", "has_answer": False},
                ]
            ],
            "answer_session_ids": ["sess-original"],
        }
    ]


def test_adapters_preserve_identity_dates_evidence_and_exclude_labels() -> None:
    lo = parse_locomo(locomo_fixture())[0]
    assert lo.identifier == "conversation-1"
    assert lo.dialogues[0].date == "8 May 2023"
    assert lo.dialogues[0].identifier == "D1:1"
    assert lo.questions[0].evidence_ids == ("D1:1",)
    assert lo.questions[1].abstention
    lm = parse_longmemeval(long_fixture())[0]
    assert lm.evidence_unit == "session_id"
    assert lm.dialogues[0].session_id == "sess-original"
    assert lm.questions[0].date == "2023/05/09"
    assert lm.questions[0].answer == "GOLD_LABEL_SHOULD_NOT_ENTER_CONTEXT"
    assert "has_answer" not in lm.dialogues[0].payload
    assert "GOLD_LABEL" not in "".join(d.payload for d in (*lo.dialogues, *lm.dialogues))


@pytest.mark.parametrize("dataset", ["locomo", "longmemeval"])
def test_actual_disk_dataset_retrieval_never_indexes_ground_truth(
    tmp_path: Path, dataset: str
) -> None:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(locomo_fixture() if dataset == "locomo" else long_fixture()))
    database = tmp_path / "memory.sqlite"
    report = run_dataset(path, dataset, database, k=2)
    assert report["mean_evidence_recall_at_k"] == 1.0
    assert report["generated_answer_accuracy"] is None
    assert report["provider_cache_read_rate"] is None
    assert report["dataset_sha256"]
    with Database(database, read_only=True) as db:
        payload = " ".join(
            str(row[0]) for row in db.connection.execute("SELECT payload FROM messages")
        )
        assert "GOLD_LABEL" not in payload
        assert "BAD_LABEL" not in payload
        assert "DERIVED_LABEL" not in payload
        assert "has_answer" not in payload
    if dataset == "locomo":
        assert report["scorable_questions"] == 1
        assert report["abstention_questions"] == 1


def test_invalid_schema_alignment_and_duplicates() -> None:
    data = long_fixture()
    data[0]["haystack_dates"] = []
    with pytest.raises(ValueError, match="align"):
        parse_longmemeval(data)
    with pytest.raises(ValueError, match="duplicate dataset"):
        parse_locomo(locomo_fixture() * 2)
    data2 = locomo_fixture()
    conversation = data2[0]["conversation"]
    assert isinstance(conversation, dict)
    conversation["session_1"] = [{"speaker": "Alice", "dia_id": "same", "text": "A"}] * 2
    with pytest.raises(ValueError, match="duplicate LoCoMo"):
        parse_locomo(data2)


def test_dataset_limits(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps(locomo_fixture()))
    assert len(load_dataset(path, "locomo", 1)) == 1
    with pytest.raises(ValueError, match="positive"):
        load_dataset(path, "locomo", 0)
    with pytest.raises(ValueError, match="unsupported"):
        load_dataset(path, "bogus")


def test_real_sawtooth_reaches_threshold_and_preserves_prefixes() -> None:
    report = run_sawtooth(1_000, seed=42)
    assert report["batches"] >= 1
    assert report["prefix_violations"] == 0
    assert report["max_view_bytes"] <= 128_000
    assert report["provider_cache_read_rate"] is None
    assert report["tokenizer_unmeasured_reason"]
    rows = report["rows"]
    assert isinstance(rows, list)
    assert len(rows) == 1_000
    assert all(row["exact_tokens"] is None for row in rows)


def test_actual_sqlite_disk_search_and_restart(tmp_path: Path) -> None:
    report = run_disk(tmp_path / "memory.sqlite", messages=128, queries=20)
    assert report["exact_symbol_precision_at_1"] == 1.0
    assert report["restart_byte_exact"]
    assert report["compaction_nodes_completed"] > 0
    assert report["db_bytes_after_close"] > 0
    assert report["wal_bytes_before_close"] > 0
    with pytest.raises(ValueError, match="already exists"):
        run_disk(tmp_path / "memory.sqlite", messages=2, queries=1)


def test_recorded_usage_weighted_counts_and_missing_fields(tmp_path: Path) -> None:
    records = [
        usage_record(
            {
                "provider": "anthropic",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 90,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 5,
                },
                "response_latency_ms": 7.5,
            }
        ),
        usage_record(
            {
                "provider": "openai",
                "usage": {"prompt_tokens": 200, "prompt_tokens_details": {"cached_tokens": 20}},
            }
        ),
        usage_record({"provider": "openai", "usage": {}}),
    ]
    result = aggregate_usage(records)
    assert result["observed_input_tokens"] == 300
    assert result["weighted_cache_read_rate"] == pytest.approx(110 / 300)
    assert result["cache_unmeasured_records"] == 1
    assert result["mean_ttft_ms"] is None
    assert result["mean_response_latency_ms"] == 7.5
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps({"provider": "openai", "usage": {}, "ttft_ms": 2.0}) + "\n")
    assert load_usage(path)[0].ttft_ms == 2.0
    with pytest.raises(ValueError, match="count"):
        load_usage(path, max_records=0)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1"])
def test_usage_rejects_unmeasured_timing_values(value: object) -> None:
    with pytest.raises(ValueError):
        usage_record({"provider": "openai", "response_latency_ms": value})


def test_usage_rejects_impossible_cache_rate() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        usage_record(
            {
                "provider": "openai",
                "usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 11}},
            }
        )


def test_atomic_json_csv_and_cli(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    report: dict[str, object] = {"rows": [{"a": "héllo", "values": ["a", "b"], "missing": None}]}
    files = write_artifacts(output, report)
    assert json.loads(files[0].read_text()) == report
    with files[1].open(newline="") as stream:
        assert next(csv.DictReader(stream))["values"] == '["a", "b"]'
    assert not list(tmp_path.glob(".report*"))
    assert (
        main(
            [
                "local",
                "--turns",
                "5",
                "--messages",
                "5",
                "--queries",
                "2",
                "--output",
                str(tmp_path / "cli.json"),
            ]
        )
        == 0
    )
    result = json.loads((tmp_path / "cli.json").read_text())
    assert result["core"]["turns"] == 5
    assert result["disk"]["exact_symbol_precision_at_1"] == 1.0
    assert result["environment"]["sqlite"]


def test_percentiles_and_invalid_bounds() -> None:
    assert percentiles([])["p99"] is None
    assert percentiles([1, 3])["p50"] == 2.0
    with pytest.raises(ValueError):
        run_sawtooth(0)


def test_real_database_usage_trace_preserves_normalized_anthropic_counts(tmp_path: Path) -> None:
    from dataclasses import asdict

    from benchmarks.cache_tracer import load_database_usage
    from fractalmem.compaction import Usage
    from fractalmem.protocol import Event
    path = tmp_path / 'traced.db'
    with Database(path) as db:
        db.append_event(Event('trace', 'session', 'event', 'user', 'text'))
        node = db.load_view('trace').nodes[0]
        observed = Usage('anthropic', 1000, 20, 800, 100, 12.5)
        db.record_compaction('trace', node.node_id, 'dual_llm', json.dumps([asdict(observed)]))
    records = load_database_usage(path, 'trace')
    assert records == [observed]
    assert aggregate_usage(records)['weighted_cache_read_rate'] == 0.8
    assert load_database_usage(path, 'other') == []
