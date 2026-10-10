from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fractalmem.core.node import HMRNode
from fractalmem.storage.db import Database, Event, EventConflictError, WriterAlreadyRunningError
from fractalmem.storage.fts import extract_symbols


def event(
    identity: str = "one", payload: str = "hello", project: str = "p", language: str | None = None
) -> Event:
    return Event(project, "session", identity, "user", payload, language)


def test_real_disk_wal_read_only_and_single_writer(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with Database(path) as writer:
        writer.append_event(event())
        assert writer.status()["journal_mode"] == "wal"
        assert (path.stat().st_mode & 0o777) == 0o600
        with pytest.raises(WriterAlreadyRunningError):
            Database(path)
        with Database(path, read_only=True) as reader:
            assert reader.get_message("p", 0) is not None
            with pytest.raises(PermissionError):
                reader.append_event(event("two"))
            with pytest.raises(sqlite3.OperationalError):
                reader.connection.execute("DELETE FROM projects")
    with Database(path) as restart:
        assert restart.load_view("p").render_buffer() == "0+1|user: hello\n"
        assert restart.append_event(event("two")).message.seq == 1


def test_read_only_missing_database_never_creates_files(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        Database(path, read_only=True)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_append_only_triggers_and_atomic_duplicate_semantics(tmp_path: Path) -> None:
    with Database(tmp_path / "test.db") as db:
        original = db.append_event(event())
        repeat = db.append_event(event())
        assert repeat.duplicate and repeat.message.seq == original.message.seq == 0
        for operation in ("UPDATE messages SET payload='changed'", "DELETE FROM messages"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.connection.execute(operation)
        with pytest.raises(EventConflictError):
            db.append_events([event("two"), event(payload="changed")])
        assert db.get_event("p", "two") is None
        assert db.status()["messages"] == 1
        assert db.load_view("p").total_messages == 1


def test_scoped_sequences_search_and_symbols(tmp_path: Path) -> None:
    with Database(tmp_path / "test.db") as db:
        db.append_event(
            event(
                payload="def camelCase():\n    snake_case = 1\n    return snake_case",
                language="python",
            )
        )
        db.append_event(
            event(project="other", payload="def camelCase():\n    return 99", language="python")
        )
        assert db.get_message("p", 0).payload != db.get_message("other", 0).payload  # type: ignore[union-attr]
        hit = db.search("p", "camelCase")[0]
        assert hit.project_id == "p" and hit.symbol == "camelCase"
        assert "99" not in hit.snippet
        assert hit.parent_handle == "0+1"
        assert db.search("p", "snake case")
        assert db.search("p", "camel case")
        assert not db.search("missing", "camelCase")


@pytest.mark.parametrize(
    "language,source,expected",
    [
        ("python", "class Engine:\n    def build(self):\n        pass\n", {"Engine", "build"}),
        (
            "javascript",
            "function camelCase() {}\nconst snake_case = 3;",
            {"camelCase", "snake_case"},
        ),
        (
            "typescript",
            "interface Config {}\nfunction parseValue(x: Config): void {}",
            {"Config", "parseValue"},
        ),
        ("tsx", "function View() { return <div/>; }", {"View"}),
    ],
)
def test_real_tree_sitter_grammars(language: str, source: str, expected: set[str]) -> None:
    symbols = extract_symbols(source, language)
    assert expected <= {s.name for s in symbols}
    raw = source.encode("utf-8")
    for symbol in symbols:
        assert raw[symbol.start_byte : symbol.end_byte].decode("utf-8") == symbol.name


def test_fenced_code_unicode_offsets_and_fts_punctuation(tmp_path: Path) -> None:
    payload = "José wrote:\n```python\ndef parseValue():\n    pass\n```\npath src/core/tree.py error E_001"
    symbol = extract_symbols(payload)[0]
    assert payload.encode()[symbol.start_byte : symbol.end_byte].decode() == "parseValue"
    with Database(tmp_path / "test.db") as db:
        db.append_event(event(payload=payload))
        for query in (
            "parseValue",
            "parse value",
            "src/core/tree.py",
            "E_001",
            '" OR parseValue --',
        ):
            assert db.search("p", query)
        with pytest.raises(ValueError):
            db.search("p", " ")
        with pytest.raises(ValueError):
            db.search("p", "a", limit=101)


def test_compaction_job_recovery_immutable_nodes_and_view_prefix(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    with Database(path) as db:
        db.append_event(event(payload="long message " * 100))
        before = db.load_view("p").render_buffer()
        jobs = db.claim_jobs()
        assert len(jobs) == 1
    with Database(path) as db:
        assert db.recover_jobs() == 1
        assert db.claim_jobs()[0].attempts == 2
        db.complete_node(HMRNode.create_leaf(0, "summary", project_id="p"))
        assert db.load_view("p").render_buffer() == before + "0+1|summary\n"
        assert db.get_node("p", jobs[0].node_id).summary_text == "summary"  # type: ignore[union-attr]
        with pytest.raises(EventConflictError):
            db.complete_node(HMRNode.create_leaf(0, "different", project_id="p"))
        db.append_event(event("two", "other"))
        assert db.load_view("p").render_buffer().startswith(before)
        assert len(db.claim_jobs()) == 1  # Both ready children schedule exactly one parent.


def test_redaction_before_all_disk_content_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    secret = "ghp_" + "x" * 36
    with Database(path) as db:
        result = db.append_event(event(payload=f"password=lowvalue\n{secret}"))
        assert secret not in result.message.payload and "lowvalue" not in result.message.payload
        assert not db.search("p", secret)
        for artifact in tmp_path.iterdir():
            assert secret.encode() not in artifact.read_bytes()
            assert b"lowvalue" not in artifact.read_bytes()
        with pytest.raises(ValueError):
            Event(secret, "s", "id", "user", "hello")


def test_schema_version_and_nested_transaction_rollback(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    with Database(path) as db:
        with db.transaction():
            db.append_event(event())
            with pytest.raises(EventConflictError):
                db.append_event(event(payload="different"))
            db.append_event(event("two"))
        assert db.status()["messages"] == 2
        db.connection.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="schema"):
        Database(path)
    with pytest.raises(RuntimeError, match="schema"):
        Database(path, read_only=True)


def test_tail_text_needle_snippet(tmp_path: Path) -> None:
    with Database(tmp_path / "memory.db") as db:
        db.append_event(
            Event("project", "session", "tail", "tool", "ordinary prose " * 300 + "rareTailNeedle")
        )
        hit = db.search("project", "rareTailNeedle")[0]
        assert "rareTailNeedle" in hit.snippet
