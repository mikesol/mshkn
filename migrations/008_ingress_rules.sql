-- Ingress rules: user-defined Starlark transforms that map external webhooks to API calls
CREATE TABLE IF NOT EXISTS ingress_rules (
    internal_id TEXT PRIMARY KEY,
    id TEXT UNIQUE NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    starlark_source TEXT NOT NULL,
    response_mode TEXT NOT NULL DEFAULT 'async',
    max_body_bytes INTEGER NOT NULL DEFAULT 10485760,
    rate_limit_rpm INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingress_rules_account_id ON ingress_rules(account_id);

CREATE TABLE IF NOT EXISTS ingress_log (
    id TEXT PRIMARY KEY,
    rule_internal_id TEXT NOT NULL REFERENCES ingress_rules(internal_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    starlark_result TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingress_log_rule_created ON ingress_log(rule_internal_id, created_at);
