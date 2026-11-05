-- Schema 2: plugin host state.
-- Per-plugin enablement, validated settings, and event-feed cursor.
CREATE TABLE plugins (
    id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    settings TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(settings) AND json_type(settings) = 'object'),
    cursor INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
-- Events a plugin failed on repeatedly and skipped.
CREATE TABLE plugin_failures (
    id INTEGER PRIMARY KEY,
    plugin TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    error TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX plugin_failures_plugin ON plugin_failures(plugin, id);
-- Private key-value storage per plugin.
CREATE TABLE plugin_state (
    plugin TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL CHECK(json_valid(value)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plugin, key)
);
-- Background work queued by plugins, claimed with leases.
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    plugin TEXT NOT NULL,
    kind TEXT NOT NULL,
    jot_id INTEGER REFERENCES jots(id) ON DELETE SET NULL,
    payload TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload)),
    dedupe_key TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    run_after REAL NOT NULL,
    lease_until REAL,
    result TEXT CHECK(result IS NULL OR json_valid(result)),
    error TEXT,
    correlation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX jobs_ready ON jobs(status, run_after);
CREATE UNIQUE INDEX jobs_dedupe ON jobs(plugin, dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');
