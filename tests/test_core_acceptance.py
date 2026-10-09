from __future__ import annotations

import pytest

from fractalmem.core.node import HMRNode
from fractalmem.core.tree import HMRTreeEngine
from fractalmem.core.view import View, ViewPersistenceError


def test_no_force_override_and_invalid_append_state() -> None:
    engine = HMRTreeEngine()
    with pytest.raises(TypeError):
        engine.sawtooth_batch_merge(force=True)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        engine.append_message("x", status="bogus")


def test_snapshot_pair_boolean_cannot_impersonate_integer() -> None:
    snapshot = View("default", 1, (HMRNode.create_leaf(0, "x"),)).to_dict()
    snapshot["view_pairs"] = [[False, False]]
    with pytest.raises(ViewPersistenceError):
        View.from_dict(snapshot)


def test_incomplete_batch_preserves_visible_prefix_until_target_reached() -> None:
    from fractalmem.core.node import HMRNode
    from fractalmem.core.tree import InMemoryNodeStore

    store = InMemoryNodeStore()
    engine = HMRTreeEngine(
        project_id="atomic", max_view_bytes=400, target_view_bytes=100, parent_resolver=store.get
    )
    for _ in range(6):
        engine.append_message("x" * 80)
    before = engine.view.render_buffer()
    assert engine.view.batch_active
    store.put(HMRNode.create(1, 0, "short", project_id="atomic"))
    assert not engine.resume_batch()
    assert engine.view.render_buffer() == before
    engine.append_message("tail")
    assert engine.view.render_buffer().startswith(before)
    for index in range(3):
        store.put(HMRNode.create(1, index, "short", project_id="atomic"))
    assert engine.resume_batch()
    assert engine.view.byte_size <= 100


def test_heap_batches_match_independent_full_scan_priority_reference() -> None:
    import random

    from fractalmem.core.node import HMRNode
    from fractalmem.core.tree import find_sibling_pairs
    from fractalmem.core.view import View

    rng = random.Random(44)
    for count in (64, 111, 256, 517):
        nodes = tuple(
            HMRNode.create_leaf(i, "x" * rng.randint(30, 200), project_id="heap")
            for i in range(count)
        )
        parents = {}
        for level in range(1, count.bit_length()):
            for index in range(count // (1 << level)):
                parents[level, index] = HMRNode.create(
                    level, index, f"summary_{level}_{index}", project_id="heap"
                )
        original = View("heap", count, nodes, batch_active=True)
        target = original.byte_size // 3
        reference = original
        while reference.byte_size > target:
            pairs = find_sibling_pairs(reference)
            if not pairs:
                break
            best = pairs[0]
            for candidate in pairs[1:]:
                if candidate.is_more_due_than(best):
                    best = candidate
            parent = parents[best.parent_level, best.parent_index]
            position = best.pair_index
            reference = View(
                "heap",
                count,
                reference.nodes[:position] + (parent,) + reference.nodes[position + 2 :],
                batch_active=True,
            )
        lookups = []

        def resolve(
            level: int,
            index: int,
            project: str,
            captured_lookups: list[tuple[int, int]] = lookups,
            captured_parents: dict[tuple[int, int], HMRNode] = parents,
        ) -> HMRNode | None:
            captured_lookups.append((level, index))
            return captured_parents.get((level, index))

        engine = HMRTreeEngine(
            project_id="heap",
            view=original,
            max_view_bytes=original.byte_size + 1,
            target_view_bytes=target,
            parent_resolver=resolve,
        )
        assert engine.resume_batch()
        assert engine.view.nodes == reference.nodes
        assert len(lookups) <= 3 * count  # Guards against per-merge full-frontier resolver scans.


def test_transactional_append_batch_validation_and_atomic_failure() -> None:
    from fractalmem.core.node import HMRNode

    engine = HMRTreeEngine(project_id="bulk")
    engine.append_leaf_nodes(
        [HMRNode.create_leaf(i, f"event{i}", project_id="bulk") for i in range(20)]
    )
    before = engine.view
    with pytest.raises(ValueError):
        engine.append_leaf_nodes([HMRNode.create_leaf(22, "gap", project_id="bulk")])
    assert engine.view == before
    engine.append_leaf_nodes([])
    assert engine.view == before


def test_ready_registry_nodes_cannot_be_replaced() -> None:
    from fractalmem.core.node import HMRNode
    from fractalmem.core.tree import InMemoryNodeStore

    store = InMemoryNodeStore()
    ready = HMRNode.create_leaf(0, "immutable")
    store.put(ready)
    store.put(ready)
    with pytest.raises(ValueError):
        store.put(HMRNode.create_leaf(0, "mutated"))
