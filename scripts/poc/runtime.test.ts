import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { checkpoint, checkpointRef, trustedDiff } from "./checkpoint.ts";
import { sha256, workspace } from "./kagemusha.ts";

test("Kagemusha download requires the reviewed SHA, full history, and matching digest", async () => {
  const data = Buffer.from("fixture");
  const base = "a".repeat(40);
  let checksum = sha256(data);
  let downloads = 0;
  const server = createServer((request, response) => {
    assert.equal(request.headers.authorization, "Bearer fixture");
    if (request.url?.endsWith("/download")) {
      downloads++;
      response.setHeader("X-Artifact-SHA256", checksum);
      response.end(data);
    } else {
      response.end(JSON.stringify({
        status: "ready", commit_sha: base, mode: "git", depth: 0,
        artifact_sha256: checksum, artifact_size: data.length,
      }));
    }
  });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  try {
    const address = server.address();
    assert(address && typeof address === "object");
    const url = new URL(`http://127.0.0.1:${address.port}`);
    const signal = AbortSignal.timeout(5000);
    await assert.rejects(workspace(url, "fixture", "copy-ab", "b".repeat(40), signal), /pinned commit/);
    assert.equal(downloads, 0);
    assert.deepEqual(await workspace(url, "fixture", "copy-ab", base, signal), data);
    checksum = "0".repeat(64);
    await assert.rejects(workspace(url, "fixture", "copy-ab", base, signal), /checksum/);
  } finally {
    server.close();
  }
});

test("checkpoint handles a clean tree, rejects unexpected files, and verifies a binary diff", async () => {
  const root = await mkdtemp(join(tmpdir(), "shogun-test-"));
  const staging = await mkdtemp(join(tmpdir(), "shogun-trusted-test-"));
  // The host shell here is only a test double using a disposable, locally authored fixture.
  const git = (...args: string[]) => execFileSync("git", args, { cwd: root, encoding: "utf8" }).trim();
  const run = async (command: string) => execFileSync("sh", ["-c",
    command.replaceAll("/tmp/shogun-poc/", root + "/")], { cwd: root, encoding: "utf8" });
  try {
    git("init", "--quiet");
    await writeFile(join(root, ".gitignore"), "*.bundle\n");
    await writeFile(join(root, "old.txt"), "delete me");
    git("add", ".");
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base");
    const base = git("rev-parse", "HEAD");
    git("bundle", "create", join(staging, "base.bundle"), "HEAD");
    assert.deepEqual(await checkpoint(run, base, base, []), { base, head: base, hasChanges: false });

    await writeFile(join(root, "new.bin"), Buffer.from([0, 1, 255]));
    await rm(join(root, "old.txt"));
    await assert.rejects(checkpoint(run, base, base, ["old.txt"]), /allowlist/);
    const saved = await checkpoint(run, base, base, ["new.bin", "old.txt"]);
    assert(saved.hasChanges);
    assert.equal(git("rev-parse", checkpointRef), saved.head);
    const diff = await trustedDiff(staging, base, saved.head, join(root, "checkpoint.bundle"));
    assert.match(diff, /GIT binary patch/);
    assert.match(diff, /deleted file mode/);
  } finally {
    await rm(root, { recursive: true, force: true });
    await rm(staging, { recursive: true, force: true });
  }
});
