"""Pinned upstream Gitleaks text rules, applied locally with bounded regex time.

This adapts the regex/keyword/secret-group/entropy text detectors; it is not
Gitleaks' Git history, path, archive or allowlist engine. Allowlists are
intentionally omitted for memory security: findings are conservatively scrubbed.
The one path-only rule does not apply to event text. Upstream MIT license and
unchanged catalog are shipped alongside this module.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import regex

CATALOG = Path(__file__).parent / "rules" / "gitleaks-v8.30.0.toml"
CATALOG_SHA256 = "e163e53b9e7e8a8511e77271e2b323ed057759542a6d988258afe3a1fa329caf"


@dataclass(frozen=True, slots=True)
class Rule:
    identifier: str
    pattern: regex.Pattern[str]
    keywords: tuple[str, ...]
    secret_group: int
    entropy: float


@lru_cache(maxsize=1)
def rules() -> tuple[Rule, ...]:
    import hashlib

    data = CATALOG.read_bytes()
    if hashlib.sha256(data).hexdigest() != CATALOG_SHA256:
        raise RuntimeError("Gitleaks catalog integrity check failed")
    entries = tomllib.loads(data.decode())["rules"]
    compiled: list[Rule] = []
    for entry in entries:
        if "regex" not in entry:  # One filename/path detector has no event-text equivalent.
            continue
        pattern = regex.compile(entry["regex"])
        group = entry.get("secretGroup", 1 if pattern.groups else 0)
        if not 0 <= group <= pattern.groups:
            raise RuntimeError("invalid Gitleaks secret group")
        compiled.append(
            Rule(
                entry["id"],
                pattern,
                tuple(k.lower() for k in entry.get("keywords", [])),
                group,
                float(entry.get("entropy", 0)),
            )
        )
    return tuple(compiled)


def redact_catalog(text: str, marker: Callable[[str], str], entropy: Callable[[str], float]) -> str:
    lowered = text.lower()
    spans: list[tuple[int, int, str]] = []
    deadline = time.monotonic() + 1.0
    # Already-scrubbed placeholders are immutable even when a generic upstream
    # detector matches a substring of their text.
    protected = [
        match.span() for match in regex.finditer(r"\[REDACTED_SECRET_[0-9a-f]{16}\]", text)
    ]
    for rule in rules():
        if rule.keywords and not any(keyword in lowered for keyword in rule.keywords):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Gitleaks text scan exceeded time budget")
        for match in rule.pattern.finditer(text, timeout=remaining):
            value = match.group(rule.secret_group)
            if not value or entropy(value) < rule.entropy:
                continue
            start, end = match.span(rule.secret_group)
            if any(start < right and end > left for left, right in protected):
                continue
            spans.append((start, end, marker(value)))
    output: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(spans, key=lambda s: (s[0], -s[1])):
        if start < cursor:
            continue
        output.extend((text[cursor:start], replacement))
        cursor = end
    output.append(text[cursor:])
    return "".join(output)
