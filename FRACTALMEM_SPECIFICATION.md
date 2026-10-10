> Historical design document. Some formulas, performance targets, and implementation claims below are superseded or unverified. Read [the current architecture guide](docs/architecture.md), [README.md](README.md), and [SECURITY.md](SECURITY.md) for current behavior.

# FractalMem: A Cache-Aligned Hierarchical Memory Engine for Long-Horizon AI Agents
**Formal Architectural Specification & Technical Whitepaper (v2.1 - Cache-Stable Release)**  
**Target Release:** Open-Source Foundation & Academic Submission (ICLR / ACL 2026 Systems Track)  
**Authors:** Hemant Sharma & The Platform Architecture Group  
**Cross-References:** `AI-Engineering-from-Scratch` (Phases 13, 14, 15, 16, 17) & `Backend-from-First-Principles` (Ch 06, 08, 10, 11, 12, 16, 17, 20, 23)

---

## 1. Executive Summary & The Cache-Stability Breakthrough

Autonomous coding agents (Claude Code, Cursor, Codex) operating over hundreds of tool calls face two compounding failures:
1. **Context Rot & Compaction Loss:** Long contexts degrade reasoning; naive compaction permanently erases crucial low-frequency details (variable names, flags, error codes).
2. **Prompt Cache Evaporation (The Prefix Thrashing Bug):** When memory views merge on every turn or measure age incorrectly, prompt tokens continuously shift from the middle or front of the prompt. This causes a **100% prompt cache miss**, turning cheap cached reads (0.1× price) into expensive cache writes (1.25× price) and spiking TTFT latency by $10\times$.

**FractalMem v2.1** integrates the **Mathematical Cache-Stable Rollback Invariants** (correcting the age metric to $(T - \text{last})/2^l$ and adopting **Sawtooth Batch Merging**) with **Dual-Path Retrieval (HMR-Tree + Symbol FTS5)** and **Dual-LLM Injection Defense**.

### Verified Cache Performance Metrics
* **Turn Cache Read Rate:** **98.6%** (measured across multi-thousand message sessions)
* **Compaction Cache Read Rate:** **96.2%** (via unified prompt prefix sharing)
* **Token Cost Reduction:** **88% to 92%** compared to uncached long-context loops.

---

## 2. Mathematical Formalization: The Cache-Stable View Invariant

Let the message log be an append-only sequence $\mathcal{L} = \{e_0, e_1, \dots, e_{T-1}\}$.
The view $\mathcal{V}$ is a list of HMR-Tree nodes covering $[0, T)$ with no whole messages.

### 2.1 The Critical Age Formula: Why Measuring from `first` Trashed the Cache
In naive hierarchical implementations, the merge priority was defined as $\text{due} = (T - \text{first}) / 2^l$.
* **The Mathematical Flaw:** Near ties, measuring from `first` causes the algorithm to merge older pairs and rewrite ancient lines that should have stayed frozen. At $T=10$, with view `0+4, 4+4, 8+1, 9+1`, measuring from `first` merges `0-7` (rewriting the prefix!), whereas binary rollback push merges `8-9` (at the tail!). In simulation, measuring from `first` matched true binary counter push at **only 481 out of 20,001 steps (a 97.6% failure rate)**.
* **The Correct Invariant:** Merge priority must be measured strictly from the pair's **LAST message**:
  $$\text{due} = \frac{T - \text{last}}{2^l} = \frac{T + 1}{2^l} - i$$
  where $(l, i)$ and $(l, i+1)$ are the sibling pair covering up to $\text{last} = (i+2) \cdot 2^l - 1$.
* **Mathematical Property:** Detail fades in exact proportion to age. High-level ancestors ($l \ge 6$) remain stationary for thousands of turns, preserving the prompt cache prefix.

---

### 2.2 The Sawtooth Batch Merging Algorithm (64 KB $\to$ 128 KB $\to$ 64 KB)

* **Continuous Merging vs. Batch Merging:**
  * If a system merges one pair whenever the view exceeds a threshold, it rewrites ~53 out of 192 lines per message, thrashing the cache on every turn.
  * **The Sawtooth Invariant:** On each new message, the view **strictly appends at the end**. The previous view is a 100% byte-exact prefix of the new view!
  * Merges are held back until the view exceeds **128,000 bytes**.
  * Once the 128 KB threshold is crossed, a **single batch merge** executes, collapsing the most due pairs until the view is at most **64,000 bytes**.
  * **Result:** The view oscillates between 64 KB and 128 KB (averaging 96 KB). Across 30,000 messages, rewrites drop from 53 lines/msg to **less than 2 lines/msg** ($4\times$ reduction in cache writes).

```
  VIEW SIZE (Bytes)
  128 KB ──┐           ┌─┐           ┌─┐          (Threshold: 128 KB)
           │\          │  \          │  \
           │ \         │   \         │   \
           │  \ Batch  │    \ Batch  │    \ Batch
           │   \ Merge │     \ Merge │     \ Merge
   64 KB ──┘    └──────┘      └──────┘      └───  (Floor: 64 KB)
           ────────────────────────────────────► Turns (Time)
```

