# AGENTS.md: FractalMem Autonomous Engineering Directive

**Target System:** `FractalMem` (Cache-Aligned Hierarchical Memory Engine for Long-Horizon AI Agents)  
**Workspace Root:** `/Users/heman10x/Downloads/claude_dev/personal_projects/Mind-Palace/OptChat`  
**Target Runtimes:** Claude Code, Cursor, Codex, Open-Source Agent Frameworks (via Model Context Protocol)

---

## 1. Prime Mission & Architectural Grounding

You are an unyielding Principal Systems Architect and Staff SWE. Your mission is to implement **FractalMem v2.1** end-to-end: a production-grade, prefix-cache-invariant hierarchical agent memory engine.

FractalMem solves two terminal problems in long-context coding agents:
1. **Context Rot & Compaction Loss:** Linear compaction permanently deletes low-frequency symbols (variable names, flags, errors). FractalMem solves this with a **Hierarchical Multi-Resolution Segment Tree (HMR-Tree)** backed by an append-only event log.
2. **Prompt Cache Evaporation (Prefix Thrashing):** Unstable tree updates rewrite the prefix, causing 100% prompt cache misses. FractalMem solves this with the **Terminal Message Age Invariant** and **Sawtooth Batch Merging (64 KB → 128 KB → 64 KB)**, ensuring a **98.6% turn cache hit rate**.

