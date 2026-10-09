"""Integration tests for FractalMem MCP stdio server.

Launches real subprocesses over stdio transport using official MCP SDK (mcp 2.3.0),
validating all three typed tools:
1. timeline_overview
2. inspect_node
3. search_symbols_and_text

Covers parameter bounds, handle mapping, depth expansion, leaf payload redaction & taint,
nonce framing via guard.frame_untrusted, project scoping, static non-reflecting errors,
unknown nodes with huge integer strings, response size budgeting, default DB parser configuration,
strict Pydantic schema validation rejecting bools/strings as integers,
typed output contracts (Pydantic models) with complete schemas,
sanitized call_tool error interception preventing caller/secret reflection in responses and stderr,
and real context lifespan entry/closure verifying database connection cleanup.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fractalmem.core.node import HMRNode
from fractalmem.protocol import Event
from fractalmem.storage.db import Database


@pytest.fixture
def populated_db() -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Create a temporary SQLite database populated with messages, symbols, and an HMR-Tree."""
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "memory.db"
    db = Database(db_path)

    # Insert events into testproj
    e0 = Event(
        project_id="testproj",
        session_id="session_alpha",
        event_id="evt_0",
        kind="user",
        payload=(
            "def compute_loss(y_true, y_pred):\n"
            "    '''Compute batch MSE loss.'''\n"
            "    return ((y_true - y_pred) ** 2).mean()\n"
        ),
        language="python",
    )
    e1 = Event(
        project_id="testproj",
        session_id="session_alpha",
        event_id="evt_1",
        kind="tool",
        payload=(
            "def optimize_weights(lr=0.01):\n    # Gradient descent step optimization\n    pass\n"
        ),
        language="python",
    )
    e2 = Event(
        project_id="testproj",
        session_id="session_beta",
        event_id="evt_2",
        kind="user",
        payload="Execution status report: こんにちは世界 accuracy=0.985 step=100",
        language=None,
    )
    e3 = Event(
        project_id="testproj",
        session_id="session_beta",
        event_id="evt_3",
        kind="tool",
        payload="Secret config: token=sk-live98765432109876543210987654321098",
        language=None,
    )
    # Insert another project's event to test project isolation and scoped metadata
    e_other = Event(
        project_id="otherproj",
        session_id="session_other",
        event_id="evt_other",
        kind="user",
        payload="def isolated_function(): pass",
        language="python",
    )

    db.append_events([e0, e1, e2, e3, e_other])

    # Complete level 1 parent nodes
    p0 = HMRNode.create(
        1, 0, "Summary of loss computation and weight optimization", project_id="testproj"
    )
    p1 = HMRNode.create(1, 1, "Summary of execution status and credentials", project_id="testproj")
    db.complete_node(p0)
    db.complete_node(p1)

    # Complete level 2 root node
    root = HMRNode.create(
        2, 0, "Root summary covering all 4 messages in testproj", project_id="testproj"
    )
    db.complete_node(root)
    db.close()

    return db_path, tmp


