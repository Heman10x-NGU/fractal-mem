"""Tests for the core mathematical invariants of FractalMem v2.1.

Verifies:
1. Exact correspondence of (T - last) / 2^l with Taelin's keep/life rollback push for 20,001 steps.
2. The T=10 counterexample demonstrating why (T - first) / 2^l fails and trashes the prompt cache.
3. Strict tie-breaking favoring the oldest pair at T=31.
4. SiblingPair strict ordering (irreflexivity: pair < pair is False).
5. SiblingPair structural validation.
"""

from __future__ import annotations

from typing import Any

import pytest

from fractalmem.core.node import HMRNode
from fractalmem.core.tree import (
    HMRTreeEngine,
    SiblingPair,
    calculate_due_direct,
    find_sibling_pairs,
)
from fractalmem.core.view import View


def push_oracle(new_state: int, states: dict[str, Any] | None) -> dict[str, Any]:
    """Independent implementation of Victor Taelin's keep/life rollback push.

    Directly translated from SPECIFICATION.md (rollback_state_list.js, 2022).
    """
    if states is None:
        return {"keep": 0, "life": 0, "state": new_state, "older": None}

    keep = states["keep"]
    life = states["life"]
    state = states["state"]
    older = states["older"]

    if keep == 0:
        return {"keep": 1, "life": life, "state": state, "older": older}
    else:
        if life > 0:
            return {
                "keep": 0,
                "life": 0,
                "state": new_state,
                "older": {"keep": 0, "life": life - 1, "state": state, "older": older},
            }
        else:
            return {
                "keep": 0,
                "life": life,
                "state": new_state,
                "older": push_oracle(state, older),
            }


def get_oracle_frontier_ranges(states: dict[str, Any], t: int) -> list[tuple[int, int]]:
    """Convert linked states list into ordered (start_seq, end_seq_inclusive) intervals covering [0, t)."""
    starts: list[int] = []
    curr: dict[str, Any] | None = states
    while curr is not None:
        starts.append(int(curr["state"]))
        curr = curr["older"]
    starts.reverse()

    frontier: list[tuple[int, int]] = []
    for i in range(len(starts)):
        start = starts[i]
        next_start = starts[i + 1] if i + 1 < len(starts) else t
        frontier.append((start, next_start - 1))
    return frontier


def test_oracle_start_ids_at_specified_checkpoints() -> None:
    """Verify independent oracle start IDs at T=4, T=8, T=10, T=11 per coordinator specification."""
    states: dict[str, Any] | None = None
    records: dict[int, list[int]] = {}

    for t in range(1, 13):
        msg_id = t - 1
        states = push_oracle(msg_id, states)
        curr: dict[str, Any] | None = states
        starts: list[int] = []
        while curr is not None:
            starts.append(int(curr["state"]))
            curr = curr["older"]
        starts.reverse()
        records[t] = starts

    assert records[4] == [0, 2], f"Expected [0, 2] at T=4, got {records[4]}"
    assert records[8] == [0, 4, 6], f"Expected [0, 4, 6] at T=8, got {records[8]}"
    assert records[10] == [0, 4, 8], f"Expected [0, 4, 8] at T=10, got {records[10]}"
    assert records[11] == [0, 8, 10], f"Expected [0, 8, 10] at T=11, got {records[11]}"


