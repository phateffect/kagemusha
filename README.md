# Kagemusha

Kagemusha 为 Coding Agent 和其他临时执行环境准备 Git Working Copy 与开发依赖缓存。它持久化 Git mirror、SQLite 状态和不可变的 Gradle/pnpm 缓存快照；外部 Orchestrator 仍负责 Agent、任务、Sandbox 与工作区生命周期。

## 运行方式

项目使用 Python 3.15.2、uv 0.13.9、FastAPI、SQLite、系统 Git、mise、GNU tar 与 zstd。API 和 Worker 使用同一镜像、共享一个 Docker named volume，但只有 API 进程持有管理 API Token。

```bash
cp .env.example .env
# 设置 KAGEMUSHA_API_TOKEN、允许的 Git Host 和 credential 引用。
docker compose up -d --build
curl http://127.0.0.1:8000/health/ready
```

Podman 同样受支持：

```bash
podman compose up -d --build
```

如果宿主机上的 uv、Podman 或 Docker 拉取资源时网络不稳定，可以按当前环境设置代理：

```bash
export HTTPS_PROXY=http://127.0.0.1:2080
```

镜像构建是否使用代理由调用方显式决定。容器内的 `127.0.0.1` 通常不是宿主机，因此应传入构建环境实际可访问的地址；不需要代理时不要传这些参数：

```bash
export BUILD_PROXY_URL=http://host.containers.internal:2080
podman build \
  --build-arg HTTP_PROXY="$BUILD_PROXY_URL" \
  --build-arg HTTPS_PROXY="$BUILD_PROXY_URL" \
  -t localhost/kagemusha:local -f Dockerfile .
```

`host.containers.internal` 只是 Podman/Docker Desktop 常见示例，其他构建环境可以使用不同的主机名、地址和端口。Kagemusha 不会自动改写代理配置。

Python、uv 和基础镜像应保持当前最新稳定版本；只有确认存在兼容问题时才回退，并记录原因。mise 从官方 `ghcr.io/jdx/mise:latest` 镜像取得。

## 配置

所有应用环境变量使用 `KAGEMUSHA_` 前缀。核心配置如下：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KAGEMUSHA_DATA_DIR` | 无 | 持久化根目录；Compose 使用 `/data` |
| `KAGEMUSHA_API_TOKEN` | API 必填 | 至少 16 字符的 Bearer Token；Worker 不需要 |
| `KAGEMUSHA_GIT_CREDENTIALS` | `{}` | credential 引用到 Git Token 的 JSON map |
| `KAGEMUSHA_ALLOWED_GIT_HOSTS` | `github.com,gitlab.com` | 明确的 HTTPS Host allowlist |
| `KAGEMUSHA_CA_BUNDLE` | 空 | 可选企业 CA PEM，不会关闭 TLS 校验 |
| `KAGEMUSHA_JOB_LEASE_SECONDS` | `900` | Worker job lease |
| `KAGEMUSHA_SEED_TIMEOUT_SECONDS` | `1800` | 单个缓存准备/验证超时 |
| `KAGEMUSHA_COPY_DEFAULT_TTL_SECONDS` | `3600` | Working Copy 默认 TTL |
| `KAGEMUSHA_SNAPSHOT_RETENTION_COUNT` | `3` | 每个 cache group 保留的最近快照数 |

例如：

```dotenv
KAGEMUSHA_API_TOKEN=replace-with-a-long-service-token
KAGEMUSHA_ALLOWED_GIT_HOSTS=github.com,gitlab.example.com
KAGEMUSHA_GIT_CREDENTIALS={"backend-reader":"read-only-token"}
```

凭证只通过临时 `GIT_ASKPASS` 环境传给 Git，不进入 URL、数据库、归档或日志。缓存构建不会继承 API Token。

## Repository 与 Working Copy

注册操作是幂等的，耗时操作返回 durable job：

```bash
curl -X PUT http://127.0.0.1:8000/repositories/backend \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"Backend",
    "remote_url":"https://gitlab.example.com/team/backend.git",
    "git_provider":"gitlab",
    "default_ref":"main",
    "credential_ref":"backend-reader"
  }'

curl -X POST http://127.0.0.1:8000/repositories/backend/fetch \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN"
```

创建完整 Git Working Copy：

```bash
curl -X POST http://127.0.0.1:8000/repositories/backend/copies \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"ref":"mr:123","depth":50,"mode":"git","branch_prefix":"agent/task-123"}'
```

轮询响应中的 job ID：

```bash
curl -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  http://127.0.0.1:8000/jobs/JOB_ID

