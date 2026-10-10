# FractalMem contributor instructions

Read README.md, SECURITY.md, docs/architecture.md and relevant implementation/tests
before editing. Root specifications and research proposals are historical;
they contain superseded formulas and unverified performance targets.

## Invariants

- Retain append-only redacted source events and immutable ready summaries.
- Nodes cover aligned power-of-two spans. For sibling level l and left index i,
  last = (i + 2) * 2**l - 1; priority = (T - last) / 2**l
  = (T + 1) / 2**l - i - 2. Break ties toward the older pair.
- Published bytes append between batches. Trigger above 128,000 bytes; publish
  a replacement at no more than 64,000 bytes. Pending summaries can exceed the trigger.
- SQLite is authoritative. Repair derived exports on restart. Surface fsync errors.
- One daemon owns writes. Queue admission is volatile; timeouts are ambiguous.
  Retries preserve identity, project, session, kind, language and payload.
- Compact flush waits for global quiescence without blocking raw writes.
  Continuous ingestion can delay it until timeout. Remove abandoned waiters.
- Bound admission by count and UTF-8 payload bytes; do not claim an RSS bound.
- Redact before persistence/indexing and model dispatch. Never execute model output.
- Persist observed provider usage per response independently of job success.
  Missing observations are unknown, not evidence of zero billing.

## Claims

One retrieval call is not O(1) search. The queue uses a nonblocking admission lock,
not a formal lock-free algorithm. The 15 ms client budget is not a wall-clock SLA.
Stable bytes do not guarantee cache hits or answer quality. Redaction is fallible.
MCP supplies project-scoped read-only tools; hosts integrate capture and prompts.

## Verification

Use Python 3.12+ on macOS/Linux:

```bash
uv sync --frozen --all-extras
uv run pytest -q
uv run mypy --strict fractalmem benchmarks
uv run ruff check fractalmem benchmarks tests
uv build
```

Add deterministic regressions, including failures, cancellation, shutdown and
restart where relevant. Use fixture providers and dummy credentials. Paid model
calls or large benchmarks require explicit authorization. Tie measured claims
to their exact revision and workload. Never commit live secrets or raw datasets.
