CREATE TABLE _migrations (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    api_key TEXT UNIQUE NOT NULL,
    vm_limit INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE computers (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    thin_volume_id INTEGER NOT NULL,
    tap_device TEXT NOT NULL,
    vm_ip TEXT NOT NULL,
    socket_path TEXT NOT NULL,
    firecracker_pid INTEGER,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'creating',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_exec_at TEXT
);

CREATE TABLE checkpoints (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    parent_id TEXT REFERENCES checkpoints(id),
    computer_id TEXT,
    manifest_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    r2_prefix TEXT NOT NULL,
    disk_delta_size_bytes INTEGER,
    memory_size_bytes INTEGER,
    label TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE capability_cache (
    manifest_hash TEXT PRIMARY KEY,
    image_path TEXT NOT NULL,
    nix_closure_size_bytes INTEGER,
    image_size_bytes INTEGER,
    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
