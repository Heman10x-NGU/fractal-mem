# Principal review after AGY's first self-review

AGY reported its core gates passed and stopped after its own review. Root reproduced 61 passing tests across the current repository and strict mypy. Passing gates do not cover the following defects. Do not accept phase 1 until these are fixed with regression tests.

## Core repair requests (AGY owns these)

1. **Silent loss in node factory.** `HMRNode.create_leaf(0, 'a'*600)` silently yields 512 bytes. Factories for ready nodes must reject oversize summaries; keep an explicit UTF-8 truncation utility only for the documented offline compressor. Pending appends must render the stable pending placeholder rather than accept arbitrarily long raw messages as summaries. Validate max_bytes in the truncation helper.
2. **Test simulation in production.** `default_synthetic_parent_builder` fabricates a summary by keeping only the left child, and `auto_synthesize_parents` makes that reachable from the production engine. Move synthetic fixtures into tests; production merging must resolve actual ready nodes or use an explicitly injected real compactor. Diagnostic reduce_to_length must require an explicit builder/resolver. Root is implementing an actual documented offline head/tail extractor in fractalmem/compaction.py; leave that file untouched.
3. **Invalid node inputs accepted.** Root physically reproduced `HMRNode.create(...status='bogus')` being accepted. Validate statuses, kinds, actual integer types (reject bool), nonempty scoped project IDs, and bounded level 0..62 before bit shifts. Validate engine max/target positive and engine/view project equality.
4. **Incorrect ordering and malformed pair metadata.** Root reproduced `pair < pair` returning True. Remove or correctly implement strict ordering. Validate SiblingPair against actual aligned sibling nodes, inclusive last index, project and total count; remove inconsistent redundant metadata if useful. The current tie test's left index 31 at level 0 is odd and therefore not a sibling pair. Use a real tie at T=31: level-2 pair covering 16..23 has due=2; level-0 pair covering 28..29 has due=2. The older pair wins. Remove failed exploratory comments and unused pair constructions from the test.
5. **Snapshot coercion accepts corrupt state.** Root physically reproduced snapshot batch_active='false' becoming True. View.from_dict must validate required JSON field types and allowed values without string/int/bool coercions. Corrupt roots, containers, missing fields, unknown statuses, invalid numeric types and byte budgets must consistently raise ViewPersistenceError. Validate view_pairs against nodes and rendered byte count. Test actual corrupt files.
6. **Public batch API.** Expose a public resume_batch or sawtooth_batch_merge operation that refuses to begin below/equal max threshold, but can resume an already active batch. Storage currently calls the private _run_sawtooth_batch; root will switch it after your API is available. Test exactly 128000 serialized bytes (no merge) and first append exceeding it (batch to <=64000 with ready parents), plus resumed pending batch.
7. **False thread-safety claim and durability.** InMemoryNodeStore is called thread-safe but has no synchronization or immutable-ready enforcement. Either implement the claim or explicitly make it an owner-thread store. Add parent-directory fsync after view snapshot rename on POSIX for actual atomic-file durability. No broad exception swallowing beyond best-effort cleanup of an already failing operation.

Run core tests, entire current pytest (real HTTP tests need socket permission), strict mypy, ruff; self-review the repairs and report commands/results. Do not edit root storage/security/compaction/docs or commit/push. Root will independently verify before phase acceptance.

## Root-owned modules under review

* Storage and security currently pass 41 real tests and strict typing. Root found credential-like metadata could be persisted; it now rejects those identifiers before disk use, and tests cover DB plus WAL artifacts.
* Real HTTP compactor integration found entropy redaction was rewriting the cryptographic framing delimiter. Root is repairing that and retaining a transport regression test. Provider protocol fixtures are explicitly distinct from live LLM quality measurements.

## Runtime and security review, second pass

An independent read-only review reproduced quoted-secret leakage in fenced or
truncated JSON, entropy bypass for >8192-character tokens, and an oversized
config-depth event poisoning the writer. Repairs add quoted-key fallback,
whole-token entropy checks and per-event security rejection isolation.
Regression tests physically exercise disk and actual socket traffic.

Additional fixes: CPU compaction preprocessing is offloaded from the socket
loop; configured HTTP clients reuse connections and gate cold-prefix followers
until first response body bytes. Transient failures have three bounded attempts,
failed jobs can be explicitly requeued, and compacting flush rejects incomplete
work. Text snippets now include tail matches. The pinned upstream Gitleaks
v8.30.0 catalog supplies all221 applicable text rules with integrity verification,
regex time bounds and license; rules compile before daemon readiness.

The full4k UDS benchmark initially failed: a180s flush deadline expired while
private batch candidates were repeatedly rebuilt; graceful drain also exceeded
its cleanup budget. The failed artifact preserves volatile receipts and missing
commits instead of calling the run successful. Structural repairs replace
per-merge whole-frontier scans with an exact-priority heap and linked frontier,
evaluate transactional ingress batches once, cache parent lookups, group view
resumption and export only changed committed views. Incomplete batches now
publish no partial rewrite. Ready registry nodes cannot change, and short
persisted leaves retain message-kind provenance.

Fresh full4k run then passed:3987 durable messages, every3550 accepted receipt
on disk,22.37s compaction flush, byte-exact restart, zero queue/failed-job/writer
failures.450 receipts timed out;437 nevertheless persisted and13 did not.
Acceptedp99 was10.72ms; all-attemptp99 was16.02ms under development load.
These are observations, not a hard real-time guarantee. Heap regression tests
compare complete frontiers against an independently scanned priority oracle
and bound resolver calls linearly.

## MCP review

AGY implemented actual SDK2 stdio integration and self-review. Root findings
required matching daemon/MCP default paths, strict integer schemas, bounded
IDs/responses, per-project status, nonce/taint fields, real lifecycle closure,
and suppression of SDK validation-value/unknown-name reflection. AGY added
Pydantic output contracts and real subprocess regressions. Root's full suite
reached133passing tests before the final query-echo regression was added.
Final acceptance uses the recorded final gate, not this intermediate count.

## Recovery and shutdown acceptance

Final independent review compared1000 randomized heap preparations with its
own repeated-scan oracle and reproduced a deferred-publication restart gap:
ready jobs persisted but the in-memory dirty-view flag was lost. Root added
startup and orderly-shutdown reconciliation from persisted active snapshots.
A real disk regression covers270messages, completed jobs, and restart without
another append. A blocked-provider cancellation test then exposed asyncio's
`Server.wait_closed()` waiting for existing flush connections before the
worker could cancel compaction. Shutdown now closes admission, cancels socket
handlers, cancels model work, drains admitted raw events, reconciles views and
closes storage. Both regressions pass on actual disk/socket lifecycles.

Root took MCP ownership after AGY's final network interruption, added valid
credential-query echo redaction, and bounded the final SDK serialization
(including duplicated text/structured output). This completes the remaining
review findings without depending on a new AGY provider turn.
