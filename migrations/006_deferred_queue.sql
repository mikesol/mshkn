CREATE TABLE IF NOT EXISTS deferred_queue (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    account_id TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deferred_queue_label ON deferred_queue(label);
