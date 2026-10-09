# Security boundaries

FractalMem runs as the current operating-system user. Socket and database locks
coordinate cooperating processes; they do not isolate hostile code running as
the same user. Protect the home directory and backups with normal OS controls.
Project scoping prevents accidental cross-project retrieval within the API.
It is not an authorization boundary between OS users sharing one database.

## Data flow

The daemon redacts an event before its first SQLite write. FTS and AST indexing
operate on that redacted text. View exports contain only persisted redacted
summaries. The compactor scrubs outbound evidence and returned summaries;
nonce framing identifies passive untrusted data. Raw request bodies, failed
provider response bodies and credentials are not written to diagnostics.

Detection combines Python AST/YAML configuration spans, quoted assignment and
Bearer detection, provider signatures, the pinned Gitleaks v8.30.0 catalog's
221 text rules, and Shannon entropy >=4.5 for tokens longer than20 characters.
The unchanged upstream catalog and MIT license are under
`fractalmem/security/rules/`. Its path-only rule, archive/Git traversal and
allowlist engine do not apply here. This adapter conservatively ignores
Gitleaks allowlists and bounds regex execution; it is not the Gitleaks CLI.
Unprocessable inputs are rejected and counted without terminating ingestion.

No detector identifies every possible secret. Low-entropy passwords with no
configuration context can be missed; prose or code identifiers can be false
positives. Placeholders contain truncated SHA256 fingerprints, not encrypted
credentials. A known low-entropy value can be guessed from its fingerprint.
Nonce framing and the two model stages are defenses, not a proof that prompt
injection has zero success probability. Compactor calls forbid tools and
never execute model output.

## Providers and airgap

Offline extractive compaction is the default and uses no external model.
Cloud dispatch requires explicit provider, endpoint and model configuration.
API keys are read from a named environment variable, never a CLI argument.
HTTPS is required for remote endpoints. Redirects and environment proxies
are disabled. `--airgap` or the working directory's `.fractalmem_airgap`
marker restricts dispatch to literal loopback IP endpoints; a marker created
after startup is checked before each HTTP dispatch. This is an application
policy, not a substitute for an OS network firewall.

## Durability and availability

Hook receipts acknowledge volatile queue admission only. A timeout can occur
after the event was admitted; retries must reuse the same event identity.
Queue overflow fails open. `flush` waits for a commit barrier and exposes
identity conflicts and invalid-event rejections. `flush --compact` additionally
requires jobs to finish; failed jobs require explicit retry after remediation.
SIGTERM drains admitted raw events, cancels model work and requeues unfinished
jobs on restart. SQLite WAL with NORMAL protects process-crash consistency;
FULL offers stronger power-loss durability. Filesystem/fsync errors surface
as writer failure and stop new admission.

Report a reproducible security issue privately to the repository owner.
Use dummy credentials and sanitized examples; never upload live secrets.
