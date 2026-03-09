-- migrations/004_capability_cache_volume_id.sql
DROP TABLE IF EXISTS capability_cache;

CREATE TABLE capability_cache (
    manifest_hash TEXT PRIMARY KEY,
    volume_id INTEGER NOT NULL,
    nix_closure_size_bytes INTEGER,
    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
