"""HMR-Tree math, due score calculations, sibling pairs, and Sawtooth batch merging."""

from __future__ import annotations

import heapq
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fractalmem.core.node import PENDING_SUMMARY_PLACEHOLDER, HMRNode, MessageKind
from fractalmem.core.view import View

DEFAULT_MAX_VIEW_BYTES: int = 128_000
DEFAULT_TARGET_VIEW_BYTES: int = 64_000

# Type alias for resolving or looking up pre-built parent nodes: (level, index_in_level, project_id) -> HMRNode | None
ParentNodeResolver = Callable[[int, int, str], HMRNode | None]


@dataclass(frozen=True, slots=True)
class SiblingPair:
    """An adjacent sibling pair in the view frontier.

    Siblings share level `l`, left index `2j`, and right index `2j + 1`.
    They cover up to inclusive index: last = (left_index + 2) * 2^l - 1.
    """

    pair_index: int  # index in view.nodes of the left node
    left_node: HMRNode
    right_node: HMRNode
    total_messages: int

    def __post_init__(self) -> None:
        if type(self.pair_index) is not int or self.pair_index < 0:
            raise ValueError(
                f"pair_index must be non-negative int, got {type(self.pair_index).__name__}: {self.pair_index!r}"
            )
        if type(self.total_messages) is not int or self.total_messages < 0:
            raise ValueError(
                f"total_messages must be non-negative int, got {type(self.total_messages).__name__}: {self.total_messages!r}"
            )
        if self.left_node.project_id != self.right_node.project_id:
            raise ValueError(
                f"Siblings must belong to same project: {self.left_node.project_id} vs {self.right_node.project_id}"
            )
        if self.left_node.level != self.right_node.level:
            raise ValueError(
                f"Sibling level mismatch: left={self.left_node.level}, right={self.right_node.level}"
            )
        if self.left_node.index_in_level % 2 != 0:
            raise ValueError(
                f"Left sibling index must be even, got {self.left_node.index_in_level}"
            )
        if self.right_node.index_in_level != self.left_node.index_in_level + 1:
            raise ValueError(
                f"Right sibling must have index left + 1, got {self.right_node.index_in_level}"
            )
        if self.left_node.end_seq + 1 != self.right_node.start_seq:
            raise ValueError(
                f"Siblings must be contiguous: left ends at {self.left_node.end_seq}, "
                f"right starts at {self.right_node.start_seq}"
            )
        if self.total_messages <= self.last_seq:
            raise ValueError(
                f"total_messages ({self.total_messages}) must be strictly greater than "
                f"last_seq ({self.last_seq})"
            )

    @property
    def level(self) -> int:
        return self.left_node.level

    @property
    def left_index(self) -> int:
        return self.left_node.index_in_level

    @property
    def start_seq(self) -> int:
        return self.left_node.start_seq

    @property
    def end_seq(self) -> int:
        return self.right_node.end_seq

    @property
    def last_seq(self) -> int:
        return self.right_node.end_seq

    @property
    def parent_level(self) -> int:
        return self.level + 1

    @property
    def parent_index(self) -> int:
        return self.left_index // 2

    @property
    def span_each(self) -> int:
        return 1 << self.level

    @property
    def due_numerator(self) -> int:
        """T - last (where T = total_messages)."""
        return self.total_messages - self.last_seq

    @property
    def due_denominator(self) -> int:
        """2^l."""
        return 1 << self.level

    @property
    def due_score(self) -> float:
        """Direct terminal-age score: (T - last) / 2^l."""
        return self.due_numerator / self.due_denominator

    def is_more_due_than(self, other: SiblingPair) -> bool:
        """Exact integer cross-multiplication comparison with oldest tie-breaking.

        Due score comparison:
        self.due_numerator / self.due_denominator vs other.due_numerator / other.due_denominator
        <=> self.due_numerator * other.due_denominator vs other.due_numerator * self.due_denominator

        Ties choose the oldest pair (smaller start_seq).
        """
        if self is other:
            return False

        lhs = self.due_numerator * other.due_denominator
        rhs = other.due_numerator * self.due_denominator

        if lhs != rhs:
            return lhs > rhs

        # Tie-break: oldest pair wins (smaller start_seq)
        return self.start_seq < other.start_seq

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SiblingPair):
            return NotImplemented
        # In strict ordering, pair < pair must be False.
        # self < other means other is strictly more due (has higher merge priority) than self.
        return other.is_more_due_than(self)

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, SiblingPair):
            return NotImplemented
        return self.is_more_due_than(other)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SiblingPair):
            return NotImplemented
        return (
            self.left_node == other.left_node
            and self.right_node == other.right_node
            and self.total_messages == other.total_messages
            and self.pair_index == other.pair_index
        )


