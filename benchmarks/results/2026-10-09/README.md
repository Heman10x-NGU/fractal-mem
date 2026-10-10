# Executed FractalMem v2.1 evaluations: 2026-10-09

All four final runs used clean commit `09b04b8f84b7cbd95ad4d4d3014ec045ba7515ec` and executed sequentially
on macOS arm64, Python 3.12.13, SQLite 3.50.4. They used real disk SQLite, real
Tree-sitter grammars, actual Unix sockets and offline extractive compaction.
The development machine was shared; timings are observations on this host.

## Results

| Workload | Physical input | Result | Timing |
|---|---|---|---|
| Core sawtooth | 30,000 turns, seed 42 | 158 batches; zero prefix violations; 99.4733% turns preserve the byte prefix | 84.18 s including token counting |
| Disk symbol retrieval | 10,000 messages, 1,000 queries | 100% exact symbol precision@1; parent handles present; byte-exact restart | query p99 0.0296 ms; ingestion + compaction 35.13 s |
| LoCoMo | 10 conversations, 5,882 turns, 1,986 questions | evidence recall@10 59.2184%; precision 7.4881%; 1,536 scorable | query p99 0.961 ms; run 18.54 s |
| LongMemEval cleaned oracle | 500 cases, 10,960 turns, 500 questions | session recall@10 97.8262%; returned-message precision 99.7872%; 470 scorable | query p99 5.357 ms; run 45.35 s |
| Four-producer UDS | 4,000 attempts including 64 payloads of 64 KiB | 3,995 durable; every 3,506 acknowledged event present; zero failed jobs; exact restart | acknowledged p99 11.28 ms; all-attempt p99 17.04 ms |

The core view averaged 95340.22 bytes, with maximum
127,997 bytes. Exact cl100k_base view-token counts totalled
484,779,220 across all turns; these exclude tools, system,
task and provider framing and are not billed-token measurements.

## Socket durability and deadline interpretation

The client retained its configured 15 ms deadline. 494
receipts timed out: 489 events nevertheless reached
disk and 5 did not.
Timeouts therefore require retry with the same identity. No acknowledged event
was missing. Queue rejections, identity conflicts, invalid inputs, pending jobs
and failed jobs were all zero after flush. This does not mean every attempt
succeeded. Observed wall time includes scheduler delays: 511 calls exceeded
15 ms, including 17 acknowledged calls. A hard real-time SLA is not established.

The compacting flush took 23.08 s; overall UDS run
26.37 s. Both shutdowns were graceful, removed their
socket, and returned exit 0. The restarted view and message count matched.

## Scope and limits

- Prefix preservation is measured locally. Provider cache-hit rates, TTFT,
  billed tokens, generated-answer quality and official LLM judging are null.
- Dataset rows score retrieval of evidence, not answers. LoCoMo has
  446 abstention questions excluded from evidence
  scoring; LongMemEval has 30. Unresolvable evidence
  remains in the recall denominator. Category-level results are in the JSON.
- LongMemEval **oracle** supplies narrower context than S/M; its result cannot
  stand in for those variants or V2. Precision counts returned messages,
  which may repeat a session ID. No cross-dataset quality comparison is implied.
- LoCoMo recall and precision show the limits of lexical retrieval for broad
  natural-language questions. No embedding or model-answer quality is claimed.
- The admission ring uses a short nonblocking producer lock; formal lock-free
  progress is not claimed. Search completes in one tool call; asymptotic O(1)
  retrieval is not claimed.

## Verification and artifacts

The physical final gate passes **138 tests**, strict mypy on 25 sources, Ruff,
frozen-lock validation and wheel/source builds. A separate installed-wheel
check passes real UDS, disk redaction and all three MCP stdio tools. The remote
[matrix run](https://github.com/Heman10x-NGU/fractal-mem/actions/runs/37868815175)
passed on Ubuntu/macOS with Python 3.12/3.13.

Generated JSON, CSV and command logs are stored as lossless gzip artifacts.
[manifest.json](manifest.json) records original and compressed SHA256 hashes,
commands, sizes and classification. For example:

```bash
gzip -dc benchmarks/results/2026-10-09/local-final.json.gz > /tmp/local-final.json
```

The earlier `uds-4k-instrumented` run failed its 180 s compaction barrier and
required forced cleanup. It is preserved as a failed development observation,
not included among final passes. `uds-4k-optimized` records the subsequent
successful development run. Their own revision/dirty metadata remain intact.
The historical principal-review working notes are not included in this repository;
use the checked-in report and manifest as the available evidence.

## Dataset attribution

LoCoMo source: [snap-research/locomo](https://github.com/snap-research/locomo),
pinned revision `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`, CC BY-NC 4.0.
LongMemEval source: [official project](https://github.com/xiaowu0162/LongMemEval)
and the official cleaned oracle dataset. These reports contain evaluation IDs
and metrics, not dialogue or question text. Raw datasets are excluded from Git
and the package. [Provenance and download hashes](../../datasets/README.md)
identify the exact inputs; dataset terms are separate from the MIT code license.
