-- 010_indexes.sql
-- Indexes for the hot queries: computers by account+status, checkpoints by
-- account/computer/label ordered by created_at, deferred queue by label.
CREATE INDEX IF NOT EXISTS idx_computers_account_status ON computers(account_id, status);
CREATE INDEX IF NOT EXISTS idx_checkpoints_account_created ON checkpoints(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checkpoints_computer_created ON checkpoints(computer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checkpoints_label ON checkpoints(label);
CREATE INDEX IF NOT EXISTS idx_deferred_queue_label_created ON deferred_queue(label, created_at);