def calculate_due_direct(t: int, last: int, level: int) -> float:
    """Compute the direct terminal-age formula: (T - last) / 2^l."""
    if type(t) is not int or t < 0:
        raise ValueError(f"T must be non-negative integer, got {t!r}")
    if type(last) is not int or last < 0:
        raise ValueError(f"last must be non-negative integer, got {last!r}")
    if type(level) is not int or level < 0:
        raise ValueError(f"level must be non-negative integer, got {level!r}")
    return (t - last) / (1 << level)


def find_sibling_pairs(view: View, age_clock: int | None = None) -> list[SiblingPair]:
    """Find all valid sibling pairs in the current frontier view."""
    nodes = view.nodes
    pairs: list[SiblingPair] = []
    t = age_clock if age_clock is not None else view.total_messages
    if t < view.total_messages:
        raise ValueError(f"age_clock ({t}) must be >= view total_messages ({view.total_messages})")

    for k in range(len(nodes) - 1):
        left = nodes[k]
        right = nodes[k + 1]

        if left.level == right.level:
            # Must be an aligned sibling pair: left is even index, right is left + 1
            if left.index_in_level % 2 == 0 and right.index_in_level == left.index_in_level + 1:
                pair = SiblingPair(
                    pair_index=k,
                    left_node=left,
                    right_node=right,
                    total_messages=t,
                )
                pairs.append(pair)

    return pairs


