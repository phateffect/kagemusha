import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from dotenv import dotenv_values

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    w = root / "workspace"
    w.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(w), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    git("init")
    (w / "AGENTS.md").write_text(
        "This is a disposable Shogun test fixture. Use relative paths. Do not commit.\n"
    )
    (w / "fixture-skill.md").write_text("Disposable repository skill text.\n")
    (w / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (w / "obsolete.txt").write_text("remove me\n")
    (w / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (w / ".pi").mkdir()
    (w / ".pi/settings.json").write_text('{"packages":["file:./evil"]}')
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "base",
    )
    head = git("rev-parse", "HEAD")
    p = root / "workspace.tar.zst"
    subprocess.run(
        ["tar", "--zstd", "-cf", str(p), "-C", d, "workspace"],
        check=True,
        env=os.environ | {"COPYFILE_DISABLE": "1"},
    )
    data = p.read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer disposable-upstream-token":
                self.send_error(401)
                return
            self.send_response(200)
            if self.path.endswith("/download"):
                self.send_header("X-Artifact-SHA256", sha)
                self.end_headers()
                self.wfile.write(data)
            else:
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "status": "ready",
                            "commit_sha": head,
                            "mode": "git",
                            "depth": 0,
                            "artifact_sha256": sha,
                            "artifact_size": len(data),
                        }
                    ).encode()
                )

        def log_message(self, *args):
            pass

    s = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=s.serve_forever, daemon=True).start()
    settings = dotenv_values(".env") | os.environ
    env = {
        name: settings[name]
        for name in (
            "PATH",
            "HOME",
            "SHOGUN_SANDBOX_REGION",
            "SHOGUN_E2B_APIKEY",
            "SHOGUN_SANDBOX_TEMPLATE",
            "SHOGUN_PI_API_KEY",
            "DEEPSEEK_API_KEY",
        )
        if settings.get(name)
    }
    company_skill = root / "company.md"
    company_skill.write_text("Shogun trusted fixture instructions.\n")
    env.update(
        {
            "SHOGUN_KAGEMUSHA_URL": f"http://127.0.0.1:{s.server_port}",
            "SHOGUN_KAGEMUSHA_API_TOKEN": "disposable-upstream-token",
            "SHOGUN_POC_OUTPUT": d,
            "SHOGUN_POC_COMPANY_SKILL": str(company_skill),
            "SHOGUN_POC_REPO_SKILLS": "fixture-skill.md",
            "SHOGUN_POC_CHECK": (
                "python3 -B -c 'from calculator import add; assert add(2,3)==5'"
                ' && test -z "$DEEPSEEK_API_KEY$SHOGUN_PI_API_KEY$SHOGUN_E2B_APIKEY$E2B_API_KEY$SHOGUN_KAGEMUSHA_API_TOKEN"'
                " && ! curl -fsS --max-time 4 https://example.com/ >/dev/null"
            ),
        }
    )

    def run(prompt, previous=None):
        args = [
            "node",
            "scripts/poc/runtime.ts",
            "copy-abc",
            head,
            prompt,
            "calculator.py,obsolete.txt,code.bin,marker.txt",
        ]
        c = subprocess.run(
            args + ([previous] if previous else []),
            env=env,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        print(c.stdout.strip(), flush=True)
        return Path(json.loads(c.stdout)["checkpoint"])

    first = run(
        "Disposable fixture. Use the edit tool once to fix add so it adds instead of subtracts. Use write to create marker.txt with exact text SHOGUN-RESTORE-MARKER. Use bash to remove obsolete.txt and create code.bin as three bytes 00 01 ff using Python. Do not commit. Do not change other files. End with a one-line summary."
    )
    assert {
        "workspace.tar.zst",
        "checkpoint.bundle",
        "session.jsonl",
        "changes.diff",
        "manifest.json",
    } <= {p.name for p in first.iterdir()}
    diff = (first / "changes.diff").read_text()
    assert (
        "+    return a + b" in diff
        and "deleted file mode" in diff
        and "GIT binary patch" in diff
    )
    second = run(
        "Read marker.txt using read, show its contents and the previous turn's change in one sentence. Do not change any files.",
        str(first),
    )
    a = json.loads((first / "manifest.json").read_text())
    b = json.loads((second / "manifest.json").read_text())
    assert a["head"] == b["head"]
    assert a["check"]["status"] == b["check"]["status"] == "passed"
    assert json.loads((first / "mock-draft-mr.json").read_text())["head"] == a["head"]
    session = (second / "session.jsonl").read_text()
    assert "SHOGUN-RESTORE-MARKER" in session and "Disposable fixture" in session
    print("Live phase 0 restore verified", flush=True)

    before = set(root.glob("shogun-checkpoint-*"))
    with subprocess.Popen(
        [
            "node",
            "scripts/poc/runtime.ts",
            "copy-abc",
            head,
            "Immediately call bash with this exact command: sleep 90 & wait. Do not do anything else.",
            "",
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as cancelling:
        assert cancelling.stderr
        for line in cancelling.stderr:
            if line.strip() == "tool_execution_start":
                time.sleep(1)
                cancelling.send_signal(signal.SIGINT)
                break
        out, err = cancelling.communicate(timeout=40)
        assert (
            cancelling.returncode != 0
            and "Sandbox cleanup: destroyed" in err
            and not out
        ), (out, err)
        assert set(root.glob("shogun-checkpoint-*")) == before
    print(
        "Live cancellation destroyed the sandbox without a new checkpoint", flush=True
    )
