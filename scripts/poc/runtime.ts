import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { CommandExitError, Sandbox } from "e2b";
import { repo, turn } from "./agent.ts";
import { checkpoint, checkpointRef, saveCheckpoint, trustedDiff, verifiedArtifact } from "./checkpoint.ts";
import { workspace } from "./kagemusha.ts";

type Artifact = { sha256: string; size: number };
type Manifest = {
  base: string; head: string; hasChanges: boolean; artifacts: Record<string, Artifact>;
};

const required = (name: string) => {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing ${name}`);
  return value;
};

async function main() {
  const [copyId, base, prompt, allowed = "", previous] = process.argv.slice(2);
  if (!copyId || !base || !prompt || !/^[a-f0-9]{40}$/.test(base)) {
    throw new Error("Usage: poc COPY_ID BASE_SHA PROMPT [ALLOWLIST_CSV] [PREVIOUS_CHECKPOINT_DIR]");
  }
  const terminate = new AbortController();
  const cancel = () => terminate.abort(new Error("PoC cancelled"));
  process.on("SIGINT", cancel);
  process.on("SIGTERM", cancel);
  const signal = AbortSignal.any([terminate.signal, AbortSignal.timeout(480_000)]);
  const scratch = await mkdtemp(join(tmpdir(), "shogun-pi-"));
  let sandbox: Sandbox | undefined;
  let cleanup: Promise<boolean> | undefined;
  let output: string | undefined;
  let complete = false;
  const dispose = () => {
    if (sandbox) cleanup ??= sandbox.kill({ requestTimeoutMs: 20_000 });
    return cleanup;
  };
  const abortSandbox = () => { void dispose()?.catch(() => undefined); };
  signal.addEventListener("abort", abortSandbox, { once: true });
  try {
    let prior: Manifest | undefined;
    let previousSession: Uint8Array | undefined;
    let previousBundle: Uint8Array | undefined;
    let archive: Uint8Array;
    if (previous) {
      prior = JSON.parse(await readFile(join(previous, "manifest.json"), "utf8")) as Manifest;
      if (prior.base !== base || !/^[a-f0-9]{40}$/.test(prior.head) || prior.hasChanges !== (prior.head !== base)) {
        throw new Error("Previous checkpoint does not match the pinned base");
      }
      archive = await verifiedArtifact(previous, "workspace.tar.zst", prior.artifacts["workspace.tar.zst"]);
      previousSession = await verifiedArtifact(previous, "session.jsonl", prior.artifacts["session.jsonl"]);
      if (prior.hasChanges) {
        previousBundle = await verifiedArtifact(previous, "checkpoint.bundle", prior.artifacts["checkpoint.bundle"]);
      }
    } else {
      archive = await workspace(new URL(required("SHOGUN_KAGEMUSHA_URL")),
        required("SHOGUN_KAGEMUSHA_API_TOKEN"), copyId, base, signal);
    }
    const region = process.env.SHOGUN_SANDBOX_REGION || "cn-shanghai";
    if (!/^[a-z0-9-]+$/.test(region)) throw new Error("Invalid region");
    const domain = `${region}.sandbox.aliyuncs.com`;
    const apiKey = required("SHOGUN_E2B_APIKEY");
    const key = process.env.SHOGUN_PI_API_KEY || required("DEEPSEEK_API_KEY");
    const template = required("SHOGUN_SANDBOX_TEMPLATE");
    signal.throwIfAborted();
    const started = performance.now();
    const instance = sandbox = await Sandbox.create(template, {
      apiKey, domain, apiUrl: `https://api.${domain}`, timeoutMs: 540_000, requestTimeoutMs: 30_000, allowInternetAccess: false,
    });
    process.stderr.write(`Sandbox provisioned in ${Math.round(performance.now() - started)} ms\n`);
    signal.throwIfAborted();
    await instance.files.write("/tmp/shogun-poc/workspace.tar.zst", Uint8Array.from(archive).buffer);
    await instance.commands.run("tar --zstd -xf workspace.tar.zst && rm workspace.tar.zst",
      { cwd: "/tmp/shogun-poc", signal, timeoutMs: 60_000 });
    const run = async (command: string) => (await instance.commands.run(command,
      { cwd: repo, signal, timeoutMs: 60_000 })).stdout;
    if ((await run("git rev-parse HEAD")).trim() !== base) throw new Error("Extracted Git HEAD does not match pinned base");
    if ((await run("git rev-parse --is-shallow-repository")).trim() !== "false") throw new Error("Shallow base cannot support PoC bundles");
    await run("git bundle create /tmp/shogun-poc/base.bundle HEAD");
    const baseBundle = await instance.files.read("/tmp/shogun-poc/base.bundle", { format: "bytes" });
    if (previousBundle) {
      await instance.files.write("/tmp/shogun-poc/restore.bundle", Uint8Array.from(previousBundle).buffer);
      await run(`git bundle verify /tmp/shogun-poc/restore.bundle &&
        git fetch --no-tags /tmp/shogun-poc/restore.bundle ${checkpointRef}:refs/shogun/restore &&
        git -c core.hooksPath=/dev/null checkout --detach refs/shogun/restore`);
      if ((await run("git rev-parse HEAD")).trim() !== prior?.head) throw new Error("Restored Git HEAD does not match checkpoint");
    }
    const baseline = new Set((await instance.commands.list()).map(({ pid }) => pid));
    const session = await turn({
      sandbox: instance, scratch, prompt, key, signal, sessionInput: previousSession,
      model: process.env.SHOGUN_PI_MODEL || "deepseek-flash",
      companyText: process.env.SHOGUN_POC_COMPANY_SKILL
        ? await readFile(process.env.SHOGUN_POC_COMPANY_SKILL, "utf8") : "",
      skills: process.env.SHOGUN_POC_REPO_SKILLS?.split(",").filter(Boolean) ?? [],
    });
    // Give provider command bookkeeping a bounded chance to settle.
    for (let attempt = 0; (await instance.commands.list()).some(({ pid }) => !baseline.has(pid)); attempt++) {
      if (attempt === 9) throw new Error("Remote commands did not quiesce");
      await sleep(250, undefined, { signal });
    }
    const state = await checkpoint(run, base, prior?.head ?? base, allowed.split(",").filter(Boolean));
    let check: { status: "passed" | "failed" | "skipped"; exitCode?: number } = { status: "skipped" };
    if (process.env.SHOGUN_POC_CHECK) {
      try {
        await run(process.env.SHOGUN_POC_CHECK);
        check = { status: "passed", exitCode: 0 };
      } catch (error) {
        if (!(error instanceof CommandExitError)) throw error;
        check = { status: "failed", exitCode: error.exitCode };
      }
      const clean = await run("git status --porcelain --untracked-files=all");
      if (clean || (await run("git rev-parse HEAD")).trim() !== state.head) {
        throw new Error("Configured check changed source or HEAD");
      }
    }
    const bundle = state.hasChanges
      ? await instance.files.read("/tmp/shogun-poc/checkpoint.bundle", { format: "bytes" }) : undefined;
    const staging = await mkdtemp(join(tmpdir(), "shogun-publisher-"));
    try {
      await writeFile(join(staging, "base.bundle"), baseBundle);
      const bundlePath = bundle ? join(staging, "checkpoint.bundle") : undefined;
      if (bundlePath && bundle) await writeFile(bundlePath, bundle);
      const diff = await trustedDiff(staging, state.base, state.head, bundlePath);
      signal.throwIfAborted();
      output = await mkdtemp(resolve(process.env.SHOGUN_POC_OUTPUT || ".", "shogun-checkpoint-"));
      const mockDraft = bundle && check.status !== "failed" ? {
        draft: true, sourceBranch: "agent/poc", targetBranch: "fixture-base", head: state.head,
      } : undefined;
      const artifacts = await saveCheckpoint(output, {
        "workspace.tar.zst": archive, "session.jsonl": session, "changes.diff": diff,
        ...(bundle ? { "checkpoint.bundle": bundle } : {}),
        ...(mockDraft ? { "mock-draft-mr.json": JSON.stringify(mockDraft) } : {}),
      });
      signal.throwIfAborted();
      await writeFile(join(output, "manifest.json"), JSON.stringify({ ...state, check, artifacts }, null, 2), {
        mode: 0o600, flag: "wx",
      });
      signal.throwIfAborted();
      complete = true;
      console.log(JSON.stringify({ checkpoint: output, ...state, draftMr: mockDraft ? "mock-verified" : "not-applicable" }));
    } finally {
      await rm(staging, { recursive: true, force: true });
    }
  } finally {
    signal.removeEventListener("abort", abortSandbox);
    process.removeListener("SIGINT", cancel);
    process.removeListener("SIGTERM", cancel);
    try {
      const destroyed = await dispose();
      if (destroyed !== undefined) process.stderr.write(`Sandbox cleanup: ${destroyed ? "destroyed" : "already absent"}\n`);
    } finally {
      await rm(scratch, { recursive: true, force: true });
      if (output && !complete) await rm(output, { recursive: true, force: true });
    }
  }
}

main().catch((error: unknown) => {
  let message = error instanceof Error ? error.message : "PoC failed";
  for (const name of ["SHOGUN_E2B_APIKEY", "SHOGUN_PI_API_KEY", "DEEPSEEK_API_KEY", "SHOGUN_KAGEMUSHA_API_TOKEN"]) {
    const secret = process.env[name];
    if (secret) message = message.replaceAll(secret, "[redacted]");
  }
  console.error(message);
  process.exitCode = 1;
});
