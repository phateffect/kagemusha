# SPEC — Kagemusha v2

## Git Repository, Working Copy & Dependency Cache Management

**Status:** Approved for implementation
**Version:** 2.0
**Language:** Python 3.15.2 / uv 0.13.9
**Deployment:** Docker Compose
**Database:** SQLite
**Toolchains:** mise / Gradle Wrapper / pnpm
**Target platform:** Linux amd64

---

# 1. Project Overview

Kagemusha is a self-hosted service responsible for preparing development workspaces for coding agents and other ephemeral execution environments.

It provides two primary capabilities:

1. Git repository and Working Copy management.
2. Shared development dependency cache preparation.

Kagemusha is designed to work with an external Orchestrator, but it must remain independently usable.

It must not depend on E2B, any specific coding agent, CI platform, or cloud vendor.

### Design principles

1. Single-host deployment.
2. Persistent state with SQLite and Docker Volumes.
3. Stateless HTTP API processes.
4. Independent background Worker.
5. Reuse existing CLI tools rather than reimplementing Git, Gradle, mise or pnpm.
6. Shared caches across repositories.
7. Immutable, reproducible published artifacts.
8. Incremental cache preparation.
9. Explicit failure handling and crash recovery.
10. Minimal operational complexity.

Because Kagemusha has not been released, existing internal API and schema designs may be replaced without backward-compatibility requirements.

Preserve working functionality and tests where useful, but prefer a consistent architecture over retaining obsolete abstractions.

---

# 2. Responsibilities and Boundaries

## 2.1 Kagemusha owns

- Git repository registration.
- Git authentication credential references.
- Git mirror storage.
- Repository synchronization.
- Working Copy creation and archival.
- Working Copy lifecycle and cleanup.
- Project toolchain detection.
- Dependency cache seeding.
- Cache compatibility grouping.
- Cache snapshot publishing.
- Incremental cache state.
- Cache artifact retrieval.
- Cache metadata and background jobs.

## 2.2 External Orchestrator owns

- Business projects.
- Coding tasks.
- Agent sessions.
- Task scheduling.
- E2B Sandbox creation.
- E2B Template construction.
- Agent permissions and execution policies.
- Deployment and CD.

Kagemusha must not contain domain models such as Agent, Task or Sandbox.

External systems should interact through stable HTTP APIs.

## 2.3 Naming

Use the following terminology consistently:

| Term | Definition |
|---|---|
| Repository | Registered remote Git repository |
| Mirror | Local bare Git mirror |
| Revision | Resolved Git commit SHA |
| Working Copy | Isolated source copy created from a revision |
| Seed Target | Repository + subdirectory + provider |
| Provider | Gradle or pnpm cache implementation |
| Cache Group | Set of mutually compatible cache contents |
| Cache Store | Mutable dependency store used during seeding |
| Cache Snapshot | Immutable published cache artifact |
| Seed Job | Background execution that updates cache contents |

Avoid using `Project` as an internal synonym for `Repository`.

---

# 3. System Architecture

Use a single Python codebase with two process entrypoints:

```bash
kagemusha serve
kagemusha worker
```

A CLI entrypoint may also expose administrative commands:

```bash
kagemusha db migrate
kagemusha db backup
kagemusha cache verify
kagemusha cache gc
```

The HTTP API handles requests and metadata operations.

The Worker handles operations that can take significant time or consume substantial resources:

- Git fetch.
- Working Copy archive creation.
- mise installation.
- Gradle execution.
- pnpm execution.
- Cache snapshot creation.
- Cache validation.

Do not execute long-running tasks inside HTTP request handlers.

## 3.1 Suggested Python modules

```text
src/kagemusha/
├── main.py
├── config.py
├── api/
│   ├── repositories.py
│   ├── copies.py
│   ├── cache_jobs.py
│   └── cache_snapshots.py
├── db/
│   ├── models.py
│   ├── migrations/
│   └── repository.py
├── git/
│   ├── mirror.py
│   ├── credentials.py
│   └── working_copy.py
├── cache/
│   ├── manager.py
│   ├── fingerprint.py
│   ├── snapshot.py
│   ├── storage.py
│   └── providers/
│       ├── gradle.py
│       └── pnpm.py
├── jobs/
│   ├── worker.py
│   ├── queue.py
│   └── executor.py
└── cli/
    └── commands.py
```

This is a suggested structure, not an instruction to create unnecessary layers.

Keep modules small and responsibilities explicit.

---

# 4. Repository Management

