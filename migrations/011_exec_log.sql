-- 011_exec_log.sql
-- The exec output of an ephemeral run, kept after the computer is gone (#58):
-- one row per run_ephemeral with a command, keyed by the computer it ran on,
-- expired by the reaper after exec_log_retention_seconds. ingress_log gains
-- the computer its action ran on so a trigger can be followed to its output.
CREATE TABLE IF NOT EXISTS exec_log (
    computer_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_checkpoint_id TEXT,
    created_checkpoint_id TEXT,
    label TEXT,
    command TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    stdout_truncated INTEGER NOT NULL DEFAULT 0,
    stderr_truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_log_created ON exec_log(created_at);

ALTER TABLE ingress_log ADD COLUMN computer_id TEXT;