def test_t10_counterexample() -> None:
    """Explicitly verify the T=10 counterexample.

    At T=10, unmerged view: [0+4, 4+4, 8+1, 9+1].
    - Naive (T - first) / 2^l prioritizes 0-7:
      (10 - 0) / 4 = 2.5 > (10 - 8) / 1 = 2.0  => rewrites the ancient prefix!
    - Direct (T - last) / 2^l prioritizes 8-9:
      (10 - 7) / 4 = 0.75 < (10 - 9) / 1 = 1.0  => merges at the tail!
    """
    t = 10
    n0 = HMRNode.create(level=2, index_in_level=0, summary_text="0-3")
    n1 = HMRNode.create(level=2, index_in_level=1, summary_text="4-7")
    n2 = HMRNode.create(level=0, index_in_level=8, summary_text="8")
    n3 = HMRNode.create(level=0, index_in_level=9, summary_text="9")

    view = View(project_id="default", total_messages=t, nodes=(n0, n1, n2, n3))
    pairs = find_sibling_pairs(view)
    assert len(pairs) == 2

    pair_0_7 = pairs[0]  # covering [0, 7]
    pair_8_9 = pairs[1]  # covering [8, 9]

    # Verify first formula calculations
    due_first_0_7 = (t - pair_0_7.start_seq) / (1 << pair_0_7.level)
    due_first_8_9 = (t - pair_8_9.start_seq) / (1 << pair_8_9.level)
    assert due_first_0_7 == (10 - 0) / 4 == 2.5
    assert due_first_8_9 == (10 - 8) / 1 == 2.0
    # Naive first would mistakenly pick pair_0_7
    assert due_first_0_7 > due_first_8_9

    # Verify direct last formula calculations
    assert pair_0_7.last_seq == 7
    assert pair_8_9.last_seq == 9
    assert pair_0_7.due_score == (10 - 7) / 4 == 0.75
    assert pair_8_9.due_score == (10 - 9) / 1 == 1.0
    assert calculate_due_direct(10, 7, 2) == 0.75
    assert calculate_due_direct(10, 9, 0) == 1.0

    # The direct terminal-age formula chooses pair_8_9
    assert pair_8_9.is_more_due_than(pair_0_7)
    assert not pair_0_7.is_more_due_than(pair_8_9)


def test_20001_steps_rollback_oracle_exact_agreement() -> None:
    """Run full 20,001 steps comparing direct terminal-age formula with rollback oracle.

    Must achieve 100% exact agreement at every single step from T=1 to T=20,001.
    """
    engine = HMRTreeEngine(project_id="default")
    oracle_state: dict[str, Any] | None = None

    def test_oracle_parent_builder(pair: SiblingPair) -> HMRNode:
        return HMRNode.create(
            level=pair.parent_level,
            index_in_level=pair.parent_index,
            summary_text=f"Summary[{pair.start_seq}..{pair.end_seq}]",
            status="ready",
            kind="summary",
            project_id=pair.left_node.project_id,
        )

    matches_last = 0
    mismatches_last = 0

    for t in range(1, 20002):
        msg_id = t - 1
        oracle_state = push_oracle(msg_id, oracle_state)
        oracle_frontier = get_oracle_frontier_ranges(oracle_state, t)
        target_len = len(oracle_frontier)

        # Append to engine
        engine.append_message(text=f"msg_{msg_id}")
        engine.reduce_to_length(target_len, parent_builder=test_oracle_parent_builder)

        # Check exact range correspondence
        engine_ranges = [(n.start_seq, n.end_seq) for n in engine.view.nodes]
        if engine_ranges == oracle_frontier:
            matches_last += 1
        else:
            mismatches_last += 1

    assert matches_last == 20001, f"Expected 20001 matches, got {matches_last}"
    assert mismatches_last == 0, f"Expected 0 mismatches, got {mismatches_last}"


def test_naive_first_formula_failure_rate_simulation() -> None:
    """Verify that (T - first) / 2^l matches the oracle in only 481 of 20,001 steps."""
    oracle_state: dict[str, Any] | None = None
    naive_view: list[tuple[int, int]] = []  # list of (start_seq, span)
    matches_first = 0

    for t in range(1, 20002):
        msg_id = t - 1
        oracle_state = push_oracle(msg_id, oracle_state)
        oracle_frontier = get_oracle_frontier_ranges(oracle_state, t)
        target_len = len(oracle_frontier)

        naive_view.append((msg_id, 1))

        while len(naive_view) > target_len:
            best_idx = -1
            best_due = -1.0
            best_start = 10**9

            for i in range(len(naive_view) - 1):
                s1, sz1 = naive_view[i]
                s2, sz2 = naive_view[i + 1]
                if sz1 == sz2 and (s1 % (2 * sz1) == 0) and s2 == s1 + sz1:
                    first = s1
                    due = (t - first) / sz1
                    if due > best_due or (due == best_due and s1 < best_start):
                        best_due = due
                        best_start = s1
                        best_idx = i

            assert best_idx != -1
            s1, sz1 = naive_view[best_idx]
            naive_view[best_idx] = (s1, sz1 * 2)
            del naive_view[best_idx + 1]

        naive_ranges = [(s, s + sz - 1) for s, sz in naive_view]
        if naive_ranges == oracle_frontier:
            matches_first += 1

    assert matches_first == 481, (
        f"Expected exactly 481 matches for (T-first)/2^l, got {matches_first}"
    )