## 4.1 Repository model

Each Repository contains:

```text
id
name
remote_url
provider
default_ref
credential_ref
status
last_fetched_at
created_at
updated_at
```

`provider` refers to Git hosting, such as GitHub, GitLab or generic Git, and must not be confused with a cache provider.

The caller may supply a Repository ID. IDs must be unique.

`remote_url` must be canonicalized and validated.

Store credentials separately from public repository metadata.

## 4.2 Registration

```http
PUT /repositories/{repository_id}
```

Example:

```json
{
  "name": "customer-service-api",
  "remote_url": "https://gitlab.example.com/team/api.git",
  "default_ref": "main",
  "credential_ref": "gitlab-ci-reader"
}
```

The operation should be idempotent.

Updating the remote URL or credential association must trigger appropriate mirror consistency checks.

## 4.3 Repository synchronization

```http
POST /repositories/{repository_id}/fetch
```

Response:

```json
{
  "job_id": "job-123",
  "status": "queued"
}
```

Fetch must be executed asynchronously by the Worker.

Use existing Git CLI commands and Git mirror mechanisms.

Ensure that concurrent operations on the same mirror do not corrupt repository state.

Apply per-repository operation locks.

## 4.4 Git credentials

Support:

- GitHub token.
- GitLab token.
- Generic HTTPS credentials where applicable.
- Existing Kagemusha supported SSH credentials, if implemented.

Use gh/glab where their capabilities are useful, but Git CLI remains the source of truth for clone/fetch behavior.

Do not require gh or glab for generic Git repositories.

Store only a credential reference in Repository metadata.

Secrets must not appear in API responses, archive contents or logs.

---

# 5. Working Copy Management

## 5.1 Creation

```http
POST /repositories/{repository_id}/copies
```

Example:

```json
{
  "ref": "main",
  "depth": 1,
  "ttl_seconds": 3600
}
```

Resolve the Git ref to an immutable commit SHA before creating the copy.

A copy created from `main` must record the exact commit, not only the branch name.

A valid `depth` option must have documented semantics. Do not apply arbitrary truncation that makes a requested Git revision unavailable.

## 5.2 Source archive

Produce `tar.zst` archives.

A copy should include the intended Git working tree content, plus `.git` metadata only when requested by the supported copy mode.

Provide two explicit modes:

- `source`: source tree only.
- `git`: working directory with usable Git metadata, suitable for agents that must commit or push.

Do not silently deliver a source-only archive when the caller expects a fully functional Git checkout.

For `git` mode, preserve the required repository metadata without embedding server credentials or leaking internal mirror paths.

Use safe, portable Git mechanisms and verify that the restored copy can execute the required Git operations.

## 5.3 Retrieval

```http
GET /copies/{copy_id}
GET /copies/{copy_id}/download
DELETE /copies/{copy_id}
```

Return snapshot metadata and a streamed artifact download.

A Working Copy must not have an implicit dependency on a Cache Snapshot.

The external Orchestrator independently selects source and dependency cache artifacts.

## 5.4 Cleanup

Support TTL-based cleanup.

Do not remove a copy while it is being created or downloaded.

Use a safe state transition before deletion.

---

# 6. Database Design

Use SQLite as the single durable metadata database.

Configuration:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

Use a formal migration system.

All database transactions must be short.

Never hold a database transaction while running Git, Gradle, pnpm, mise, compression or network operations.

## 6.1 Tables

Use the following logical schema.

### repositories

```text
id                    TEXT PRIMARY KEY
name                  TEXT NOT NULL
remote_url            TEXT NOT NULL
git_provider          TEXT NOT NULL
default_ref           TEXT NOT NULL
credential_ref        TEXT
status                TEXT NOT NULL
last_fetched_at       TEXT
created_at            TEXT NOT NULL
updated_at            TEXT NOT NULL
```

### working_copies

```text
id                    TEXT PRIMARY KEY
repository_id         TEXT NOT NULL
requested_ref         TEXT NOT NULL
commit_sha            TEXT NOT NULL
mode                  TEXT NOT NULL
status                TEXT NOT NULL
artifact_path         TEXT
artifact_sha256       TEXT
created_at            TEXT NOT NULL
expires_at            TEXT
error                 TEXT
```

### jobs

Use one common job system for Git and Cache operations.

```text
id                    TEXT PRIMARY KEY
type                  TEXT NOT NULL
status                TEXT NOT NULL
payload_json          TEXT NOT NULL
result_json           TEXT
attempt               INTEGER NOT NULL DEFAULT 0
worker_id             TEXT
lease_expires_at      TEXT
created_at            TEXT NOT NULL
started_at            TEXT
finished_at           TEXT
error                 TEXT
```

