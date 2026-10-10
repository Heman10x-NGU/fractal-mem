# Current architecture and operational contracts

This describes the implementation. Root research/specification documents are
historical proposals; their cache percentages and asymptotic claims are not
release guarantees.

```text
Agent -> hook/client -> Unix socket -> bounded queue -> one SQLite writer
                                             |                |
                                        flush waiters     durable jobs
                                                              |
                                                     <=8 compaction tasks
                                                              |
                                                  writer publishes ready view

SQLite event log + FTS/symbol index + ready tree -> read-only MCP tools
```

## Admission and persistence

Admission acknowledges an in-memory event. It does not promise a committed row.
Each event has a 262,144-byte UTF-8 payload limit. The daemon defaults to 16,384
queue entries and 64 MiB of queued payload, configurable using `--capacity` and
`--queue-bytes`. Rejection leaves the caller running (fail-open ingestion).
The payload budget excludes Python objects, metadata, socket buffers, popped
batches and compaction copies. It is not a process RSS or total pending-memory cap.

Ordinary flush processes admissions ahead of its barrier. Inspect conflicts and
invalid-event counters alongside durability. They are daemon-wide cumulative
counters, not per-event receipts. Retry the same event using the same project,
session, ID, kind, language and payload; a timeout may occur after admission.

## Compaction and barriers

One coordinator schedules at most eight compaction tasks. A compacting flush
registers a waiter instead of pausing raw-event ingestion. It waits for global
compaction quiescence and resulting view publication. Later admitted traffic can
extend that wait; continuous traffic may cause timeout. This is deliberately a
global-idle barrier, not a captured per-project sequence-cutoff API.

Failed jobs settle the wait with `compacted: false`. The CLI reports failure;
inspect status, fix the cause and request `retry-failed`. Timeout cancels the
waiter, not useful compaction work. Shutdown drains admitted raw events, cancels
provider work, and leaves unfinished durable jobs recoverable on restart.

## Usage and failures

Each observed provider response is recorded independently before later stages.
The job's final record contains no duplicate token observations. Rejected replies
with readable usage are also retained. Task-local observers isolate concurrent
jobs without changing custom compactor method signatures.

Accounting cannot observe lost responses or survive every crash between receiving
and recording usage. Null means unknown. This ledger does not replace invoices.

SQLite remains authoritative. Derived snapshots use write, file fsync, replace,
then directory fsync where supported. A failure after replacement surfaces as a
durability error even though the replacement can already exist. Restart repairs
exports from SQLite; errors must not be hidden or treated as successful rollback.

## Memory and retrieval

Ready summaries are immutable and bounded to 512 UTF-8 bytes. Publication waits
for a contiguous ready prefix. View bytes append between batches; above 128,000
bytes, a replacement is published only after reduction to at most 64,000 bytes.
Pending summaries can temporarily exceed the trigger. Source evidence remains.

FTS5 and Tree-sitter provide lexical and exact-symbol retrieval, without
embeddings. One MCP call is not constant-time algorithmic search. MCP does not
automatically capture host events or inject its memory into the host's prompts.

## Verification scope

Run pytest, strict mypy, Ruff and package builds using AGENTS.md. Regression tests
cover delayed providers, continued raw persistence, cancellation, observed usage,
byte admission and directory-fsync failures. Socket/stdio integration tests use
real local processes; provider tests use deterministic fixtures, not live-model
quality evaluations. Historical benchmarks are archived with revision metadata.
