-- L3 capability memory cache: FC memory snapshots per manifest hash
CREATE TABLE IF NOT EXISTS snapshot_templates (
    manifest_hash TEXT PRIMARY KEY,
    vmstate_path TEXT NOT NULL,
    memory_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
