"""Tests for atomic view persistence, frontier validation, and round-trip byte preservation."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from fractalmem.core.node import HMRNode
from fractalmem.core.view import (
    VIEW_SNAPSHOT_VERSION,
    View,
    ViewPersistenceError,
    ViewValidationError,
)


def test_empty_view_persistence(tmp_path: Path) -> None:
    """Verify that an empty view round-trips correctly through atomic persistence."""
    view_path = tmp_path / "view.json"
    view = View.empty(project_id="test_empty")
    view.save_atomic(view_path)

    assert view_path.exists()
    loaded = View.load_atomic(view_path)
    assert loaded.project_id == "test_empty"
    assert loaded.total_messages == 0
    assert len(loaded.nodes) == 0
    assert loaded.batch_active is False
    assert loaded.byte_size == 0


def test_view_roundtrip_preserves_exact_rendered_bytes(tmp_path: Path) -> None:
    """Verify that a complex multi-level view with Unicode strings preserves exact bytes across reload."""
    view_path = tmp_path / "view.json"

    n0 = HMRNode.create(
        level=3,
        index_in_level=0,
        summary_text="Epoch 1: Initialization with λ=0.01 and α=0.9.",
        project_id="proj_alpha",
    )
    n1 = HMRNode.create(
        level=2,
        index_in_level=2,
        summary_text="Step 8-11: Data ingested successfully 🚀 こんにちは世界.",
        project_id="proj_alpha",
    )
    n2 = HMRNode.create(
        level=1,
        index_in_level=6,
        summary_text="Step 12-13: Validation loss reached 0.042.",
        project_id="proj_alpha",
    )
    n3 = HMRNode.create_leaf(seq=14, text="Turn 14 user query.", project_id="proj_alpha")
    n4 = HMRNode.create_leaf(seq=15, text="Turn 15 unii response.", project_id="proj_alpha")

    original_view = View(
        project_id="proj_alpha",
        total_messages=16,
        nodes=(n0, n1, n2, n3, n4),
        batch_active=True,
    )

    original_bytes = original_view.byte_size
    original_rendered = original_view.render_buffer()

    original_view.save_atomic(view_path)
    loaded_view = View.load_atomic(view_path)

    assert loaded_view.project_id == original_view.project_id
    assert loaded_view.total_messages == original_view.total_messages
    assert loaded_view.batch_active is True
    assert loaded_view.byte_size == original_bytes
    assert loaded_view.render_buffer() == original_rendered
    assert len(loaded_view.nodes) == len(original_view.nodes)

    for orig_node, loaded_node in zip(original_view.nodes, loaded_view.nodes, strict=True):
        assert loaded_node.level == orig_node.level
        assert loaded_node.index_in_level == orig_node.index_in_level
        assert loaded_node.start_seq == orig_node.start_seq
        assert loaded_node.end_seq == orig_node.end_seq
        assert loaded_node.summary_text == orig_node.summary_text
        assert loaded_node.byte_size == orig_node.byte_size
        assert loaded_node.status == orig_node.status
        assert loaded_node.node_id == orig_node.node_id


def test_corrupt_snapshot_coercions_rejected(tmp_path: Path) -> None:
    """Regression test for Item 5: reject string/bool/int coercions and malformed structures."""
    valid_view = View(
        project_id="test_coercion",
        total_messages=1,
        nodes=(HMRNode.create_leaf(0, "valid text", project_id="test_coercion"),),
        batch_active=False,
    )
    snapshot = valid_view.to_dict()

    # 1. Non-dict root
    with pytest.raises(ViewPersistenceError, match="Snapshot root must be a dict"):
        View.from_dict(["not", "a", "dict"])

    # 2. String 'false' for batch_active must NOT become True
    bad_batch = dict(snapshot)
    bad_batch["batch_active"] = "false"
    with pytest.raises(ViewPersistenceError, match="batch_active must be a bool"):
        View.from_dict(bad_batch)

    # 3. Numeric 0 for batch_active must NOT be accepted
    bad_batch_num = dict(snapshot)
    bad_batch_num["batch_active"] = 0
    with pytest.raises(ViewPersistenceError, match="batch_active must be a bool"):
        View.from_dict(bad_batch_num)

    # 4. Bool for total_messages must NOT be coerced to int
    bad_total = dict(snapshot)
    bad_total["total_messages"] = True
    with pytest.raises(ViewPersistenceError, match="total_messages must be a non-negative integer"):
        View.from_dict(bad_total)

    # 5. Missing required field
    bad_missing = dict(snapshot)
    del bad_missing["rendered_bytes"]
    with pytest.raises(ViewPersistenceError, match="missing required fields"):
        View.from_dict(bad_missing)

    # 6. Unknown node status
    bad_node_status: dict[str, Any] = json.loads(json.dumps(snapshot))
    bad_node_status["nodes"][0]["status"] = "bogus"
    with pytest.raises(ViewPersistenceError, match="status invalid"):
        View.from_dict(bad_node_status)

    # 7. Mismatched view_pairs
    bad_pairs: dict[str, Any] = json.loads(json.dumps(snapshot))
    bad_pairs["view_pairs"] = [[99, 99]]
    with pytest.raises(ViewPersistenceError, match="view_pairs does not match reconstructed nodes"):
        View.from_dict(bad_pairs)

    # 8. Mismatched rendered_bytes
    bad_bytes = dict(snapshot)
    bad_bytes["rendered_bytes"] = 999999
    with pytest.raises(ViewPersistenceError, match="View rendered bytes mismatch upon reload"):
        View.from_dict(bad_bytes)


def test_unsupported_version_rejection(tmp_path: Path) -> None:
    """Verify that a snapshot with an incompatible version is rejected."""
    view_path = tmp_path / "view.json"
    n0 = HMRNode.create_leaf(seq=0, text="Message 0")
    view = View(project_id="default", total_messages=1, nodes=(n0,))
    view.save_atomic(view_path)

    with open(view_path, encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = VIEW_SNAPSHOT_VERSION + 1
    with open(view_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    with pytest.raises(ViewPersistenceError, match="Unsupported view snapshot version"):
        View.load_atomic(view_path)


def test_frontier_validation_gap() -> None:
    """Verify that a sequence gap in the frontier raises ViewValidationError."""
    n0 = HMRNode.create(level=1, index_in_level=0, summary_text="covers [0, 1]")
    n1 = HMRNode.create(level=1, index_in_level=2, summary_text="covers [4, 5]")

    with pytest.raises(ViewValidationError, match="Frontier gap or overlap"):
        View(project_id="default", total_messages=6, nodes=(n0, n1))


def test_frontier_validation_overlap() -> None:
    """Verify that an overlapping sequence in the frontier raises ViewValidationError."""
    n0 = HMRNode.create(level=1, index_in_level=0, summary_text="covers [0, 1]")
    n1 = HMRNode.create_leaf(seq=0, text="covers [0, 0]")

    with pytest.raises(ViewValidationError, match="Frontier gap or overlap"):
        View(project_id="default", total_messages=2, nodes=(n0, n1))


def test_frontier_validation_start_not_zero() -> None:
    """Verify that a frontier not starting at 0 raises ViewValidationError."""
    n0 = HMRNode.create_leaf(seq=1, text="starts at 1")
    with pytest.raises(ViewValidationError, match="Frontier gap or overlap"):
        View(project_id="default", total_messages=2, nodes=(n0,))


def test_frontier_validation_end_mismatch() -> None:
    """Verify that total_messages not matching last node end_seq raises ViewValidationError."""
    n0 = HMRNode.create_leaf(seq=0, text="covers [0, 0]")
    with pytest.raises(ViewValidationError, match="Frontier end mismatch"):
        View(project_id="default", total_messages=5, nodes=(n0,))


def test_random_valid_frontier_coverage(tmp_path: Path) -> None:
    """Property test: generate random valid binary segment partitions and verify persistence."""
    rng = random.Random(42)

    def generate_random_valid_tree(start: int, count: int) -> list[tuple[int, int]]:
        if count == 0:
            return []
        segments: list[tuple[int, int]] = []
        curr = start
        remaining = count
        while remaining > 0:
            max_level = 0
            while (1 << (max_level + 1)) <= remaining and (curr % (1 << (max_level + 1)) == 0):
                max_level += 1
            level = rng.randint(0, min(max_level, 4))
            span = 1 << level
            idx = curr // span
            segments.append((level, idx))
            curr += span
            remaining -= span
        return segments

    for test_idx in range(20):
        t = rng.randint(1, 200)
        segments = generate_random_valid_tree(0, t)

        nodes: list[HMRNode] = []
        for level, idx in segments:
            node = HMRNode.create(
                level=level,
                index_in_level=idx,
                summary_text=f"Node L{level} I{idx} rand_{test_idx}",
                project_id="rand_test",
            )
            nodes.append(node)

        view = View(
            project_id="rand_test",
            total_messages=t,
            nodes=tuple(nodes),
            batch_active=rng.choice([True, False]),
        )
        assert view.total_messages == t
        assert view.nodes[0].start_seq == 0
        assert view.nodes[-1].end_seq == t - 1

        p = tmp_path / f"view_{test_idx}.json"
        view.save_atomic(p)
        loaded = View.load_atomic(p)
        assert loaded.byte_size == view.byte_size
        assert loaded.render_buffer() == view.render_buffer()
