"""Active frontier view management, byte-exact rendering, and atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fractalmem.core.node import (
    MAX_TREE_LEVEL,
    VALID_MESSAGE_KINDS,
    VALID_NODE_STATUSES,
    ByteBudgetExceededError,
    HMRNode,
    MessageKind,
    NodeStatus,
    NodeValidationError,
    make_node_id,
    validate_int_field,
    validate_project_id,
)

VIEW_SNAPSHOT_VERSION: int = 1
REQUIRED_VIEW_SNAPSHOT_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "project_id",
        "total_messages",
        "batch_active",
        "rendered_bytes",
        "view_pairs",
        "nodes",
    }
)
REQUIRED_NODE_SNAPSHOT_KEYS: frozenset[str] = frozenset(
    {
        "level",
        "index_in_level",
        "start_seq",
        "end_seq",
        "summary_text",
        "byte_size",
        "status",
        "kind",
        "project_id",
        "node_id",
    }
)


class ViewValidationError(ValueError):
    """Raised when an active view violates contiguous frontier invariants."""


class ViewPersistenceError(IOError):
    """Raised when view serialization or deserialization fails."""


@dataclass(frozen=True, slots=True)
class View:
    """Active frontier view covering [0, total_messages).

    Invariants:
    - total_messages >= 0 (genuine int)
    - If total_messages == 0: nodes is empty.
    - If total_messages > 0:
      - nodes[0].start_seq == 0
      - For k in 1..len(nodes)-1: nodes[k].start_seq == nodes[k-1].end_seq + 1
      - nodes[-1].end_seq == total_messages - 1
    - batch_active is a genuine boolean recording whether a batch merge is in progress.
    """

    project_id: str
    total_messages: int
    nodes: tuple[HMRNode, ...]
    batch_active: bool = False

    def __post_init__(self) -> None:
        clean_project = validate_project_id(self.project_id)
        if clean_project != self.project_id:
            object.__setattr__(self, "project_id", clean_project)

        validate_int_field("total_messages", self.total_messages, min_val=0)

        if not isinstance(self.batch_active, bool):
            raise ViewValidationError(
                f"batch_active must be a bool, got {type(self.batch_active).__name__}: {self.batch_active!r}"
            )

        if not isinstance(self.nodes, tuple):
            raise ViewValidationError(
                f"nodes must be a tuple of HMRNode, got {type(self.nodes).__name__}"
            )

        for n in self.nodes:
            if not isinstance(n, HMRNode):
                raise ViewValidationError(
                    f"All view nodes must be HMRNode instances, got {type(n).__name__}"
                )

        self.validate()

    def validate(self) -> None:
        """Validate contiguous coverage of [0, total_messages)."""
        if self.total_messages == 0:
            if len(self.nodes) != 0:
                raise ViewValidationError(
                    f"View with 0 messages must contain 0 nodes, got {len(self.nodes)}"
                )
            return

        if len(self.nodes) == 0:
            raise ViewValidationError(
                f"View with {self.total_messages} messages cannot have empty nodes"
            )

        expected_start = 0
        for idx, node in enumerate(self.nodes):
            if node.project_id != self.project_id:
                raise ViewValidationError(
                    f"Node {node.node_id} project_id '{node.project_id}' does not match "
                    f"view project_id '{self.project_id}'"
                )
            if node.start_seq != expected_start:
                raise ViewValidationError(
                    f"Frontier gap or overlap at index {idx}: expected start {expected_start}, "
                    f"got {node.start_seq} (node {node.node_id})"
                )
            expected_start = node.end_seq + 1

        last_node = self.nodes[-1]
        if last_node.end_seq != self.total_messages - 1:
            raise ViewValidationError(
                f"Frontier end mismatch: last node ends at {last_node.end_seq}, "
                f"expected total_messages - 1 = {self.total_messages - 1}"
            )

    @classmethod
    def empty(cls, project_id: str = "default") -> View:
        """Create an empty view for a new project."""
        return cls(project_id=project_id, total_messages=0, nodes=(), batch_active=False)

    def render_buffer(self, include_header: bool = False) -> str:
        """Render the view buffer into a single string.

        Notice: To maintain the strict append-only prefix cache invariant across turns,
        the compared view buffer avoids a trailing '</chat>' tag.
        Full prompt builders can wrap this buffer with '</chat>' at turn dispatch time.
        """
        lines = [f"{n.rendered_line}\n" for n in self.nodes]
        joined = "".join(lines)
        if include_header:
            return f"<chat>\n{joined}"
        return joined

    def render_prompt_view(self) -> str:
        """Render view wrapped in <chat> tags for model turn input."""
        return f"<chat>\n{self.render_buffer(include_header=False)}</chat>"

    def serialized_byte_size(self, include_header: bool = False) -> int:
        """Return the exact UTF-8 byte size of the view buffer including IDs and separators."""
        return len(self.render_buffer(include_header=include_header).encode())

    @property
    def byte_size(self) -> int:
        """Total UTF-8 byte size of all rendered node lines (including separators and newlines)."""
        return sum(node.line_byte_size for node in self.nodes)

    @property
    def view_pairs(self) -> list[list[int]]:
        """Return the view as [level, index_in_level] pairs for compatibility with view.json."""
        return [[n.level, n.index_in_level] for n in self.nodes]

    def to_dict(self) -> dict[str, Any]:
        """Convert view to a structured serializable dictionary."""
        return {
            "version": VIEW_SNAPSHOT_VERSION,
            "project_id": self.project_id,
            "total_messages": self.total_messages,
            "batch_active": self.batch_active,
            "rendered_bytes": self.byte_size,
            "view_pairs": self.view_pairs,
            "nodes": [
                {
                    "level": n.level,
                    "index_in_level": n.index_in_level,
                    "start_seq": n.start_seq,
                    "end_seq": n.end_seq,
                    "summary_text": n.summary_text,
                    "byte_size": n.byte_size,
                    "status": n.status,
                    "kind": n.kind,
                    "project_id": n.project_id,
                    "node_id": n.node_id,
                }
                for n in self.nodes
            ],
        }

    @classmethod
    def from_dict(cls, data: Any) -> View:
        """Reconstruct and validate a View from a dictionary snapshot without coercion."""
        if not isinstance(data, dict):
            raise ViewPersistenceError(f"Snapshot root must be a dict, got {type(data).__name__}")

        missing_keys = REQUIRED_VIEW_SNAPSHOT_KEYS - set(data.keys())
        if missing_keys:
            raise ViewPersistenceError(
                f"View snapshot missing required fields: {sorted(missing_keys)}"
            )

        # Version validation (genuine int, no coercion)
        raw_version = data["version"]
        if type(raw_version) is not int or raw_version != VIEW_SNAPSHOT_VERSION:
            raise ViewPersistenceError(
                f"Unsupported view snapshot version: {raw_version!r} (expected {VIEW_SNAPSHOT_VERSION})"
            )

        # project_id validation
        raw_project_id = data["project_id"]
        if not isinstance(raw_project_id, str) or not raw_project_id.strip():
            raise ViewPersistenceError(
                f"project_id must be a non-empty string, got {raw_project_id!r}"
            )
        project_id = raw_project_id.strip()

        # total_messages validation (genuine int >= 0)
        raw_total_messages = data["total_messages"]
        if type(raw_total_messages) is not int or raw_total_messages < 0:
            raise ViewPersistenceError(
                f"total_messages must be a non-negative integer, got {raw_total_messages!r}"
            )
        total_messages = raw_total_messages

        # batch_active validation (genuine bool, reject 'false' string or int)
        raw_batch_active = data["batch_active"]
        if not isinstance(raw_batch_active, bool):
            raise ViewPersistenceError(
                f"batch_active must be a bool, got {type(raw_batch_active).__name__}: {raw_batch_active!r}"
            )
        batch_active = raw_batch_active

        # rendered_bytes validation
        raw_rendered_bytes = data["rendered_bytes"]
        if type(raw_rendered_bytes) is not int or raw_rendered_bytes < 0:
            raise ViewPersistenceError(
                f"rendered_bytes must be a non-negative integer, got {raw_rendered_bytes!r}"
            )
        expected_rendered_bytes = raw_rendered_bytes

        # view_pairs validation
        raw_view_pairs = data["view_pairs"]
        if not isinstance(raw_view_pairs, list):
            raise ViewPersistenceError(
                f"view_pairs must be a list, got {type(raw_view_pairs).__name__}"
            )
        for pair in raw_view_pairs:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or any(type(value) is not int for value in pair)
            ):
                raise ViewPersistenceError("view_pairs must contain pairs of genuine integers")

        # nodes list validation
        raw_nodes = data["nodes"]
        if not isinstance(raw_nodes, list):
            raise ViewPersistenceError(f"nodes must be a list, got {type(raw_nodes).__name__}")

        nodes: list[HMRNode] = []
        for idx, raw in enumerate(raw_nodes):
            if not isinstance(raw, dict):
                raise ViewPersistenceError(
                    f"Node at index {idx} must be a dict, got {type(raw).__name__}"
                )

            missing_node_keys = REQUIRED_NODE_SNAPSHOT_KEYS - set(raw.keys())
            if missing_node_keys:
                raise ViewPersistenceError(
                    f"Node at index {idx} missing required fields: {sorted(missing_node_keys)}"
                )

            try:
                # Strictly validate each field's genuine type
                level = validate_int_field(
                    f"node[{idx}].level", raw["level"], min_val=0, max_val=MAX_TREE_LEVEL
                )
                index_in_level = validate_int_field(
                    f"node[{idx}].index_in_level", raw["index_in_level"], min_val=0
                )
                start_seq = validate_int_field(
                    f"node[{idx}].start_seq", raw["start_seq"], min_val=0
                )
                end_seq = validate_int_field(f"node[{idx}].end_seq", raw["end_seq"], min_val=0)
                byte_size = validate_int_field(
                    f"node[{idx}].byte_size", raw["byte_size"], min_val=0
                )

                summary_text = raw["summary_text"]
                if not isinstance(summary_text, str):
                    raise ViewPersistenceError(
                        f"node[{idx}].summary_text must be str, got {type(summary_text).__name__}"
                    )

                status = raw["status"]
                if not isinstance(status, str) or status not in VALID_NODE_STATUSES:
                    raise ViewPersistenceError(
                        f"node[{idx}].status invalid: {status!r}; expected {sorted(VALID_NODE_STATUSES)}"
                    )

                kind = raw["kind"]
                if not isinstance(kind, str) or kind not in VALID_MESSAGE_KINDS:
                    raise ViewPersistenceError(
                        f"node[{idx}].kind invalid: {kind!r}; expected {sorted(VALID_MESSAGE_KINDS)}"
                    )

                node_project = raw["project_id"]
                if not isinstance(node_project, str) or node_project != project_id:
                    raise ViewPersistenceError(
                        f"node[{idx}].project_id '{node_project}' does not match snapshot project_id '{project_id}'"
                    )

                node_id = raw["node_id"]
                expected_node_id = make_node_id(level, index_in_level, start_seq, end_seq)
                if not isinstance(node_id, str) or node_id != expected_node_id:
                    raise ViewPersistenceError(
                        f"node[{idx}].node_id mismatch: expected '{expected_node_id}', got '{node_id}'"
                    )

                node = HMRNode(
                    level=level,
                    index_in_level=index_in_level,
                    start_seq=start_seq,
                    end_seq=end_seq,
                    summary_text=summary_text,
                    byte_size=byte_size,
                    status=cast(NodeStatus, status),
                    kind=cast(MessageKind, kind),
                    project_id=project_id,
                    node_id=expected_node_id,
                )
                nodes.append(node)
            except (NodeValidationError, ByteBudgetExceededError) as e:
                raise ViewPersistenceError(
                    f"Corrupt node at index {idx} in view snapshot: {e}"
                ) from e

        try:
            view = cls(
                project_id=project_id,
                total_messages=total_messages,
                nodes=tuple(nodes),
                batch_active=batch_active,
            )
        except ViewValidationError as e:
            raise ViewPersistenceError(f"Frontier validation failed on loaded snapshot: {e}") from e

        # Validate view_pairs against reconstructed nodes
        expected_pairs = [[n.level, n.index_in_level] for n in view.nodes]
        if raw_view_pairs != expected_pairs:
            raise ViewPersistenceError(
                f"view_pairs does not match reconstructed nodes: recorded {raw_view_pairs}, "
                f"expected {expected_pairs}"
            )

        # Validate rendered bytes
        actual_bytes = view.byte_size
        if actual_bytes != expected_rendered_bytes:
            raise ViewPersistenceError(
                f"View rendered bytes mismatch upon reload: recorded {expected_rendered_bytes}, "
                f"computed {actual_bytes}"
            )

        return view

    def save_atomic(self, file_path: Path | str) -> None:
        """Atomically persist view snapshot to disk with write-flush-replace and directory fsync."""
        target_path = Path(file_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        temp_dir = target_path.parent
        prefix = f".tmp_{target_path.name}_{uuid.uuid4().hex}_"

        temp_fd, temp_path_str = tempfile.mkstemp(dir=temp_dir, prefix=prefix, text=True)
        temp_path = Path(temp_path_str)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, target_path)

            # Durably sync directory entry on POSIX systems supporting O_DIRECTORY
            if hasattr(os, "O_DIRECTORY"):
                dir_fd = os.open(temp_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise ViewPersistenceError(
                f"Failed to atomically persist view to {target_path}: {e}"
            ) from e

    @classmethod
    def load_atomic(cls, file_path: Path | str) -> View:
        """Load and validate an atomic view snapshot from disk."""
        target_path = Path(file_path).resolve()
        if not target_path.exists():
            raise ViewPersistenceError(f"View snapshot file not found: {target_path}")

        try:
            with open(target_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise ViewPersistenceError(f"Failed to read view snapshot {target_path}: {e}") from e

        return cls.from_dict(data)

