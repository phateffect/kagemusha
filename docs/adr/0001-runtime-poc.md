# ADR 0001 — Shogun runtime proof of concept

Status: evidence recorded, production decisions pending.

## Reproduce

From the repository root, use Node.js 24 and pnpm 11.25.0. Neither the Python Shogun preview service nor the future database/UI is needed.

```sh
pnpm --dir scripts/poc install --frozen-lockfile
pnpm --dir scripts/poc check
pnpm --dir scripts/poc test
.venv/bin/python -u scripts/poc/live-smoke.py
```

The last command is opt-in and makes real calls to Alibaba Cloud Sandbox and DeepSeek. Use disposable provider credentials, the Shanghai PoC template, and a disposable project. The harness reads the Shogun sandbox and model settings from the environment or local `.env`; it serves a generated Git repository over a loopback Kagemusha contract fixture. It runs a Python source fix, checks the result, and tests cancellation with a remote shell child. It creates no GitLab branches or merge requests. The Python harness needs the repository venv and `tar` with zstd support.

For an already prepared disposable Kagemusha copy, use a ready `mode=git`, `depth=0` artifact. Provide the exact commit from the copy record and an explicit allowlist of changed paths.

```sh
export SHOGUN_KAGEMUSHA_URL=http://127.0.0.1:8000
# Supply SHOGUN_KAGEMUSHA_API_TOKEN via your local secret configuration.
pnpm --dir scripts/poc poc COPY_ID BASE_SHA "Fix the disposable fixture" calculator.py
pnpm --dir scripts/poc poc COPY_ID BASE_SHA "Review the earlier fix" calculator.py /path/to/checkpoint
```

The CLI writes an owner-only checkpoint directory and publishes its path after the artifact index is written. Set `SHOGUN_POC_OUTPUT` to an existing private directory to choose the output location. `SHOGUN_POC_REPO_SKILLS` takes an explicitly selected list of remote Markdown files. `SHOGUN_POC_COMPANY_SKILL` is an operator-provided trusted text file. `SHOGUN_POC_CHECK` is an operator-provided remote check.

## Verified

- **Pi 1.6.1.** The pinned SDK exports `createAgentSession`, the tool-definition factories with operations, `DefaultResourceLoader`, `ModelRuntime`, and `SessionManager.create/open/getSessionFile`. The run uses an isolated host directory, disables discovery, and explicitly exposes only the four remote tools. It subscribes to SDK events and waits for `prompt()` and `waitForIdle()`; the live run observed `agent_settled` after tool execution. The CLI reports event types without raw deltas or tool output. Abort delegates to Pi and triggers disposal of the whole sandbox. JSONL, rather than the CLI progress stream, restores the session.
- **E2B JS 2.31.0 and Alibaba Cloud Shanghai.** Real runs created sandboxes from template `0026xpadb3sq0lbkngd8`, uploaded a checked archive, ran shell/check commands, used filesystem operations and destroyed the sandboxes. Provisioning took roughly 0.4–0.7 seconds in the observed fixture runs, excluding transfer and inference. Fixed template sizing was used. Command discovery includes pre-existing provider/template processes, so the PoC compares against a baseline. An interrupted tool run with a shell child caused confirmed sandbox destruction and no new checkpoint.
- **Credential and network fixture.** The live fixture checked that the model, E2B management and Kagemusha token environment variables were absent in the sandbox. A curl request to a public test destination failed with `allowInternetAccess: false`. This probe does not establish isolation from private networks, metadata, IPv6, or every transport. No push credentials were configured.
- **Kagemusha contract.** Actual FastAPI endpoints are `POST /repositories/{id}/copies`, `GET /copies/{id}` and `GET /copies/{id}/download`. The job hands off an artifact with `commit_sha`, `artifact_sha256`, `artifact_size`, `expires_at`, and the download returns `X-Artifact-SHA256`. The live CLI exercised this wire contract through a disposable fixture. The existing Python suite exercised the actual service. A SHA mismatch, a truncated or corrupt archive, a shallow copy, and an oversized archive are independently rejected by the adapter or by the post-extraction Git checks.
- **Checkpoint and mock Draft MR.** The live coding task produced an edit, a new text file, a deletion, and a binary addition. The finalizer staged explicitly allowed paths, wrote an incremental bundle ref, and a separate bare host repo checked the bundle and generated the binary-capable review diff. The mock Draft MR payload was bound to the verified head. Recreating a sandbox from the same SHA and importing the bundle plus Pi JSONL retained both the marker file and prior conversation. A separate deterministic test covered the clean base/no-change case and rejected unexpected paths.
- **Checks run.** Strict TypeScript check; two Node tests; 31 Python tests; live fixture with real Pi/DeepSeek and E2B; verified recovery and cancellation. The representative fixture was Python, not Kotlin/Gradle.

## Proposed changes and Phase 1 gates

1. **Kagemusha exact-SHA recovery and transfer.** The existing resolver rejects raw 40/64-digit hashes, and downloads require a Kagemusha Bearer Token. Add a pinned-commit copy contract with retention/expiry semantics and a scoped expiring artifact URL. This PoC verifies and privately retains the original full-depth archive and uploads it from the host, capped at 25 MiB. It does not re-resolve a moving branch or send the service token into E2B. Cache manifest integration is pending.
2. **Provider boundaries.** Verify private-network and metadata denial with controlled endpoints and the actual deployment policy. Launch-time CPU/memory overrides, cancellation of individually detached processes, and orphan reconciliation were not established. Provider command lists do not prove arbitrary detached process quiescence. For the fixture we destroy the entire sandbox on cancellation, retain no incomplete checkpoint, and use the provider TTL as a cleanup fallback. Test timeouts, failure after create, compromised Git config, and sandbox egress before allowing production projects.
3. **Finalizer breadth.** The fixture supports SHA-1, small full-depth workspaces, explicit file paths, and working-tree changes. It rejects agent-created commits, changed symlinks/gitlinks, and cumulative reversions with a non-base HEAD. The production finalizer must preserve approved agent commits and validate every reachable object/path and artifact size, scan history/artifacts for secrets, safely handle Git attributes/filters and linked worktrees, and prove the checked tree matches the publication candidate. The bare-host import is a PoC review, not a proof that arbitrary untrusted Git artifacts are safe for a production publisher.
4. **Durability.** This PoC emits a local manifest last and validates checksums on restore. Phase 1 needs size-limited immutable storage, transactional references and fencing, explicit cleanup status, and no automatic prompt replay. No production scheduling, persistence, UI, or GitLab publication is included.
