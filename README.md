# Kagemusha — Git clones that fight your battles.

Kagemusha 是一个面向 Coding Agent Orchestrator 的 Git Workspace Service。它持久化 Bare Repository 缓存，每次请求都重新验证远端 Ref，并生成可下载、可迁移且不包含用户 Git 凭证的独立 `workspace.tar.zst`。服务不管理 E2B Sandbox 或 Agent Task。

## 技术栈

- Python 3.15.2、FastAPI、Uvicorn
- mise 管理开发工具，uv 管理并锁定 Python 依赖
- 系统 Git CLI、GNU tar、zstd、Linux `flock`
- pytest、httpx2，以及使用真实 `git http-backend` 的本地 HTTPS 测试服务
- Debian Trixie Slim 多阶段镜像、Docker Compose 兼容配置、Named Volume

不依赖 GitPython、pygit2、libgit2、gh/glab、数据库、Redis 或 E2B SDK。

## 启动

要求 Git、tar、zstd，以及 Podman 或 Docker。以下使用 Podman：

```bash
cp .env.example .env
# 修改 .env 中的 KAGEMUSHA_API_TOKEN；不要提交该文件。
set -a
. ./.env
set +a

podman build -t localhost/kagemusha:local -f Dockerfile .
podman compose up -d
podman compose ps
curl http://127.0.0.1:8000/healthz
```

Docker Compose 兼容入口：

```bash
docker compose up -d --build
```

服务在容器内监听 `0.0.0.0:8000`，默认只发布到主机 `127.0.0.1:8000`。跨主机部署时必须放在 TLS 或受保护的反向代理之后。Uvicorn 固定单 Worker，Compose 固定单实例。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KAGEMUSHA_DATA_DIR` | 必填 | Bare 缓存、元数据、锁和临时文件根目录；Compose 使用 `/data` |
| `KAGEMUSHA_API_TOKEN` | 必填 | 至少 16 个字符的服务间 Bearer Token，不能是 Git Token |
| `KAGEMUSHA_MAX_CONCURRENT_GIT` | `4` | Git/归档子进程最大并发数 |
| `KAGEMUSHA_GIT_TIMEOUT_SECONDS` | `300` | 单个子进程超时，超时或取消会终止进程组 |
| `KAGEMUSHA_DEFAULT_DEPTH` | `50` | 未传 `depth` 时的历史深度 |
| `KAGEMUSHA_MAX_DEPTH` | `1000` | 最大有限深度，不能超过 1000 |
| `KAGEMUSHA_ZSTD_LEVEL` | `3` | zstd 压缩级别 |
| `KAGEMUSHA_ZSTD_THREADS` | `1` | zstd 压缩线程数 |
| `KAGEMUSHA_MAX_ARCHIVE_BYTES` | `1073741824` | 最大归档字节数，超过返回 413 |
| `KAGEMUSHA_MAX_REQUEST_BODY_BYTES` | `65536` | 最大 HTTP 请求体字节数，解析 JSON 和认证前强制执行 |
| `KAGEMUSHA_TEMP_BUDGET_BYTES` | `2147483648` | 临时空间预算，超过返回 507 |
| `KAGEMUSHA_TEMP_TTL_SECONDS` | `3600` | 启动时清理过期临时资源和快照 Ref 的 TTL |
| `KAGEMUSHA_MAX_SUBMODULES` | `100` | 单个 Workspace 最多初始化的 Submodule 数量 |
| `KAGEMUSHA_MAX_SUBMODULE_DEPTH` | `10` | Submodule 最大递归层级 |
| `KAGEMUSHA_ALLOWED_GIT_HOSTS` | `github.com,gitlab.com` | 逗号分隔的显式 Host Allowlist，不能为空 |
| `KAGEMUSHA_CA_BUNDLE` | 空 | 可选的企业 CA PEM 文件路径，不会关闭 TLS 校验 |
| `KAGEMUSHA_HTTP_PORT` | `8000` | Compose 发布到本机的端口 |

Allowlist 中不带端口的条目只允许 HTTPS 默认端口 443。非标准端口必须显式写出，例如 `git.example.com:8443`。自建 GitHub/GitLab 域名需要加入该列表。若配置企业 CA，请用受控的只读文件或 Secret 挂载 PEM，并设置 `KAGEMUSHA_CA_BUNDLE`；不要使用 `http.sslVerify=false`。

## Git Token 权限

每次业务请求都需要两类凭证：

- `Authorization: Bearer $KAGEMUSHA_API_TOKEN`：Orchestrator 与服务之间的凭证。
- JSON 中的 `credential.token`：仅本次 Clone、Fetch、同 Host Submodule 和 LFS 下载使用的 Git Token。

GitHub 使用用户名 `x-access-token`，Token 至少需要目标仓库的 Contents 只读权限。GitLab 使用用户名 `oauth2`，Token 至少需要 `read_repository`。服务通过临时 `GIT_ASKPASS` 环境传参，不把 Token 拼入 URL 或命令行，不持久化 Token，也不信任继承的 Git Credential Helper。

GitHub 支持 `pr:<number>`，映射到 `refs/pull/<number>/head`；GitLab 支持 `mr:<number>`，映射到 `refs/merge-requests/<number>/head`。二者都使用源 HEAD，不是合并结果 Ref。普通分支可直接写 `main`、`feature/login`，Tag 必须写完整形式 `refs/tags/v1.0.0`；短名称始终优先解释为 Branch。Annotated Tag 会解析到对应 Commit。

## 初始化 Project

下面用临时 JSON 文件演示。真实 Token 不应长期写入磁盘或 Shell 历史：

```bash
export KAGEMUSHA_API_TOKEN='replace-with-a-long-service-secret'
export GIT_TOKEN='read-only-git-token'
umask 077
cat > init-project.json <<EOF
{
  "repository": "https://gitlab.example.com/team/project.git",
  "provider": "gitlab",
  "credential": {"type": "token", "token": "$GIT_TOKEN"}
}
EOF
curl -X POST \
  http://127.0.0.1:8000/projects/my-project \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @init-project.json