### seed_targets

Each entry represents one cache target inside a repository.

```text
id                    TEXT PRIMARY KEY
repository_id         TEXT NOT NULL
relative_path         TEXT NOT NULL
provider              TEXT NOT NULL
enabled               INTEGER NOT NULL
config_json           TEXT NOT NULL
created_at            TEXT NOT NULL
updated_at            TEXT NOT NULL
```

Enforce uniqueness on:

```text
(repository_id, relative_path, provider)
```

### cache_groups

```text
id                    TEXT PRIMARY KEY
provider              TEXT NOT NULL
compatibility_key     TEXT NOT NULL
platform              TEXT NOT NULL
active_snapshot_id    TEXT
created_at            TEXT NOT NULL
```

`compatibility_key` describes the cache format compatibility requirements.

Do not require identical Kotlin, Quarkus or JDK versions to share a Gradle dependency store.

### cache_seed_states

```text
target_id             TEXT NOT NULL
cache_group_id        TEXT NOT NULL
commit_sha            TEXT NOT NULL
fingerprint           TEXT NOT NULL
snapshot_id           TEXT NOT NULL
seeded_at             TEXT NOT NULL

PRIMARY KEY (target_id, cache_group_id)
```

This table represents successfully **published** seed state, not merely a finished subprocess.

### cache_snapshots

```text
id                    TEXT PRIMARY KEY
cache_group_id        TEXT NOT NULL
status                TEXT NOT NULL
artifact_path         TEXT NOT NULL
artifact_sha256       TEXT NOT NULL
artifact_size         INTEGER NOT NULL
manifest_path         TEXT NOT NULL
created_at            TEXT NOT NULL
published_at          TEXT
```

### cache_snapshot_targets

```text
snapshot_id           TEXT NOT NULL
target_id             TEXT NOT NULL
commit_sha            TEXT NOT NULL
fingerprint           TEXT NOT NULL

PRIMARY KEY (snapshot_id, target_id)
```

The association table is critical for traceability and incremental seeding.

### Additional requirements

- Foreign keys for references.
- Unique constraints and indexes.
- Validated state transitions.
- UTC timestamps.
- Schema migrations.
- Integrity checks.
- Safe handling of partially published artifacts.

Use SQLite transactions when updating a published snapshot pointer and the corresponding seed states.

The filesystem cannot participate in SQLite transactions; publish artifacts to their final immutable paths before committing the metadata transaction.

---

# 7. Toolchain Detection

Kagemusha supports two cache providers:

- Gradle.
- pnpm.

mise is a toolchain manager, not a cache provider in V1.

## 7.1 Detection

Detect Gradle from:

```text
gradlew
gradle/wrapper/gradle-wrapper.properties
settings.gradle
settings.gradle.kts
```

Detect pnpm from:

```text
pnpm-lock.yaml
package.json
pnpm-workspace.yaml
```

Detection must support monorepos.

For example:

```text
repository/
├── backend/
│   ├── gradlew
│   └── build.gradle.kts
├── frontend/
│   ├── package.json
│   └── pnpm-lock.yaml
└── admin/
    └── package.json
```

The Seeder should create separate targets for `backend` and the correct pnpm workspace root.

Do not automatically seed the same pnpm workspace multiple times because it contains multiple packages.

## 7.2 Configuration

A Repository can declare targets explicitly:

```yaml
cache:
  targets:
    - path: backend
      provider: gradle
      tasks:
        - classes
        - testClasses

    - path: frontend
      provider: pnpm
```

Support automatic detection as the default.

Allow administrators to override incorrect detection.

Detection results must be queryable without triggering seeding.

---

# 8. mise Toolchain Management

Use mise to select and install tools.

Supported project configuration includes:

```text
mise.toml
.mise.toml
.tool-versions
```

Respect project-defined versions.

For Gradle, always use the project's `./gradlew`.

For pnpm, use the version selected by the supported mise/package-manager configuration.

Record the resolved exact tool versions.

Set persistent mise directories under:

```text
/data/mise/
```

Use documented mise environment variables, such as `MISE_DATA_DIR`, rather than inventing environment variable names.

Tools downloaded during one Seed Job should be reusable by later jobs.

### Missing toolchain versions

If a project does not declare a version:

1. Use an explicit Kagemusha default.
2. Record that a fallback was used.
3. Do not silently depend on whichever binary happens to be in `PATH`.

### Security

Project mise configurations may contain hooks or environment directives.

Treat them as executable project configuration.

Do not execute untrusted project configurations with unrestricted service credentials.

---

# 9. Gradle Cache Provider

## 9.1 Shared Gradle User Home

All Gradle Seed Targets in a compatible seeding environment should share one mutable dependency cache.

Example:

```text
/data/cache/work/gradle/
```

Set:

```bash
GRADLE_USER_HOME=/data/cache/work/gradle
```

Each target runs its own Wrapper:

```bash
mise exec -- ./gradlew classes testClasses --no-daemon
```

The configured tasks may differ by project.

Do not assume `dependencies` resolves every required artifact.

## 9.2 Dependency sharing

Gradle handles dependency caching and deduplication.

Kagemusha must not implement an artifact-level dependency database.

Different dependency versions can coexist.

Multiple repositories may contribute to the same cache.

Gradle cache metadata compatibility must be tested for the Wrapper versions being used.

## 9.3 Snapshot contents

Publish:

```text
modules-2/
```

from:

```text
$GRADLE_USER_HOME/caches/modules-2
```

Exclude lock files and `gc.properties`, according to Gradle's documented cache-copy guidance.

Create the snapshot only after all participating Gradle processes have exited.

Do not publish project-local `.gradle` or `build` directories.

## 9.4 Consumer

The snapshot must support:

```bash
GRADLE_RO_DEP_CACHE=/opt/kagemusha/gradle
```

With:

```text
/opt/kagemusha/gradle/modules-2/
```

Consumer workspaces use an independent writable `GRADLE_USER_HOME`.

If a dependency is missing, Gradle may download it into the consumer's writable cache.

## 9.5 Wrapper distributions

Gradle Wrapper distributions are a separate artifact group.

Source:

```text
$GRADLE_USER_HOME/wrapper/dists/
```

They must not be placed into `GRADLE_RO_DEP_CACHE`.

Provide restoration instructions for the E2B Template Builder.

## 9.6 Validation

For each supported project:

1. Restore a published cache into a clean environment.
2. Configure `GRADLE_RO_DEP_CACHE`.
3. Use a fresh writable Gradle User Home.
4. Execute configured verification tasks with `--offline`.
5. Verify success and capture errors.

A successful offline run proves the selected verification tasks are satisfied, not that every possible Gradle task is fully cached.

Validation must use the project's Wrapper version.

---

# 10. pnpm Cache Provider

## 10.1 Shared package store

pnpm Seed Targets should reuse a common package store within a compatible cache group.

Use an explicit store path when executing pnpm.

Example:

```bash
mise exec -- pnpm fetch \
  --store-dir /data/cache/work/pnpm
```

The implementation must discover the effective store structure rather than assuming all pnpm releases use the same layout.

## 10.2 Compatibility

Cache compatibility must consider:

- pnpm store format.
- pnpm version.
- OS.
- CPU architecture.
- Relevant installation configuration.

Do not assume pnpm major version alone fully determines compatibility.

Allow multiple pnpm cache groups.

## 10.3 Snapshot

Snapshot the package store, not `node_modules`.

Do not snapshot compiled application outputs.

Use a separate artifact from Gradle.

## 10.4 Consumer

V1 should restore the pnpm store into a writable directory.

Example:

```bash
pnpm install \
  --offline \
  --frozen-lockfile \
  --store-dir /workspace/.cache/pnpm
```

Do not assume pnpm shares Gradle's read-only cache semantics.

Native packages and install scripts may require additional work.

## 10.5 Validation

Use a clean temporary copy of the project's required manifests and files.

Perform:

```bash
pnpm install --offline --frozen-lockfile
```

With the restored store.

Validate pnpm workspaces as well as normal projects.

Never modify the original lockfile during validation.

---

# 11. Incremental Seeding

This is a first-class requirement.

## 11.1 Dependency fingerprint

Compute a fingerprint for each Seed Target.

Gradle fingerprint inputs include:

- Gradle settings and build scripts.
- Version catalogs.
- Wrapper configuration.
- Dependency declaration files.
- Relevant `buildSrc` and included-build configuration.
- mise configuration.
- Configured seed tasks.
- Cache provider version and compatibility settings.

pnpm fingerprint inputs include:

- `pnpm-lock.yaml`.
- `pnpm-workspace.yaml`.
- Relevant package manifests.
- Non-secret installation configuration.
- Toolchain configuration.
- Cache provider version and compatibility settings.

