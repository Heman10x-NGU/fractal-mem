"""Tests for the Sawtooth batch merging algorithm, exact prefix caching, and byte budgets."""

from __future__ import annotations

import concurrent.futures

import pytest

from fractalmem.core.node import (
    PENDING_SUMMARY_PLACEHOLDER,
    RULER_512,
    RULER_512_LINES,
    ByteBudgetExceededError,
    HMRNode,
    NodeValidationError,
    format_compression_prompt,
    format_merge_prompt,
    truncate_to_utf8_bytes,
)
from fractalmem.core.tree import (
    HMRTreeEngine,
    InMemoryNodeStore,
)
from fractalmem.core.view import View


def test_512_dash_ruler_exact_format() -> None:
    """Verify that the 512-dash ruler has exactly 512 dashes formatted per specification."""
    assert len(RULER_512_LINES) == 6
    assert all(len(line) == 100 for line in RULER_512_LINES[:5])
    assert len(RULER_512_LINES[5]) == 12

    total_dashes = sum(len(line) for line in RULER_512_LINES)
    assert total_dashes == 512
    assert RULER_512.count("-") == 512

    comp_prompt = format_compression_prompt(42, "user", "Hello world")
    assert RULER_512 in comp_prompt
    assert "one line of at most 512 bytes" in comp_prompt
    assert "<input>\nuser: Hello world\n</input>" in comp_prompt

    merge_prompt = format_merge_prompt("0+2", "2+2", "line a", "line b", 0, 3)
    assert RULER_512 in merge_prompt
    assert "merge lines 0+2 and 2+2" in merge_prompt


def test_utf8_byte_budget_and_truncation_limits() -> None:
    """Verify UTF-8 multi-byte encoding checks and truncation bounds."""
    jp_text = "こんにちは世界"  # 7 chars, 21 bytes
    assert len(jp_text.encode("utf-8")) == 21
    truncated_jp = truncate_to_utf8_bytes(jp_text, max_bytes=10)
    assert len(truncated_jp.encode("utf-8")) == 9
    assert truncated_jp == "こんに"

    emoji_text = "🚀" * 150  # 600 bytes
    truncated_emoji = truncate_to_utf8_bytes(emoji_text, max_bytes=512)
    assert len(truncated_emoji.encode("utf-8")) == 512
    assert truncated_emoji == "🚀" * 128

    # Truncation helper must validate max_bytes is positive integer
    with pytest.raises(ValueError, match="max_bytes must be a positive integer"):
        truncate_to_utf8_bytes("test", max_bytes=0)
    with pytest.raises(ValueError, match="max_bytes must be a positive integer"):
        truncate_to_utf8_bytes("test", max_bytes=-5)


def test_node_factories_reject_silent_truncation() -> None:
    """Regression test for Item 1: factories for ready nodes must reject oversize summaries."""
    oversize = "a" * 600

    # HMRNode.create_leaf must raise ByteBudgetExceededError, not silently truncate
    with pytest.raises(ByteBudgetExceededError):
        HMRNode.create_leaf(0, oversize)

    with pytest.raises(ByteBudgetExceededError):
        HMRNode.create(level=0, index_in_level=0, summary_text=oversize, status="ready")

    # Pending appends must render the stable placeholder, not arbitrary raw text
    pending_leaf = HMRNode.create_leaf(0, oversize, status="pending")
    assert pending_leaf.status == "pending"
    assert pending_leaf.summary_text == PENDING_SUMMARY_PLACEHOLDER
    assert pending_leaf.byte_size == len(PENDING_SUMMARY_PLACEHOLDER.encode())


