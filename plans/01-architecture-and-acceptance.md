# FractalMem v2.1 implementation contract

Date: 2026-10-09. User authorized implementation, GitHub creation, phased commits and pushes, AGY implementation and self-review, followed by principal review.

## Current base and proposed design

The workspace initially contains seven design documents and no code or Git repository. AGENTS.md and FRACTALMEM_SPECIFICATION.md are the primary requirements; SPECIFICATION.md supplies the independent rollback reference algorithm. Build a Python package with a pure mathematical core, SQLite repositories, a single-writer local daemon, read-only stdio MCP, security at ingress, provider compaction adapters, and reproducible benchmark CLIs.

An all-in-one script makes lifecycle and persistence defects difficult to isolate. A distributed database and queue introduce unnecessary operational dependencies for a local engine. Choose typed modules around the prescribed embedded SQLite and UDS boundaries. Every alternative decision must document correctness, maintainability, latency, and failure recovery tradeoffs.

## Required mathematical corrections and evidence limits

* T is the count of events, spanning [0,T); last is an inclusive index.
* A sibling pair at level l with left index i ends at (i+2)*2**l-1. The exact due score is (T-last)/2**l = (T+1)/2**l-i-2. The specification's final expression is shifted by +2, which preserves ordering but is not literal equality. Implement the direct terminal-age expression. Compare ratios by integer cross multiplication where useful; ties choose the oldest pair.
* Test 20,001 actual pushes against the original keep/life rollback algorithm from SPECIFICATION.md, independently implemented in the test. Do not substitute a standard binary representation that has a different rollback policy.
* One MCP call is a round-trip property; FTS and B-tree queries are not constant-time algorithms. Publish measured latency and corpus size.
* 98.6% and 96.2% are inherited reference claims, not guaranteed provider outcomes. Prefix bytes, token cache reads, cache writes, request hit rates, and answer accuracy are different metrics.
* Python has no portable compare-and-swap primitive for a general MPSC queue. Use a bounded nonblocking thread-safe ring with short critical sections and document that it is not formally lock-free. Never rely on the GIL to make compound operations atomic.
* A 15ms client deadline bounds transport waiting, not OS scheduling. Report measured p50/p95/p99, failures, and queue drops. Receipt means volatile queue admission unless a durable receipt is explicitly awaited.
* Nonces and prompt instructions are defense layers, not a proof of zero injection attacks. Preserve taint; no tool execution in compaction.

## Phase 1: Pure core

Implement immutable nodes with scoped IDs, level/index/range validation, UTF-8 byte budgets and 512 dash ruler. Active frontier covers [0,T) exactly. A stable line renderer has append-only bytes between batches; avoid a closing chat tag in the compared view buffer. Full prompts may wrap that buffer. Count serialized view bytes including IDs and separators. Merge only above 128000, down to <=64000. If parent nodes are pending, persist batch-active state and resume only that unfinished batch. Ready summaries must never silently replace existing prefix lines between batches. Atomic versioned view.json snapshots validate ranges and retain exact rendered bytes across reload. Persistence is a disposable export of the authoritative transactional database snapshot once phase 2 exists; reconcile stale exports before use.

Gate: pytest for 20,001 rollback steps, T=10 counterexample, oldest ties, random frontier coverage, Unicode limits, threshold boundaries, pending parents, persistence and exact prefix comparisons.

## Phase 2: Storage and tri-path retrieval

SQLite WAL, NORMAL synchronous by default (explicit durability tradeoff), foreign keys, migrations/user_version, append-only message enforcement. Scope all IDs, queries and unique constraints by project. Assign sequence in the writer transaction, never client-side. Store event_id for idempotent retries; reject a duplicate identity with different content. Persist nodes, child relationships, active view and compaction jobs transactionally. Readers use mode=ro and query_only and cannot create missing databases. FTS5 weighted BM25 over original code tokens plus snake/camel/path components. Parameterize SQL, quote FTS queries as data, cap query length/result size. Real Tree-sitter indexing for Python and JS/TS; preserve names, type, ranges, message ID and active ancestor handle. Unsupported syntax has an explicit text-only path. Search hits include actual snippet, node handle, session, sequence and score. No full corpus scans for exact symbol lookup.

Gate: real disk DB, WAL, restart, append-only restrictions, project isolation, duplicate IDs, FTS punctuation/camel/path queries, actual AST definitions and active parent mapping after merges.

## Phase 3: Daemon and client

