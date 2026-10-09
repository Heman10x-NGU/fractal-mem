"""HMR-Tree math, due score calculations, sibling pairs, and Sawtooth batch merging."""

from __future__ import annotations

import threading
from collections.abc import Callable
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


def find_sibling_pairs(view: View) -> list[SiblingPair]:
    """Find all valid sibling pairs in the current frontier view."""
    nodes = view.nodes
    pairs: list[SiblingPair] = []
    t = view.total_messages

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
            self._nodes[(node.project_id, node.level, node.index_in_level)] = node

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

        self.project_id = clean_project
        self.max_view_bytes = max_view_bytes
        self.target_view_bytes = target_view_bytes
        self.parent_resolver = parent_resolver

        self.view: View = view if view is not None else View.empty(project_id=self.project_id)

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
        """Execute or continue a batch merge down towards target_view_bytes.

        Returns True if batch completed (view.byte_size <= target_view_bytes),
        False if batch remains active (e.g. pending parents).
        """
        while self.view.byte_size > self.target_view_bytes:
            pairs = find_sibling_pairs(self.view)
            if not pairs:
                break

            # Find all pairs with ready parents
            candidate_pairs: list[tuple[SiblingPair, HMRNode]] = []
            for pair in pairs:
                parent = self._resolve_parent(pair)
                if parent is not None and parent.status == "ready":
                    candidate_pairs.append((pair, parent))

            if not candidate_pairs:
                # Cannot merge any more pairs because parents are pending or unbuilt
                break

            # Select the pair with highest due score (oldest tie-break)
            best_pair, best_parent = candidate_pairs[0]
            for pair, parent in candidate_pairs[1:]:
                if pair.is_more_due_than(best_pair):
                    best_pair = pair
                    best_parent = parent

            # Apply merge to frontier
            k = best_pair.pair_index
            new_nodes = self.view.nodes[:k] + (best_parent,) + self.view.nodes[k + 2 :]
            self.view = View(
                project_id=self.project_id,
                total_messages=self.view.total_messages,
                nodes=new_nodes,
                batch_active=True,
            )

        is_completed = self.view.byte_size <= self.target_view_bytes
        self.view = View(
            project_id=self.project_id,
            total_messages=self.view.total_messages,
            nodes=self.view.nodes,
            batch_active=not is_completed,
        )
        return is_completed

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
            pairs = find_sibling_pairs(self.view)
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
