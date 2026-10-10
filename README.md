# FractalMem

**Compact the context. Keep the evidence.**

FractalMem is a local memory engine for long-running coding agents. It stores
redacted events in SQLite, builds a compact hierarchical view, and retrieves
code symbols and text that a summary may leave out.

**Python 3.12+ · macOS / Linux · SQLite + FTS5 · stdio MCP · MIT**

[Quickstart](#quickstart) · [Connect MCP](#connect-mcp) · [How it works](#how-it-works) · [Results](#measured-results) · [Security](SECURITY.md)

## What you get

- **Persistent evidence:** redacted source events remain available after compaction.
- **Stable context between batches:** a hierarchical summary view with byte-exact append behavior.
- **Code-aware retrieval:** exact-symbol lookup plus lexical search over identifiers and prose.
- **Multi-process ingestion:** hooks send events to one daemon, which owns database writes.
- **An offline starting point:** deterministic extraction needs no model credentials.

FractalMem supplies memory infrastructure. You wire your agent's event capture
and prompt construction. Registering its MCP server adds retrieval tools;
it does not automatically capture events, inject memory, or replace the built-in
compaction of Claude Code, Cursor, Codex, or another host.

## Quickstart

Requires Python 3.12+, macOS or Linux, and a Python SQLite build with FTS5.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
git clone https://github.com/Heman10x-NGU/fractal-mem.git
cd fractal-mem
uv sync --frozen --all-extras
uv run fractald
```

Cloning this private repository requires GitHub access. The daemon defaults to
`~/.fractalmem/memory.db` and `~/.fractalmem/fractald.sock`. It creates a new socket
directory with mode `0700`; an existing directory must be owned by you and have
that mode. Offline compaction is the default.

In a second terminal, from the same repository:

```bash
printf '%s' 'def computeMemoryBudget(size): return size * 2' \
  | uv run fractalmem-hook --project my-project --session coding-1 \
      --kind tool --language python --event-id quickstart-001

uv run fractalmem-hook --operation flush --compact
uv run fractalmem-hook --operation status
```

The ingest receipt reports `accepted`, `durable`, `event_id`, and `reason`.
`accepted: true` acknowledges an in-memory queue admission, not a database
commit. The explicit flush waits for earlier admissions to be processed;
check its `conflicts` and `rejected_invalid` counters as well as `durable`.
A failed compacting flush exits with an error; run `--operation status` to
inspect pending/failed jobs. A successful compacting response includes
`compacted: true`. These counters cover the daemon, not just this project.

A timeout can occur after admission. Retry the exact ingest command above,
keeping the same event ID, project, session, kind, language, and payload bytes.
IDs are unique per project; use a new ID for a new event. The hook fails open
so an unavailable daemon does not stop the calling agent.

## Connect MCP

Configure your MCP host to launch the installed executable with the same
project and database used above. For hosts accepting `mcpServers` JSON:

```json
{
  "mcpServers": {
    "fractalmem": {
      "command": "/absolute/path/fractal-mem/.venv/bin/fractalmem-mcp",
      "args": ["--project", "my-project"]
    }
  }
}
```

Replace the executable path. Add `"--db", "/absolute/path/memory.db"` to `args`
if the daemon uses a custom database. The daemon must initialize storage first;
MCP readers do not create it. Each server is scoped to its configured project.

Ask your host to call `search_symbols_and_text` with
`{"query":"computeMemoryBudget","limit":5}`. The result includes a snippet and
a node handle; pass the returned `node_id` to `inspect_node` to explore it.
These are MCP calls, not shell commands.

| Tool | Purpose | Arguments |
|---|---|---|
| `search_symbols_and_text` | Find code/text evidence and its tree handle | `query` up to 4096 UTF-8 bytes; `limit` 1–100 |
| `inspect_node` | Inspect a summary, children, or leaf evidence | `node_id`; `depth` 0–8 |
| `timeline_overview` | Read the active timeline and storage/batch status | `limit` 1–128 |

Returned memory is untrusted evidence, not permission to act. Project scoping
is not an OS-level authorization boundary.

## How it works

```text
Agent events --> Hook / Python client --> Private Unix socket
                                                 |
                                          Admission queue
                                                 |
                                           Single writer
                                                 |
                                           SQLite (WAL)
                                          /      |      \
                                    Event log  Search  Durable jobs
                                               index       |
                                                 |     Compactor
                                                 |         |
                                                 |   Writer publishes
                                                 |   tree + stable view
                                                 |         |
                                                 +----+----+
                                                      |
                                                Read-only MCP
                                                      |
                                                 Agent tools
```

Events are redacted before persistence and indexing. Tree-sitter indexes
supported definitions in Python, JavaScript, TypeScript, and TSX; FTS5 handles
text and code-token components. Search needs no embeddings. A result carries
a covering tree handle, so an agent can find evidence without first traversing
the hierarchy. One tool call does not imply constant-time search.

The hierarchical multi-resolution (HMR) tree groups events into power-of-two
spans. Its published view appends exact bytes between batches.
Above **128,000 bytes**, a batch starts and publishes a replacement at
**64,000 bytes or less**. While summaries are pending, the old prefix stays
visible and the view may exceed the trigger. Source events remain stored.

SQLite is authoritative; per-project `view.json` exports are repaired on restart.
The 16,384-event admission queue uses a nonblocking lock; it is not lock-free.

### Prompt integration

For custom hosts, `fractalmem.compaction.PromptPrefix` provides shared prefix
formatting. Open `Database` with `read_only=True` and inject
`load_view(project_id).render_buffer()` into your prompt. Main and compactor calls
must share your actual tools/system prefix and provider instance for alignment.

Provider cache reuse also depends on model, request, and TTL; stable bytes do
not guarantee cache hits. Recorded compactor usage is available in `compaction_runs`. Treat it as
observations, not a complete billing ledger: missing responses are not evidence
of zero cost. Missing cache or time-to-first-token observations remain null.

## Optional model compaction

Offline extraction retains text within a 512-byte summary budget; it does not
reason about meaning. For model-based compaction, choose an endpoint explicitly:

```bash
# Local OpenAI-compatible service; replace the model name.
uv run fractald --provider openai \
  --base-url http://127.0.0.1:8000/v1 --model YOUR_LOCAL_MODEL --airgap

# Remote Anthropic service; set ANTHROPIC_API_KEY in your environment first.
uv run fractald --provider anthropic \
  --base-url https://api.anthropic.com/v1 --model YOUR_MODEL \
  --api-key-env ANTHROPIC_API_KEY
```

Run one daemon for a given database/socket; stop the offline instance before
switching modes. Remote compaction sends redacted evidence to your configured
provider. No cloud fallback is automatic. `--airgap` or a `.fractalmem_airgap`
marker in the daemon's working directory permits only literal loopback IP
model endpoints; it is an application policy, not a network firewall.

Model compaction extracts facts, then enforces the summary budget. After fixing
a failed job's cause, retry it and wait for completion:

```bash
uv run fractalmem-hook --project my-project --operation retry-failed
uv run fractalmem-hook --operation flush --compact
```

Redaction can miss secrets or remove useful text. Nonce framing and tool-free
compaction do not eliminate prompt injection. Read [SECURITY.md](SECURITY.md)
before storing sensitive history or enabling a remote provider.

## Measured results

Archived offline evaluations from **2026-10-09**, revision `09b04b8f84b7`, on a
shared macOS arm64 machine with Python 3.12.13 and SQLite 3.50.4:

| Workload | Observed result |
|---|---|
| 30,000 core turns | 99.4733% preserved the byte prefix; zero prefix violations |
| 10,000-message disk index | 1,000/1,000 exact-symbol top matches; query p99 0.0296 ms |
| LoCoMo, 1,536 scorable questions | Evidence recall@10 59.22%; precision 7.49% |
| LongMemEval cleaned oracle, 470 scorable questions | Session recall@10 97.83% |
| Four producers, 4,000 socket attempts | 3,506 acknowledged events all durable; 3,995 persisted overall |

These measure local behavior and evidence retrieval, not generated-answer
quality or provider cache hits. LongMemEval oracle uses narrower context than
S/M. Of 494 timed-out socket calls, 489 still persisted; all-attempt p99 was
17.04 ms, so the configured 15 ms client deadline is not a hard wall-clock SLA.

See the [full report and archived artifacts](benchmarks/results/2026-10-09/README.md)
for denominators, environment, revision, hashes, limitations, and reproduction
commands. These are historical results, not a validation of every later commit.

## Development and further reading

```bash
uv run pytest -q
uv run mypy --strict fractalmem benchmarks
uv run ruff check fractalmem benchmarks tests
uv build
```

[CI](https://github.com/Heman10x-NGU/fractal-mem/actions/workflows/ci.yml) covers
Ubuntu/macOS and Python 3.12/3.13. Check the run for your revision.

- [Security, data flow, and durability](SECURITY.md)
- [Executed evaluations and reproduction commands](benchmarks/results/2026-10-09/README.md)
- [Dataset provenance and licensing](benchmarks/datasets/README.md)
- Implementation: [`core`](fractalmem/core), [`storage`](fractalmem/storage), [`daemon`](fractalmem/daemon), [`mcp`](fractalmem/mcp), [`compaction.py`](fractalmem/compaction.py)
- Historical design: [architecture](FRACTALMEM_SPECIFICATION.md), [research proposal](RESEARCH_PAPER_PROPOSAL.md), [benchmark strategy](LAUNCH_AND_BENCHMARK_STRATEGY.md)

Design documents retain hypotheses and superseded claims. Use implementation,
security guidance, and executed evidence for current behavior. Contributions
should add regression coverage and keep performance claims tied to measurements.

## License

Code: [MIT](LICENSE). Third-party terms apply separately, including LoCoMo's CC BY-NC 4.0 license.
