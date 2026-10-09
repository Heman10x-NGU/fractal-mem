"""One-call code-aware BM25 and real Tree-sitter symbol retrieval."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

SupportedLanguage = Literal["python", "javascript", "typescript", "tsx"]
LANGUAGES: tuple[str, ...] = ("python", "javascript", "typescript", "tsx")
TOKEN = re.compile(r"[\w$]+(?:[./:@#-][\w$]+)*", re.UNICODE)
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
FENCE = re.compile(r"```(python|py|javascript|js|typescript|ts|tsx)\s*\n(.*?)```", re.DOTALL)
ALIASES = {"py": "python", "js": "javascript", "ts": "typescript"}
STOPWORDS = frozenset(
    "a an the is are was were do does did what when where who which why how in on of to for and or with my i me it at as be has have had".split()
)


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: str
    language: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    project_id: str
    session_id: str
    seq: int
    snippet: str
    score: float
    node_id: str
    parent_handle: str
    symbol: str | None = None
    source: str = "text"


def code_tokens(text: str) -> str:
    result: list[str] = []
    for match in TOKEN.finditer(text):
        token = match.group()
        result.append(token)
        for part in re.split(r"[_./:@#-]+", token):
            result.extend(CAMEL.split(part))
    return " ".join(result)


def _language(name: str) -> Language:
    if name == "python":
        return Language(tree_sitter_python.language())
    if name == "javascript":
        return Language(tree_sitter_javascript.language())
    if name == "typescript":
        return Language(tree_sitter_typescript.language_typescript())
    if name == "tsx":
        return Language(tree_sitter_typescript.language_tsx())
    raise ValueError("unsupported AST language")


def extract_symbols(text: str, language: str | None = None) -> list[Symbol]:
    """Parse supplied language or fenced code; prose remains on the FTS path."""
    blocks: list[tuple[str, str, int]] = []
    if language is not None:
        if language not in LANGUAGES:
            raise ValueError("unsupported AST language")
        blocks.append((language, text, 0))
    else:
        for match in FENCE.finditer(text):
            name = ALIASES.get(match.group(1), match.group(1))
            blocks.append((name, match.group(2), len(text[: match.start(2)].encode("utf-8"))))
    symbols: list[Symbol] = []
    for name, source, offset in blocks:
        raw = source.encode("utf-8")
        root = Parser(_language(name)).parse(raw).root_node
        stack: list[Node] = [root]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.named_children))
            symbol_node: Node | None = None
            kind = ""
            if node.type in ("function_definition", "function_declaration", "method_definition"):
                symbol_node, kind = node.child_by_field_name("name"), "function"
            elif node.type in (
                "class_definition",
                "class_declaration",
                "interface_declaration",
                "type_alias_declaration",
            ):
                symbol_node, kind = node.child_by_field_name("name"), "class"
            elif node.type in ("assignment", "variable_declarator"):
                symbol_node = node.child_by_field_name("left") or node.child_by_field_name("name")
                kind = "variable"
            if symbol_node is not None and symbol_node.type in (
                "identifier",
                "type_identifier",
                "property_identifier",
            ):
                value = raw[symbol_node.start_byte : symbol_node.end_byte].decode("utf-8")
                symbols.append(
                    Symbol(
                        value,
                        kind,
                        name,
                        offset + symbol_node.start_byte,
                        offset + symbol_node.end_byte,
                    )
                )
    return symbols


def index_message(
    connection: sqlite3.Connection,
    row_id: int,
    project_id: str,
    seq: int,
    payload: str,
    language: str | None,
) -> None:
    connection.execute(
        "INSERT INTO fts_messages(rowid,project_id,seq,body,identifiers) VALUES(?,?,?,?,?)",
        (row_id, project_id, seq, payload, code_tokens(payload)),
    )
    connection.executemany(
        "INSERT INTO ast_symbols(project_id,message_seq,symbol_name,symbol_type,language,start_byte,end_byte) VALUES(?,?,?,?,?,?,?)",
        [
            (project_id, seq, s.name, s.kind, s.language, s.start_byte, s.end_byte)
            for s in extract_symbols(payload, language)
        ],
    )


def _hit(
    connection: sqlite3.Connection,
    project: str,
    row: sqlite3.Row,
    score: float,
    symbol: str | None,
    terms: list[str] | None = None,
) -> SearchHit:
    seq = int(row["seq"])
    ancestor = connection.execute(
        "SELECT node_id,start_seq,end_seq FROM active_frontier WHERE project_id=? AND start_seq<=? ORDER BY start_seq DESC LIMIT 1",
        (project, seq),
    ).fetchone()
    if ancestor is None or int(ancestor["end_seq"]) < seq:
        raise RuntimeError("search index has no covering active node")
    payload = str(row["payload"])
    if symbol:
        position = payload.find(symbol)
        start = max(position - 100, 0)
    else:
        lowered = payload.lower()
        positions = [lowered.find(term.lower()) for term in (terms or [])]
        found = [position for position in positions if position >= 0]
        start = max(min(found) - 100, 0) if found else 0
    return SearchHit(
        project_id=project,
        session_id=str(row["session_id"]),
        seq=seq,
        snippet=payload[start : start + 1200],
        score=score,
        node_id=str(ancestor["node_id"]),
        parent_handle=f"{ancestor['start_seq']}+{int(ancestor['end_seq']) - int(ancestor['start_seq']) + 1}",
        symbol=symbol,
        source="symbol" if symbol else "text",
    )


def search(
    connection: sqlite3.Connection, project_id: str, query: str, limit: int = 10
) -> list[SearchHit]:
    if not 1 <= limit <= 100:
        raise ValueError("search limit must be between 1 and 100")
    if not query.strip() or len(query.encode("utf-8")) > 4096:
        raise ValueError("query must contain 1 to 4096 UTF-8 bytes")
    exact = connection.execute(
        "SELECT m.* FROM ast_symbols s JOIN messages m ON m.project_id=s.project_id AND m.seq=s.message_seq WHERE s.project_id=? AND s.symbol_name=? GROUP BY m.id ORDER BY m.seq DESC LIMIT ?",
        (project_id, query.strip(), limit),
    ).fetchall()
    hits = [_hit(connection, project_id, row, 0.0, query.strip()) for row in exact]
    seen = {hit.seq for hit in hits}
    if len(hits) >= limit:
        return hits
    terms = [t for t in TOKEN.findall(code_tokens(query)) if t.lower() not in STOPWORDS]
    # Treat punctuation/FTS operators as data. OR supplies candidate recall for
    # natural-language questions; exact symbols take precedence above.
    terms = list(dict.fromkeys(terms))[:32]
    if not terms:
        return hits
    expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    rows = connection.execute(
        "SELECT m.*,bm25(fts_messages,0.0,0.0,1.0,4.0) AS score FROM fts_messages JOIN messages m ON m.id=fts_messages.rowid WHERE fts_messages MATCH ? AND m.project_id=? ORDER BY score,m.seq DESC LIMIT ?",
        (expression, project_id, limit + len(seen)),
    ).fetchall()
    for row in rows:
        if int(row["seq"]) not in seen:
            hits.append(_hit(connection, project_id, row, float(row["score"]), None, terms))
            seen.add(int(row["seq"]))
        if len(hits) >= limit:
            break
    return hits
