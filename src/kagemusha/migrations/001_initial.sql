CREATE TABLE repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    remote_url TEXT NOT NULL,
    git_provider TEXT NOT NULL CHECK (git_provider IN ('github', 'gitlab', 'generic')),
    default_ref TEXT NOT NULL,
    credential_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('registered', 'syncing', 'ready', 'error')),
    last_fetched_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE working_copies (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    requested_ref TEXT NOT NULL,
    commit_sha TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('source', 'git')),
    branch_prefix TEXT,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'creating', 'ready', 'downloading', 'failed', 'deleting')),
    artifact_path TEXT,
    artifact_sha256 TEXT,
    artifact_size INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    error TEXT
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    payload_json TEXT NOT NULL,
    result_json TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);

CREATE INDEX jobs_claim_idx ON jobs(status, created_at);
CREATE INDEX jobs_lease_idx ON jobs(status, lease_expires_at);
CREATE INDEX copies_expiry_idx ON working_copies(status, expires_at);

CREATE TABLE seed_targets (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('gradle', 'pnpm')),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repository_id, relative_path, provider)
);

CREATE TABLE cache_groups (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    compatibility_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    active_snapshot_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(provider, compatibility_key, platform)
);

CREATE TABLE cache_snapshots (
    id TEXT PRIMARY KEY,
    cache_group_id TEXT NOT NULL REFERENCES cache_groups(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('staging', 'published', 'failed')),
    artifact_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    artifact_size INTEGER NOT NULL,
    manifest_path TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    download_count INTEGER NOT NULL DEFAULT 0 CHECK (download_count >= 0),
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE cache_seed_states (
    target_id TEXT NOT NULL REFERENCES seed_targets(id) ON DELETE CASCADE,
    cache_group_id TEXT NOT NULL REFERENCES cache_groups(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES cache_snapshots(id),
    seeded_at TEXT NOT NULL,
    PRIMARY KEY(target_id, cache_group_id)
);

CREATE TABLE cache_snapshot_targets (
    snapshot_id TEXT NOT NULL REFERENCES cache_snapshots(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES seed_targets(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, target_id)
);

CREATE INDEX snapshots_group_idx ON cache_snapshots(cache_group_id, published_at);
CREATE INDEX targets_repository_idx ON seed_targets(repository_id, enabled);