rm -f init-project.json
```

成功返回 HTTP 201 和默认分支。重复初始化返回 409，已有缓存不会被覆盖。`project_id` 只允许 `[a-zA-Z0-9_-]{1,128}`。

## 创建并下载 Workspace

```bash
umask 077
cat > create-copy.json <<EOF
{
  "ref": "mr:123",
  "depth": 50,
  "branch_prefix": "agent/task-123",
  "credential": {"type": "token", "token": "$GIT_TOKEN"}
}
EOF
curl -f -X POST \
  http://127.0.0.1:8000/projects/my-project/copies \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @create-copy.json \
  -o workspace.tar.zst
rm -f create-copy.json
```

响应包含 `X-Git-Commit`、`X-Git-Ref`、`X-Git-Depth`；HEAD 位于分支时还包含 `X-Git-Branch`。正文是 `application/zstd`。每次 Copy 都必须成功 Fetch 本次指定的远端 Ref；远端 Ref 删除或不可访问时不会回退到旧缓存，也不接受任意 Commit SHA 或 Revision Expression。

### Working Branch

- 普通 Branch 默认检出同名本地分支，并跟踪对应的 `origin/*`。
- PR/MR 默认检出 `kagemusha/pr-<number>` 或 `kagemusha/mr-<number>`，不伪造 upstream。
- Tag 默认保持 Detached HEAD。
- 传入 `branch_prefix` 后，所有 Ref 都创建 `<prefix>/<对应名称>` 工作分支且不设置 upstream；例如 `agent/task-123/main`、`agent/task-123/mr-456`、`agent/task-123/tag-v1.0.0`。

`branch_prefix` 必须能组成安全的 Git Branch 名称，最长 128 字符。

### Submodule 与 Git LFS

服务递归初始化 HTTPS Submodule，并严格检出主仓库 Commit 记录的 gitlink。相对 URL 和同 Host URL 可复用本次临时 Token；跨 Host Submodule 不会收到主仓库 Token，因此公开仓库可以下载，私有仓库会失败。每一级 URL 都受协议、Host Allowlist、DNS/SSRF、数量和递归深度限制。

如果主仓库或任意 Submodule 包含 Git LFS pointer，服务会在返回前下载并校验全部 LFS 对象。LFS endpoint 同 Host 时使用本次临时 Token；自定义跨 Host endpoint 不会收到该 Token。任何必要的 Submodule 或 LFS 对象下载失败时，整个 Copy 请求失败，不返回残缺归档。

### Depth

| 值 | 行为 |
| --- | --- |
| 未传 | 使用 `KAGEMUSHA_DEFAULT_DEPTH`，默认 50 |
| `0` | 目标 Commit 的完整历史 |
| `1` | 只获取目标 Commit |
| `1～1000` | 对应有限历史深度 |

`depth` 必须是 JSON 整数，不接受字符串、浮点数、负数或大于 1000 的值。共享 Bare 缓存始终保持完整历史，只有 Workspace 可能是 Shallow Repository。历史不足指定深度时正常返回完整短历史。Shallow Workspace 不保证包含与 `main` 的 Merge Base；需要完整历史时使用 `depth: 0`。

## 在 E2B 中解压

E2B Template 需要安装 `git`、`git-lfs`、GNU tar 和 `zstd`：

```bash
mkdir -p /workspace/project
tar --zstd \
  -xf workspace.tar.zst \
  -C /workspace/project \
  --strip-components=1
cd /workspace/project
git status
git log
git branch --show-current
```

归档顶层目录固定为 `workspace/`，包含 `.git`、已初始化的 Submodule、已下载的 LFS 内容、权限、可执行位和符号链接。HEAD 固定为响应中的 Commit，并遵循上述 Branch 规则。对象不使用 alternates，也不依赖服务端路径；归档生成后可以删除远端和缓存，仍能独立执行 `status`、`log`、`diff`、`switch`、`commit` 和 `fsck`。

## 持久化与运维

所有长期状态位于 Named Volume `kagemusha-data` 的 `/data` 下，不使用 Host Bind Mount。容器以非 Root 用户运行，空 Volume 首次挂载时继承镜像中 `/data` 的所有权。

```bash
podman compose down
podman compose up -d
# 上述重建流程保留缓存。

podman compose down -v
# 警告：这会永久删除 Named Volume 中的全部 Git 缓存和元数据。
```

Git 自动 GC 和后台 Prune 被禁用，缓存可能持续增长。启动时会恢复仓库/元数据不完整的 Project，并清理超过 TTL 的临时目录和快照 Ref。服务输出结构化 JSON 操作日志，包含请求 ID、Ref、Commit、Depth、阶段耗时、归档大小和结果，但不记录 Token、Authorization Header、完整请求体或 ASKPASS 环境。

## 测试

开发环境：

```bash
mise install
mise exec -- uv sync --locked
mise exec -- uv run ruff check src tests
mise exec -- uv run ruff format --check src tests
mise exec -- uv run ty check src/kagemusha
mise exec -- uv build --no-sources
mise exec -- uv run pytest
mise exec -- uv run pip-audit
```

测试使用独立 CA 和真实 HTTPS Git Smart HTTP 服务，不关闭 TLS 校验，不需要真实 GitHub/GitLab 账号。覆盖 Branch、Tag、Annotated Tag、PR/MR、120 个 Commit 的 Depth 用例、并发、跨进程锁、取消、超时、断流、启动恢复、归档独立性和凭证安全。

容器端到端验收：

```bash
mise exec -- uv run python tests/container_integration.py
```

该脚本使用 Podman 构建镜像、启动隔离的 Compose Project、执行真实 HTTP API、验证归档和 Volume 重建保留，最后只删除本次创建的容器与测试 Volume。若公共依赖或镜像下载遇到网络问题，可设置 `HTTPS_PROXY=http://127.0.0.1:2080` 后重试。

## 已知限制与安全边界

- 只支持 HTTPS Git；Host 必须在 Allowlist 中。禁止 URL Userinfo、非预期协议、Git Ext Transport 和重定向；仓库改名导致的 HTTP 重定向也会被拒绝。
- 为防通过缓存越权读取对象，必须每次远端 Fetch 指定 Ref，不支持直接 SHA。服务依赖可信 Orchestrator 分配 Project ID、隔离授权安全域并保护 `KAGEMUSHA_API_TOKEN`。
- Token 在子进程环境中短暂存在，本机特权进程仍可能读取，因此服务主机和容器必须视为受信任执行环境。不要运行不可信仓库中的 Hook、过滤器或安装脚本。
- 单实例、单 Worker；不支持跨节点缓存、多实例或分布式锁。Linux `flock` 只协调同一主机上的进程。
- 不实现 Push、PR/MR 创建、Provider API Client、Web UI、Token 持久化、SSH Key、E2B SDK、对象存储或长期 Workspace 存储。
- 跨 Host 私有 Submodule 或 LFS endpoint 暂不支持额外凭据；主仓库 Token 不会跨 Host 发送。
- 不调用 Provider API，因此 PR/MR 使用确定性的本地工作分支，不解析 Fork 源分支或 Push Remote。
- 大归档受 `KAGEMUSHA_MAX_ARCHIVE_BYTES` 和临时空间预算限制；调用方仍应规划足够的 Volume 与临时磁盘容量。