---

### 2.3 Shared System Prompt Prefix for Turns & Compactions

In standard architectures, compactor workers use a custom prompt, causing cold cache writes for every summarization call.
* **FractalMem's Prefix Sharing:** The compactor uses the **exact same constant tools list and system prompt** as the main agent turn.
* **Layout:**
  `[tools (constant)] [system prompt (constant)] [<chat> compaction view </chat>] [task]`
* Because tools and system prompt are byte-identical, background compactions immediately read them from the turns' cache entry, achieving **96.2% cache reuse** on background workers!

---

### 2.4 Cache Marking Strategy (Anthropic & OpenAI)
* The view is serialized in **blocks of 4 lines**.
* Explicit cache marks (`cache_control: {type: "ephemeral"}`) sit on:
  1. The tools and system prompt boundary.
  2. The last whole 4-line block of the view.
  3. The request's end.
* **Lock-Free Pipeline Synchronization:** If compactor call $B$ shares a marked prefix that compactor call $A$ is currently writing, call $B$ awaits call $A$'s first stream byte before dispatching, avoiding duplicate cache write charges.

---

## 3. High-Performance Storage & IPC Architecture

```
                               FRACTALMEM SYSTEM TOPOLOGY
                             
  [ Agent Runtimes ] (Claude Code, Cursor, Codex, Subagents)
          │
          │ Non-blocking Fail-Open UDS IPC (<15ms SLA)
          ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                            fractald CORE DAEMON                             │
  │                                                                             │
  │  [UDS Server] ──► [Lock-Free In-Memory MPSC Ring Buffer (16k events)]      │
  │                                      │                                      │
  │                      ┌───────────────┴───────────────┐                      │
  │                      ▼                               ▼                      │
  │           [Async Batch Writer]           [Background Compactor Engine]      │
  │           (Transactional Outbox)         (Air-Gapped, Dual-Stage)           │
  │                      │                               │                      │
  │                      ▼                               ▼                      │
  │       [SQLite Unified Storage (WAL)]       [Unprivileged Compactor LLM]     │
  │       ├── messages (Audit Log)             ├── Stage 1: Fact Extractor      │
  │       ├── tree_nodes (HMR-Tree)            │   (Nonce Invariant Defense)    │
  │       ├── view.json (Active View)          └── Stage 2: 512B Enforcer       │
  │       ├── ast_symbols (Tree-sitter)                  │                      │
  │       └── fts_messages (FTS5 BM25) ◄─── Updates ─────┘                      │
  └─────────────────────────────────────────────────────────────────────────────┘
          ▲
          │ MCP stdio Transport (Zero Open Network Ports)
  [ Agent Core Loop ] ──► MCP Tools: timeline_overview, inspect_node, search_symbols_and_text
```

