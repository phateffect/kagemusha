import { execFile } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import { sha256 } from "./kagemusha.ts";

const exec = promisify(execFile);
export const checkpointRef = "refs/shogun/checkpoints/poc";
const git = "git --literal-pathspecs -c core.hooksPath=/dev/null";
export const quote = (value: string) => "'" + value.replaceAll("'", "'\"'\"'") + "'";
export type Execute = (command: string) => Promise<string>;

export async function checkpoint(run: Execute, base: string, startingHead: string, allowed: string[]) {
  if ((await run(`${git} rev-parse HEAD`)).trim() !== startingHead) {
    throw new Error("PoC finalizer requires agent changes in the working tree");
  }
  const tracked = await run(`${git} diff --name-only --no-renames -z HEAD --`);
  const untracked = await run(`${git} ls-files --others --exclude-standard -z`);
  const paths = [...new Set((tracked + untracked).split("\0").filter(Boolean))].sort();
  for (const path of paths) {
    if (!allowed.includes(path) || !/^[\w./-]+$/.test(path) ||
        path.split("/").some((part) => !part || part.startsWith("."))) {
      throw new Error("Change outside the disposable fixture allowlist");
    }
  }
  if (paths.length) {
    await run(`${git} add -- ${paths.map(quote).join(" ")}`);
    const entries = await run(`${git} ls-files --stage -- ${paths.map(quote).join(" ")}`);
    if (entries.trim() && entries.split("\n").filter(Boolean).some((entry) => !/^100(?:644|755) /.test(entry))) {
      throw new Error("PoC refuses symlinks and gitlinks in changed paths");
    }
    await run(`${git} -c user.name=Shogun -c user.email=shogun@example.invalid -c commit.gpgSign=false commit -m 'Shogun PoC checkpoint'`);
  }
  const head = (await run(`${git} rev-parse HEAD`)).trim();
  if (!/^[a-f0-9]{40}$/.test(base) || !/^[a-f0-9]{40}$/.test(head)) throw new Error("Invalid object ID");
  await run(`${git} merge-base --is-ancestor ${base} ${head}`);
  const changed = (await run(`${git} diff --name-only -z ${base} ${head}`)).length > 0;
  if (changed) {
    await run(`${git} update-ref ${checkpointRef} ${head}`);
    await run(`${git} bundle create /tmp/shogun-poc/checkpoint.bundle ${checkpointRef} ^${base}`);
  }
  if (!changed && head !== base) throw new Error("PoC cannot represent a commit with no cumulative changes");
  return { base, head, hasChanges: changed };
}

export async function trustedDiff(directory: string, base: string, head: string, bundle?: string) {
  const env = { PATH: process.env.PATH, HOME: directory, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_SYSTEM: "/dev/null" };
  const bare = join(directory, "publisher.git");
  const run = async (...args: string[]) => (await exec("git", ["--git-dir", bare,
    "-c", "core.hooksPath=/dev/null", ...args], { env, maxBuffer: 25 * 1024 ** 2 })).stdout;
  await run("init", "--bare", "--template=");
  await run("fetch", "--no-tags", resolve(directory, "base.bundle"), "HEAD:refs/shogun/base");
  if ((await run("rev-parse", "refs/shogun/base")).trim() !== base) throw new Error("Publisher base mismatch");
  if (bundle) {
    await run("bundle", "verify", bundle);
    const heads = await run("bundle", "list-heads", bundle);
    if (heads.trim() !== `${head} ${checkpointRef}`) throw new Error("Unexpected bundle heads");
    await run("fetch", "--no-tags", bundle, `${checkpointRef}:${checkpointRef}`);
    await run("merge-base", "--is-ancestor", base, checkpointRef);
    if ((await run("rev-parse", checkpointRef)).trim() !== head) throw new Error("Publisher head mismatch");
  } else if (base !== head) {
    throw new Error("Changed checkpoint has no bundle");
  }
  return await run("diff", "--no-ext-diff", "--no-textconv", "--binary", base, head, "--");
}

export async function saveCheckpoint(directory: string, data: Record<string, string | Uint8Array>) {
  const artifacts: Record<string, { sha256: string; size: number }> = {};
  for (const [name, value] of Object.entries(data)) {
    const bytes = typeof value === "string" ? Buffer.from(value) : value;
    await writeFile(join(directory, name), bytes, { mode: 0o600, flag: "wx" });
    artifacts[name] = { sha256: sha256(bytes), size: bytes.byteLength };
  }
  return artifacts;
}

export async function verifiedArtifact(directory: string, name: string, metadata: { sha256: string; size: number }) {
  const data = await readFile(join(directory, name));
  if (data.length !== metadata.size || sha256(data) !== metadata.sha256) throw new Error("Checkpoint artifact verification failed");
  return data;
}
