# Execution ledger

## Initial inspection

2026-10-09: Read all seven existing specification/context files. No code or Git repository existed. Confirmed selected AGY pane w7:p1Z in OptChat, Gemini 3.8 Flash (High), idle. GitHub account Heman10x-NGU verified with authenticated gh api user outside network sandbox; repository fractal-mem did not exist (404). Sandbox-only gh auth status was misleading; authenticated live API succeeded.

## Acceptance status

Planning: prepared and pushed (434654e). Phase 1: principal review and repair completed; preparing physical gate evidence and milestone commit. Phases 2-6: implementation underway. Provider quality/cache benchmarks remain unmeasured.

## Phase 1 review and repairs

AGY completed its own review, then principal review physically reproduced silent truncation, malformed snapshot coercion, invalid node states, inconsistent pair ordering and a synthetic left-only summary builder in production. Seven repair requests are recorded in 05-principal-review.md. AGY implemented regression tests and reran its gates. Root additionally removed the force-threshold bypass, rejected invalid append states, and checked view-pair integer types. Independent core run: 25 tests passed, including 20001 rollback steps; strict mypy passes all four core source files. Synthetic parent fixtures are confined to tests. Production merges require actual ready nodes.

## Concurrent module progress

Root security/storage tests: 41 passed on real disk; strict typing passed. Root HTTP compactor protocol tests and redaction: 36 passed after actual loopback transport revealed entropy redaction mutating nonce boundaries; that defect was repaired. No live provider quality or cache claim is made from protocol fixtures. Root owns daemon/client/compactor/storage/security; AGY now owns MCP and its tests.

## Dataset provenance

LoCoMo downloaded from snap-research/locomo revision 3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376: 10 conversations, 2805274 bytes, SHA256 79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4. Upstream license CC BY-NC 4.0; raw data excluded from Git.

Cleaned LongMemEval oracle: 500 questions, 15388478 bytes, SHA256 821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c. Downloaded from the official cleaned dataset resolve/main URL; content hash freezes this run. This is not LongMemEval-S or V2. Raw data excluded from Git.

## Runtime integration progress

Core milestone8184389 and storage/security milestoneb4088f2 were each committed
and pushed once. Root independently verified133 tests in a complete run after
real daemon/MCP integration. Strict mypy passed25 production/benchmark sources;
Ruff passed. Final packaging/recorded gates are still underway.

The first local physical suite ran30000 core turns with cl100k_base exact view
tokens:158 batches, zero prefix violations,99.4733% prefix-preserving turns,
mean95340 bytes.10000-message disk corpus:1000/1000 exact symbol queries correct,
restart exact; provider cache/TTFT unknown. These preliminary artifacts record
a dirty implementation revision; a final committed-code run supersedes timings.

Full LoCoMo retrieval:1986 questions,1536 scorable, evidence recall@10=0.592184;
cleaned LongMemEval oracle:500 questions,470scorable, session recall@10=0.978262.
No generated answers/official judge were executed. Inputs and original hashes
remain recorded separately from package licensing.

A real4k UDS run exposed repeated private preparation and failed its180s flush.
Root fixed algorithmic work, preserving the failed evidence. Fresh optimized
run passed in25.43s:3550accepted volatile receipts,450timeouts,3987committed,
no acknowledged events missing, pending/failed jobs zero, restart exact.
Separate administrative budgets do not alter the15ms hook transport deadline.