curl -o workspace.tar.zst \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  http://127.0.0.1:8000/copies/COPY_ID/download
```

`mode=git` 的归档可独立执行 `status`、`log`、`switch` 和 `commit`，没有 alternates；`mode=source` 会删除主仓库和 submodule 的 `.git` 元数据。两种模式都会先递归准备 HTTPS submodule，并在存在 LFS pointer 时完成 LFS checkout。`branch_prefix` 可由每次 API 请求设置。

## Gradle 与 pnpm 缓存

Kagemusha 自动发现 monorepo 中带 Gradle Wrapper 或 `pnpm-lock.yaml` 的 seed target，也允许显式配置 target。

```bash
curl -X POST \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  http://127.0.0.1:8000/repositories/backend/seed-targets/discover

curl -X POST http://127.0.0.1:8000/cache-jobs \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"repository_ids":["backend"],"providers":["gradle","pnpm"],"mode":"incremental"}'
```

模式语义：

- `incremental`：依赖指纹未变化的 target 跳过；变化的 target 更新共享 mutable cache。
- `force`：重新执行选中的 target，但保留共享 mutable cache。
- `full`：从空共享 cache 重新执行完整 target 集合。

新快照只有在干净目录恢复并通过 Gradle/pnpm offline 验证后才原子切换为 active。发布失败不会替换旧 active snapshot。Gradle dependency cache、Gradle Wrapper distribution 和 pnpm store 使用独立 cache group 与快照。

下载接口：

```text
GET /cache-groups
GET /cache-snapshots
GET /cache-snapshots/{id}/download
```

消费者应校验 `X-Artifact-SHA256`，然后将快照解压到独立目录。Gradle dependency snapshot 作为只读 `GRADLE_RO_DEP_CACHE` 使用，同时给构建提供可写的 `GRADLE_USER_HOME`；pnpm snapshot 作为匹配版本的 `--store-dir`，再执行 `pnpm install --offline --frozen-lockfile`。

## 运维

```bash
# SQLite 在线备份
docker compose exec api kagemusha db backup /data/backups/manual.db

# 完整性与缓存校验
docker compose exec api kagemusha db migrate
docker compose exec worker kagemusha cache verify

# 保留 active、pinned 和最近快照，清理其余快照
docker compose exec worker kagemusha cache gc
```

SQLite、WAL 和共享 Volume 必须位于支持本地文件锁的真实文件系统，不要放在 NFS。`docker compose down` 保留 named volume；`docker compose down -v` 会永久删除所有 Kagemusha 状态。

API 文档位于 `/docs`、`/redoc` 和 `/openapi.json`。健康检查为 `/health/live`、`/health/ready`，Worker 使用 `kagemusha worker-health`。

## 开发与验证

### Secret 检测

项目使用 mise 安装 `hk` 和 `gitleaks`。安装依赖和当前仓库的 Git hooks：

```bash
mise install
hk install --mise
```

`pre-commit` 会扫描暂存内容，`pre-push` 会扫描即将推送的完整提交范围，避免某个提交曾加入密码、随后又删除而漏检。也可以手动扫描整个 Git 历史：

```bash
hk check --all
```

gitleaks 默认规则始终启用。需要检测公司内部 token 前缀或其他项目专属关键词时，在仓库根目录创建 `.gitleaks.local.toml`：

```toml
title = "Local secret rules"

[extend]
useDefault = true

[[rules]]
id = "local-sensitive-keyword"
description = "Local sensitive keyword"
regex = '''REPLACE_WITH_A_LOCAL_REGEX'''
```

`.gitleaks.local.toml` 已被 Git 忽略，真实关键词和内部规则不会进入仓库。不要删除 `[extend] useDefault = true`，否则本地配置会取代而不是扩展 gitleaks 默认规则。

```bash
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check src/kagemusha
uv run pytest
uv build --no-sources
uv run pip-audit
```

容器端到端测试会启动真实 HTTPS Git 服务、API 与 Worker，验证 durable job、归档可移植性、非 root 运行及 Volume 重启保留：

```bash
uv run python tests/container_integration.py
```

需要代理构建测试镜像时显式转发 build arguments：

```bash
uv run python tests/container_integration.py \
  --build-arg HTTP_PROXY="$BUILD_PROXY_URL" \
  --build-arg HTTPS_PROXY="$BUILD_PROXY_URL"
```

完整架构、安全边界、数据模型和验收标准见 [SPEC.md](SPEC.md)。
