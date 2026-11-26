import { createHash } from "node:crypto";

export const sha256 = (data: Uint8Array) => createHash("sha256").update(data).digest("hex");

export async function workspace(url: URL, token: string, copyId: string, expectedBase: string, signal: AbortSignal) {
  if (!/^[a-f0-9]{40}$/.test(expectedBase)) throw new Error("Expected a full SHA-1 commit");
  if (!/^copy-[a-f0-9]+$/.test(copyId)) throw new Error("Invalid working-copy ID");
  const get = async (suffix = "") => {
    const response = await fetch(new URL(`/copies/${copyId}${suffix}`, url), {
      headers: { Authorization: `Bearer ${token}` },
      redirect: "error",
      signal,
    });
    if (!response.ok) throw new Error(`Kagemusha returned HTTP ${response.status}`);
    return response;
  };
  const copy: unknown = await (await get()).json();
  if (!copy || typeof copy !== "object" || !("status" in copy) || copy.status !== "ready"
      || !("commit_sha" in copy) || copy.commit_sha !== expectedBase
      || !("mode" in copy) || copy.mode !== "git" || !("depth" in copy) || copy.depth !== 0
      || !("artifact_sha256" in copy) || typeof copy.artifact_sha256 !== "string"
      || !("artifact_size" in copy) || typeof copy.artifact_size !== "number"
      || !Number.isSafeInteger(copy.artifact_size) || copy.artifact_size <= 0 || copy.artifact_size > 25 * 1024 ** 2) {
    throw new Error("Expected a ready, full-history git copy for the pinned commit, at most 25 MiB");
  }
  const download = await get("/download");
  const reader = download.body?.getReader();
  if (!reader) throw new Error("Missing workspace body");
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > copy.artifact_size) throw new Error("Workspace exceeds declared size");
      chunks.push(value);
    }
  } finally {
    await reader.cancel();
  }
  const archive = Buffer.concat(chunks);
  if (length !== copy.artifact_size || sha256(archive) !== copy.artifact_sha256
      || download.headers.get("X-Artifact-SHA256") !== copy.artifact_sha256) {
    throw new Error("Workspace checksum or size mismatch");
  }
  return archive;
}