class InMemoryNodeStore:
    """Thread-safe in-memory registry of immutable HMRNode instances."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[tuple[str, int, int], HMRNode] = {}

    def put(self, node: HMRNode) -> None:
        """Store an immutable HMRNode instance in the registry."""
        if not isinstance(node, HMRNode):
            raise TypeError(f"Expected HMRNode, got {type(node).__name__}")
        with self._lock:
            key = (node.project_id, node.level, node.index_in_level)
            existing = self._nodes.get(key)
            if existing is not None and existing.status == "ready" and existing != node:
                raise ValueError("ready node content is immutable")
            self._nodes[key] = node

    def get(self, level: int, index_in_level: int, project_id: str) -> HMRNode | None:
        """Retrieve a node by level and index_in_level for a project."""
        with self._lock:
            return self._nodes.get((project_id, level, index_in_level))

    def __contains__(self, key: tuple[str, int, int]) -> bool:
        with self._lock:
            return key in self._nodes

    def __len__(self) -> int:
        with self._lock:
            return len(self._nodes)


class HMRTreeEngine:
    """Stateful engine managing the HMR-Tree active view and Sawtooth batching."""

    def __init__(
        self,
        project_id: str = "default",
        view: View | None = None,
        max_view_bytes: int = DEFAULT_MAX_VIEW_BYTES,
        target_view_bytes: int = DEFAULT_TARGET_VIEW_BYTES,
        parent_resolver: ParentNodeResolver | None = None,
        age_clock: int | None = None,
    ) -> None:
        if type(max_view_bytes) is not int or max_view_bytes <= 0:
            raise ValueError(f"max_view_bytes must be a positive integer, got {max_view_bytes!r}")
        if type(target_view_bytes) is not int or target_view_bytes <= 0:
            raise ValueError(
                f"target_view_bytes must be a positive integer, got {target_view_bytes!r}"
            )
        if max_view_bytes <= target_view_bytes:
            raise ValueError(
                f"max_view_bytes ({max_view_bytes}) must be strictly greater than "
                f"target_view_bytes ({target_view_bytes})"
            )

        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError(f"project_id must be a non-empty string, got {project_id!r}")
        clean_project = project_id.strip()

        if view is not None:
            if view.project_id != clean_project:
                raise ValueError(
                    f"View project_id '{view.project_id}' does not match engine project_id '{clean_project}'"
                )

        if age_clock is not None:
            if type(age_clock) is not int or age_clock < 0:
                raise ValueError(f"age_clock must be a non-negative integer, got {age_clock!r}")
            current_total = view.total_messages if view is not None else 0
            if age_clock < current_total:
                raise ValueError(
                    f"age_clock ({age_clock}) must be >= view total_messages ({current_total})"
                )

        self.project_id = clean_project
        self.max_view_bytes = max_view_bytes
        self.target_view_bytes = target_view_bytes
        self.parent_resolver = parent_resolver
        self.age_clock = age_clock

        self.view: View = view if view is not None else View.empty(project_id=self.project_id)

    def set_age_clock(self, age_clock: int | None) -> None:
        """Update explicit age clock T for storage-backed merges."""
        if age_clock is not None:
            if type(age_clock) is not int or age_clock < 0:
                raise ValueError(f"age_clock must be a non-negative integer, got {age_clock!r}")
            if age_clock < self.view.total_messages:
                raise ValueError(
                    f"age_clock ({age_clock}) must be >= view total_messages ({self.view.total_messages})"
                )
        self.age_clock = age_clock

    def append_leaf_node(self, node: HMRNode) -> View:
        """Append an externally built level 0 node and evaluate Sawtooth batch merging."""
        if node.level != 0:
            raise ValueError(f"Can only append level 0 leaf nodes, got level {node.level}")
        if node.project_id != self.project_id:
            raise ValueError(
                f"Node project_id '{node.project_id}' does not match engine '{self.project_id}'"
            )
        if node.start_seq != self.view.total_messages:
            raise ValueError(
                f"Node start_seq {node.start_seq} does not match expected total messages "
                f"{self.view.total_messages}"
            )

        new_total = self.view.total_messages + 1
        if self.age_clock is not None and new_total > self.age_clock:
            raise ValueError("published coverage cannot exceed the explicit age clock")
        new_nodes = self.view.nodes + (node,)
        new_view = View(
            project_id=self.project_id,
            total_messages=new_total,
            nodes=new_nodes,
            batch_active=self.view.batch_active,
        )
        self.view = new_view

        # Check if batch merge should trigger or resume
        if not self.view.batch_active:
            if self.view.byte_size > self.max_view_bytes:
                self._run_sawtooth_batch()
        else:
            # Batch is already active from previous incomplete run; resume it
            self._run_sawtooth_batch()

        return self.view

    def append_message(
        self,
        text: str,
        kind: MessageKind = "user",
        status: str = "ready",
    ) -> HMRNode:
        """Create and append a leaf message node for the next sequence position.

        Pending messages use the stable placeholder summary. Ready messages must fit 512 bytes.
        """
        seq = self.view.total_messages
        if status not in ("ready", "pending"):
            raise ValueError("new message status must be ready or pending")
        leaf_summary = PENDING_SUMMARY_PLACEHOLDER if status == "pending" else text
        leaf = HMRNode.create_leaf(
            seq=seq,
            text=leaf_summary,
            kind=kind,
            status="ready" if status == "ready" else "pending",
            project_id=self.project_id,
        )
        self.append_leaf_node(leaf)
        return leaf

    def _resolve_parent(self, pair: SiblingPair) -> HMRNode | None:
        """Resolve parent node from the registered ParentNodeResolver."""
        if self.parent_resolver is None:
            return None

        parent = self.parent_resolver(
            pair.parent_level,
            pair.parent_index,
            self.project_id,
        )
        if parent is not None and parent.status == "ready":
            return parent
        return None

    def _run_sawtooth_batch(self) -> bool:
        """Prepare an exact-priority batch in O(n log n), publish at target only.

        A linked frontier and a lazy priority heap avoid rebuilding and querying
        the entire frontier at every merge. All priorities use the same T and
        integer cross multiplication; ties retain the oldest-pair policy.
        """
        original = self.view
        nodes = {node.start_seq: node for node in original.nodes}
        starts = [node.start_seq for node in original.nodes]
        after: dict[int, int | None] = {
            key: starts[i + 1] if i + 1 < len(starts) else None for i, key in enumerate(starts)
        }
        before: dict[int, int | None] = {
            key: starts[i - 1] if i else None for i, key in enumerate(starts)
        }
        size = original.byte_size

        @dataclass(slots=True)
        class Candidate:
            pair: SiblingPair
            parent: HMRNode

            def __lt__(self, other: Candidate) -> bool:
                return self.pair.is_more_due_than(other.pair)

        heap: list[Candidate] = []
        t = self.age_clock if self.age_clock is not None else original.total_messages
        if t < original.total_messages:
            raise ValueError(
                f"age_clock ({t}) must be >= view total_messages ({original.total_messages})"
            )

        def enqueue(left_start: int | None) -> None:
            if left_start is None or left_start not in nodes:
                return
            right_start = after[left_start]
            if right_start is None:
                return
            left, right = nodes[left_start], nodes[right_start]
            if (
                left.level != right.level
                or left.index_in_level % 2
                or right.index_in_level != left.index_in_level + 1
            ):
                return
            pair = SiblingPair(0, left, right, t)
            parent = self._resolve_parent(pair)
            if parent is None:
                return
            if (
                parent.project_id != self.project_id
                or parent.level != pair.parent_level
                or parent.index_in_level != pair.parent_index
            ):
                raise ValueError("resolver returned a parent for a different span or project")
            heapq.heappush(heap, Candidate(pair, parent))

        for key in starts:
            enqueue(key)
        while size > self.target_view_bytes and heap:
            candidate = heapq.heappop(heap)
            pair, parent = candidate.pair, candidate.parent
            left, right = pair.left_node.start_seq, pair.right_node.start_seq
            if (
                nodes.get(left) is not pair.left_node
                or nodes.get(right) is not pair.right_node
                or after.get(left) != right
            ):
                continue
            previous, following = before[left], after[right]
            size += (
                parent.line_byte_size
                - pair.left_node.line_byte_size
                - pair.right_node.line_byte_size
            )
            nodes[left] = parent
            del nodes[right]
            after[left] = following
            del after[right]
            del before[right]
            if following is not None:
                before[following] = left
            enqueue(previous)
            enqueue(left)
        completed = size <= self.target_view_bytes
        if completed:
            result: list[HMRNode] = []
            position = starts[0] if starts else None
            while position is not None:
                result.append(nodes[position])
                position = after[position]
            frontier = tuple(result)
        else:
            frontier = original.nodes
        self.view = View(
            self.project_id, original.total_messages, frontier, batch_active=not completed
        )
        return completed

    def append_leaf_nodes(self, leaves: Sequence[HMRNode]) -> None:
        """Append a committed ingress batch and evaluate its visible frontier once.

        Intermediate states inside a storage transaction are not published.
        Each leaf remains an immutable, contiguous event in the append log.
        """
        if not leaves:
            return
        start = self.view.total_messages
        if self.age_clock is not None and start + len(leaves) > self.age_clock:
            raise ValueError("published coverage cannot exceed the explicit age clock")
        for offset, leaf in enumerate(leaves):
            if (
                leaf.project_id != self.project_id
                or leaf.level != 0
                or leaf.start_seq != start + offset
                or leaf.status not in ("ready", "pending")
            ):
                raise ValueError("batch leaves must be contiguous ready/pending project events")
        self.view = View(
            self.project_id,
            start + len(leaves),
            self.view.nodes + tuple(leaves),
            batch_active=self.view.batch_active,
        )
        if self.view.batch_active or self.view.byte_size > self.max_view_bytes:
            self.sawtooth_batch_merge()

    def sawtooth_batch_merge(self) -> bool:
        """Public operation to initiate or execute a Sawtooth batch merge.

        Refuses to begin a new batch if view.byte_size <= max_view_bytes and batch_active is False,
        Can resume an already active batch.
        Returns True if batch completed (view.byte_size <= target_view_bytes), False if batch remains active.
        """
        if not self.view.batch_active and self.view.byte_size <= self.max_view_bytes:
            raise ValueError(
                f"Cannot begin batch merge: view size {self.view.byte_size} bytes is below or equal "
                f"to max threshold {self.max_view_bytes} bytes"
            )
        return self._run_sawtooth_batch()

    def resume_batch(self) -> bool:
        """Public operation to resume an already active unfinished batch merge.

        Refuses if batch_active is False.
        Returns True if batch completed, False if batch remains active.
        """
        if not self.view.batch_active:
            raise ValueError("No active batch merge to resume")
        return self._run_sawtooth_batch()

    def reduce_to_length(
        self,
        target_length: int,
        parent_builder: Callable[[SiblingPair], HMRNode],
    ) -> None:
        """Step-by-step reduction to a target node count for exact mathematical oracle checks.

        Requires an explicit parent_builder; no production shortcuts.
        """
        while len(self.view.nodes) > target_length:
            pairs = find_sibling_pairs(self.view, age_clock=self.age_clock)
            if not pairs:
                raise ValueError(
                    f"No sibling pairs available to reduce from {len(self.view.nodes)} "
                    f"to {target_length}"
                )

            best_pair = pairs[0]
            for pair in pairs[1:]:
                if pair.is_more_due_than(best_pair):
                    best_pair = pair

            parent = parent_builder(best_pair)
            k = best_pair.pair_index
            new_nodes = self.view.nodes[:k] + (parent,) + self.view.nodes[k + 2 :]
            self.view = View(
                project_id=self.project_id,
                total_messages=self.view.total_messages,
                nodes=new_nodes,
                batch_active=self.view.batch_active,
            )
