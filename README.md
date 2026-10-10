# FractalMem

**Keep the history. Stabilize the view. Retrieve the details.**

Local-first memory for long-running coding agents: a redacted event log,
a hierarchical summary view, and exact-symbol/text retrieval over SQLite.
Start offline with no model credentials; connect an MCP host when you want
an agent to explore the stored evidence.

**Python 3.12+ · macOS / Linux · SQLite WAL + FTS5 · stdio MCP · MIT**

[Quickstart](#quickstart) · [MCP tools](#native-mcp) · [How it works](#architecture-and-invariants) · [Results](#published-evaluation-snapshot) · [Security](SECURITY.md)

## Why FractalMem?

Long agent sessions accumulate decisions, tool output, and code identifiers.
A compact summary is useful until the next task needs the exact function name
or the evidence behind an earlier decision. Rewriting that summary every turn
can also disturb a reusable prompt prefix.

FractalMem separates those concerns:

| Need | Mechanism | Practical benefit |
|---|---|---|
| Recover a detail omitted by a summary | Persist redacted events and retrieve symbols/text | Inspect the original stored evidence without relying on the summary alone |
| Keep a compact chronological view | Hierarchical summaries with batched reduction | Preserve exact view bytes between compaction batches |
| Share memory across agent processes | Unix-socket ingestion and a single SQLite writer | Keep database writes out of producer hooks |
| Navigate memory from an agent | Three project-scoped, read-only MCP tools | Search, inspect a node, or scan the timeline |
| Begin without a model service | Deterministic offline extraction | Try the storage and retrieval workflow without API credentials |

**Good fit:** coding-agent integrations that need searchable session history,
explicit durability barriers, and control over how memory enters the prompt.
**Not a turnkey host replacement:** registering MCP tools does not automatically
capture a host's events, inject its memory view, or replace its compaction.
You must wire ingestion and prompt integration yourself. Stable local bytes
are not a guarantee of provider cache hits or better generated answers.

## Install

Requires Python 3.12+ and macOS or Linux (Unix sockets and POSIX file locks).
Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
git clone https://github.com/Heman10x-NGU/fractal-mem.git
cd fractal-mem
uv sync --frozen --all-extras
```

The repository is private; cloning requires GitHub access. Package dependencies
include the real MCP SDK, SQLite FTS5, Tree-sitter grammars, HTTPX, regex and
tiktoken. SQLite ships with Python; its build must include FTS5. The locked
versions make development and CI reproducible. An editable pip installation
also works: `python -m pip install -e '.[dev]'`.

## Quickstart

Start the daemon in one terminal:

```bash
uv run fractald
```

Defaults: database `~/.fractalmem/memory.db`, socket
`~/.fractalmem/fractald.sock`. The socket directory must be owned by you with
mode 0700; a new default directory is created with that mode. Existing insecure
directories and live sockets are rejected. Offline extractive compaction is
the default and needs no model credentials or network service.

Send events from another terminal:

```bash
printf '%s\n' 'We chose SQLite WAL and function computeMemoryBudget.' \
  | uv run fractalmem-hook --project my-project --session coding-1 --kind user

printf '%s\n' 'def computeMemoryBudget(size): return size * 2' \
  | uv run fractalmem-hook --project my-project --session coding-1 \
      --kind tool --language python --event-id stable-event-001

uv run fractalmem-hook --operation status
uv run fractalmem-hook --operation flush --compact
```

A hook receipt with `accepted:true,durable:false` means **volatile queue
admission**. Explicit flush waits for the writer to commit all earlier
admissions. `--compact` also requires current compaction jobs to finish.
Identity conflicts and rejected security inputs appear in status/barrier
counters. A timeout is ambiguous: the daemon may already have admitted the
event. Retry using the same `--event-id`; IDs are unique per project and
conflicting content is rejected. Fail-open ingestion reports failures without
interrupting the calling agent. CLI startup/stdin reading is outside the socket
deadline; a long-lived client is the lower-overhead integration.

```python
from fractalmem.client import FractalClient

client = FractalClient("my-project", "coding-1")
receipt = client.ingest_event(
    kind="tool", content="def computeMemoryBudget(size): return size * 2",
    language="python", event_id="python-example-001",
)
print(receipt)
client.flush(compact=True)  # Separate administrative operation, default 30s.
```

The Python example uses a different event ID from the shell example. For a retry,
reuse both the original ID and the exact payload; even a trailing newline changes
the payload. Do not reuse an ID for a different event.

Use SIGTERM/Ctrl-C for graceful shutdown: admitted raw events drain, unfinished
model jobs recover on restart. Use `--synchronous FULL` for stronger power-loss
durability; default WAL/NORMAL offers process-crash consistency. See
[security and durability boundaries](SECURITY.md).

## Native MCP

Launch one read-only stdio server per project:

```bash
uv run fractalmem-mcp --project my-project
```

Example configuration for a host that accepts `mcpServers` JSON (replace the
executable with the absolute installed path):

```json
{
  "mcpServers": {
    "fractalmem": {
      "command": "/absolute/path/fractal-mem/.venv/bin/fractalmem-mcp",
      "args": ["--project", "my-project", "--db", "/absolute/path/memory.db"]
    }
  }
}
```

Once connected, try asking your host to call `search_symbols_and_text` with
`{"query":"computeMemoryBudget","limit":5}`. It returns stored evidence and a
covering tree handle; use `inspect_node` to examine the matching history.
These are MCP tool calls, not additional shell commands.

Tool contracts:

| Tool | Arguments | Result |
|---|---|---|
| `timeline_overview` | `limit` 1..128 | Scoped timeline, summaries, byte budget, batch status |
| `inspect_node` | `node_id`, `depth` 0..8 | Node metadata, bounded children, redacted leaf evidence and taint |
| `search_symbols_and_text` | `query` <=4096 UTF-8 bytes, `limit` 1..100 | Code/text matches, scores, snippets and HMR node/parent handles |

Node identifiers can be canonical (`L0_P0_M0_0`), chronological (`0+1`), or
project-scoped (`my-project:0+1`). Foreign scopes fail. Tool output carries
untrusted-data framing; memory is evidence, never authorization for an action.
The daemon must initialize the database first. Missing storage produces a safe
tool error and is never created by a reader. stdout is reserved for protocol.

## Architecture and invariants

```mermaid
flowchart LR
    Hooks[Agent hooks] -->|15ms transport budget| UDS[Private Unix socket]
    UDS --> Ring[16384 event admission ring]
    Ring --> Writer[One writer owner thread]
    Writer --> DB[(SQLite WAL)]
    Writer --> Jobs[Durable compaction jobs]
    Jobs --> Compactor[Offline or configured model compactor]
    Compactor --> Writer
    DB --> Tree[HMR tree and frozen view]
    DB --> Search[FTS5 and Tree-sitter symbols]
    Tree --> MCP[Read-only stdio MCP]
    Search --> MCP
    MCP --> Agent[Agent memory tools]
```

- Each node covers an aligned power-of-two message span. Merge priority is
  `(T-last)/2**level`; the independent 20,001-step rollback oracle verifies the
  terminal-message formula. For left index `i`, exact algebra is
  `(T+1)/2**level-i-2`. The original spec omitted the constant `-2`.
- Serialized views append exact bytes between batches. A batch starts only
  **above 128,000 bytes**, then publishes its replacement at **<=64,000 bytes**.
  Pending summaries retain the original visible prefix; an unfinished batch
  can temporarily exceed the maximum until compaction catches up. Ready
  summaries never replace existing lines between batches.
- Original redacted events remain available when summaries omit details.
  SQLite snapshots are authoritative; atomic per-project `view.json` exports
  under `<database>.views/<project>/` are repaired from SQLite on restart.
- Exact-symbol lookup uses indexed Tree-sitter definitions for Python,
  JavaScript, TypeScript and TSX. FTS preserves identifiers and adds camel,
  snake and path components with weighted BM25. Unsupported syntax uses text
  retrieval. One tool call returns a match and its covering active-tree handle;
  query complexity is not constant-time.
- The portable Python MPSC ring uses a short synchronization section and
  **nonblocking producer admission**. It makes no formal lock-free claim.
  Producers never write SQLite; mutable storage and socket leases reject
  another cooperating writer.

### Shared prompt prefix

`fractalmem.compaction.PromptPrefix` supplies canonical constant tool
definitions/system text and four-line Anthropic view blocks. Supply your
host's **actual** tools/system prefix and share the same prefix/provider
instance for main and compactor calls. Provider adapters reuse connections and
coordinate cold-prefix dispatch until the first response body byte. Tools are
forbidden in compaction. Real normalized provider usage is persisted in
`compaction_runs`; absent cache/TTFT observations remain null.

**Integration boundary:** MCP registration alone does not insert the memory view into Claude Code,
Cursor, Codex or another host's existing prompt, or replace its compaction.
Your host must explicitly inject `Database(...,read_only=True).load_view(...)
.render_buffer()` through this prompt seam. Cache reuse depends on provider,
model, TTL and request contents; the proposal's 98.6%/96.2% numbers are reference
claims, not measured guarantees for this package. See the provider's
[prompt-caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching)
for matching and eligibility requirements.

## Compaction modes

Offline extraction preserves the head and tail of text within 512 UTF-8 bytes;
it is a deterministic compressor, not LLM reasoning. For semantic compaction,
configure an OpenAI-compatible or Anthropic endpoint explicitly:

```bash
uv run fractald --provider openai \
  --base-url http://127.0.0.1:8000/v1 --model local-model --airgap

# Remote endpoint example: the named environment variable must already be set.
uv run fractald --provider anthropic \
  --base-url https://api.anthropic.com/v1 --model YOUR_MODEL \
  --api-key-env ANTHROPIC_API_KEY
```

Two stages extract passive facts and enforce the 512-byte summary budget with
at most five enforcement attempts. Concurrency is bounded at eight jobs.
Transient HTTP 429/5xx/transport failures receive three bounded attempts with
backoff; other failures remain visible. No cloud fallback is automatic.
Remediate failures, then retry through the daemon:

```bash
uv run fractalmem-hook --project my-project --operation retry-failed
uv run fractalmem-hook --operation flush --compact
```

`--airgap` or a `.fractalmem_airgap` marker in the daemon's working directory
allows only literal loopback IP model endpoints. Redirects and environment
proxies are disabled. Detection includes AST/YAML configuration parsing,
known provider signatures, all 221 applicable text rules from the pinned
Gitleaks catalog, and Shannon entropy >=4.5. Redaction and nonces cannot prove
universal secret detection or zero prompt-injection success.

## Published evaluation snapshot

These are **archived results from the 2026-10-09 evaluation**, not a fresh run
of every subsequent commit. The report identifies benchmark revision
`09b04b8f84b7cbd95ad4d4d3014ec045ba7515ec`, macOS arm64, Python 3.12.13,
and SQLite 3.50.4 on a shared development machine.

| Evaluation | Measured result |
|---|---|
| 30,000 core turns | 99.4733% preserve the byte prefix; zero invariant violations |
| 10,000-message disk index | 1,000/1,000 exact symbol matches with parent handles; p99 0.030 ms |
| LoCoMo evidence retrieval | recall@10 59.22%, precision 7.49% across 1,536 scorable questions |
| LongMemEval cleaned oracle | session recall@10 97.83% across 470 scorable questions |
| Four producers, 4,000 UDS attempts | all 3,506 acknowledged events durable; 3,995 total persisted; exact restart |

Acknowledged socket calls had p99 11.28 ms; all attempts had p99 17.04 ms.
There were 494 timed-out receipts, including 489 events that still persisted.
The client uses a 15 ms configured deadline; a hard wall-clock SLA is not
established. Provider cache rates and answer-generation quality remain
unmeasured. Dataset metrics describe retrieval, and oracle context is narrower
than LongMemEval S/M.

See the [complete measured results and archived JSON/CSV](benchmarks/results/2026-10-09/README.md)
for environment, denominators, hashes, timing scope, failed-run evidence and
reproduction commands.

The archived verification gate reports 138 passing tests plus typing, lint,
build, and installed-wheel checks. That is a historical count, not a fixed
current-suite total. For current revisions, inspect [GitHub Actions](https://github.com/Heman10x-NGU/fractal-mem/actions/workflows/ci.yml)
and run the commands below. CI covers Ubuntu and macOS on Python 3.12 and 3.13.

## Tests and reproducible evaluations

```bash
uv run pytest -q
uv run mypy --strict fractalmem benchmarks
uv run ruff check fractalmem benchmarks tests
uv build
```

The following commands run workloads and write new measurements; they are
optional evaluation runs, separate from the checks above:

```bash
uv run fractalmem-eval local --tokenizer cl100k_base --output results/local/local.json
uv run fractalmem-uds-bench --output results/local/uds.json
```

Evaluations use real disk files, actual daemon processes/Unix sockets and real
Tree-sitter grammars. The local suite defaults to 30,000 core turns, 10,000 disk
messages and 1,000 identifier queries. Token counts use a named tokenizer and
cover exact rendered-view bytes, excluding provider tools/system/task framing.
No local metric is substituted for provider-billed tokens or cache reads.
Reports preserve per-turn/per-question JSON/CSV, hashes, commands, revision,
configuration and environment. Dataset downloads/results scratch directories
are ignored by Git. See [benchmark strategy and methodology](LAUNCH_AND_BENCHMARK_STRATEGY.md)
and [executed benchmark artifacts](benchmarks/results/2026-10-09/README.md).

Dataset retrieval commands, after downloading the official files described in
[dataset provenance](benchmarks/datasets/README.md):

```bash
uv run fractalmem-eval datasets --dataset locomo \
  --path benchmarks/datasets/downloads/locomo10.json --output results/local/locomo.json
uv run fractalmem-eval datasets --dataset longmemeval \
  --path benchmarks/datasets/downloads/longmemeval_oracle.json --output results/local/longmemeval.json
uv run fractalmem-eval usage --path actual-provider-usage.jsonl --output results/local/cache.json
uv run fractalmem-eval usage --database --project my-project \
  --path ~/.fractalmem/memory.db --output results/local/compactor-cache.json
```

LoCoMo evaluation measures dialogue evidence precision/recall; LongMemEval
measures answer-bearing session retrieval. The **oracle** set is a narrowed
context and cannot stand in for LongMemEval S/M or V2. Ground-truth answers and
labels are never indexed. Generated-answer accuracy, official LLM judging,
real provider-cache hit rates and TTFT require an explicitly configured model
and actual response/stream observations. They remain unmeasured in offline runs.

LoCoMo data is CC BY-NC 4.0 and is not distributed under this package's MIT
license. Dataset licenses and provenance remain separate from code licensing.
Original proposal documents retain historical hypotheses/reference numbers;
use executed artifacts for conclusions about this implementation.

## Repository map

```text
fractal-mem/
|-- fractalmem/
|   |-- core/           # HMR nodes, merge scheduling, stable views
|   |-- storage/        # SQLite persistence, FTS5, symbol indexing
|   |-- daemon/         # Unix socket, admission queue, single writer
|   |-- security/       # Redaction, framing, tool-schema defenses
|   |-- mcp/            # Project-scoped, read-only stdio tools
|   |-- client.py       # Event ingestion and administrative barriers
|   `-- compaction.py   # Offline extraction and model adapters
|-- tests/              # Core, storage, concurrency, MCP, security
|-- benchmarks/
|   |-- datasets/       # Provenance; raw downloads are not committed
|   `-- results/        # Archived reports, manifests, compressed evidence
|-- .github/workflows/  # Linux/macOS verification matrix
|-- pyproject.toml
|-- uv.lock
|-- SECURITY.md
`-- *_SPECIFICATION.md  # Historical design documents; see caveats below
```

## Troubleshooting

| Symptom | Check first |
|---|---|
| Clone is denied | This repository is private; your GitHub account needs access |
| Hook is not accepted | Read its receipt and daemon status; a timeout can still mean the event was admitted |
| Retry reports an identity conflict | Reuse the same ID only for the exact same event payload |
| MCP says the database is missing | Start `fractald` first; point MCP at the same database |
| Socket directory is rejected | Use a directory you own with mode 0700; do not loosen the safety check |
| Compaction remains pending or failed | Check status, fix the provider/configuration issue, then use `retry-failed` and `flush --compact` |
| No provider cache benefit | MCP registration is not prompt integration; check the shared prefix and actual provider usage |

For data-flow, redaction limitations, same-user isolation, and shutdown/durability
boundaries, read [SECURITY.md](SECURITY.md). No secret detector or nonce wrapper
is a universal protection against leaks or prompt injection.

## Engineering record

Start with this README, [SECURITY.md](SECURITY.md), the implementation, and
[executed evidence](benchmarks/results/2026-10-09/README.md) for current behavior.
The documents below preserve the design history and research hypotheses;
they can contain superseded formulas, targets, and stronger claims than the
implementation establishes. In particular, do not treat O(1) retrieval,
formal lock-free progress, a hard 15 ms SLA, or proposal cache percentages as
verified package guarantees.

- [Architecture proposal](FRACTALMEM_SPECIFICATION.md)
- [UniiChat specification analysis](SPECIFICATION.md)
- [Research hypotheses and mathematical proposal](RESEARCH_PAPER_PROPOSAL.md)
- [Benchmark and launch strategy](LAUNCH_AND_BENCHMARK_STRATEGY.md)
- [Evaluation artifacts and reproduction record](benchmarks/results/2026-10-09/README.md)
- [Dataset provenance](benchmarks/datasets/README.md)

For contributions, keep claims tied to their measurement scope, add regression
coverage for behavioral changes, and run the verification commands above.
Do not commit credentials, local databases, or raw third-party datasets.

## License

Code is available under the [MIT license](LICENSE). Third-party rule catalogs
and datasets retain their own licenses; LoCoMo's CC BY-NC 4.0 terms are not
replaced by this repository's MIT license.
