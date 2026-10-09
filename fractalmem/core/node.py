"""Immutable HMR-Tree node definitions, scoped IDs, validation, and 512-byte budget tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

NodeStatus = Literal["pending", "running", "ready", "failed"]
MessageKind = Literal["user", "unii", "tool", "echo", "work", "note", "summary"]

VALID_NODE_STATUSES: frozenset[str] = frozenset({"pending", "running", "ready", "failed"})
VALID_MESSAGE_KINDS: frozenset[str] = frozenset(
    {"user", "unii", "tool", "echo", "work", "note", "summary"}
)

MAX_SUMMARY_BYTES: int = 512
MAX_TREE_LEVEL: int = 62
PENDING_SUMMARY_PLACEHOLDER: str = "(not summarized yet: zoom it)"

# 512-dash ruler formatted as 5 lines of 100 dashes and 1 line of 12 dashes
RULER_512_LINES: tuple[str, ...] = (
    "-" * 100,
    "-" * 100,
    "-" * 100,
    "-" * 100,
    "-" * 100,
    "-" * 12,
)
RULER_512: str = "\n".join(RULER_512_LINES)


class NodeValidationError(ValueError):
    """Raised when an HMR-Tree node violates structural invariants."""


class ByteBudgetExceededError(ValueError):
    """Raised when node summary exceeds the 512 UTF-8 byte budget."""


def validate_int_field(
    name: str,
    val: Any,
    min_val: int = 0,
    max_val: int | None = None,
) -> int:
    """Validate that val is a genuine integer (not bool) within [min_val, max_val]."""
    if type(val) is not int:
        raise NodeValidationError(f"{name} must be an integer, got {type(val).__name__}: {val!r}")
    if val < min_val:
        raise NodeValidationError(f"{name} must be >= {min_val}, got {val}")
    if max_val is not None and val > max_val:
        raise NodeValidationError(f"{name} must be <= {max_val}, got {val}")
    return val


def validate_project_id(val: Any) -> str:
    """Validate that project_id is a non-empty string."""
    if not isinstance(val, str) or not val.strip():
        raise NodeValidationError(f"project_id must be a non-empty string, got {val!r}")
    return val.strip()


def validate_status(val: Any) -> NodeStatus:
    """Validate that status is one of the permitted NodeStatus literals."""
    if not isinstance(val, str) or val not in VALID_NODE_STATUSES:
        raise NodeValidationError(
            f"Invalid status {val!r}; expected one of {sorted(VALID_NODE_STATUSES)}"
        )
    return val  # type: ignore[return-value]


def validate_kind(val: Any) -> MessageKind:
    """Validate that kind is one of the permitted MessageKind literals."""
    if not isinstance(val, str) or val not in VALID_MESSAGE_KINDS:
        raise NodeValidationError(
            f"Invalid kind {val!r}; expected one of {sorted(VALID_MESSAGE_KINDS)}"
        )
    return val  # type: ignore[return-value]


def sanitize_summary_line(text: str) -> str:
    """Normalize multi-line text into a single line by converting newlines to spaces."""
    return " ".join(text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").split())


def truncate_to_utf8_bytes(text: str, max_bytes: int = MAX_SUMMARY_BYTES) -> str:
    """Truncate text to at most max_bytes in UTF-8 without slicing partial code points.

    Intended for offline text extraction; production ready nodes reject oversize summaries.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError(f"max_bytes must be a positive integer, got {max_bytes!r}")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def make_node_id(level: int, index_in_level: int, start_seq: int, end_seq: int) -> str:
    """Generate canonical node ID according to schema (e.g., 'L2_P0_M0_3')."""
    return f"L{level}_P{index_in_level}_M{start_seq}_{end_seq}"


