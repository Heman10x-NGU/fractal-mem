"""Physical disk, prefix stability and evidence retrieval evaluations.

No generated answer accuracy, paid model dispatch, provider cache hits or TTFT
are inferred from local measurements. Run ``python -m benchmarks.eval_harness
--help``. JSON and CSV artifacts retain per-turn/per-question measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from benchmarks.cache_tracer import aggregate_usage, load_database_usage, load_usage
from fractalmem.compaction import Compactor, extractive_summary
from fractalmem.core.node import HMRNode
from fractalmem.core.tree import HMRTreeEngine, InMemoryNodeStore
from fractalmem.protocol import Event
from fractalmem.storage.db import Database

MAX_DATASET_BYTES = 268_435_456


@dataclass(frozen=True, slots=True)
class Dialogue:
    identifier: str
    session_id: str
    date: str
    role: str
    text: str

    @property
    def payload(self) -> str:
        return f"Date: {self.date}\nSpeaker: {self.role}\n{self.text}"


@dataclass(frozen=True, slots=True)
class Question:
    identifier: str
    text: str
    category: str
    evidence_ids: tuple[str, ...]
    answer: object
    date: str = ""
    abstention: bool = False


@dataclass(frozen=True, slots=True)
class Case:
    identifier: str
    dialogues: tuple[Dialogue, ...]
    questions: tuple[Question, ...]
    evidence_unit: str


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("dataset object expected")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("dataset array expected")
    return cast(list[object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("dataset string expected")
    return value


def _strings(value: object) -> tuple[str, ...]:
    return tuple(_string(item) for item in _list(value))


def parse_locomo(value: object) -> tuple[Case, ...]:
    cases: list[Case] = []
    for entry in _list(value):
        root = _object(entry)
        identifier = _string(root.get("sample_id"))
        conversation = _object(root.get("conversation"))
        session_keys = sorted(
            (key for key in conversation if re.fullmatch(r"session_\d+", key)),
            key=lambda key: int(key.split("_")[1]),
        )
        if not session_keys:
            raise ValueError("LoCoMo conversation has no sessions")
        dialogues: list[Dialogue] = []
        seen: set[str] = set()
        for key in session_keys:
            date = _string(conversation.get(key + "_date_time"))
            for raw in _list(conversation[key]):
                turn = _object(raw)
                dia_id = _string(turn.get("dia_id"))
                if dia_id in seen:
                    raise ValueError("duplicate LoCoMo dialogue ID")
                seen.add(dia_id)
                # Text is the official input; summaries/observations/QA are excluded.
                dialogues.append(
                    Dialogue(
                        dia_id, key, date, _string(turn.get("speaker")), _string(turn.get("text"))
                    )
                )
        questions: list[Question] = []
        for number, raw in enumerate(_list(root.get("qa"))):
            qa = _object(raw)
            category = qa.get("category")
            if type(category) is not int:
                raise ValueError("LoCoMo category must be an integer")
            evidence = _strings(qa.get("evidence", []))
            # Upstream can include unresolvable evidence. Preserve it in the
            # denominator rather than silently removing difficult annotations.
            questions.append(
                Question(
                    f"{identifier}:q{number}",
                    _string(qa.get("question")),
                    str(category),
                    evidence,
                    qa.get("answer", qa.get("adversarial_answer")),
                    abstention=category == 5,
                )
            )
        cases.append(Case(identifier, tuple(dialogues), tuple(questions), "dialogue_id"))
    _unique_cases(cases)
    return tuple(cases)


def parse_longmemeval(value: object) -> tuple[Case, ...]:
    cases: list[Case] = []
    for raw in _list(value):
        root = _object(raw)
        identifier = _string(root.get("question_id"))
        ids = _strings(root.get("haystack_session_ids"))
        dates = _strings(root.get("haystack_dates"))
        sessions = _list(root.get("haystack_sessions"))
        if len(ids) != len(dates) or len(ids) != len(sessions) or len(set(ids)) != len(ids):
            raise ValueError("LongMemEval session IDs/dates/sessions must align uniquely")
        dialogues: list[Dialogue] = []
        for session_id, date, session in zip(ids, dates, sessions, strict=True):
            for turn_index, raw_turn in enumerate(_list(session)):
                turn = _object(raw_turn)
                role = _string(turn.get("role"))
                if role not in ("user", "assistant", "system"):
                    raise ValueError("unsupported LongMemEval speaker role")
                # has_answer is a label: it is never placed in the payload.
                dialogues.append(
                    Dialogue(
                        f"{session_id}:t{turn_index}",
                        session_id,
                        date,
                        role,
                        _string(turn.get("content")),
                    )
                )
        evidence = _strings(root.get("answer_session_ids"))
        if not set(evidence).issubset(ids):
            raise ValueError("answer session ID absent from haystack")
        question = Question(
            identifier,
            _string(root.get("question")),
            _string(root.get("question_type")),
            evidence,
            root.get("answer"),
            _string(root.get("question_date")),
            identifier.endswith("_abs"),
        )
        cases.append(Case(identifier, tuple(dialogues), (question,), "session_id"))
    _unique_cases(cases)
    return tuple(cases)


def _unique_cases(cases: Sequence[Case]) -> None:
    if len({case.identifier for case in cases}) != len(cases):
        raise ValueError("duplicate dataset case ID")


def load_dataset(path: Path, dataset: str, max_items: int | None = None) -> tuple[Case, ...]:
    if path.stat().st_size > MAX_DATASET_BYTES:
        raise ValueError("dataset exceeds 256 MiB bounded loader; shard the input")
    if max_items is not None and max_items <= 0:
        raise ValueError("max_items must be positive")
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if max_items is not None:
        value = _list(value)[:max_items]
    if dataset == "locomo":
        return parse_locomo(value)
    if dataset == "longmemeval":
        return parse_longmemeval(value)
    raise ValueError("unsupported dataset")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment() -> dict[str, object]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    versions: dict[str, str | None] = {}
    for name in ("tiktoken", "tree-sitter", "mcp", "httpx"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "utc": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": __import__("sqlite3").sqlite_version,
        "git_revision": revision,
        "git_dirty": dirty,
        "dependencies": versions,
    }


def percentiles(values: Sequence[float]) -> dict[str, float | None]:
    ordered = sorted(values)

    def percentile(fraction: float) -> float | None:
        if not ordered:
            return None
        position = (len(ordered) - 1) * fraction
        low, high = math.floor(position), math.ceil(position)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": max(ordered) if ordered else None,
    }


def _tokenizer(name: str | None) -> tuple[Callable[[str], int] | None, str | None]:
    if name is None:
        return None, "unconfigured; per-turn exact tokens are unmeasured"
    try:
        import tiktoken

        encoding = tiktoken.get_encoding(name)
    except (ImportError, ValueError, OSError) as error:
        return None, f"tokenizer unavailable: {type(error).__name__}"
    return lambda text: len(encoding.encode(text, disallowed_special=())), None


def run_sawtooth(
    turns: int = 30_000, *, seed: int = 42, tokenizer: str | None = None
) -> dict[str, object]:
    if not 1 <= turns <= 1_000_000:
        raise ValueError("turns must be 1..1000000")
    rng = random.Random(seed)
    store = InMemoryNodeStore()

    def resolve(level: int, index: int, project: str) -> HMRNode | None:
        existing = store.get(level, index, project)
        if existing is not None:
            return existing
        left, right = (
            store.get(level - 1, index * 2, project),
            store.get(level - 1, index * 2 + 1, project),
        )
        if left is None or right is None:
            return None
        parent = HMRNode.create(
            level,
            index,
            extractive_summary(left.summary_text + "\n" + right.summary_text),
            project_id=project,
        )
        store.put(parent)
        return parent

    engine = HMRTreeEngine(project_id="sawtooth", parent_resolver=resolve)
    count_tokens, tokenizer_error = _tokenizer(tokenizer)
    rows: list[dict[str, object]] = []
    previous = b""
    batch_count = 0
    violations = 0
    corpus_digest = hashlib.sha256()
    started = time.perf_counter()
    for turn in range(turns):
        text = (
            f"Turn {turn}: function computeMemory{turn} changed src/module_{turn % 127}.py; "
            + "preserve decision and terminal error context " * rng.randint(4, 8)
        )
        corpus_digest.update(text.encode())
        corpus_digest.update(b"\0")
        leaf = HMRNode.create_leaf(turn, text, project_id="sawtooth")
        store.put(leaf)
        tick = time.perf_counter()
        engine.append_leaf_node(leaf)
        buffer = engine.view.render_buffer()
        current = buffer.encode()
        appended = current.startswith(previous)
        common = (
            len(previous)
            if appended
            else next(
                (
                    index
                    for index, pair in enumerate(zip(previous, current, strict=False))
                    if pair[0] != pair[1]
                ),
                min(len(previous), len(current)),
            )
        )
        if not appended:
            batch_count += 1
            if len(previous) + leaf.line_byte_size <= 128_000 or len(current) > 64_000:
                violations += 1
        append_ms = (time.perf_counter() - tick) * 1000
        token_started = time.perf_counter()
        exact_tokens = count_tokens(buffer) if count_tokens else None
        token_ms = (time.perf_counter() - token_started) * 1000 if count_tokens else None
        rows.append(
            {
                "turn": turn + 1,
                "view_bytes": len(current),
                "exact_tokens": exact_tokens,
                "token_counting_ms": token_ms,
                "append_prefix": appended,
                "common_prefix_bytes": common,
                "batch_count": batch_count,
                "append_and_render_ms": append_ms,
            }
        )
        previous = current
    sizes = [cast(int, row["view_bytes"]) for row in rows]
    return {
        "measurement": "synthetic_seeded_core_simulation_with_actual_extractive_parents",
        "corpus_sha256": corpus_digest.hexdigest(),
        "turns": turns,
        "seed": seed,
        "max_bytes": 128_000,
        "target_bytes": 64_000,
        "batches": batch_count,
        "prefix_preserving_turn_rate": (turns - batch_count) / turns,
        "prefix_violations": violations,
        "mean_view_bytes": sum(sizes) / len(sizes),
        "max_view_bytes": max(sizes),
        "elapsed_seconds": time.perf_counter() - started,
        "latency_ms": percentiles([cast(float, row["append_and_render_ms"]) for row in rows]),
        "tokenizer": tokenizer,
        "total_exact_view_tokens": sum(cast(int, row["exact_tokens"]) for row in rows)
        if count_tokens
        else None,
        "provider_input_tokens": None,
        "token_count_scope": "exact rendered view buffer; excludes tools, system, task and provider framing",
        "tokenizer_unmeasured_reason": tokenizer_error,
        "provider_cache_read_rate": None,
        "provider_ttft_ms": None,
        "rows": rows,
    }


async def drain_compaction(db: Database) -> int:
    compactor = Compactor()
    completed = 0
    while jobs := db.claim_jobs():
        for job in jobs:
            node = db.get_node(job.project_id, job.node_id)
            if node is None:
                raise RuntimeError("claimed compaction node missing")
            if node.level == 0:
                message = db.get_message(job.project_id, node.start_seq)
                if message is None:
                    raise RuntimeError("compaction source missing")
                source = message.kind + ": " + message.payload
            else:
                left = db.resolve_parent(node.level - 1, node.index_in_level * 2, node.project_id)
                right = db.resolve_parent(
                    node.level - 1, node.index_in_level * 2 + 1, node.project_id
                )
                if (
                    left is None
                    or right is None
                    or left.status != "ready"
                    or right.status != "ready"
                ):
                    raise RuntimeError("compaction children not ready")
                source = left.summary_text + "\n" + right.summary_text
            ready, _ = await compactor.build_node(
                node, source, db.load_view(job.project_id).render_buffer()
            )
            db.complete_node(ready)
            completed += 1
    return completed


def run_disk(
    path: Path, *, messages: int = 10_000, queries: int = 1_000, seed: int = 42
) -> dict[str, object]:
    if not 1 <= messages <= 1_000_000 or not 1 <= queries <= 100_000:
        raise ValueError("invalid disk corpus or query count")
    if path.exists():
        raise ValueError("benchmark database path already exists")
    rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    started = time.perf_counter()
    completed = 0
    corpus_digest = hashlib.sha256()
    with Database(path) as db:
        for start in range(0, messages, 128):
            events = [
                Event(
                    "diskbench",
                    "corpus",
                    f"event{i}",
                    "tool",
                    f"def memoryNeedle{i}(value):\n    return value + {i}\n# src/module_{i % 127}.py error E_{i}\n",
                    "python",
                )
                for i in range(start, min(start + 128, messages))
            ]
            for event in events:
                corpus_digest.update(event.payload.encode())
                corpus_digest.update(b"\0")
            db.append_events(events)
            completed += asyncio.run(drain_compaction(db))
        insert_seconds = time.perf_counter() - started
        for number in range(queries):
            target = rng.randrange(messages)
            query = f"memoryNeedle{target}"
            tick = time.perf_counter()
            hits = db.search("diskbench", query, 1)
            latency = (time.perf_counter() - tick) * 1000
            correct = bool(
                hits and hits[0].seq == target and hits[0].symbol == query and hits[0].parent_handle
            )
            rows.append(
                {
                    "query_number": number,
                    "query": query,
                    "expected_seq": target,
                    "returned_seq": hits[0].seq if hits else None,
                    "parent_handle": hits[0].parent_handle if hits else None,
                    "correct": correct,
                    "latency_ms": latency,
                }
            )
        view = db.load_view("diskbench")
        before = view.render_buffer().encode()
        status: dict[str, object] = dict(db.status())
        pragmas = {
            name: db.connection.execute("PRAGMA " + name).fetchone()[0]
            for name in ("journal_mode", "synchronous", "foreign_keys")
        }
        wal_bytes = (
            path.with_name(path.name + "-wal").stat().st_size
            if path.with_name(path.name + "-wal").exists()
            else 0
        )
        files_bytes = sum(
            file.stat().st_size for file in path.parent.glob(path.name + "*") if file.is_file()
        )
    with Database(path, read_only=True) as reader:
        restored = reader.load_view("diskbench").render_buffer().encode()
    return {
        "measurement": "actual_disk_sqlite_wal_tree_sitter_and_fts",
        "corpus_sha256": corpus_digest.hexdigest(),
        "pragmas": pragmas,
        "messages": messages,
        "queries": queries,
        "seed": seed,
        "insert_and_compaction_seconds": insert_seconds,
        "compaction_nodes_completed": completed,
        "exact_symbol_precision_at_1": sum(bool(row["correct"]) for row in rows) / queries,
        "query_latency_ms": percentiles([cast(float, row["latency_ms"]) for row in rows]),
        "disk_bytes_before_close": files_bytes,
        "wal_bytes_before_close": wal_bytes,
        "db_bytes_after_close": path.stat().st_size,
        "view_bytes": len(before),
        "restart_byte_exact": restored == before,
        "status": status,
        "provider_cache_read_rate": None,
        "rows": rows,
    }


def run_dataset(
    path: Path, dataset: str, database_path: Path, *, k: int = 10, max_items: int | None = None
) -> dict[str, object]:
    if not 1 <= k <= 100:
        raise ValueError("k must be 1..100")
    cases = load_dataset(path, dataset, max_items)
    if database_path.exists():
        raise ValueError("benchmark database path already exists")
    rows: list[dict[str, object]] = []
    context_digest = hashlib.sha256()
    started = time.perf_counter()
    inserted = 0
    with Database(database_path) as db:
        for case in cases:
            project = "case_" + hashlib.sha256(case.identifier.encode()).hexdigest()[:20]
            events: list[Event] = []
            for seq, dialogue in enumerate(case.dialogues):
                session = "session_" + hashlib.sha256(dialogue.session_id.encode()).hexdigest()[:20]
                kind = (
                    "unii"
                    if dialogue.role == "assistant"
                    else "note"
                    if dialogue.role == "system"
                    else "user"
                )
                events.append(Event(project, session, f"turn{seq}", kind, dialogue.payload))
                context_digest.update(dialogue.payload.encode())
                context_digest.update(b"\0")
            for start in range(0, len(events), 128):
                db.append_events(events[start : start + 128])
                asyncio.run(drain_compaction(db))
            inserted += len(events)
            for question in case.questions:
                tick = time.perf_counter()
                hits = db.search(project, question.text, k)
                latency = (time.perf_counter() - tick) * 1000
                retrieved_ids = [
                    case.dialogues[hit.seq].identifier
                    if case.evidence_unit == "dialogue_id"
                    else case.dialogues[hit.seq].session_id
                    for hit in hits
                ]
                truth = set(question.evidence_ids)
                matched = truth.intersection(retrieved_ids)
                # Category 5 LoCoMo adversarial questions are excluded from
                # evidence scoring; context retrieval cannot prove abstention.
                scorable = bool(truth) and not question.abstention
                rows.append(
                    {
                        "case_id": case.identifier,
                        "question_id": question.identifier,
                        "category": question.category,
                        "question_date": question.date,
                        "abstention": question.abstention,
                        "evidence_unit": case.evidence_unit,
                        "evidence_ids": list(question.evidence_ids),
                        "retrieved_ids": retrieved_ids,
                        "retrieved_sequences": [hit.seq for hit in hits],
                        "evidence_precision_at_k": sum(item in truth for item in retrieved_ids)
                        / len(retrieved_ids)
                        if retrieved_ids and scorable
                        else (0.0 if scorable else None),
                        "evidence_recall_at_k": len(matched) / len(truth) if scorable else None,
                        "retrieval_latency_ms": latency,
                        "returned_hits": len(hits),
                        "unresolved_evidence_ids": sorted(
                            truth
                            - {
                                d.identifier
                                if case.evidence_unit == "dialogue_id"
                                else d.session_id
                                for d in case.dialogues
                            }
                        ),
                        "generated_answer_accuracy": None,
                    }
                )
        status: dict[str, object] = dict(db.status())
    scorable_rows = [row for row in rows if row["evidence_recall_at_k"] is not None]
    categories: dict[str, object] = {}
    for category in sorted({str(row["category"]) for row in rows}):
        subset = [row for row in scorable_rows if row["category"] == category]
        categories[category] = {
            "scorable_questions": len(subset),
            "mean_evidence_recall": sum(cast(float, row["evidence_recall_at_k"]) for row in subset)
            / len(subset)
            if subset
            else None,
        }
    return {
        "measurement": "retrieval_only_no_answer_generation_or_official_judge",
        "dataset": dataset,
        "input_order": "official supplied session/turn order; LoCoMo sessions sorted by numeric session ID",
        "dataset_path": str(path),
        "dataset_sha256": sha256_file(path),
        "dataset_bytes": path.stat().st_size,
        "context_sha256": context_digest.hexdigest(),
        "max_items": max_items,
        "cases": len(cases),
        "dialogue_turns": inserted,
        "questions": len(rows),
        "scorable_questions": len(scorable_rows),
        "abstention_questions": sum(bool(row["abstention"]) for row in rows),
        "k": k,
        "precision_denominator": "returned messages; session IDs may repeat for LongMemEval",
        "recall_denominator": "unique ground-truth evidence units; unresolvable units retained",
        "mean_evidence_precision_at_k": sum(
            cast(float, row["evidence_precision_at_k"]) for row in scorable_rows
        )
        / len(scorable_rows)
        if scorable_rows
        else None,
        "mean_evidence_recall_at_k": sum(
            cast(float, row["evidence_recall_at_k"]) for row in scorable_rows
        )
        / len(scorable_rows)
        if scorable_rows
        else None,
        "query_latency_ms": percentiles([cast(float, row["retrieval_latency_ms"]) for row in rows]),
        "elapsed_seconds": time.perf_counter() - started,
        "categories": categories,
        "status": status,
        "generated_answer_accuracy": None,
        "provider_cache_read_rate": None,
        "provider_ttft_ms": None,
        "rows": rows,
    }


def _atomic(path: Path, write: Callable[[int], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        write(fd)
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_artifacts(output: Path, report: dict[str, object]) -> tuple[Path, Path]:
    if output.suffix != ".json":
        raise ValueError("output must have a .json extension")

    def write_json(fd: int) -> None:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    _atomic(output, write_json)
    csv_path = output.with_suffix(".csv")
    rows = cast(list[dict[str, object]], report.get("rows", []))

    def write_csv(fd: int) -> None:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            fields = list(dict.fromkeys(key for row in rows for key in row))
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: json.dumps(value, ensure_ascii=False)
                        if isinstance(value, (list, dict))
                        else value
                        for key, value in row.items()
                    }
                )
            stream.flush()
            os.fsync(stream.fileno())

    _atomic(csv_path, write_csv)
    return output, csv_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    local = sub.add_parser("local", help="seeded core and actual disk search")
    local.add_argument("--turns", type=int, default=30_000)
    local.add_argument("--messages", type=int, default=10_000)
    local.add_argument("--queries", type=int, default=1_000)
    local.add_argument("--seed", type=int, default=42)
    local.add_argument(
        "--tokenizer", default=None, help="explicit tiktoken encoding; may download its vocabulary"
    )
    data = sub.add_parser("datasets", help="retrieval metrics on original dialogue schemas")
    data.add_argument("--dataset", choices=("locomo", "longmemeval"), required=True)
    data.add_argument("--path", type=Path, required=True)
    data.add_argument("--max-items", type=int)
    data.add_argument("--k", type=int, default=10)
    usage = sub.add_parser("usage", help="aggregate recorded real provider usage JSONL")
    usage.add_argument("--path", type=Path, required=True)
    usage.add_argument(
        "--database", action="store_true", help="Read actual compaction_runs from SQLite"
    )
    usage.add_argument("--project", help="Scope recorded database usage")
    for command in (local, data, usage):
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="fractalmem-eval-") as directory:
            if args.command == "local":
                core = run_sawtooth(args.turns, seed=args.seed, tokenizer=args.tokenizer)
                disk = run_disk(
                    Path(directory) / "memory.sqlite",
                    messages=args.messages,
                    queries=args.queries,
                    seed=args.seed,
                )
                report: dict[str, object] = {
                    "suite": "local",
                    "environment": environment(),
                    "core": {key: value for key, value in core.items() if key != "rows"},
                    "disk": {key: value for key, value in disk.items() if key != "rows"},
                    "rows": [
                        {"component": "core", **row}
                        for row in cast(list[dict[str, object]], core["rows"])
                    ]
                    + [
                        {"component": "disk", **row}
                        for row in cast(list[dict[str, object]], disk["rows"])
                    ],
                }
            elif args.command == "datasets":
                report = run_dataset(
                    args.path,
                    args.dataset,
                    Path(directory) / "memory.sqlite",
                    k=args.k,
                    max_items=args.max_items,
                )
                report["environment"] = environment()
            else:
                report = aggregate_usage(
                    load_database_usage(args.path, args.project)
                    if args.database
                    else load_usage(args.path)
                )
                report["environment"] = environment()
                report["source_sha256"] = sha256_file(args.path)
            report["command"] = list(argv) if argv is not None else __import__("sys").argv[1:]
            files = write_artifacts(args.output, report)
        print(
            json.dumps(
                {
                    "artifacts": [str(file) for file in files],
                    "summary": {key: value for key, value in report.items() if key != "rows"},
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
