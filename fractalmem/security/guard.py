"""Cryptographic framing for untrusted external text.

Framing establishes provenance and an instruction boundary. It is not an
injection-proof parser or an authorization system. Compactors must have no
tool execution capability and must retain this provenance downstream.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from fractalmem.security.redact import MAX_INPUT_BYTES, RedactionError

INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above|system)\s+(?:instructions|rules|prompts)"
    ),
    re.compile(
        r"(?i)(?:reveal|print|exfiltrate)\s+(?:the\s+)?(?:system\s+prompt|api\s+key|secrets|passwords)"
    ),
    re.compile(r"(?i)(?:you\s+are\s+now|act\s+as)\s+(?:the\s+)?(?:system|developer|administrator)"),
)


@dataclass(frozen=True, slots=True)
class FramedData:
    nonce: str
    content: str
    taint: str = "UNTRUSTED_EXTERNAL"

    @property
    def opening(self) -> str:
        return f"<<<EXTERNAL_UNTRUSTED_NONCE_{self.nonce}>>>"

    @property
    def closing(self) -> str:
        return f"<<<END_EXTERNAL_UNTRUSTED_NONCE_{self.nonce}>>>"

    @property
    def text(self) -> str:
        return f"{self.opening}\n{self.content}\n{self.closing}"


def sanitize_injection(text: str) -> str:
    """Label a limited set of overt override attempts, retaining surrounding data."""
    for pattern in INJECTION_PATTERNS:
        text = pattern.sub("[Blocked injection attempt: untrusted instruction]", text)
    return text


def frame_untrusted(text: str, *, sanitize: bool = False) -> FramedData:
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise RedactionError("external text exceeds framing byte limit")
    if sanitize:
        text = sanitize_injection(text)
    # A nonce is generated per request and never controlled by external input.
    # Retry boundedly rather than accepting a collision with the current content.
    for _ in range(8):
        nonce = secrets.token_hex(32)
        if nonce not in text:
            return FramedData(nonce=nonce, content=text)
    raise RedactionError("could not generate a distinct untrusted-data boundary")