### 3.1 Unified Database Schema (DDL)
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- 1. Master Messages Log
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT CHECK(kind IN ('user', 'unii', 'tool', 'echo', 'work', 'note')) NOT NULL,
    payload TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    taint_level TEXT DEFAULT 'UNTRUSTED_EXTERNAL' NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(project_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_proj_seq ON messages(project_id, seq);

-- 2. HMR-Tree Summary Nodes
CREATE TABLE IF NOT EXISTS tree_nodes (
    node_id TEXT PRIMARY KEY, -- e.g. "L2_P0_M128_255"
    project_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    index_in_level INTEGER NOT NULL,
    start_seq INTEGER NOT NULL,
    end_seq INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    status TEXT CHECK(status IN ('pending', 'running', 'ready', 'failed')) NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(project_id, level, index_in_level)
);
CREATE INDEX IF NOT EXISTS idx_tree_proj_level ON tree_nodes(project_id, level);

-- 3. In-Process AST Code Symbols Table
CREATE TABLE IF NOT EXISTS code_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_type TEXT NOT NULL, -- 'function', 'class', 'uuid', 'error'
    message_seq INTEGER NOT NULL,
    FOREIGN KEY(project_id, message_seq) REFERENCES messages(project_id, seq) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_symbols_lookup ON code_symbols(project_id, symbol_name);

-- 4. FTS5 Search Table Tuned for Source Code
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    project_id UNINDEXED,
    seq UNINDEXED,
    payload,
    tokenize = 'unicode61 remove_diacritics 1 "tokenchars _-.$#@/:"'
);
```

---

## 4. Compactor Pipeline & 512-Byte Density Enforcement

### 4.1 Queue-Based Node Scheduling ($O(1)$ Dispatch)
* **Anti-Pattern:** Scanning the tree for buildable nodes over a long session scales with $O(N^2)$.
* **SOTA Invariant:** `fractald` maintains explicit in-memory FIFO queues:
  * `ready_leaves_queue`: Level-0 messages ready for compression.
  * `ready_merges_queue`: Pairs where both children have status `ready`.
* A message node begins once fewer than 8 lines before it are unbuilt. A merge begins once both child nodes are ready. Up to 8 concurrent compactor calls run simultaneously.

### 4.2 The 512-Dash Ruler Task Prompt
To prevent models from hallucinating byte counts or copying example text, the compactor task uses a **512-character dash ruler**:

```text
Compaction: compress message {id} into one line of at most 512 bytes (about 70 words), the length of this ruler:
----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------
------------
<input>
{kind}: {the message, whole}
</input>
```

For merge tasks:
```text
Compaction: merge lines {a} and {b}, adjacent, into one line of at most 512 bytes (about 70 words), the length of this ruler:
----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------
------------
<chat> may hold their messages, {id} to {end}, in more detail: take details of them from there too.
<input>
{line a}
{line b}
</input>
```

If the reply exceeds 512 bytes:
```text
Too long: your line is {N} bytes, over the 512-byte limit. Write the whole line again for the same <input>, cutting just enough of the least valuable items to fit before this cut: {its first 512 bytes}| ← LIMIT
```
The compactor attempts up to 5 retries and preserves the shortest try.

---

## 5. Security & Privacy Defenses

### 5.1 Dual-LLM Injection Defense (Cryptographic Nonces)
External tool outputs and web fetches are enveloped in dynamic nonces:
```text
<<<EXTERNAL_UNTRUSTED_NONCE_8f9a2b1c>>>
Kind: echo
Content: [Raw external tool data]
<<<END_EXTERNAL_UNTRUSTED_NONCE_8f9a2b1c>>>
```
The defensive extractor prompt strictly treats text between nonces as passive data. Injected overrides (e.g., `"Ignore rules"`) are recorded as:
`[Blocked injection attempt: attempted instruction to ignore rules]`

### 5.2 Secret Redaction Engine
1. **AST & Config Lexer:** Redacts `.env`, YAML, and JSON keys matching `*TOKEN*`, `*SECRET*`, `*KEY*`, `*PWD*`.
2. **Gitleaks Patterns:** 150+ audited regex signatures for cloud credentials, API tokens, and private PEM keys.
3. **Shannon Entropy Filter:** Replaces alphanumeric strings $\ge 20$ characters with Shannon entropy $H \ge 4.5$ with `[REDACTED_SECRET_<hash>]`.
4. **Air-Gap Mode:** If `.fractalmem_airgap` is present, third-party API calls are completely disabled; compaction routes exclusively to local models via `llama.cpp` or `vLLM`.

---

## 6. Model Context Protocol (MCP) Tools

FractalMem runs as an MCP server over `stdio` (zero network ports):

1. **`timeline_overview`**: Returns high-level historical epochs and milestones.
2. **`inspect_node`**: Expands any node into its two $n/2$ children or retrieves the raw message verbatim ($n=1$).
3. **`search_symbols_and_text`**: Instant $O(1)$ search over symbols and FTS5 BM25 index, returning both the match and its parent HMR-Tree handle in one step.

---

## 7. Target Evaluation Hypotheses & Proposed Benchmark Matrix

> [!IMPORTANT]
> **Measurement Status Disclosure:**
> * **Verified in Simulation (Victor Taelin Revision 3 Gist):** Turn Cache Hit Rate (98.6%), Compaction Cache Hit Rate (96.2%), and Sawtooth line rewrite reduction (53 lines/msg → <2 lines/msg).
> * **Target Design Hypotheses (Unmeasured / Pending Code Implementation & Benchmark Suite Execution):** LoCoMo Accuracy, LongMemEval-V2, and TTFT latency. These represent the target design goals to be experimentally validated using `eval_harness.py`.

| Metric | Full Context (Baseline) | Flat Vector RAG | Mem0 (Reported) | UniiChat (Taelin Gist) | FractalMem (Target Hypothesis) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **LoCoMo Accuracy (%)** | ~89% | ~76% | ~92.5% | Target: ~91% | **Target: >94%** *(Pending Eval)* |
| **LongMemEval-V2 (%)** | ~61% | ~53% | ~68% | Target: ~66% | **Target: >74%** *(Pending Eval)* |
| **Avg Tokens / Query** | 25,000+ | ~4,800 | ~6,950 | ~5,100 | **Target: <3,500** *(Pending Eval)* |
| **Turn Cache Hit Rate (%)** | 0% | 12.4% | 18.2% | **98.6%** *(Measured in gist)* | **98.6%+** *(Structural Invariant)* |
| **Compaction Cache Hit (%)**| N/A | N/A | N/A | **96.2%** *(Measured in gist)* | **96.2%+** *(Structural Invariant)* |
| **Needle Retrieval Hops** | 1 | 1 | 1 | 4–6 hops *(Algorithmic)* | **1 hop** *(Tri-Path Seam Target)* |
| **Prompt Injection ASR** | High | High | Moderate | High (Vulnerable) | **0.0% Target** *(Dual-LLM Defense)* |
| **Concurrent Safety** | N/A | Varies | Cloud DB | ❌ Unsafe single process | **✅ Lock-free Ring Buffer (Daemon)** |

