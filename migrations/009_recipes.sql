-- 009_recipes.sql
-- Recipe system: Docker-based environment builds replacing Nix capabilities

CREATE TABLE IF NOT EXISTS recipes (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    dockerfile TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    build_log TEXT,
    base_volume_id INTEGER,
    template_vmstate TEXT,
    template_memory TEXT,
    created_at TEXT NOT NULL,
    built_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_account_hash ON recipes(account_id, content_hash)
    WHERE status != 'failed';

ALTER TABLE computers ADD COLUMN recipe_id TEXT REFERENCES recipes(id);
ALTER TABLE checkpoints ADD COLUMN recipe_id TEXT REFERENCES recipes(id);