def test_invalid_node_inputs_rejected() -> None:
    """Regression test for Item 3: invalid statuses, kinds, booleans, bounds, and projects."""
    # Invalid status
    with pytest.raises(NodeValidationError, match="Invalid status 'bogus'"):
        HMRNode.create(level=0, index_in_level=0, summary_text="ok", status="bogus")  # type: ignore[arg-type]

    # Invalid kind
    with pytest.raises(NodeValidationError, match="Invalid kind 'bogus'"):
        HMRNode.create(level=0, index_in_level=0, summary_text="ok", kind="bogus")  # type: ignore[arg-type]

    # Reject bool for integer fields
    with pytest.raises(NodeValidationError, match="level must be an integer, got bool"):
        HMRNode.create(level=True, index_in_level=0, summary_text="ok")

    with pytest.raises(NodeValidationError, match="index_in_level must be an integer, got bool"):
        HMRNode.create(level=0, index_in_level=False, summary_text="ok")

    # Bounded level 0..62
    with pytest.raises(NodeValidationError, match="level must be <= 62"):
        HMRNode.create(level=63, index_in_level=0, summary_text="ok")

    with pytest.raises(NodeValidationError, match="level must be >= 0"):
        HMRNode.create(level=-1, index_in_level=0, summary_text="ok")

    # Non-empty scoped project ID
    with pytest.raises(NodeValidationError, match="project_id must be a non-empty string"):
        HMRNode.create(level=0, index_in_level=0, summary_text="ok", project_id="")

    with pytest.raises(NodeValidationError, match="project_id must be a non-empty string"):
        HMRNode.create(level=0, index_in_level=0, summary_text="ok", project_id="   ")

    # Engine threshold validations
    with pytest.raises(ValueError, match="max_view_bytes must be a positive integer"):
        HMRTreeEngine(max_view_bytes=0, target_view_bytes=10)

    with pytest.raises(ValueError, match="target_view_bytes must be a positive integer"):
        HMRTreeEngine(max_view_bytes=20, target_view_bytes=-5)

    with pytest.raises(ValueError, match="strictly greater than"):
        HMRTreeEngine(max_view_bytes=50, target_view_bytes=50)

    # Engine and view project ID mismatch
    mismatched_view = View.empty(project_id="project_a")
    with pytest.raises(ValueError, match="does not match engine project_id"):
        HMRTreeEngine(project_id="project_b", view=mismatched_view)


def test_newline_sanitization_in_nodes() -> None:
    """Verify that node creation collapses newlines to preserve single-line view invariant."""
    multi_line = "First line.\nSecond line.\r\nThird line."
    node = HMRNode.create(level=0, index_in_level=0, summary_text=multi_line)
    assert "\n" not in node.summary_text
    assert "\r" not in node.summary_text
    assert node.summary_text == "First line. Second line. Third line."

    with pytest.raises(NodeValidationError):
        HMRNode(
            level=0,
            index_in_level=0,
            start_seq=0,
            end_seq=0,
            summary_text="Illegal\nline",
            byte_size=len(b"Illegal\nline"),
            status="ready",
        )


def test_exact_prefix_cache_invariance_between_batches() -> None:
    """Verify that between batch merges, new view buffers strictly append to previous buffers."""
    store = InMemoryNodeStore()

    def test_resolver(level: int, index: int, project_id: str) -> HMRNode | None:
        return store.get(level, index, project_id)

    engine = HMRTreeEngine(
        project_id="test_cache",
        max_view_bytes=1000,
        target_view_bytes=500,
        parent_resolver=test_resolver,
    )

    prev_buffer_bytes: bytes = b""
    turns_checked = 0

    for i in range(20):
        # Pre-populate parent nodes in store so when batch triggers, it can resolve ready parents
        if i % 2 == 1:
            left_seq = i - 1
            right_seq = i
            p = HMRNode.create(
                level=1,
                index_in_level=left_seq // 2,
                summary_text=f"Summary[{left_seq}..{right_seq}]",
                project_id="test_cache",
            )
            store.put(p)

        prev_buffer = engine.view.render_buffer(include_header=False)
        prev_buffer_bytes = prev_buffer.encode()

        engine.append_message(text=f"Turn message {i} content.")
        new_buffer = engine.view.render_buffer(include_header=False)
        new_buffer_bytes = new_buffer.encode()

        if len(new_buffer_bytes) > len(prev_buffer_bytes) and new_buffer_bytes.startswith(
            prev_buffer_bytes
        ):
            turns_checked += 1

    assert turns_checked >= 15, f"Expected predominantly append-only turns, got {turns_checked}"


