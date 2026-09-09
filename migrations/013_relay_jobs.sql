-- 013_relay_jobs.sql
-- The relay (#110): one row per job. The forwarded headers live in their own
-- column and are cleared the moment the upstream call settles; the response is
-- stored whole; the delivery (a fork by label) has its own status. Expired by
-- the reaper with exec_log_retention_seconds.
CREATE TABLE IF NOT EXISTS relay_jobs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    api_key_id TEXT REFERENCES api_keys(id),
    status TEXT NOT NULL,
    target TEXT NOT NULL,
    method TEXT NOT NULL,
    forward_headers_json TEXT,
    body_json TEXT,
    retry_json TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    deliver_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    response_status INTEGER,
    response_headers_json TEXT,
    response_body_json TEXT,
    delivery_status TEXT,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_computer_id TEXT,
    delivery_deferred_id TEXT,
    delivery_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relay_jobs_created ON relay_jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_relay_jobs_status ON relay_jobs(status);