Do not hash secrets into publicly exposed metadata.

Calculate fingerprints using deterministic ordering.

A Git commit change alone must not necessarily trigger reseeding.

## 11.2 Incremental algorithm

At the beginning of a Seed Job:

1. Resolve all enabled Seed Targets.
2. Fetch relevant Git revisions.
3. Calculate current fingerprints.
4. Identify the active published Snapshot for each cache group.
5. Verify the snapshot and mutable working cache are valid.
6. Compare fingerprints against the states represented by the active snapshot.
7. Seed changed or missing targets.
8. Reuse existing dependencies from the shared cache.
9. Create candidate snapshots.
10. Validate candidates.
11. Publish new snapshots.
12. Atomically update published states in SQLite.

If nothing changed and the existing snapshot is valid, complete the job without generating a duplicate snapshot.

## 11.3 Important invariant

**A target must not be considered successfully seeded merely because its build command completed.**

It becomes successfully seeded for publication purposes only when its required contents are represented by a successfully published, validated snapshot.

## 11.4 Mutable cache recovery

Incremental seeding relies on an existing mutable cache that is compatible with the current published state.

If the mutable cache is missing or cannot be trusted:

- Restore from the current published snapshot when supported.
- Otherwise perform a full reseed.
- Never treat missing local cache files as a cache hit based only on SQLite metadata.

For Gradle, restore a snapshot into a separate writable Seeder cache area; do not mutate the published immutable artifact.

## 11.5 Full rebuild

Support:

```json
{
  "mode": "full"
}
```

A Full Rebuild:

- Uses a fresh mutable cache directory.
- Processes all enabled targets in the selected groups.
- Creates candidate snapshots.
- Validates them.
- Replaces the stable snapshot pointer only after success.

This is the primary cache compaction strategy.

Do not implement an artifact-level garbage collector for Gradle or pnpm in V1.

## 11.6 Dynamic dependencies

Some Gradle dependencies use:

- Dynamic versions.
- SNAPSHOT versions.
- Changing modules.
- External configuration.

Fingerprinting cannot detect every possible dependency change.

Support:

- Forced reseeding.
- Periodic full rebuilds.
- Optional dependency refresh policies.

Document that a fingerprint match is not a universal proof that remote dependencies are unchanged.

---

# 12. Job System

Use SQLite-backed durable jobs.

Supported types:

```text
repository.fetch
copy.create
cache.seed
cache.full_rebuild
cache.verify
cache.gc
```

## 12.1 State machine

```text
queued
   |
   v
running
   |
   +---> succeeded
   |
   +---> failed
   |
   +---> cancelled
```

Store partial results at the target level.

The overall Seed Job may also report `partial` if the system intentionally supports publishing validated results from a subset of targets.

Do not silently publish incomplete groups.

## 12.2 Worker claiming

Use an atomic SQLite claim operation.

Store:

- Worker ID.
- Attempt count.
- Lease expiry.
- Last progress timestamp, if needed.

Use a lease or heartbeat policy appropriate for long-running commands.

A Worker must not hold a database transaction while executing subprocesses.

## 12.3 Crash recovery

On Worker restart:

- Detect expired jobs.
- Inspect temporary artifacts.
- Remove or quarantine incomplete staging directories.
- Requeue safe idempotent work or fail jobs with an explicit reason.
- Do not allow two Workers to mutate the same cache group simultaneously.

V1 may enforce a single Worker instance and one active seed job.

Still implement recovery in a way that does not permanently strand jobs.

## 12.4 Process management

Use asyncio subprocess APIs.

Apply configurable:

- Execution timeout.
- CPU and memory limits.
- Output size limits.
- Graceful termination timeout.

On cancellation:

1. Send termination to the subprocess group.
2. Allow a bounded grace period.
3. Force termination if necessary.
4. Reap child processes.
5. Clean temporary resources.

---

# 13. Cache Snapshots

## 13.1 Artifact groups

At minimum:

```text
gradle-dependencies
gradle-wrapper
pnpm-store
```

Artifacts are immutable.

Use `tar.zst`.

Each snapshot includes a manifest and archive checksum.

## 13.2 Snapshot identity

Use a stable, content-derived identifier.

Record:

```text
snapshot_id
provider
cache_group_id
platform
created_at
tool_versions
source_targets
fingerprints
archive_sha256
archive_size
validation_result
```

The manifest must be sufficient to explain where a snapshot came from, even without querying the database.

## 13.3 Publication