def test_public_batch_api_thresholds_and_resumption() -> None:
    """Regression test for Item 6: exact 128000 byte refusal, first append batch to <=64000, and resume_batch."""
    store = InMemoryNodeStore()

    def resolver(level: int, index: int, project_id: str) -> HMRNode | None:
        return store.get(level, index, project_id)

    max_bytes = 128_000
    target_bytes = 64_000

    engine = HMRTreeEngine(
        project_id="test_128k",
        max_view_bytes=max_bytes,
        target_view_bytes=target_bytes,
        parent_resolver=resolver,
    )

    # 1. Refusal to resume when no batch is active
    with pytest.raises(ValueError, match="No active batch merge to resume"):
        engine.resume_batch()

    # 2. Craft a view with exactly 128,000 serialized bytes
    # 250 level-0 nodes with line_byte_size = 512 each: 250 * 512 = 128,000 bytes
    nodes: list[HMRNode] = []
    for i in range(250):
        handle = f"{i}+1"
        prefix_len = len(handle.encode()) + 1 + 1  # handle + '|' + '\n'
        needed_summary = 512 - prefix_len
        text = "x" * needed_summary
        node = HMRNode.create_leaf(i, text, project_id="test_128k")
        nodes.append(node)

    view_128k = View(project_id="test_128k", total_messages=250, nodes=tuple(nodes))
    assert view_128k.byte_size == 128_000
    engine.view = view_128k

    # Exactly 128000 bytes: calling sawtooth_batch_merge() must be refused
    with pytest.raises(ValueError, match="Cannot begin batch merge: view size 128000 bytes is below or equal"):
        engine.sawtooth_batch_merge()

    # 3. First append exceeding 128000 bytes with pending parents
    # Parents are not yet in store, so batch merge triggers, but cannot collapse -> batch_active becomes True
    engine.append_message("Message 250 pushing over 128KB")
    assert engine.view.byte_size > 128_000
    assert engine.view.batch_active is True

    # 4. Now populate ready parents in store (Level 1 and Level 2)
    for i in range(0, 250, 2):
        p1 = HMRNode.create(
            level=1,
            index_in_level=i // 2,
            summary_text=f"Summary L1 for {i}..{i+1}",
            project_id="test_128k",
        )
        store.put(p1)

    for i in range(0, 125, 2):
        p2 = HMRNode.create(
            level=2,
            index_in_level=i // 2,
            summary_text=f"Summary L2 for {i*2}..{(i+1)*2+1}",
            project_id="test_128k",
        )
        store.put(p2)

    # 5. Call public resume_batch() to resume the unfinished batch
    resumed_done = engine.resume_batch()
    assert resumed_done is True
    assert engine.view.byte_size <= target_bytes
    assert engine.view.batch_active is False


def test_in_memory_node_store_concurrency() -> None:
    """Regression test for Item 7: InMemoryNodeStore thread-safety and type enforcement."""
    store = InMemoryNodeStore()

    # Type enforcement
    with pytest.raises(TypeError, match="Expected HMRNode"):
        store.put("not_a_node")  # type: ignore[arg-type]

    # Multi-threaded concurrent writes
    def worker(worker_id: int) -> None:
        for i in range(50):
            node = HMRNode.create(
                level=0,
                index_in_level=worker_id * 100 + i,
                summary_text=f"Worker {worker_id} msg {i}",
                project_id="conc_test",
            )
            store.put(node)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(worker, w) for w in range(4)]
        concurrent.futures.wait(futures)

    assert len(store) == 200
    for w in range(4):
        for i in range(50):
            n = store.get(0, w * 100 + i, "conc_test")
            assert n is not None
            assert n.summary_text == f"Worker {w} msg {i}"
