CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL DEFAULT 0 CHECK(next_seq >= 0)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK(seq >= 0),
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('user','unii','tool','echo','work','note')),
    payload TEXT NOT NULL,
    language TEXT,
    byte_size INTEGER NOT NULL CHECK(byte_size = length(CAST(payload AS BLOB))),
    taint_level TEXT NOT NULL DEFAULT 'UNTRUSTED_EXTERNAL',
    created_ns INTEGER NOT NULL,
    UNIQUE(project_id, seq),
    UNIQUE(project_id, event_id)
);
CREATE TRIGGER IF NOT EXISTS messages_no_update BEFORE UPDATE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;
CREATE TRIGGER IF NOT EXISTS messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;

CREATE TABLE IF NOT EXISTS tree_nodes (
    project_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 62),
    index_in_level INTEGER NOT NULL CHECK(index_in_level >= 0),
    start_seq INTEGER NOT NULL,
    end_seq INTEGER NOT NULL CHECK(end_seq >= start_seq),
    summary_text TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size = length(CAST(summary_text AS BLOB))),
    status TEXT NOT NULL CHECK(status IN ('pending','running','ready','failed')),
    kind TEXT NOT NULL,
    PRIMARY KEY(project_id, node_id),
    UNIQUE(project_id, level, index_in_level),
    FOREIGN KEY(project_id,start_seq) REFERENCES messages(project_id,seq),
    FOREIGN KEY(project_id,end_seq) REFERENCES messages(project_id,seq),
    CHECK(status != 'ready' OR byte_size <= 512)
);
CREATE INDEX IF NOT EXISTS idx_tree_range ON tree_nodes(project_id,start_seq,end_seq);
CREATE INDEX IF NOT EXISTS idx_tree_leaves ON tree_nodes(project_id,level,start_seq);
CREATE TRIGGER IF NOT EXISTS nodes_ready_immutable BEFORE UPDATE ON tree_nodes
WHEN OLD.status = 'ready'
BEGIN SELECT RAISE(ABORT, 'ready nodes are immutable'); END;

CREATE TABLE IF NOT EXISTS views (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    generation INTEGER NOT NULL,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_frontier (
    project_id TEXT NOT NULL,
    start_seq INTEGER NOT NULL,
    end_seq INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    PRIMARY KEY(project_id,start_seq),
    FOREIGN KEY(project_id,node_id) REFERENCES tree_nodes(project_id,node_id)
);
CREATE TABLE IF NOT EXISTS legacy_views_archive (
    project_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    reason TEXT NOT NULL,
    archived_at_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS compaction_watermarks (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id) ON DELETE CASCADE,
    ready_watermark_seq INTEGER NOT NULL DEFAULT -1 CHECK(ready_watermark_seq >= -1),
    updated_at_ns INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS compaction_jobs (
    project_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','ready','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    ordinal INTEGER PRIMARY KEY,
    level INTEGER NOT NULL DEFAULT 0,
    start_seq INTEGER NOT NULL DEFAULT 0,
    end_seq INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id,node_id),
    FOREIGN KEY(project_id,node_id) REFERENCES tree_nodes(project_id,node_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON compaction_jobs(status,ordinal);
CREATE INDEX IF NOT EXISTS idx_tree_nodes_status ON tree_nodes(project_id,level,status,start_seq);

CREATE TABLE IF NOT EXISTS ast_symbols (
    project_id TEXT NOT NULL,
    message_seq INTEGER NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_type TEXT NOT NULL,
    language TEXT NOT NULL,
    start_byte INTEGER NOT NULL,
    end_byte INTEGER NOT NULL,
    PRIMARY KEY(project_id,message_seq,symbol_name,start_byte),
    FOREIGN KEY(project_id,message_seq) REFERENCES messages(project_id,seq)
);
CREATE INDEX IF NOT EXISTS idx_symbols_lookup ON ast_symbols(project_id,symbol_name,message_seq);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(
    project_id UNINDEXED,
    seq UNINDEXED,
    body,
    identifiers,
    tokenize = "unicode61 remove_diacritics 2 tokenchars '_-./:$#@'"
);

CREATE TABLE IF NOT EXISTS compaction_runs (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    FOREIGN KEY(project_id,node_id) REFERENCES tree_nodes(project_id,node_id)
);