def test_oldest_ties_preference_at_t31() -> None:
    """Verify that at T=31, tied due scores strictly prioritize the oldest pair.

    Real tie at T=31:
    - Level-2 pair covering 16..23 has due = (31 - 23) / 4 = 8 / 4 = 2.0.
    - Level-0 pair covering 28..29 has due = (31 - 29) / 1 = 2 / 1 = 2.0.
    - Older pair (start 16 < 28) strictly wins the tie break.
    """
    t = 31

    # Level-2 pair covering 16..23
    l2_left = HMRNode.create(level=2, index_in_level=4, summary_text="L2 node 16..19")
    l2_right = HMRNode.create(level=2, index_in_level=5, summary_text="L2 node 20..23")
    pair_older = SiblingPair(
        pair_index=0,
        left_node=l2_left,
        right_node=l2_right,
        total_messages=t,
    )

    # Level-0 pair covering 28..29 (left index 28 is even, valid sibling pair)
    l0_left = HMRNode.create(level=0, index_in_level=28, summary_text="L0 node 28")
    l0_right = HMRNode.create(level=0, index_in_level=29, summary_text="L0 node 29")
    pair_newer = SiblingPair(
        pair_index=3,
        left_node=l0_left,
        right_node=l0_right,
        total_messages=t,
    )

    assert pair_older.due_score == 2.0
    assert pair_newer.due_score == 2.0
    assert pair_older.due_numerator * pair_newer.due_denominator == (
        pair_newer.due_numerator * pair_older.due_denominator
    )

    # Oldest pair (start_seq 16 < 28) wins
    assert pair_older.is_more_due_than(pair_newer) is True
    assert pair_newer.is_more_due_than(pair_older) is False

    # Strict ordering: pair < pair must be FALSE (irreflexivity)
    assert (pair_older < pair_older) is False
    assert (pair_newer < pair_newer) is False

    # Greater / Less relationships:
    # pair_older has higher merge priority, so pair_newer < pair_older is True
    assert (pair_newer < pair_older) is True
    assert (pair_older > pair_newer) is True
    assert (pair_older < pair_newer) is False


def test_sibling_pair_structural_validation() -> None:
    """Verify SiblingPair rejects invalid or misaligned sibling metadata."""
    n0 = HMRNode.create(level=0, index_in_level=0, summary_text="0")
    n1 = HMRNode.create(level=0, index_in_level=1, summary_text="1")

    # Odd left index cannot be a sibling pair
    n_odd = HMRNode.create(level=0, index_in_level=1, summary_text="1")
    n_next = HMRNode.create(level=0, index_in_level=2, summary_text="2")
    with pytest.raises(ValueError, match="Left sibling index must be even"):
        SiblingPair(pair_index=0, left_node=n_odd, right_node=n_next, total_messages=3)

    # Disjoint sequence numbers
    n_gap = HMRNode.create(level=0, index_in_level=3, summary_text="3")
    with pytest.raises(ValueError, match="Right sibling must have index left \\+ 1"):
        SiblingPair(pair_index=0, left_node=n0, right_node=n_gap, total_messages=4)

    # Cross project siblings
    n_other_proj = HMRNode.create(level=0, index_in_level=1, summary_text="1", project_id="other")
    with pytest.raises(ValueError, match="Siblings must belong to same project"):
        SiblingPair(pair_index=0, left_node=n0, right_node=n_other_proj, total_messages=2)

    # Level mismatch
    n_l1 = HMRNode.create(level=1, index_in_level=1, summary_text="1")
    with pytest.raises(ValueError, match="Sibling level mismatch"):
        SiblingPair(pair_index=0, left_node=n0, right_node=n_l1, total_messages=3)

    # total_messages <= last_seq
    with pytest.raises(ValueError, match="total_messages .* must be strictly greater"):
        SiblingPair(pair_index=0, left_node=n0, right_node=n1, total_messages=1)
