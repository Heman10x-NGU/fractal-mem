"""Model Context Protocol (MCP) stdio server for FractalMem.

Exposes exactly three typed tools:
1. timeline_overview
2. inspect_node
3. search_symbols_and_text

All tools operate over a fixed project scope supplied at server launch (--project).
Protocol communication strictly uses stdout; diagnostics and errors go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from pydantic import BaseModel, Field, ValidationError

from fractalmem.core.node import HMRNode
from fractalmem.protocol import validate_identifier
from fractalmem.security.guard import frame_untrusted
from fractalmem.security.redact import Redactor
from fractalmem.storage.db import Database

logger = logging.getLogger("fractalmem.mcp")

DEFAULT_DB_PATH: Path = Path("~/.fractalmem/memory.db").expanduser()
MAX_RESPONSE_BYTES: int = 1_048_576  # 1 MB
MAX_EXPANDED_NODES: int = 511
MAX_NODE_ID_BYTES: int = 256
MAX_QUERY_BYTES: int = 4096

ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "timeline_overview",
        "inspect_node",
        "search_symbols_and_text",
    }
)

ALLOWED_TOOL_ARGS: dict[str, frozenset[str]] = {
    "timeline_overview": frozenset({"limit"}),
    "inspect_node": frozenset({"node_id", "depth"}),
    "search_symbols_and_text": frozenset({"query", "limit"}),
}


# --- Typed Output Contracts for Tools ---


class FrontierNodeModel(BaseModel):
    node_id: str
    scoped_id: str
    handle: str
    level: int
    index_in_level: int
    start_seq: int
    end_seq: int
    summary: str
    byte_size: int
    status: str
    kind: str


class StorageStatusModel(BaseModel):
    journal_mode: str
    schema_version: int
    messages: int
    pending_jobs: int
    failed_jobs: int
    committed_messages: int = 0
    ready_watermark_seq: int = -1
    published_coverage: int = 0
    published_watermark_seq: int = -1


class TimelineOverviewResult(BaseModel):
    project_id: str
    total_messages: int
    batch_active: bool
    rendered_bytes: int
    frontier_nodes_count: int
    returned_nodes_count: int
    status: StorageStatusModel
    nodes: list[FrontierNodeModel]


class AncestorMetaModel(BaseModel):
    parent_id: str | None
    parent_handle: str | None
    parent_level: int
    parent_index: int


class LeafMessageModel(BaseModel):
    seq: int
    session_id: str
    event_id: str
    kind: str
    payload: str
    framed_payload: str
    nonce: str
    language: str | None
    taint_level: str
    passive_taint: str
    created_ns: int


class ChildNodeModel(BaseModel):
    node_id: str
    scoped_id: str
    handle: str
    level: int
    index_in_level: int
    start_seq: int
    end_seq: int
    summary: str
    status: str
    kind: str
    children: list[ChildNodeModel] = Field(default_factory=list)


class InspectNodeResult(BaseModel):
    project_id: str
    node_id: str
    scoped_id: str
    handle: str
    level: int
    index_in_level: int
    start_seq: int
    end_seq: int
    summary: str
    status: str
    kind: str
    ancestors: AncestorMetaModel
    leaf_message: LeafMessageModel | None = None
    children: list[ChildNodeModel] = Field(default_factory=list)


class SearchHitModel(BaseModel):
    seq: int
    session_id: str
    snippet: str
    framed_snippet: str
    nonce: str
    score: float
    node_id: str
    parent_handle: str
    symbol: str | None = None
    source: str
    taint: str
    published: bool = True


class SearchResult(BaseModel):
    project_id: str
    query: str
    count: int
    hits: list[SearchHitModel]


# --- Server Context and Sanitized MCP Server ---


class MCPServerContext:
    """Manages read-only repository lifecycle and scoped validation."""

    def __init__(self, project_id: str, db_path: Path | str) -> None:
        validate_identifier(project_id, "project_id")
        self.project_id = project_id
        self.db_path = Path(db_path).expanduser().resolve()
        self._db: Database | None = None

    def get_db(self) -> Database:
        """Return read-only database instance or raise ToolError if missing/uninitialized."""
        if not self.db_path.is_file():
            raise ToolError(
                "Database file not found; ensure fractald daemon has initialized storage"
            )
        if self._db is None:
            try:
                self._db = Database(self.db_path, read_only=True)
            except sqlite3.Error:
                raise ToolError("Failed to open database: storage error") from None
            except Exception:
                raise ToolError("Failed to open database: initialization error") from None
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None


class SanitizedMCPServer(MCPServer[MCPServerContext]):
    """MCPServer subclass that intercepts call_tool to ensure error responses never reflect caller inputs.

    Prevents leaking credentials or user payloads that might be present in invalid arguments,
    unknown tool names, extra argument keys, or Pydantic ValidationError traces.
    """

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[MCPServerContext, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        # Reject unknown tool names before SDK call_tool to avoid logging/reflecting tool name
        if name not in ALLOWED_TOOLS:
            return CallToolResult(
                content=[TextContent(type="text", text="Unknown tool name")],
                is_error=True,
            )

        # Reject extra argument keys without echoing caller keys
        extra_args = set(arguments.keys()) - ALLOWED_TOOL_ARGS[name]
        if extra_args:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Invalid arguments for tool '{name}'")],
                is_error=True,
            )

        try:
            result = await super().call_tool(name, arguments, context)
            # SDK results contain both text and structured content. Bound that
            # complete serialization, not just the pre-conversion output model.
            if len(result.model_dump_json(by_alias=True).encode()) > MAX_RESPONSE_BYTES - 2048:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text="Response exceeds byte limit; request fewer nodes or search smaller snippets",
                        )
                    ],
                    is_error=True,
                )
            return result
        except MCPError:
            raise
        except ToolError as exc:
            # Handle Pydantic argument validation errors by sanitizing field locations
            if isinstance(exc.__cause__, ValidationError):
                field_errors: list[str] = []
                for err in exc.__cause__.errors():
                    loc = ".".join(str(p) for p in err.get("loc", ()))
                    err_type = err.get("type", "invalid")
                    field_errors.append(f"{loc or 'argument'}: {err_type}")
                desc = ", ".join(field_errors) if field_errors else "invalid arguments"
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=f"Validation error for tool '{name}': {desc}",
                        )
                    ],
                    is_error=True,
                )
            # When tool handler explicitly raised a ToolError, preserve its static safe text directly
            if isinstance(exc.__cause__, ToolError):
                return CallToolResult(
                    content=[TextContent(type="text", text=str(exc.__cause__))],
                    is_error=True,
                )
            return CallToolResult(
                content=[TextContent(type="text", text=f"Tool execution failed for '{name}'")],
                is_error=True,
            )
        except Exception:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Tool execution failed for '{name}'")],
                is_error=True,
            )


def create_mcp_server(project_id: str, db_path: Path | str) -> SanitizedMCPServer:
    """Create and configure a SanitizedMCPServer instance with exactly three typed tools and lifespan."""
    ctx = MCPServerContext(project_id, db_path)

    @asynccontextmanager
    async def server_lifespan(app: MCPServer[Any]) -> AsyncIterator[MCPServerContext]:
        try:
            yield ctx
        finally:
            ctx.close()

    server: SanitizedMCPServer = SanitizedMCPServer(
        name="fractalmem",
        lifespan=server_lifespan,
    )

    @server.tool(
        name="timeline_overview",
        description=(
            "Returns high-level active frontier overview, node summaries, and storage status "
            "for the configured project. Timeline is bounded up to 128 nodes."
        ),
    )
    async def timeline_overview(
        limit: Annotated[
            int,
            Field(
                strict=True,
                ge=1,
                le=128,
                description="Max frontier nodes to return (1..128)",
            ),
        ] = 64,
    ) -> TimelineOverviewResult:
        if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= 128:
            raise ToolError("limit must be an integer between 1 and 128")

        db = ctx.get_db()
        try:
            with db.read_snapshot():
                view = db.load_view(ctx.project_id)
                db_status = db.status(ctx.project_id)
        except sqlite3.Error:
            raise ToolError("Failed to read timeline snapshot: storage error") from None
        except Exception:
            raise ToolError("Failed to read timeline snapshot: query error") from None

        nodes_slice = view.nodes[:limit]
        result = TimelineOverviewResult(
            project_id=ctx.project_id,
            total_messages=view.total_messages,
            batch_active=view.batch_active,
            rendered_bytes=view.byte_size,
            frontier_nodes_count=len(view.nodes),
            returned_nodes_count=len(nodes_slice),
            status=StorageStatusModel(
                journal_mode=str(db_status["journal_mode"]),
                schema_version=int(db_status["schema_version"]),
                messages=int(db_status["messages"]),
                pending_jobs=int(db_status["pending_jobs"]),
                failed_jobs=int(db_status["failed_jobs"]),
                committed_messages=int(db_status.get("committed_messages", 0)),
                ready_watermark_seq=int(db_status.get("ready_watermark_seq", -1)),
                published_coverage=int(db_status.get("published_coverage", 0)),
                published_watermark_seq=int(db_status.get("published_watermark_seq", -1)),
            ),
            nodes=[
                FrontierNodeModel(
                    node_id=n.node_id,
                    scoped_id=n.scoped_id,
                    handle=n.handle,
                    level=n.level,
                    index_in_level=n.index_in_level,
                    start_seq=n.start_seq,
                    end_seq=n.end_seq,
                    summary=n.summary_text,
                    byte_size=n.byte_size,
                    status=n.status,
                    kind=n.kind,
                )
                for n in nodes_slice
            ],
        )

        encoded_len = len(result.model_dump_json().encode("utf-8"))
        if encoded_len > MAX_RESPONSE_BYTES:
            raise ToolError("Response exceeds 1MB budget; reduce limit")

        return result

    @server.tool(
        name="inspect_node",
        description=(
            "Drill down into an HMR-Tree node by ID or handle. Expands children down to bounded "
            "depth (<=8) or returns raw redacted payload and taint for leaf nodes."
        ),
    )
    async def inspect_node(
        node_id: Annotated[
            str,
            Field(
                strict=True,
                min_length=1,
                max_length=256,
                description="Canonical or scoped node ID or UniiChat handle (e.g. 0+1)",
            ),
        ],
        depth: Annotated[
            int,
            Field(
                strict=True,
                ge=0,
                le=8,
                description="Child expansion recursion depth (0..8)",
            ),
        ] = 1,
    ) -> InspectNodeResult:
        if not isinstance(node_id, str) or not node_id.strip():
            raise ToolError("node_id must be a non-empty string")
        clean_id = node_id.strip()

        if len(clean_id.encode("utf-8")) > MAX_NODE_ID_BYTES:
            raise ToolError("node_id exceeds maximum length of 256 bytes")

        if type(depth) is not int or isinstance(depth, bool) or not 0 <= depth <= 8:
            raise ToolError("depth must be an integer between 0 and 8")

        # Reject foreign project scoped prefix
        if ":" in clean_id:
            prefix, canonical_id = clean_id.split(":", 1)
            if prefix != ctx.project_id:
                raise ToolError("Cross-project node inspection rejected")
        else:
            canonical_id = clean_id

        db = ctx.get_db()
        try:
            with db.read_snapshot():
                node = db.get_node(ctx.project_id, canonical_id)
                if node is None and "+" in canonical_id:
                    # Support bounded lookup by handle format "start+span"
                    parts = canonical_id.split("+")
                    if len(parts) == 2:
                        start_str, span_str = parts
                        if (
                            start_str.isdigit()
                            and span_str.isdigit()
                            and len(start_str) <= 19
                            and len(span_str) <= 19
                        ):
                            start = int(start_str)
                            span = int(span_str)
                            if (
                                0 < span <= (1 << 62)
                                and (span & (span - 1)) == 0
                                and start >= 0
                                and start % span == 0
                            ):
                                level = span.bit_length() - 1
                                index = start // span
                                mapped_id = f"L{level}_P{index}_M{start}_{start + span - 1}"
                                node = db.get_node(ctx.project_id, mapped_id)

                if node is None:
                    raise ToolError("Node not found in current project")

                # Ancestors metadata: parent of (level, index)
                parent_level = node.level + 1
                parent_index = node.index_in_level // 2
                parent = db.resolve_parent(parent_level, parent_index, ctx.project_id)
                ancestor_meta = AncestorMetaModel(
                    parent_id=parent.node_id if parent else None,
                    parent_handle=parent.handle if parent else None,
                    parent_level=parent_level,
                    parent_index=parent_index,
                )

                # Leaf node inspection (level 0) returns redacted payload and taint
                if node.level == 0:
                    msg = db.get_message(ctx.project_id, node.start_seq)
                    leaf_data: LeafMessageModel | None = None
                    if msg is not None:
                        framed = frame_untrusted(msg.payload)
                        leaf_data = LeafMessageModel(
                            seq=msg.seq,
                            session_id=msg.session_id,
                            event_id=msg.event_id,
                            kind=msg.kind,
                            payload=msg.payload,  # exact redacted raw payload
                            framed_payload=framed.text,  # cryptographically framed
                            nonce=framed.nonce,
                            language=msg.language,
                            taint_level=msg.taint_level,  # original taint level
                            passive_taint=framed.taint,  # explicit untrusted external taint
                            created_ns=msg.created_ns,
                        )
                    leaf_result = InspectNodeResult(
                        project_id=ctx.project_id,
                        node_id=node.node_id,
                        scoped_id=node.scoped_id,
                        handle=node.handle,
                        level=node.level,
                        index_in_level=node.index_in_level,
                        start_seq=node.start_seq,
                        end_seq=node.end_seq,
                        summary=node.summary_text,
                        status=node.status,
                        kind=node.kind,
                        ancestors=ancestor_meta,
                        leaf_message=leaf_data,
                        children=[],
                    )
                    if len(leaf_result.model_dump_json().encode("utf-8")) > MAX_RESPONSE_BYTES:
                        raise ToolError("Response exceeds 1MB budget; reduce depth")
                    return leaf_result

                # Summary node inspection: recursively expand children down to depth <= 8, max 511 nodes
                expanded_count = 0

                def expand_children(curr_node: HMRNode, current_depth: int) -> list[ChildNodeModel]:
                    nonlocal expanded_count
                    if (
                        current_depth <= 0
                        or curr_node.level == 0
                        or expanded_count >= MAX_EXPANDED_NODES
                    ):
                        return []
                    child_level = curr_node.level - 1
                    left_idx = curr_node.index_in_level * 2
                    right_idx = left_idx + 1

                    left = db.resolve_parent(child_level, left_idx, ctx.project_id)
                    right = db.resolve_parent(child_level, right_idx, ctx.project_id)

                    results: list[ChildNodeModel] = []
                    for ch in (left, right):
                        if ch is not None and expanded_count < MAX_EXPANDED_NODES:
                            expanded_count += 1
                            results.append(
                                ChildNodeModel(
                                    node_id=ch.node_id,
                                    scoped_id=ch.scoped_id,
                                    handle=ch.handle,
                                    level=ch.level,
                                    index_in_level=ch.index_in_level,
                                    start_seq=ch.start_seq,
                                    end_seq=ch.end_seq,
                                    summary=ch.summary_text,
                                    status=ch.status,
                                    kind=ch.kind,
                                    children=expand_children(ch, current_depth - 1),
                                )
                            )
                    return results

                children = expand_children(node, depth)
                summary_result = InspectNodeResult(
                    project_id=ctx.project_id,
                    node_id=node.node_id,
                    scoped_id=node.scoped_id,
                    handle=node.handle,
                    level=node.level,
                    index_in_level=node.index_in_level,
                    start_seq=node.start_seq,
                    end_seq=node.end_seq,
                    summary=node.summary_text,
                    status=node.status,
                    kind=node.kind,
                    ancestors=ancestor_meta,
                    leaf_message=None,
                    children=children,
                )
                if len(summary_result.model_dump_json().encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise ToolError("Response exceeds 1MB budget; reduce depth")
                return summary_result
        except ToolError:
            raise
        except sqlite3.Error:
            raise ToolError("Failed to inspect node: storage error") from None
        except Exception:
            raise ToolError("Failed to inspect node: query error") from None

    @server.tool(
        name="search_symbols_and_text",
        description=(
            "One-call code symbol and BM25 text retrieval, returning exact snippet, score, "
            "and parent HMR-Tree handle for instantaneous navigation."
        ),
    )
    async def search_symbols_and_text(
        query: Annotated[
            str,
            Field(
                strict=True,
                min_length=1,
                max_length=4096,
                description="Code identifier or natural language query (1..4096 bytes)",
            ),
        ],
        limit: Annotated[
            int,
            Field(
                strict=True,
                ge=1,
                le=100,
                description="Max search hits to return (1..100)",
            ),
        ] = 10,
    ) -> SearchResult:
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query must be a non-empty string")
        clean_query = query.strip()
        if len(clean_query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise ToolError("query exceeds maximum length of 4096 bytes")

        if type(limit) is not int or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ToolError("limit must be an integer between 1 and 100")

        db = ctx.get_db()
        try:
            hits = db.search(ctx.project_id, clean_query, limit=limit)
        except ValueError:
            raise ToolError("Invalid search query syntax") from None
        except sqlite3.Error:
            raise ToolError("Search execution failed: storage error") from None
        except Exception:
            raise ToolError("Search execution failed") from None

        hits_data: list[SearchHitModel] = []
        for h in hits:
            framed = frame_untrusted(h.snippet)
            hits_data.append(
                SearchHitModel(
                    seq=h.seq,
                    session_id=h.session_id,
                    snippet=h.snippet,  # exact redacted raw snippet
                    framed_snippet=framed.text,  # cryptographically framed
                    nonce=framed.nonce,
                    score=h.score,
                    node_id=h.node_id,
                    parent_handle=h.parent_handle,
                    symbol=h.symbol,
                    source=h.source,
                    taint=framed.taint,  # explicit untrusted external taint
                    published=h.published,
                )
            )

        search_result = SearchResult(
            project_id=ctx.project_id,
            query=Redactor().redact(clean_query),
            count=len(hits_data),
            hits=hits_data,
        )

        if len(search_result.model_dump_json().encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ToolError("Response exceeds 1MB budget; reduce limit")

        return search_result

    return server


def build_argument_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for the FractalMem MCP server."""
    parser = argparse.ArgumentParser(description="FractalMem Stdio MCP Server")
    parser.add_argument(
        "--project",
        default="default",
        help="Fixed project scope identifier (cannot be selected per tool)",
    )
    parser.add_argument(
        "--db",
        "--database",
        dest="db_path",
        default=str(DEFAULT_DB_PATH),
        help="Path to SQLite database (default: ~/.fractalmem/memory.db)",
    )
    return parser


def main() -> None:
    """CLI entry point for running the FractalMem MCP server over stdio."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = build_argument_parser()
    args = parser.parse_args()

    server = create_mcp_server(project_id=args.project, db_path=args.db_path)
    logger.info("Starting FractalMem MCP server for project '%s' on %s", args.project, args.db_path)
    try:
        asyncio.run(server.run_stdio_async())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
