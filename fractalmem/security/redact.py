"""Redact before persistence and model dispatch.

Provider signatures cover a documented subset of Gitleaks-style patterns,
not the full upstream ruleset. Configuration values are located with real
Python AST and YAML syntax nodes (YAML also accepts JSON). Offsets preserve
the surrounding source text. Entropy is a final heuristic, not a proof that
a string is or is not a secret.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from fractalmem.security.gitleaks import redact_catalog

MAX_INPUT_BYTES = 1_048_576
MAX_CONFIG_DEPTH = 64
REDACTED_PATTERN = re.compile(r"\[REDACTED_SECRET_[0-9a-f]{16}\]")
SENSITIVE_KEY = re.compile(
    r"token|secret|password|passwd|pwd|credential|api[_-]?key|private[_-]?key|"
    r"access[_-]?key|authorization|key",
    re.IGNORECASE,
)

# Patterns are deliberately bounded and have no nested repeating groups.
# Contextual values, including low-entropy passwords, are handled separately.
PROVIDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem",
        re.compile(
            r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|CERTIFICATE)-----[\s\S]*?"
            r"(?:-----END (?:[A-Z ]*PRIVATE KEY|CERTIFICATE)-----|\Z)"
        ),
    ),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,255}\b")),
    ("github-fine-grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,255}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}\b")),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b")),
    ("stripe-secret", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,255}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,255}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,255}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,8192}\."
            r"[A-Za-z0-9_-]{8,2048}\b"
        ),
    ),
)

ASSIGNMENT = re.compile(
    r"[\"']?(?P<key>[A-Za-z_][\w.-]*)[\"']?\s*(?:=|:)\s*"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;#\n]+)"
)
BEARER = re.compile(r"(?i)\bbearer\s+(?P<value>[A-Za-z0-9._~+/-]{4,8192}=*)")
ENTROPY_TOKEN = re.compile(r"(?<![\w])([A-Za-z0-9_+/=-]{21,})(?![\w])")


class RedactionError(ValueError):
    """Input cannot safely be processed within the configured bounds."""


def shannon_entropy(value: str) -> float:
    """Shannon entropy in bits per character, with H(empty)=0."""
    if not value:
        return 0.0
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in Counter(value).values())


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY.search(key))


def _marker(value: str) -> str:
    if REDACTED_PATTERN.fullmatch(value):
        return value
    fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"[REDACTED_SECRET_{fingerprint}]"


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    replacement: str


def _replace_spans(text: str, spans: list[_Span]) -> str:
    """Apply outermost spans once; nested secret structures become one marker."""
    result: list[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if span.start < cursor:
            continue
        result.append(text[cursor : span.start])
        result.append(span.replacement)
        cursor = span.end
    result.append(text[cursor:])
    return "".join(result)


def _yaml_spans(text: str) -> list[_Span]:
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError, ValueError):
        return []  # Arbitrary prose is not a YAML document; other detectors still apply.
    if root is None:
        return []
    spans: list[_Span] = []
    seen: set[int] = set()
    stack: list[tuple[Node, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_CONFIG_DEPTH:
            raise RedactionError("configuration exceeds maximum nesting depth")
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if isinstance(key, ScalarNode) and is_sensitive_key(key.value):
                    start, end = value.start_mark.index, value.end_mark.index
                    original = text[start:end]
                    # Quoted YAML/JSON value remains valid even for block/container values.
                    marker = _marker(value.value if isinstance(value, ScalarNode) else original)
                    if isinstance(value, ScalarNode) and REDACTED_PATTERN.fullmatch(value.value):
                        continue
                    anchor = re.match(r"&[A-Za-z0-9_-]+\s+", original)
                    prefix = anchor.group() if anchor else ""
                    spans.append(_Span(start, end, f'{prefix}"{marker}"'))
                    # Alias source locations belong to the defining value, so secret aliases
                    # also scrub that original source rather than leaving a cleartext anchor.
                else:
                    stack.append((value, depth + 1))
                    stack.append((key, depth + 1))
        elif isinstance(node, SequenceNode):
            stack.extend((child, depth + 1) for child in node.value)
    return spans


def _target_sensitive(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return is_sensitive_key(node.id)
    if isinstance(node, ast.Attribute):
        return is_sensitive_key(node.attr)
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return isinstance(node.slice.value, str) and is_sensitive_key(node.slice.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(_target_sensitive(child) for child in node.elts)
    return False


def _python_spans(text: str) -> list[_Span]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    raw_lines = text.encode("utf-8").splitlines(keepends=True)
    offsets = [0]
    for line in raw_lines:
        offsets.append(offsets[-1] + len(line))
    raw = text.encode("utf-8")
    spans: list[_Span] = []

    def add(value: ast.expr) -> None:
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and REDACTED_PATTERN.fullmatch(value.value)
        ):
            return
        if value.end_lineno is None or value.end_col_offset is None:
            raise RedactionError("Python AST lacks source boundaries")
        start_byte = offsets[value.lineno - 1] + value.col_offset
        end_byte = offsets[value.end_lineno - 1] + value.end_col_offset
        start = len(raw[:start_byte].decode("utf-8"))
        end = start + len(raw[start_byte:end_byte].decode("utf-8"))
        source = text[start:end]
        secret = (
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else source
        )
        spans.append(_Span(start, end, json.dumps(_marker(secret))))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(_target_sensitive(t) for t in node.targets):
            add(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and _target_sensitive(node.target):
            if node.value is not None:
                add(node.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and is_sensitive_key(key.value)
                ):
                    add(value)
        elif isinstance(node, ast.keyword) and node.arg is not None and is_sensitive_key(node.arg):
            add(node.value)
    return spans


@dataclass(frozen=True, slots=True)
class Redactor:
    """Bounded syntax/signature/entropy redactor, safe to reuse across batches."""

    entropy_threshold: float = 4.5
    max_input_bytes: int = MAX_INPUT_BYTES

    def __post_init__(self) -> None:
        if not math.isfinite(self.entropy_threshold) or not 0 <= self.entropy_threshold <= 8:
            raise ValueError("entropy threshold must be finite and between 0 and 8")
        if self.max_input_bytes <= 0 or self.max_input_bytes > MAX_INPUT_BYTES:
            raise ValueError("input byte limit must be between 1 and 1048576")

    def redact(self, text: str) -> str:
        if len(text.encode("utf-8")) > self.max_input_bytes:
            raise RedactionError("input exceeds redaction byte limit")
        # Provider keys first, including multi-line PEM before syntax parsers see it.
        clean = text
        for _, pattern in PROVIDER_PATTERNS:
            clean = pattern.sub(lambda match: _marker(match.group()), clean)
        # Run AST before YAML: Python offsets are exact and preserve source spelling.
        clean = _replace_spans(clean, _python_spans(clean))
        clean = _replace_spans(clean, _yaml_spans(clean))

        def assignment(match: re.Match[str]) -> str:
            if not is_sensitive_key(match.group("key")):
                return match.group()
            value = match.group("value")
            unquoted = (
                value[1:-1]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
                else value
            )
            if REDACTED_PATTERN.fullmatch(unquoted):
                return match.group()
            prefix = match.group()[: match.start("value") - match.start()]
            return prefix + '"' + _marker(unquoted) + '"'

        clean = ASSIGNMENT.sub(assignment, clean)
        clean = BEARER.sub(lambda match: "Bearer " + _marker(match.group("value")), clean)

        def entropy(match: re.Match[str]) -> str:
            value = match.group()
            if re.fullmatch(r"(?:END_)?EXTERNAL_UNTRUSTED_NONCE_[0-9a-f]{64}", value):
                return value
            # Markers and conventional hex hashes do not match H>=4.5.
            return _marker(value) if shannon_entropy(value) >= self.entropy_threshold else value

        clean = ENTROPY_TOKEN.sub(entropy, clean)
        try:
            return redact_catalog(clean, _marker, shannon_entropy)
        except TimeoutError as error:
            raise RedactionError("credential scan exceeded processing budget") from error