@dataclass(frozen=True, slots=True)
class HMRNode:
    """Immutable Hierarchical Multi-Resolution Segment Tree Node.

    Invariants:
    - 0 <= level <= 62 (genuine int)
    - index_in_level >= 0 (genuine int)
    - span = 1 << level
    - start_seq = index_in_level * span
    - end_seq = start_seq + span - 1
    - project_id is non-empty string
    - status in {'pending', 'running', 'ready', 'failed'}
    - kind in {'user', 'unii', 'tool', 'echo', 'work', 'note', 'summary'}
    - byte_size = len(summary_text.encode('utf-8')) <= 512 (when ready)
    - summary_text has no raw newlines
    - when pending, summary_text is '(not summarized yet: zoom it)'
    """

    level: int
    index_in_level: int
    start_seq: int
    end_seq: int
    summary_text: str
    byte_size: int
    status: NodeStatus = "ready"
    kind: MessageKind = "summary"
    project_id: str = "default"
    node_id: str = ""

    def __post_init__(self) -> None:
        validate_int_field("level", self.level, min_val=0, max_val=MAX_TREE_LEVEL)
        validate_int_field("index_in_level", self.index_in_level, min_val=0)
        validate_int_field("start_seq", self.start_seq, min_val=0)
        validate_int_field("end_seq", self.end_seq, min_val=0)
        validate_int_field("byte_size", self.byte_size, min_val=0)
        clean_project = validate_project_id(self.project_id)
        if clean_project != self.project_id:
            object.__setattr__(self, "project_id", clean_project)
        validate_status(self.status)
        validate_kind(self.kind)

        expected_span = 1 << self.level
        expected_start = self.index_in_level * expected_span
        expected_end = expected_start + expected_span - 1

        if self.start_seq != expected_start:
            raise NodeValidationError(
                f"Invalid start_seq: expected {expected_start} for level={self.level}, "
                f"index_in_level={self.index_in_level}, got {self.start_seq}"
            )
        if self.end_seq != expected_end:
            raise NodeValidationError(
                f"Invalid end_seq: expected {expected_end} for level={self.level}, "
                f"index_in_level={self.index_in_level}, got {self.end_seq}"
            )

        if not isinstance(self.summary_text, str):
            raise NodeValidationError(
                f"summary_text must be str, got {type(self.summary_text).__name__}"
            )

        if "\n" in self.summary_text or "\r" in self.summary_text:
            raise NodeValidationError("summary_text must not contain newline characters")

        actual_byte_size = len(self.summary_text.encode("utf-8"))
        if actual_byte_size != self.byte_size:
            raise NodeValidationError(
                f"byte_size mismatch: recorded {self.byte_size}, actual {actual_byte_size}"
            )

        if self.status == "ready" and actual_byte_size > MAX_SUMMARY_BYTES:
            raise ByteBudgetExceededError(
                f"Node summary byte size {actual_byte_size} exceeds budget {MAX_SUMMARY_BYTES}"
            )

        if self.status == "pending" and self.summary_text != PENDING_SUMMARY_PLACEHOLDER:
            raise NodeValidationError(
                f"Pending node summary must be '{PENDING_SUMMARY_PLACEHOLDER}', "
                f"got {self.summary_text!r}"
            )

        expected_id = make_node_id(self.level, self.index_in_level, self.start_seq, self.end_seq)
        if not self.node_id:
            object.__setattr__(self, "node_id", expected_id)
        elif self.node_id != expected_id:
            raise NodeValidationError(
                f"node_id mismatch: expected {expected_id}, got {self.node_id}"
            )

    @property
    def span(self) -> int:
        """Number of messages covered by this node (2^level)."""
        return 1 << self.level

    @property
    def handle(self) -> str:
        """UniiChat handle format 'id+n', e.g. '0+1024' or '8+1'."""
        return f"{self.start_seq}+{self.span}"

    @property
    def scoped_id(self) -> str:
        """Project-scoped globally unique node identifier."""
        return f"{self.project_id}:{self.node_id}"

    @property
    def rendered_line(self) -> str:
        """Render single line in the view format: 'id+n|summary'."""
        return f"{self.handle}|{self.summary_text}"

    @property
    def line_byte_size(self) -> int:
        """UTF-8 byte length of the rendered line plus trailing newline."""
        return len(f"{self.rendered_line}\n".encode())

    @classmethod
    def create(
        cls,
        level: int,
        index_in_level: int,
        summary_text: str,
        status: NodeStatus = "ready",
        kind: MessageKind = "summary",
        project_id: str = "default",
    ) -> HMRNode:
        """Factory method to construct a validated immutable HMRNode.

        Rejects oversize ready summaries; never silently truncates production inputs.
        Pending nodes automatically receive the stable placeholder summary.
        """
        validate_int_field("level", level, min_val=0, max_val=MAX_TREE_LEVEL)
        validate_int_field("index_in_level", index_in_level, min_val=0)
        clean_status = validate_status(status)
        clean_kind = validate_kind(kind)
        clean_project = validate_project_id(project_id)

        span = 1 << level
        start_seq = index_in_level * span
        end_seq = start_seq + span - 1

        if clean_status == "pending":
            clean_text = PENDING_SUMMARY_PLACEHOLDER
        else:
            clean_text = sanitize_summary_line(summary_text) if summary_text else ""
            byte_size = len(clean_text.encode("utf-8"))
            if clean_status == "ready" and byte_size > MAX_SUMMARY_BYTES:
                raise ByteBudgetExceededError(
                    f"Node summary byte size {byte_size} exceeds budget {MAX_SUMMARY_BYTES}"
                )

        byte_size = len(clean_text.encode("utf-8"))
        node_id = make_node_id(level, index_in_level, start_seq, end_seq)
        return cls(
            level=level,
            index_in_level=index_in_level,
            start_seq=start_seq,
            end_seq=end_seq,
            summary_text=clean_text,
            byte_size=byte_size,
            status=clean_status,
            kind=clean_kind,
            project_id=clean_project,
            node_id=node_id,
        )

    @classmethod
    def create_leaf(
        cls,
        seq: int,
        text: str,
        kind: MessageKind = "user",
        status: NodeStatus = "ready",
        project_id: str = "default",
    ) -> HMRNode:
        """Factory to create a level 0 leaf node for message sequence `seq`.

        Rejects oversize summaries when status='ready'.
        """
        return cls.create(
            level=0,
            index_in_level=seq,
            summary_text=text,
            status=status,
            kind=kind,
            project_id=project_id,
        )

    @classmethod
    def create_pending(
        cls,
        level: int,
        index_in_level: int,
        project_id: str = "default",
    ) -> HMRNode:
        """Factory to create a pending unbuilt node with the stable placeholder."""
        return cls.create(
            level=level,
            index_in_level=index_in_level,
            summary_text=PENDING_SUMMARY_PLACEHOLDER,
            status="pending",
            kind="summary",
            project_id=project_id,
        )


def format_compression_prompt(message_id: int, kind: str, text: str) -> str:
    """Format the 512-dash ruler task prompt for compressing a single message."""
    return (
        f"Compaction: compress message {message_id} into one line of at most 512 bytes "
        f"(about 70 words), the length of this ruler:\n"
        f"{RULER_512}\n"
        f"<input>\n"
        f"{kind}: {text}\n"
        f"</input>"
    )


def format_merge_prompt(
    line_a_handle: str,
    line_b_handle: str,
    line_a_text: str,
    line_b_text: str,
    start_id: int,
    end_id: int,
) -> str:
    """Format the 512-dash ruler task prompt for merging two adjacent lines."""
    return (
        f"Compaction: merge lines {line_a_handle} and {line_b_handle}, adjacent, into one line "
        f"of at most 512 bytes (about 70 words), the length of this ruler:\n"
        f"{RULER_512}\n"
        f"<chat> may hold their messages, {start_id} to {end_id}, in more detail: "
        f"take details of them from there too.\n"
        f"<input>\n"
        f"{line_a_text}\n"
        f"{line_b_text}\n"
        f"</input>"
    )
