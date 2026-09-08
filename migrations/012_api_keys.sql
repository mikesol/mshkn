-- 012_api_keys.sql
-- Scoped API keys (#88). accounts.api_key stays the unrestricted key; a row
-- here is a second kind of credential that can only do what its scopes say.
-- computers.api_key_id records which scoped key created a computer (NULL for
-- the account key) and is how "only computers this key created" is enforced.
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    secret TEXT UNIQUE NOT NULL,
    scopes_json TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL
);
ALTER TABLE computers ADD COLUMN api_key_id TEXT REFERENCES api_keys(id);