Publication sequence:

1. Acquire cache group lock.
2. Prepare candidate in a staging directory.
3. Finish all seeding processes.
4. Produce immutable archive.
5. Compute checksum.
6. Verify archive integrity.
7. Perform offline validation.
8. Place archive and manifest at their final immutable paths.
9. Commit SQLite metadata and active snapshot pointer.
10. Release lock.
11. Clean unused staging artifacts.

If any step fails, retain the previous active snapshot.

If the process crashes after step 8 and before step 9, startup recovery must identify the orphaned artifact.

## 13.4 Retention

Initial policy:

- Retain the active snapshot.
- Retain the last three previously published snapshots per group.
- Retain artifacts referenced by active operations.
- Remove unreferenced expired artifacts during scheduled GC.

Do not remove snapshots used by an E2B Template merely because they are no longer active.

Support explicit pinning or retention of snapshots by immutable ID.

---

# 14. HTTP API

All APIs should use consistent naming and status semantics.

Use asynchronous jobs for expensive operations.

## 14.1 Repositories

```http
PUT    /repositories/{id}
GET    /repositories
GET    /repositories/{id}
DELETE /repositories/{id}

POST   /repositories/{id}/fetch
POST   /repositories/{id}/copies
```

## 14.2 Working Copies

```http
GET    /copies/{id}
GET    /copies/{id}/download
DELETE /copies/{id}
```

## 14.3 Seed Targets

```http
GET    /repositories/{id}/seed-targets
PUT    /repositories/{id}/seed-targets/{target_id}
DELETE /repositories/{id}/seed-targets/{target_id}
```

Automatic discovery:

```http
POST /repositories/{id}/seed-targets/discover
```

Discovery may run asynchronously if repository synchronization is required.

## 14.4 Cache Jobs

```http
POST /cache-jobs
GET  /cache-jobs
GET  /cache-jobs/{id}
POST /cache-jobs/{id}/cancel
```

Example request:

```json
{
  "repository_ids": [
    "api",
    "web"
  ],
  "providers": [
    "gradle",
    "pnpm"
  ],
  "mode": "incremental"
}
```

If `repository_ids` is omitted, use all enabled repositories authorized for the request.

Supported modes:

```text
incremental
full
force
```

The exact distinction between `full` and `force` must be implemented consistently: `force` reruns selected targets while `full` builds a fresh shared cache from the selected complete target set.

## 14.5 Cache Snapshots

```http
GET /cache-groups
GET /cache-groups/{id}

GET /cache-snapshots
GET /cache-snapshots/{id}
GET /cache-snapshots/{id}/download
```

Expose current active snapshot IDs in Cache Group metadata.

Do not overload a global `stable` endpoint when multiple incompatible pnpm or Gradle cache groups exist.

## 14.6 Administrative actions

```http
POST /cache-groups/{id}/verify
POST /cache-groups/{id}/rebuild
POST /maintenance/gc
```

Administrative endpoints must have explicit access control.

## 14.7 API requirements

- OpenAPI documentation.
- Structured errors.
- Request validation.
- Authentication.
- Correct HTTP status codes.
- Paginated list operations.
- Stable resource IDs.
- Streaming downloads.
- Idempotent resource creation where possible.

Avoid exposing internal filesystem paths in public API responses.

---

# 15. Storage Layout

Use a Docker named volume mounted at `/data`.

Suggested structure:

```text
/data/
├── kagemusha.db
├── git/
│   └── mirrors/
├── copies/
│   ├── staging/
│   └── artifacts/
├── cache/
│   ├── work/
│   │   ├── gradle/
│   │   └── pnpm/
│   ├── staging/
│   └── snapshots/
│       ├── gradle-dependencies/
│       ├── gradle-wrapper/
│       └── pnpm-store/
├── mise/
├── jobs/
└── backups/
```

The SQLite database and WAL sidecars must remain on a real filesystem supporting SQLite locking.

Do not mount SQLite over NFS.

All persistent paths must be configurable but default to `/data`.

Snapshot storage should expose a small abstraction allowing S3-compatible storage in the future.

Do not implement OSS/TOS in V1 unless the existing repository already contains a suitable storage abstraction.

---

# 16. Docker Deployment

Provide:

- Dockerfile.
- compose.yaml.
- Health checks.
- Persistent named volume.
- Non-root execution.
- Resource limits.
- Graceful shutdown.

Recommended base image:

```text
Debian trixie-slim
```

Use uv to install the Python application.

Install only required system tools:

- Git.
- mise.
- tar.
- zstd.
- CA certificates.
- Required shell/runtime dependencies.

Use mise to install Java, Node.js and pnpm.

Do not preinstall every possible toolchain version into the image.

Both API and Worker should run from the same application image.

Example:

```yaml
services:
  api:
    build: .
    command: ["kagemushaa", "serve"]
    volumes:
      - kagemushaa-data:/data
    restart: unless-stopped

  worker:
    build: .
    command: ["kagemushaa", "worker"]
    volumes:
      - kagemushaa-data:/data
    restart: unless-stopped

volumes:
  kagemushaa-data:
```

Provide an operational deployment configuration with health checks, environment variables and resource limits.

No local bind mounts should be required.

---

# 17. Security

## 17.1 Trust boundary

Repositories are executable code.

A Gradle build, pnpm lifecycle script, mise hook or project-specific configuration can execute arbitrary commands.

Consequently, seeding must never run with Kagemusha API administration privileges.

## 17.2 Isolation

At minimum:

- Worker runs as non-root.
- No Docker socket mount.
- No privileged container.
- No host filesystem mount.
- Separate CPU/memory limits.
- Strict subprocess timeouts.
- Controlled Git credentials.
- Restricted filesystem access.
- No API signing key or administration tokens passed into builds.

V1 may explicitly support only trusted, administrator-registered repositories.

For untrusted repositories, require a stronger isolated execution backend before enabling automatic seeding.

A Python subprocess alone is not a security sandbox.

## 17.3 Credentials

Use references to stored/injected credentials.

Never include credentials in:

- Cache snapshots.
- Working Copy artifacts.
- Job metadata.
- Logs.
- API responses.
- Published manifests.

Do not automatically copy user `.npmrc`, `.netrc`, Gradle properties, or mise environment files into cache snapshots.

## 17.4 Archive safety

Prevent:

- Path traversal.
- Malicious symlinks.
- Extraction outside destination.
- Unbounded decompression.
- Overwriting unrelated files.

Verify checksums before extraction.

---

# 18. Operational Reliability

## 18.1 Backups

Use SQLite Online Backup API.

Provide:

```bash
kagemusha db backup
```

Backups must include consistent database state.

Snapshot artifacts require an independent backup/retention policy.

Do not assume SQLite backup alone can restore dependency archives.

## 18.2 Health checks

Provide:

```http
GET /health/live
GET /health/ready
```

Readiness should verify necessary local dependencies, including database access.

Worker health must be observable independently from API health.

## 18.3 Logging

Use structured logs.

Include:

```text
request_id
job_id
repository_id
seed_target_id
cache_group_id
snapshot_id
duration
status
error
```

Never log full credentials or sensitive environment variables.

## 18.4 Metrics

V1 may use logs and database queries.

Do not require a Prometheus server.

Design for future metrics without coupling the application to an observability vendor.

---

# 19. Testing and Acceptance

## 19.1 Git

Verify:

- Repository registration.
- Idempotent updates.
- Credential use.
- Mirror fetch.
- Concurrent fetch safety.
- Working Copy archive creation.
- Restored Git-mode copy can operate as intended.
- TTL cleanup.

## 19.2 SQLite

Verify:

- Schema migrations.
- Referential integrity.
- Concurrent API/Worker access.
- Atomic job claiming.
- Job recovery after restart.
- Recovery from interrupted snapshot publication.

## 19.3 Gradle

Use at least two real test projects with overlapping dependencies.

Verify:

- Shared dependency downloads.
- Multiple Wrapper versions.
- Different dependency versions.
- Cache snapshot generation.
- Cache read-only compatibility.
- Offline task execution.
- No mutation of published cache.

## 19.4 pnpm

Use at least two projects with overlapping packages.

Verify:

- Shared package store.
- Lockfile-based fetch.
- pnpm workspace handling.
- Store snapshot restoration.
- Offline installation.
- Compatibility separation.

## 19.5 Incremental seeding

Test:

1. Initial full seed.
2. Second run without changes.
3. Change only one target's dependencies.
4. Verify only changed targets are reseeded.
5. Publish the new shared snapshot.
6. Restore it in a clean environment.
7. Verify unchanged targets still work.
8. Delete mutable working cache.
9. Verify correct recovery/rebuild.
10. Trigger a failed seed and confirm old stable snapshot remains usable.

## 19.6 End-to-end acceptance

The following workflow must work:

```text
Register Repository A
Register Repository B
      |
      v
Fetch repositories
      |
      v
Discover Gradle/pnpm targets
      |
      v
Run full cache seed
      |
      v
Publish immutable snapshots
      |
      v
Modify dependency in A
      |
      v
Run incremental seed
      |
      v
Only changed targets execute
      |
      v
New validated snapshots published
      |
      v
Clean consumer restores caches
      |
      v
Gradle/pnpm offline verification succeeds
```

---

# 20. Implementation Plan

Implement in five stages.

## Stage 1 — Repository and persistence foundation

- Refactor `projects` to `repositories`.
- Normalize repository and Working Copy APIs.
- Introduce SQLite migrations.
- Implement durable jobs.
- Split API and Worker entrypoints.
- Keep Git operations functional.

**Milestone:** Kagemusha remains a fully working Git Working Copy service with durable state.

## Stage 2 — Gradle seeding

- Add mise integration.
- Add Seed Target discovery.
- Implement Gradle provider.
- Introduce shared mutable cache.
- Implement snapshot creation.
- Implement offline verification.
- Introduce Cache Group and Snapshot models.

**Milestone:** Multiple Gradle projects share a reusable cache snapshot.

## Stage 3 — pnpm

- Implement pnpm provider.
- Implement compatibility grouping.
- Support pnpm workspaces.
- Support package store snapshots.
- Implement offline validation.

**Milestone:** Gradle and pnpm both work through the same Cache Manager.

## Stage 4 — Incremental and resilience

- Add dependency fingerprints.
- Implement incremental seeding.
- Implement full rebuild.
- Add job recovery.
- Add publication recovery.
- Add snapshot retention.
- Add Worker cancellation and timeouts.

**Milestone:** Incremental cache updates are safe across failures and restarts.

## Stage 5 — Operations and documentation

- Complete Docker Compose deployment.
- Add SQLite backup.
- Add health checks.
- Add API documentation.
- Add E2B consumer examples.
- Add integration tests.
- Document maintenance and disaster recovery.

**Milestone:** Single-command deployment and reliable end-to-end operations.

---

# 21. Explicit Non-goals for V1

Do not implement:

- PostgreSQL.
- Redis.
- Celery.
- Kubernetes.
- Distributed Cache Workers.
- E2B SDK integration.
- Git hosting provider management UI.
- Remote Gradle Build Cache.
- Maven/npm registry proxies.
- Global deduplication across unrelated cache formats.
- Custom dependency resolution engines.
- Complex cache eviction based on individual packages.
- Automatic seeding on every Working Copy creation.
- CI/CD pipelines.
- Agent execution or orchestration.

Do not build abstractions for hypothetical requirements unless they directly simplify the current implementation.

---

# 22. Instructions to the Coding Agent

This document defines the target architecture.

Before implementing:

1. Inspect the entire existing Kagemusha repository.
2. Identify reusable code.
3. Identify conflicting models/APIs.
4. Create a concise implementation plan.
5. Refactor freely where necessary because the project is not released.
6. Avoid migration compatibility layers for unused legacy APIs.
7. Implement in the specified stages.
8. Add tests with each stage.
9. Keep each stage independently verifiable.

Use existing project conventions where sensible, but prefer this SPEC where the architecture conflicts.

Do not ask for confirmation on ordinary implementation choices.

If a documented tool behavior differs from assumptions in this SPEC, follow the tool's current official behavior, document the difference and adjust the implementation accordingly.

Prioritize:

**Correctness → Reliability → Simplicity → Performance.**

The result should be a cohesive, maintainable Kagemusha service—not a collection of independent scripts.

---

# 23. Definition of Done

Kagemusha v2 is complete when:

- Repositories and Working Copies have a unified API and data model.
- SQLite persistently manages repository and job state.
- Cache targets support monorepos.
- mise resolves project toolchains.
- Gradle dependencies are shared across repositories.
- pnpm packages are shared across compatible repositories.
- Immutable snapshots can be restored in clean environments.
- Incremental seeding skips unchanged targets safely.
- Full rebuilds can recover or compact caches.
- Failures do not corrupt published snapshots.
- API and Worker run through Docker Compose.
- Existing functionality is preserved or intentionally replaced.
- No external database, message queue or cache server is required.
- The system is independent of E2B and any specific Agent.

**Final architectural invariant:**

Kagemusha owns Git source preparation and development dependency cache preparation. The external Orchestrator owns agent execution and workspace lifecycle. Cache formats remain managed by their native tools; Kagemusha owns only their preparation, versioning, distribution and recovery.
