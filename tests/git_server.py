from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import urlsplit

import trustme


class GitThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


@dataclass
class GitRemote:
    bare: Path
    port: int
    ca_bundle: Path
    server: GitThreadingHTTPServer
    thread: threading.Thread
    controller: GitHttpsServer
    used_tokens: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/remote.git"

    def url_for(self, repository: str) -> str:
        return f"https://127.0.0.1:{self.port}/{repository}"

    @property
    def container_url(self) -> str:
        return f"https://host.containers.internal:{self.port}/remote.git"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(
            {f"127.0.0.1:{self.port}", f"host.containers.internal:{self.port}"}
        )

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class GitHttpsServer:
    def __init__(self, bare: Path, ca: trustme.CA) -> None:
        self.bare = bare
        self.used_tokens: list[str] = []
        self.valid_tokens = {"test-git-token", "second-token", "third-token"}
        self.delay_seconds = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self.handle_git()

            def do_POST(self) -> None:
                self.handle_git()

            def log_message(self, format: str, *args: object) -> None:
                return

            def address_string(self) -> str:
                return self.client_address[0]

            def handle_git(self) -> None:
                if not self.authorized():
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if outer.delay_seconds:
                    time.sleep(outer.delay_seconds)
                parsed = urlsplit(self.path)
                if parsed.path.endswith("/info/lfs/objects/batch"):
                    self.handle_lfs_batch(parsed.path)
                    return
                if "/info/lfs/objects/" in parsed.path and self.command == "GET":
                    self.handle_lfs_object(parsed.path)
                    return
                if not re.match(r"^/[A-Za-z0-9_.-]+\.git/", parsed.path):
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": "/nonexistent",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_PROJECT_ROOT": str(bare.parent),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": parsed.path,
                    "QUERY_STRING": parsed.query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "REMOTE_ADDR": self.client_address[0],
                    "SERVER_PROTOCOL": self.request_version,
                }
                result = subprocess.run(
                    ["git", "http-backend"],
                    input=body,
                    capture_output=True,
                    env=env,
                    check=False,
                )
                header_bytes, separator, response_body = result.stdout.partition(
                    b"\r\n\r\n"
                )
                if not separator:
                    self.send_error(500)
                    return
                headers = header_bytes.decode("latin-1").split("\r\n")
                status = 200
                for header in headers:
                    name, _, value = header.partition(":")
                    if name.lower() == "status":
                        status = int(value.split()[0])
                self.send_response(status)
                for header in headers:
                    name, separator, value = header.partition(":")
                    if separator and name.lower() not in {
                        "status",
                        "content-length",
                        "connection",
                    }:
                        self.send_header(name, value.strip())
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(response_body)
                    except BrokenPipeError:
                        pass

            def handle_lfs_batch(self, path: str) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                repository_name = path.split("/", 2)[1]
                objects = []
                for requested in request.get("objects", []):
                    oid = requested["oid"]
                    object_path = (
                        bare.parent / repository_name / "lfs" / "objects" / oid
                    )
                    if (
                        not re.fullmatch(r"[0-9a-f]{64}", oid)
                        or not object_path.is_file()
                    ):
                        objects.append(
                            {
                                "oid": oid,
                                "size": requested["size"],
                                "error": {
                                    "code": 404,
                                    "message": "LFS object not found",
                                },
                            }
                        )
                        continue
                    host = self.headers["Host"]
                    objects.append(
                        {
                            "oid": oid,
                            "size": object_path.stat().st_size,
                            "actions": {
                                "download": {
                                    "href": f"https://{host}/{repository_name}/info/lfs/objects/{oid}"
                                }
                            },
                        }
                    )
                response = json.dumps(
                    {"transfer": "basic", "objects": objects}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.git-lfs+json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def handle_lfs_object(self, path: str) -> None:
                parts = path.split("/")
                repository_name = parts[1]
                oid = parts[-1]
                object_path = bare.parent / repository_name / "lfs" / "objects" / oid
                if not re.fullmatch(r"[0-9a-f]{64}", oid) or not object_path.is_file():
                    self.send_error(404)
                    return
                content = object_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def authorized(self) -> bool:
                authorization = self.headers.get("Authorization", "")
                if not authorization.startswith("Basic "):
                    return False
                try:
                    username, password = (
                        base64.b64decode(authorization[6:]).decode().split(":", 1)
                    )
                except ValueError, UnicodeDecodeError:
                    return False
                if username not in {"x-access-token", "oauth2"}:
                    return False
                if password not in outer.valid_tokens:
                    return False
                outer.used_tokens.append(password)
                return True

        self.server = GitThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.server.daemon_threads = True
        certificate = ca.issue_cert(
            "127.0.0.1", "localhost", "host.containers.internal"
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        certificate.configure_cert(context)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> tuple[int, threading.Thread]:
        self.thread.start()
        return self.server.server_port, self.thread
