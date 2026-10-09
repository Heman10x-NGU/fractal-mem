"""Lightweight event contract shared by hooks and the writer.

This module intentionally uses only the standard library so a hook does not
load SQLite, YAML, Tree-sitter or the MCP SDK before connecting.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

MAX_EVENT_BYTES = 262_144
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
KINDS = frozenset(("user", "unii", "tool", "echo", "work", "note"))
LANGUAGES = ("python", "javascript", "typescript", "tsx")
CREDENTIAL_PREFIX = re.compile(
    r"^(?:gh[pousr]_|github_pat_|sk-|[rs]k_(?:live|test)_|glpat-|npm_|xox[baprs]-|AIza|(?:AKIA|ASIA)[A-Z0-9]{16}$)"
)


def validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{field} must be 1..128 ASCII letters, digits, dots, underscores or hyphens"
        )
    entropy = 0.0
    if len(value) > 20:
        entropy = -sum(
            (count / len(value)) * math.log2(count / len(value))
            for count in Counter(value).values()
        )
    if CREDENTIAL_PREFIX.search(value) or entropy >= 4.5:
        raise ValueError(f"{field} resembles a credential and cannot be persisted")


@dataclass(frozen=True, slots=True)
class Event:
    project_id: str
    session_id: str
    event_id: str
    kind: str
    payload: str
    language: str | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("project_id", self.project_id),
            ("session_id", self.session_id),
            ("event_id", self.event_id),
        ):
            validate_identifier(value, field)
        if self.kind not in KINDS:
            raise ValueError("invalid event kind")
        if self.language is not None and self.language not in LANGUAGES:
            raise ValueError("unsupported AST language")
        if not isinstance(self.payload, str) or len(self.payload.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("event payload exceeds 262144 UTF-8 bytes or is not text")