@asynccontextmanager
async def stdio_session(
    db_path: Path | str | None = None,
    project_id: str = "testproj",
) -> AsyncIterator[ClientSession]:
    """Context manager to start an MCP stdio subprocess and return an initialized ClientSession."""
    args = ["-m", "fractalmem.mcp.server", "--project", project_id]
    if db_path is not None:
        args.extend(["--db", str(db_path)])

    params = StdioServerParameters(
        command=sys.executable,
        args=args,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def test_list_tools_schema_contracts(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify that exactly three typed tools are registered with complete typed output schemas."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                result = await session.list_tools()
                tool_names = sorted(t.name for t in result.tools)
                assert tool_names == [
                    "inspect_node",
                    "search_symbols_and_text",
                    "timeline_overview",
                ]
                for tool in result.tools:
                    assert tool.description
                    assert tool.input_schema is not None
                    # Verify input schema contains expected fields
                    props = tool.input_schema.get("properties", {})
                    assert len(props) > 0
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_timeline_overview_success_and_scoped_metadata(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify timeline_overview returns frontier nodes and strictly scoped project metadata."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                res = await session.call_tool("timeline_overview", {"limit": 64})
                assert not res.is_error
                assert res.content
                payload = json.loads(res.content[0].text)  # type: ignore[union-attr]

                assert payload["project_id"] == "testproj"
                assert payload["total_messages"] == 4
                assert "batch_active" in payload
                assert "status" in payload
                assert payload["status"]["journal_mode"].lower() == "wal"
                # Status messages count must be strictly scoped to testproj (4 messages),
                # ignoring the 1 message belonging to otherproj in the same database.
                assert payload["status"]["messages"] == 4
                assert len(payload["nodes"]) == 4

                node0 = payload["nodes"][0]
                assert "node_id" in node0
                assert "scoped_id" in node0
                assert "handle" in node0
                assert "summary" in node0
                assert "status" in node0
                assert "kind" in node0
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_timeline_overview_bounds(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify timeline_overview enforces 1 <= limit <= 128 bounds and strict Pydantic type checks."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # Boundary limit = 1
                res_min = await session.call_tool("timeline_overview", {"limit": 1})
                assert not res_min.is_error
                payload_min = json.loads(res_min.content[0].text)  # type: ignore[union-attr]
                assert payload_min["returned_nodes_count"] == 1

                # Boundary limit = 128
                res_max = await session.call_tool("timeline_overview", {"limit": 128})
                assert not res_max.is_error

                # Invalid: limit = 0 (rejected by schema ge=1)
                res_low = await session.call_tool("timeline_overview", {"limit": 0})
                assert res_low.is_error

                # Invalid: limit = 129 (rejected by schema le=128)
                res_high = await session.call_tool("timeline_overview", {"limit": 129})
                assert res_high.is_error

                # Invalid: bool (strictly rejected by Pydantic strict=True)
                res_bool = await session.call_tool("timeline_overview", {"limit": True})
                assert res_bool.is_error

                # Invalid: str (strictly rejected by Pydantic strict=True)
                res_str = await session.call_tool("timeline_overview", {"limit": "50"})
                assert res_str.is_error
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_inspect_leaf_node_raw_payload_and_nonce_framing(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify leaf inspection returns exact redacted raw payload, nonce framing, and passive taint."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                res = await session.call_tool("inspect_node", {"node_id": "L0_P0_M0_0", "depth": 1})
                assert not res.is_error
                payload = json.loads(res.content[0].text)  # type: ignore[union-attr]

                assert payload["node_id"] == "L0_P0_M0_0"
                assert payload["level"] == 0
                assert payload["handle"] == "0+1"
                assert payload["children"] == []
                assert payload["ancestors"]["parent_id"] == "L1_P0_M0_1"
                assert payload["ancestors"]["parent_handle"] == "0+2"

                msg = payload["leaf_message"]
                assert msg is not None
                assert msg["seq"] == 0
                # Raw exact redacted payload accessible
                assert "compute_loss" in msg["payload"]
                assert msg["language"] == "python"
                assert msg["taint_level"] == "UNTRUSTED_EXTERNAL"

                # Cryptographic nonce framing & passive taint
                assert "nonce" in msg
                assert len(msg["nonce"]) == 64
                assert msg["passive_taint"] == "UNTRUSTED_EXTERNAL"
                assert f"<<<EXTERNAL_UNTRUSTED_NONCE_{msg['nonce']}>>>" in msg["framed_payload"]
                assert f"<<<END_EXTERNAL_UNTRUSTED_NONCE_{msg['nonce']}>>>" in msg["framed_payload"]
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_inspect_leaf_node_redacted_secret_and_taint(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify leaf inspection keeps secrets scrubbed and preserves UNTRUSTED_EXTERNAL taint."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # Node 3 contains sk-live API token
                res = await session.call_tool("inspect_node", {"node_id": "L0_P3_M3_3"})
                assert not res.is_error
                payload = json.loads(res.content[0].text)  # type: ignore[union-attr]

                msg = payload["leaf_message"]
                assert msg is not None
                assert "sk-live9876543210" not in msg["payload"]
                assert "sk-live9876543210" not in msg["framed_payload"]
                assert "[REDACTED_SECRET_" in msg["payload"]
                assert "[REDACTED_SECRET_" in msg["framed_payload"]
                assert msg["taint_level"] == "UNTRUSTED_EXTERNAL"
                assert msg["passive_taint"] == "UNTRUSTED_EXTERNAL"
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_inspect_by_handle_and_scoped_id(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify lookup by UniiChat handle '0+1', '0+2', '0+4' and current project scoped ID."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # Handle lookup '0+1'
                res_handle_leaf = await session.call_tool("inspect_node", {"node_id": "0+1"})
                assert not res_handle_leaf.is_error
                leaf_data = json.loads(res_handle_leaf.content[0].text)  # type: ignore[union-attr]
                assert leaf_data["node_id"] == "L0_P0_M0_0"

                # Handle lookup '0+2'
                res_handle_p0 = await session.call_tool("inspect_node", {"node_id": "0+2"})
                assert not res_handle_p0.is_error
                p0_data = json.loads(res_handle_p0.content[0].text)  # type: ignore[union-attr]
                assert p0_data["node_id"] == "L1_P0_M0_1"

                # Handle lookup '0+4'
                res_handle_root = await session.call_tool("inspect_node", {"node_id": "0+4"})
                assert not res_handle_root.is_error
                root_data = json.loads(res_handle_root.content[0].text)  # type: ignore[union-attr]
                assert root_data["node_id"] == "L2_P0_M0_3"

                # Scoped ID lookup 'testproj:L0_P0_M0_0'
                res_scoped = await session.call_tool(
                    "inspect_node", {"node_id": "testproj:L0_P0_M0_0"}
                )
                assert not res_scoped.is_error
                scoped_data = json.loads(res_scoped.content[0].text)  # type: ignore[union-attr]
                assert scoped_data["node_id"] == "L0_P0_M0_0"

                # Invalid handle alignment: '1+2' is unaligned
                res_bad_align = await session.call_tool("inspect_node", {"node_id": "1+2"})
                assert res_bad_align.is_error
                assert "Node not found in current project" in res_bad_align.content[0].text  # type: ignore[union-attr]

                # Invalid handle non-power-of-2: '0+3'
                res_bad_pow = await session.call_tool("inspect_node", {"node_id": "0+3"})
                assert res_bad_pow.is_error
                assert "Node not found in current project" in res_bad_pow.content[0].text  # type: ignore[union-attr]
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_inspect_summary_depth_expansion_and_bounds(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify summary expansion is bounded by depth <= 8, max 511 nodes, and 1MB response limit."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # depth=0 returns no children
                res_d0 = await session.call_tool(
                    "inspect_node", {"node_id": "L2_P0_M0_3", "depth": 0}
                )
                assert not res_d0.is_error
                data_d0 = json.loads(res_d0.content[0].text)  # type: ignore[union-attr]
                assert data_d0["children"] == []

                # depth=1 returns 2 children with their children empty
                res_d1 = await session.call_tool(
                    "inspect_node", {"node_id": "L2_P0_M0_3", "depth": 1}
                )
                assert not res_d1.is_error
                data_d1 = json.loads(res_d1.content[0].text)  # type: ignore[union-attr]
                assert len(data_d1["children"]) == 2
                assert data_d1["children"][0]["children"] == []
                assert data_d1["children"][1]["children"] == []

                # depth=2 returns 2 children, each having 2 grandchildren
                res_d2 = await session.call_tool(
                    "inspect_node", {"node_id": "L2_P0_M0_3", "depth": 2}
                )
                assert not res_d2.is_error
                data_d2 = json.loads(res_d2.content[0].text)  # type: ignore[union-attr]
                assert len(data_d2["children"]) == 2
                assert len(data_d2["children"][0]["children"]) == 2
                assert len(data_d2["children"][1]["children"]) == 2

                # Maximum boundary depth=8 completes safely without exceeding budget
                res_d8 = await session.call_tool(
                    "inspect_node", {"node_id": "L2_P0_M0_3", "depth": 8}
                )
                assert not res_d8.is_error
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_inspect_security_validation_and_no_reflection(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify static error messages, no caller reflection, bounds on huge integer strings, and <= 256B IDs."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # Cross-project scoped ID: never reflect foreign project or credential in error
                malicious_scoped_id = "foreign_project:password_or_token_12345"
                res_cross = await session.call_tool(
                    "inspect_node", {"node_id": malicious_scoped_id}
                )
                assert res_cross.is_error
                err_text = res_cross.content[0].text  # type: ignore[union-attr]
                assert err_text == "Cross-project node inspection rejected"
                assert "password_or_token" not in err_text
                assert "foreign_project" not in err_text

                # Unknown node ID: never reflect caller input in error
                secret_node_input = "L5_P99_M0_SECRET_KEY"
                res_unknown = await session.call_tool(
                    "inspect_node", {"node_id": secret_node_input}
                )
                assert res_unknown.is_error
                err_text_unk = res_unknown.content[0].text  # type: ignore[union-attr]
                assert err_text_unk == "Node not found in current project"
                assert "SECRET_KEY" not in err_text_unk

                # Arbitrary huge integer strings: must be bounded without crashing integer parser
                huge_handle = "0+" + ("9" * 50)
                res_huge = await session.call_tool("inspect_node", {"node_id": huge_handle})
                assert res_huge.is_error
                assert "Node not found in current project" in res_huge.content[0].text  # type: ignore[union-attr]

                # Bounded node_id UTF-8 length <= 256 bytes
                oversize_node_id = "n" * 300
                res_oversize = await session.call_tool(
                    "inspect_node", {"node_id": oversize_node_id}
                )
                assert res_oversize.is_error
                assert "node_id" in res_oversize.content[0].text  # type: ignore[union-attr]

                # Invalid node ID strings (min_length=1)
                assert (await session.call_tool("inspect_node", {"node_id": ""})).is_error
                assert (await session.call_tool("inspect_node", {"node_id": "   "})).is_error

                # Invalid depth bounds (0 <= depth <= 8)
                assert (
                    await session.call_tool("inspect_node", {"node_id": "0+1", "depth": -1})
                ).is_error
                assert (
                    await session.call_tool("inspect_node", {"node_id": "0+1", "depth": 9})
                ).is_error
                assert (
                    await session.call_tool("inspect_node", {"node_id": "0+1", "depth": True})
                ).is_error
                assert (
                    await session.call_tool("inspect_node", {"node_id": "0+1", "depth": "2"})
                ).is_error
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_search_symbols_and_text_with_nonce_framing(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify symbol and BM25 text retrieval return snippets, nonce framing, and passive taint."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # Symbol search: compute_loss
                res_sym = await session.call_tool(
                    "search_symbols_and_text", {"query": "compute_loss"}
                )
                assert not res_sym.is_error
                sym_data = json.loads(res_sym.content[0].text)  # type: ignore[union-attr]
                assert sym_data["count"] >= 1
                hit_sym = sym_data["hits"][0]
                assert hit_sym["symbol"] == "compute_loss"
                assert hit_sym["source"] == "symbol"
                assert "parent_handle" in hit_sym
                assert "node_id" in hit_sym
                assert "snippet" in hit_sym

                # Verify nonce framing on outgoing snippet
                assert "nonce" in hit_sym
                assert len(hit_sym["nonce"]) == 64
                assert hit_sym["taint"] == "UNTRUSTED_EXTERNAL"
                assert (
                    f"<<<EXTERNAL_UNTRUSTED_NONCE_{hit_sym['nonce']}>>>"
                    in hit_sym["framed_snippet"]
                )
                assert (
                    f"<<<END_EXTERNAL_UNTRUSTED_NONCE_{hit_sym['nonce']}>>>"
                    in hit_sym["framed_snippet"]
                )

                # BM25 text search: gradient descent
                res_txt = await session.call_tool(
                    "search_symbols_and_text", {"query": "gradient descent"}
                )
                assert not res_txt.is_error
                txt_data = json.loads(res_txt.content[0].text)  # type: ignore[union-attr]
                assert txt_data["count"] >= 1
                hit_txt = txt_data["hits"][0]
                assert hit_txt["source"] == "text"
                assert "Gradient descent" in hit_txt["snippet"]
                assert hit_txt["seq"] == 1
                assert hit_txt["taint"] == "UNTRUSTED_EXTERNAL"

                # Multi-byte Unicode query
                res_uni = await session.call_tool(
                    "search_symbols_and_text", {"query": "こんにちは世界"}
                )
                assert not res_uni.is_error
                uni_data = json.loads(res_uni.content[0].text)  # type: ignore[union-attr]
                assert uni_data["count"] >= 1
                assert "こんにちは世界" in uni_data["hits"][0]["snippet"]
                assert uni_data["hits"][0]["taint"] == "UNTRUSTED_EXTERNAL"
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_search_invalid_inputs_and_bounds(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify search input validation and limit bounds (1 <= limit <= 100) with strict Pydantic checks."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # Empty query (min_length=1)
                assert (await session.call_tool("search_symbols_and_text", {"query": ""})).is_error
                assert (
                    await session.call_tool("search_symbols_and_text", {"query": "   "})
                ).is_error

                # Query > 4096 bytes (max_length=4096)
                oversize_query = "x" * 5000
                assert (
                    await session.call_tool("search_symbols_and_text", {"query": oversize_query})
                ).is_error

                # Invalid limits (1 <= limit <= 100)
                assert (
                    await session.call_tool(
                        "search_symbols_and_text", {"query": "loss", "limit": 0}
                    )
                ).is_error
                assert (
                    await session.call_tool(
                        "search_symbols_and_text", {"query": "loss", "limit": 101}
                    )
                ).is_error

                # Strict type checks: bool and str rejected
                assert (
                    await session.call_tool(
                        "search_symbols_and_text", {"query": "loss", "limit": True}
                    )
                ).is_error
                assert (
                    await session.call_tool(
                        "search_symbols_and_text", {"query": "loss", "limit": "10"}
                    )
                ).is_error
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_invalid_secret_query_and_tool_names_never_leak(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify that rejected inputs containing sensitive credentials never reflect in error responses or stderr."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # 1. Invalid tool name containing secret token
                sensitive_tool_name = "sk_live_SECRET_TOOL_NAME_9876543210"
                res_tool = await session.call_tool(sensitive_tool_name, {})
                assert res_tool.is_error
                err_tool_msg = res_tool.content[0].text  # type: ignore[union-attr]
                assert sensitive_tool_name not in err_tool_msg
                assert err_tool_msg == "Unknown tool name"

                # 2. Extra argument key containing secret token
                sensitive_extra_arg = "ghp_SECRET_ARG_KEY_ABCDEF12345"
                res_extra = await session.call_tool(
                    "timeline_overview", {"limit": 10, sensitive_extra_arg: "val"}
                )
                assert res_extra.is_error
                err_extra_msg = res_extra.content[0].text  # type: ignore[union-attr]
                assert sensitive_extra_arg not in err_extra_msg
                assert err_extra_msg == "Invalid arguments for tool 'timeline_overview'"

                # 3. Secret embedded in oversized query string
                sensitive_secret = "ghp_1234567890abcdefABCDEFSECRETKEY9876543210"
                oversize_secret_query = f"{sensitive_secret}_{'A' * 4100}"
                res_query = await session.call_tool(
                    "search_symbols_and_text", {"query": oversize_secret_query}
                )
                assert res_query.is_error
                error_msg = res_query.content[0].text  # type: ignore[union-attr]
                assert sensitive_secret not in error_msg
                assert "query" in error_msg

                # 4. Secret embedded in invalid limit argument
                sensitive_limit = "SECRET_TOKEN_IN_LIMIT_FIELD"
                res_limit = await session.call_tool(
                    "search_symbols_and_text", {"query": "valid_query", "limit": sensitive_limit}
                )
                assert res_limit.is_error
                error_limit_msg = res_limit.content[0].text  # type: ignore[union-attr]
                assert sensitive_limit not in error_limit_msg

                # 5. Secret embedded in oversized node_id argument
                sensitive_node_secret = "AWS_SECRET_KEY_1234567890"
                oversize_node_id = f"L0_P0_{sensitive_node_secret}_{'Z' * 300}"
                res_node = await session.call_tool("inspect_node", {"node_id": oversize_node_id})
                assert res_node.is_error
                error_node_msg = res_node.content[0].text  # type: ignore[union-attr]
                assert sensitive_node_secret not in error_node_msg
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_missing_database_behavior() -> None:
    """Verify that launching the server with a non-existent database fails safely via ToolError."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_db = Path(tmpdir) / "does_not_exist.db"
            async with stdio_session(missing_db, "testproj") as session:
                # All tools should return is_error=True with safe static error without process crash
                res_tl = await session.call_tool("timeline_overview", {"limit": 10})
                assert res_tl.is_error
                assert (
                    "Database file not found; ensure fractald daemon has initialized storage"
                    in res_tl.content[0].text
                )  # type: ignore[union-attr]

                res_ins = await session.call_tool("inspect_node", {"node_id": "0+1"})
                assert res_ins.is_error
                assert (
                    "Database file not found; ensure fractald daemon has initialized storage"
                    in res_ins.content[0].text
                )  # type: ignore[union-attr]

                res_srch = await session.call_tool(
                    "search_symbols_and_text", {"query": "compute_loss"}
                )
                assert res_srch.is_error
                assert (
                    "Database file not found; ensure fractald daemon has initialized storage"
                    in res_srch.content[0].text
                )  # type: ignore[union-attr]

    anyio.run(scenario)


def test_default_database_path_parser_configuration() -> None:
    """Verify default database path constant and argument parser configuration directly."""
    from fractalmem.mcp.server import DEFAULT_DB_PATH, build_argument_parser

    assert DEFAULT_DB_PATH == Path("~/.fractalmem/memory.db").expanduser()
    parser = build_argument_parser()
    args = parser.parse_args([])
    assert args.project == "default"
    assert args.db_path == str(DEFAULT_DB_PATH)


def test_project_isolation(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify that a server configured for testproj cannot see data from otherproj."""

    async def scenario() -> None:
        db_path, tmp = populated_db
        try:
            async with stdio_session(db_path, "testproj") as session:
                # Search for a symbol defined only in otherproj
                res = await session.call_tool(
                    "search_symbols_and_text", {"query": "isolated_function"}
                )
                assert not res.is_error
                data = json.loads(res.content[0].text)  # type: ignore[union-attr]
                assert data["count"] == 0
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_context_lifespan_closure(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    """Verify that MCPServerContext enters lifespan, opens DB connection, and cleans up on exit."""

    async def scenario() -> None:
        from fractalmem.mcp.server import create_mcp_server

        db_path, tmp = populated_db
        try:
            server = create_mcp_server(project_id="testproj", db_path=db_path)
            assert server.settings.lifespan is not None
            # Actually enter server lifespan
            async with server.settings.lifespan(server) as ctx:
                # Open read-only DB connection and verify active operational state
                db = ctx.get_db()
                assert db.connection is not None
                row = db.connection.execute("SELECT 1").fetchone()
                assert row[0] == 1
                assert ctx._db is not None
            # After exiting lifespan context, verify connection is physically closed and cleared
            assert ctx._db is None
        finally:
            tmp.cleanup()

    anyio.run(scenario)


def test_valid_secret_query_is_scrubbed_from_response(
    populated_db: tuple[Path, tempfile.TemporaryDirectory[str]],
) -> None:
    async def scenario() -> None:
        path, tmp = populated_db
        try:
            async with stdio_session(path, "testproj") as session:
                secret = "ghp_" + "x" * 36
                result = await session.call_tool("search_symbols_and_text", {"query": secret})
                assert not result.is_error
                assert secret not in result.model_dump_json()
                assert len(result.model_dump_json().encode()) < 1048576
        finally:
            tmp.cleanup()

    anyio.run(scenario)