Consult these specifications in this directory before writing code:
- **Architecture Spec:** [`FRACTALMEM_SPECIFICATION.md`](file:///Users/heman10x/Downloads/claude_dev/personal_projects/Mind-Palace/OptChat/FRACTALMEM_SPECIFICATION.md)
- **Taelin's UniiChat Rev 3 Gist Breakdown:** [`SPECIFICATION.md`](file:///Users/heman10x/Downloads/claude_dev/personal_projects/Mind-Palace/OptChat/SPECIFICATION.md)
- **Academic Proofs & Hypotheses:** [`RESEARCH_PAPER_PROPOSAL.md`](file:///Users/heman10x/Downloads/claude_dev/personal_projects/Mind-Palace/OptChat/RESEARCH_PAPER_PROPOSAL.md)
- **GTM & Benchmark Harness Strategy:** [`LAUNCH_AND_BENCHMARK_STRATEGY.md`](file:///Users/heman10x/Downloads/claude_dev/personal_projects/Mind-Palace/OptChat/LAUNCH_AND_BENCHMARK_STRATEGY.md)

---

## 2. Unbreakable Engineering Invariants

1. **The Terminal Message Age Formula (Strict Math Invariant):**
   - **NEVER** calculate merge urgency from the pair's first message (`(T - first)/2^l` is mathematically broken and trashes the prompt cache at a 97.6% failure rate).
   - **ALWAYS** measure merge urgency strictly from the pair's **LAST** message:
     $$\text{due} = \frac{T - \text{last}}{2^l} = \frac{T + 1}{2^l} - i$$
     where $(l, i)$ and $(l, i+1)$ are sibling lines covering up to $\text{last} = (i+2) \cdot 2^l - 1$.
2. **Sawtooth Batch Merging Invariant:**
   - Between merges, new messages **only append to the tail**. The previous view is a 100% byte-exact prefix of the new view.
   - Do **NOT** merge on every turn. Accumulate appends until the view reaches **128,000 bytes**.
   - Trigger a single batch merge collapsing the most due pairs until the view is at most **64,000 bytes** (averaging ~96 KB).
3. **Prefix-Sharing Compactor Invariant:**
   - Background compaction calls must share the **exact same constant tool definitions and system prompt prefix** as main turns (`[tools] [system prompt] [<chat> view </chat>] [task]`).
   - This guarantees a **96.2% cache hit rate** on background compactor calls.
4. **Tri-Path Retrieval Seam (No Pure-Tree Needles):**
   - Do NOT force agents to do 4–6 sequential `zoom` calls to find variable names or error hashes.
   - Support SQLite FTS5 (BM25 tuned for code tokens) + Tree-sitter AST symbol indexing so needles resolve in **1 tool call ($O(1)$)**, returning both the match and its parent HMR-Tree handle.
5. **Single-Writer Concurrency (`fractald` Daemon):**
   - Agent hooks and subagents must never write directly to SQLite (avoids `SQLITE_BUSY`).
   - Multi-agent hooks communicate with `fractald` over a non-blocking Unix Domain Socket (UDS) with a **15ms fail-open SLA**.
   - `fractald` buffers events into an in-memory MPSC lock-free ring buffer (16k capacity) and commits via SQLite WAL mode in asynchronous batches.
6. **Dual-LLM Nonce Framing & Secret Redaction:**
   - Envelop untrusted tool outputs in cryptographically generated nonces: `<<<EXTERNAL_UNTRUSTED_NONCE_<hex>>>>`.
   - Scrub credentials using AST config parsing, Gitleaks regex patterns, and Shannon entropy ($H \ge 4.5$) before writing to disk or sending to background LLMs.

---

## 3. Target Codebase Structure to Implement

Create and build the following physical directory layout inside this folder:

```
/Users/heman10x/Downloads/claude_dev/personal_projects/Mind-Palace/OptChat/
├── AGENTS.md                               # This autonomous agent directive
├── pyproject.toml                          # Poetry/Hatch/Pip build config
├── fractalmem/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── tree.py                         # HMR-Tree, due = (T-last)/2^l math, Sawtooth batching
│   │   ├── node.py                         # Segment tree node representation & 512B byte ruler
│   │   └── view.py                         # view.json persistence, cache block alignment
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── db.py                           # SQLite WAL connection, migrations, PRAGMAs
│   │   ├── schema.sql                      # DDL for messages, tree_nodes, ast_symbols, fts5
│   │   └── fts.py                          # BM25 code tokenization and symbol indexing
│   ├── daemon/
│   │   ├── __init__.py
│   │   ├── server.py                       # fractald UDS server, non-blocking 15ms fail-open
│   │   ├── ring_buffer.py                  # In-memory MPSC lock-free event ring buffer
│   │   └── worker.py                       # Async batch writer and compactor queue
│   ├── security/
│   │   ├── __init__.py
│   │   ├── redact.py                       # AST lexer, Gitleaks regex, Shannon entropy (H >= 4.5)
│   │   └── guard.py                        # Dual-LLM nonce framing and prompt injection sanitizer
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── server.py                       # Stdio MCP Server (timeline_overview, inspect_node, search_symbols_and_text)
│   └── client.py                           # CLI hook client (<15ms fail-open socket client)
├── benchmarks/
│   ├── __init__.py
│   ├── eval_harness.py                     # LoCoMo and LongMemEval dataset ingestion & test runner
│   ├── cache_tracer.py                     # Provider cache-hit validation (Anthropic/OpenAI)
│   └── datasets/                           # Benchmark sample fixtures
└── tests/
    ├── test_math_invariants.py             # Unit tests for (T-last)/2^l vs binary rollback push
    ├── test_sawtooth.py                    # 64KB -> 128KB -> 64KB cache stability simulations
    ├── test_storage_concurrency.py         # Multi-process write stress test under UDS daemon
    ├── test_mcp_tools.py                   # MCP stdio tool contracts
    └── test_redaction.py                   # Secret scrubbing & injection nonce defenses
```

---

## 4. Phase-by-Phase Execution Roadmap

### Phase 1: Core Mathematical Engine & Invariants
- Implement `fractalmem/core/tree.py`:
  - Calculate pair due score: `due = (T - last) / (1 << level)`.
  - Maintain the active view list of nodes covering $[0, T)$.
  - Implement `append_message(msg)` and `sawtooth_batch_merge(view, max_bytes=128000, target_bytes=64000)`.
- Write `tests/test_math_invariants.py`:
  - Run the 20,001-step simulation comparing `(T - last)/2^l` against true binary counter rollback push. Confirm 100% agreement.

### Phase 2: SQLite WAL Storage & FTS5 Indexing
- Implement `fractalmem/storage/db.py` and `schema.sql`:
  - Enable WAL mode (`PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;`).
  - Tables: `messages`, `tree_nodes`, `ast_symbols`, and virtual table `fts_messages USING fts5`.
- Implement `fractalmem/storage/fts.py`:
  - Parse code identifiers (camelCase, snake_case, file paths) and index into FTS5.
  - Implement `search(query)` returning symbol occurrences with their parent tree node IDs.

### Phase 3: The `fractald` Daemon & Concurrency Engine
- Implement `fractalmem/daemon/ring_buffer.py`:
  - In-memory thread-safe / lock-free ring buffer (16,384 event capacity).
- Implement `fractalmem/daemon/server.py`:
  - Unix Domain Socket (`~/.fractalmem/fractald.sock`).
  - Asynchronous background worker polling the ring buffer and executing batch SQLite commits.
- Implement `fractalmem/client.py`:
  - Fast client with 15ms socket timeout and fail-open exception handling.

### Phase 4: Native Model Context Protocol (MCP) Server
- Implement `fractalmem/mcp/server.py` using Python `mcp` SDK over `stdio`:
  - Expose tool `timeline_overview()`: returns current epoch summaries and view status.
  - Expose tool `inspect_node(node_id, depth)`: drills down $n/2$ children or fetches raw messages.
  - Expose tool `search_symbols_and_text(query)`: instant $O(1)$ search over FTS5 and symbol table.

### Phase 5: Security Sanitizer & Automated Benchmark Harness
- Implement `fractalmem/security/redact.py`:
  - Regex patterns for API keys, AWS keys, GitHub tokens, PEM certificates.
  - Shannon entropy calculation ($H \ge 4.5$ on strings $>20$ chars).
- Implement `benchmarks/eval_harness.py`:
  - Automated evaluation harness capable of loading multi-turn dialogues and measuring:
    1. Exact token consumption per turn.
    2. Prompt cache read rate across Anthropic/OpenAI headers.
    3. Retrieval precision and latency.

---

## 5. Quality & Verification Gates

Before declaring any phase complete:
1. **Physical Verification:** Execute `pytest` and confirm all tests pass.
2. **No Hallucinated Benchmarks:** Benchmark reports generated by `eval_harness.py` must print actual JSON/CSV logs from physical runs.
3. **Strict Type Annotations:** Code must pass `mypy --strict` or standard Python type checking.
4. **Zero Production Leaks:** Run `test_redaction.py` against dummy credentials to verify zero leakage.