Single owner lock for each database/socket; refuse a second writer before opening mutable storage. Socket directory 0700 and socket 0600; do not delete a live socket or unrelated file. Versioned bounded newline JSON protocol; strict kind, string, size and ID validation. Multiple producers, one writer, 16384-event queue, bounded overload and explicit rejection. Deadline-based nonblocking connect/send/read client with one monotonic 15ms transport budget, bounded payload and fail-open result. Batched work runs outside the socket event loop. Redact before any disk/log/provider use. Drain on SIGTERM; recover running jobs on restart. Worker failure is visible and rejects new admission; never acknowledge unpersisted messages as durable. CLI includes daemon run, ingest, health/status and a durable flush barrier with a separate timeout.

Gate: actual UDS subprocess with multiprocess producers; restart, idempotency, saturation, failed writer, malformed input, partial frames, lock exclusion, shutdown drain, client deadline and disk evidence.

## Phase 4: MCP

Use official mcp SDK and stdio. Register exactly timeline_overview, inspect_node and search_symbols_and_text with typed input and output, explicit project scope, bounded depth/results, safe errors, and read-only connection lifecycle. stdout contains protocol only; diagnostics go to stderr. Leaf inspection returns redacted original payload and taint. Missing/cross-project handles fail explicitly.

Gate: actual subprocess using SDK stdio_client, list_tools and call_tool for all three, incorrect input, unknown/cross-project IDs, Unicode and missing database behavior.

## Phase 5: Security and actual compaction

Typed ingress sanitizer handles dotenv/config assignments and Python AST, nested JSON and YAML secret keys, known API/cloud/GitHub/PEM patterns plus Shannon H>=4.5 for strings >20. Do not pretend a handful of regexes is the complete Gitleaks catalog; describe coverage and pin any imported rules/license. Stable non-reversible redaction markers; scrub before persistence and before provider dispatch, including provider replies. Crypto nonce boundaries wrap external data, preserve taint and do not let attacker-supplied delimiter text terminate the generated frame.

Shared PromptPrefix supplies byte-identical tools/system for main and compactor calls; task/context follow it. Expose provider-neutral prompt construction plus real configured HTTP adapters (Anthropic/OpenAI-compatible local). Two-stage fact extraction and byte enforcement, <=5 retry attempts, bounded concurrency <=8, child-ready queues and durable jobs. Oversize failures remain explicit; no silent loss of raw source. Offline deterministic extraction is a documented usable mode, never described as LLM reasoning. Airgap rejects all remote endpoints, including redirects, and permits only configured local endpoints. Never silently fallback to cloud.

Gate: dummy secrets absent from DB/WAL/files and outgoing captured HTTP payload, nonce collision/injection vectors, config nesting, provider response parsing, retry budgets, prefix identity, no tools, job restart and airgap.

## Phase 6: Benchmarks, docs and release review

Actual JSON/CSV outputs include command/config, UTC run time, revision, platform, corpus/fixture hashes and measured values. Local disk suite: 20001 mathematical steps, >=30000-message sawtooth run, actual SQLite search throughput/latency, UDS concurrency/latency, restart and redaction. LoCoMo and cleaned LongMemEval adapters preserve IDs, dates, evidence and categories. Ground truth must never be indexed or included in model context. Clearly label synthetic regression fixtures. Measure evidence precision/recall separately from answer accuracy. Token counts use a named tokenizer; exact provider usage only from real responses. Missing provider cache/TTFT metrics are null/unmeasured, never zero or fabricated. Full benchmark downloads and answer generation are opt-in with explicit model and dataset identity.

README: install, daemon, ingest, MCP config examples, project scope, offline/provider compaction, airgap, quickstart, testing, benchmark commands, limitations and source attribution. Include CI pytest + strict mypy + lint, packaging/wheel check, LICENSE and security guidance. Original proposals remain historical documents; explicitly disavow their unexecuted accuracy numbers.

## Review and commit protocol

AGY implements one assigned phase, physically runs pytest, mypy --strict and lint, performs adversarial self-review and fixes findings, then reports exact commands/results and stops. Root reviews code and real behavior, records findings, requests fixes, verifies, then creates one meaningful commit and pushes it once. Root owns plans, README final edits, GitHub, Git commits, evidence records and final acceptance. AGY owns assigned implementation/tests and must not edit root-owned files or commit/push. Avoid empty graph-padding commits, force pushes and secrets in git. A failing phase is unfinished.
