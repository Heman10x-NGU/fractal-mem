"""Marker-aware summary budgeting and strict UTF-8 length enforcement."""

from __future__ import annotations

from fractalmem.core.node import sanitize_summary_line
from fractalmem.security.redact import REDACTED_PATTERN, Redactor

_SUMMARY_DELIMS: str = " \t\n\r\"'\\=:;,"


class SummaryBudgetError(RuntimeError):
    """Raised when a summary candidate cannot meet the byte budget or stability contract."""


def validate_summary_candidate(
    candidate: str, budget: int = 512, redactor: Redactor | None = None
) -> bool:
    """Return True if candidate meets the byte budget and redactor fixed-point contract."""
    if not candidate or len(candidate.encode("utf-8")) > budget:
        return False
    if redactor is None:
        redactor = Redactor()
    return redactor.redact(candidate) == candidate and sanitize_summary_line(candidate) == candidate


def _marker_spans(text: str) -> list[tuple[int, int]]:
    """Return the start and end byte offsets of all complete redaction markers in text.

    Computes byte offsets incrementally in a single linear pass over the text.
    """
    spans: list[tuple[int, int]] = []
    last_char = 0
    current_byte_offset = 0
    for match in REDACTED_PATTERN.finditer(text):
        m_start_char = match.start()
        m_end_char = match.end()
        current_byte_offset += len(text[last_char:m_start_char].encode("utf-8"))
        start_byte = current_byte_offset
        marker_byte_len = len(text[m_start_char:m_end_char].encode("utf-8"))
        end_byte = start_byte + marker_byte_len
        spans.append((start_byte, end_byte))
        current_byte_offset = end_byte
        last_char = m_end_char
    return spans


def budget_sanitized_summary(
    text: str, budget: int = 512, redactor: Redactor | None = None
) -> str:
    """Iteratively sanitize, redact, and budget text to <= budget UTF-8 bytes.

    Guarantees:
    1. Returns a single-line string whose UTF-8 byte length is <= budget (32..512).
    2. Idempotent under redactor: redactor.redact(result) == result.
    3. Never splits redaction markers or leaves dangling quotes/assignments that expand upon re-redaction.
    4. Safe for all node levels (both level 0 leaves and higher-level parent nodes).
    5. Preserves legitimate bracketed code and identifiers in extractive tails.
    6. Candidate is non-empty: empty or whitespace-only inputs fail the candidate contract and raise SummaryBudgetError.
    """
    if not 32 <= budget <= 512:
        raise ValueError("summary budget must be 32..512 bytes")
    if redactor is None:
        redactor = Redactor()

    # Initial sanitization and redaction
    current = sanitize_summary_line(redactor.redact(text))
    if validate_summary_candidate(current, budget=budget, redactor=redactor):
        return current

    raw = current.encode("utf-8")

    # Extractive head ... tail split using exact marker byte spans
    marker = " … "
    marker_bytes = len(marker.encode("utf-8"))
    if len(raw) > budget and budget > marker_bytes + 16:
        rem = budget - marker_bytes
        head_bytes = rem * 2 // 3
        tail_bytes = rem - head_bytes

        spans = _marker_spans(current)
        # Avoid cutting inside a marker for head
        cut_head_bytes = head_bytes
        for m_start, m_end in spans:
            if m_start < cut_head_bytes < m_end:
                cut_head_bytes = m_start
                break
        head = raw[:cut_head_bytes].decode("utf-8", errors="ignore").rstrip(_SUMMARY_DELIMS)

        # Avoid cutting inside a marker for tail
        tail_start = len(raw) - tail_bytes
        for m_start, m_end in spans:
            if m_start < tail_start < m_end:
                tail_start = m_end
                break
        tail = raw[tail_start:].decode("utf-8", errors="ignore").lstrip(_SUMMARY_DELIMS)

        candidate = sanitize_summary_line(redactor.redact(head + marker + tail))
        if validate_summary_candidate(candidate, budget=budget, redactor=redactor):
            return candidate
        current = candidate

    # Monotonic bounded reduction loop with strictly decreasing source-cut budget
    for target in range(budget, 31, -8):
        raw = current.encode("utf-8")
        if len(raw) <= target:
            re_redacted = redactor.redact(current)
            if validate_summary_candidate(re_redacted, budget=budget, redactor=redactor):
                return re_redacted
            current = sanitize_summary_line(re_redacted)
            if validate_summary_candidate(current, budget=budget, redactor=redactor):
                return current

        # Cut strictly to target using exact marker spans
        spans = _marker_spans(current)
        cut_len = min(len(current.encode("utf-8")), target)
        for m_start, m_end in spans:
            if m_start < cut_len < m_end:
                cut_len = m_start
                break
        trimmed = current.encode("utf-8")[:cut_len].decode("utf-8", errors="ignore").rstrip(_SUMMARY_DELIMS)
        current = sanitize_summary_line(redactor.redact(trimmed))
        if validate_summary_candidate(current, budget=budget, redactor=redactor):
            return current

    # Final candidate validation
    if validate_summary_candidate(current, budget=budget, redactor=redactor):
        return current

    raise SummaryBudgetError(
        f"unable to budget summary to <= {budget} bytes with redactor fixed-point stability"
    )
