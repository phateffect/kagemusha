# Git Workspace Service — Implementation Specification

**Version:** 1.1  
**Status:** Ready for Implementation  
**Language:** Python  
**Deployment:** Docker Compose  
**Purpose:** Git Repository Cache & Portable Workspace Generation

---

# 1. 项目概述

## 1.1 背景

我们正在构建一个基于 E2B Sandbox 的 Coding Agent 平台。

外部 Orchestrator 负责创建 Agent Task、管理 Sandbox、调度 Agent 执行工作。

为了避免每个 Sandbox 都需要 Git 凭证并重复从远程仓库 Clone 代码，需要实现独立的 Git Workspace Service。

该服务负责：

1. 初始化远端 Git 仓库的本地缓存。
2. 按需 Fetch 最新远端 Ref。
3. 生成指定 Commit 的独立 Working Copy。
4. 可选限制 Git 历史深度。
5. 压缩并通过 HTTP 返回 Workspace。
6. 保证用户 Git 凭证不会进入 Workspace。

服务本身不管理 E2B，也不负责 Agent 任务生命周期。

## 1.2 设计原则

- Simple：尽量少的组件与抽象。
- Stateless Credentials：不持久化用户凭证。
- Stateful Cache：持久化 Git 仓库缓存。
- Portable Workspace：Workspace 不依赖服务器文件系统。
- Secure by Default：隔离凭证、限制远程访问。
- Reproducible：每个 Workspace 对应确定的 Commit SHA。
- Concurrency Safe：支持同一个 Project 并发创建多个 Workspace。
- Minimal Dependencies：优先使用成熟的系统 Git CLI。

## 1.3 不采用的技术

第一版不引入：

- GitPython
- pygit2
- libgit2
- gh / glab
- PostgreSQL / SQLite
- Redis
- Celery
- Kubernetes
- E2B SDK
- 对象存储

---

# 2. 技术栈

| 组件 | 选型 |
|---|---|
| Language | Python 3.13 |
| Package Manager | uv |
| HTTP Framework | FastAPI |
| ASGI | Uvicorn |
| Git | Git CLI |
| Archive | GNU tar |
| Compression | Zstandard |
| Subprocess | asyncio |
| Lock | Linux flock |
| Tests | pytest + httpx2 |
| Container | Docker |
| Base Image | Debian Trixie Slim |
| Orchestration | Docker Compose |
| Storage | Docker Named Volume |

Python 负责请求验证、流程编排和子进程管理。

Git CLI 负责所有 Git 操作。

GNU tar 和 zstd 负责压缩归档。

不自行实现 Git Protocol。

---

# 3. 系统架构

```text
                   Agent Orchestrator
                           |
                           | HTTPS
                           | Git Token (per request)
                           v
                 Git Workspace Service
                  Python + FastAPI
                           |
             +-------------+-------------+
             |             |             |
          Git CLI      Git Cache      tar + zstd
             |             |             |
             |       Bare Repository     |
             |       Docker Volume       |
             |             |             |
             +-------------+-------------+
                           |
                           v
                    workspace.tar.zst
                           |
                           v
                   Agent Orchestrator
                           |
                           | E2B Files API
                           v
                       E2B Sandbox
                           |
                           v
                        Pi Agent
```

## 3.1 组件职责

### Orchestrator

负责：

- 用户认证与权限管理
- 用户 Git Token 的提供与管理
- Project ID 分配
- Task 生命周期管理
- Sandbox 生命周期管理
- Workspace 归档下载与上传
- 后续 Push、创建 PR/MR

### Git Workspace Service

负责：

- Git Repo 初始化
- Git Repo 缓存
- Fetch
- Ref 解析
- Workspace 生成
- Git 历史截断
- 打包压缩
- 归档交付

### E2B Sandbox

负责：

- 解压 Workspace
- 运行 Agent
- 修改代码
- 创建 Commit

E2B 不需要 Git Token。

---

# 4. 数据模型与存储

## 4.1 Project

Project 是一个持久化的 Git 仓库缓存。

每个 Project 由外部控制器生成的 ID 标识。

元数据：

```json
{
  "id": "project-a",
  "repository": "https://gitlab.example.com/team/project.git",
  "provider": "gitlab",
  "default_branch": "main"
}
```

Project 元数据不包含任何凭证。

## 4.2 持久化目录

```text
/data/
├── projects/
│   ├── project-a.git/
│   └── project-b.git/
├── metadata/
│   ├── project-a.json
│   └── project-b.json
├── locks/
│   ├── project-a.lock
│   └── project-b.lock
└── tmp/
```

所有持久化数据放入一个 Docker Named Volume。

`tmp` 也可以放入该 Volume，避免大型归档占用容器可写层；临时文件需定期清理。

Project 的缓存为 Bare Git Repository。

不要保留长期存在的普通 Git Working Directory。

## 4.3 Project 隔离

一个 Project 必须绑定固定的 Git Repository 和授权安全域。

第一版由可信 Orchestrator 保证：

- Project ID 不能跨用户随意复用。
- 当前请求方拥有访问该 Project 的权限。
- 不同授权安全域不得共享可直接读取的 Bare Repo。

Git Workspace Service 还必须保证不能通过本地缓存绕过远端 Ref 的访问权限。

即使某个 Commit 已经存在于缓存中，也不能让调用方通过任意 SHA 读取它。

---

# 5. API

第一版只实现两个业务 API。

另提供一个健康检查接口。

## 5.1 初始化项目

`POST /projects/{project_id}`

Request:

```json
{
  "repository": "https://gitlab.example.com/team/project.git",
  "provider": "gitlab",
  "credential": {
    "type": "token",
    "token": "glpat-xxxxx"
  }
}
```

### 参数

| 字段 | 必填 | 说明 |
|---|---|---|
| project_id | 是 | 控制端生成的 ID |
| repository | 是 | HTTPS Git URL |
| provider | 是 | github / gitlab |
| credential.type | 是 | 固定为 token |
| credential.token | 是 | 本次请求使用的 Token |

`project_id` 限制为安全字符集：

```text
[a-zA-Z0-9_-]{1,128}
```

不允许通过 ID 构造任意文件系统路径。

### 执行流程

1. 验证服务 API 身份。
2. 验证 Project ID、Repository URL、Provider。
3. 获取 Project Lock。
4. 检查 Project 是否已存在。
5. 在临时目录 Clone 远端仓库。
6. 验证 Bare Repository 完整性。
7. 识别远端默认分支。
8. 清除所有可能包含凭证的配置。
9. 将仓库原子移动到最终目录。
10. 原子写入 Project 元数据。
11. 释放 Lock。
12. 清理临时凭证。

建议使用：

```bash
git clone --bare \
  https://gitlab.example.com/team/project.git \
  /data/tmp/clone-<uuid>
```

使用带有 Token 的临时 ASKPASS 完成认证。

不得将 Token 拼入 URL。

对于进程崩溃可能导致的仓库与元数据部分提交问题，启动时应进行一致性检查与恢复。

### 成功响应

HTTP 201：

```json
{
  "project_id": "project-a",
  "repository": "https://gitlab.example.com/team/project.git",
  "provider": "gitlab",
  "default_branch": "main"
}
```

已经存在的 Project 返回 HTTP 409。

不得覆盖已有仓库。

---

## 5.2 创建 Working Copy

`POST /projects/{project_id}/copies`

Request:

```json
{
  "ref": "mr:123",
  "depth": 50,
  "branch_prefix": "agent/task-123",
  "credential": {
    "type": "token",
    "token": "glpat-xxxxx"
  }
}
```

### 参数

| 字段 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| ref | 是 | — | Branch、Tag、PR/MR |
| depth | 否 | 50 | Git 历史深度 |
| branch_prefix | 否 | — | 可选的本地工作分支前缀 |
| credential.type | 是 | — | token |
| credential.token | 是 | — | 本次 Fetch 的 Token |

### Depth 规则

| Depth | 行为 |
|---|---|
| 0 | 完整历史 |
| 1 | 只获取目标 Commit |
| 50 | 默认，最近 50 层历史 |
| 100 | 更深历史 |
| 1000 | 允许的最大有限深度 |

有效值：

```text
0 或 1～1000
```

`depth=0` 表示完整历史，不应直接传给 Git 的 `--depth` 参数。

必须使用严格的整数验证，不接受负数、浮点数或字符串形式的非法值。

### 执行流程

1. 验证 Project 存在。
2. 验证 Ref 格式及 Provider 匹配。
3. 验证请求授权。
4. 获取项目 Fetch Lock。
5. 使用本次请求的 Token Fetch 指定远端 Ref。
6. 从本次 Fetch 结果解析 Commit SHA。
7. 为 Commit 创建内部临时 Ref。
8. 释放 Fetch Lock。
9. 创建临时 Git Workspace。
10. 按指定 Depth 从缓存仓库 Fetch。
11. Checkout 固定 Commit，并按 Ref 类型或 `branch_prefix` 配置工作分支。
12. 递归校验并初始化 HTTPS Submodule。
13. 下载并校验主仓库及所有 Submodule 的 Git LFS 对象。
14. 验证 Git 状态与对象独立性。
15. 生成 tar.zst 归档。
16. 返回归档。
17. 删除内部临时 Ref 和临时 Workspace。

内部临时 Ref 的创建与删除必须协调，防止并发 GC 回收正在使用的对象。

### 成功响应

HTTP 200：

```http
Content-Type: application/zstd
Content-Disposition: attachment; filename="workspace.tar.zst"
Content-Length: <archive-bytes>
X-Git-Commit: <sha>
X-Git-Ref: mr:123
X-Git-Depth: 50
X-Git-Branch: kagemusha/mr-123
```

响应主体为二进制归档。

### 归档结构

```text
workspace/
├── .git/
│   ├── HEAD
│   ├── config
│   ├── objects/
│   ├── refs/
│   └── shallow          # depth > 0 时可能存在
├── src/
├── README.md
├── build.gradle.kts
└── ...
```

解压后的仓库必须能独立运行：

```bash
git status
git log
git diff
git switch -c agent/task-123
git commit
```

普通 Branch 默认检出同名 tracking branch；PR/MR 默认检出 `kagemusha/pr-*` 或 `kagemusha/mr-*`；Tag 默认允许 Detached HEAD。传入 `branch_prefix` 时，所有 Ref 都创建带该前缀且无虚假 upstream 的本地工作分支。

Submodule 必须递归完成，Git LFS pointer 必须在返回前 hydration。任何必要对象下载失败时请求整体失败。

---

# 6. Ref 解析规范

## 6.1 支持的 Ref

| 输入 | 类型 |
|---|---|
| main | Branch |
| feature/login | Branch |
| refs/heads/main | Branch |
| refs/tags/v1.0.0 | Tag |
| pr:123 | GitHub PR |
| mr:123 | GitLab MR |

## 6.2 GitHub

```text
pr:123
    ↓
refs/pull/123/head
```

## 6.3 GitLab

```text
mr:123
    ↓
refs/merge-requests/123/head
```

这里使用的是 PR/MR Source HEAD，而不是合并结果 Ref。

## 6.4 普通分支

```text
main
    ↓
refs/heads/main
```

```text
feature/login
    ↓
refs/heads/feature/login
```

## 6.5 Tag

```text
refs/tags/v1.0.0
```

如果短名称同时匹配 Branch 和 Tag，优先 Branch。

需要指定 Tag 时使用完整 Ref。

Annotated Tag 必须解析为对应的 Commit。

## 6.6 安全限制

必须验证 Ref 格式。

禁止接受任意 Git Revision Expression，例如：

```text
HEAD~3
main^{tree}
abc123...
```

第一版不支持直接传入任意 Commit SHA。

原因是缓存仓库可能持有当前调用方无权访问的历史对象。

必须在本次请求中成功从远端 Fetch 指定 Ref，才能创建对应 Working Copy。

不要通过已有的本地 Ref 静默回退。

---

# 7. Git Fetch 与缓存管理

## 7.1 Bare Repo

Bare Repo 保持完整历史。

不要对共享 Bare Repo 使用 Shallow Clone。

初始化项目后，后续 Fetch 仅更新指定 Ref。

对于任意普通分支：

```bash
git --git-dir=/data/projects/project-a.git \
  fetch --no-tags origin \
  refs/heads/main
```

对特殊 PR/MR 使用对应的完整 RefSpec。

实际实现必须确保本次 Fetch 得到的目标 Commit 被可靠保存，不能在 Fetch 后重新读取一个可能已被并发请求更新的共享 Ref 来判断 Commit。

可以采用请求唯一的内部 Ref：

```text
refs/kagemusha/snapshots/<request-uuid>
```

每次请求的固定 Commit 生命周期需要覆盖整个 Workspace 生成过程。

## 7.2 Fetch Lock

同一 Project 的 Fetch 串行化。

不同 Project 的 Fetch 可以并发。

采用 Linux flock。

不要只依赖：

```python
asyncio.Lock()
```

因为它无法在多个 Worker 或进程间协调。

第一版 Uvicorn 使用单 Worker，但文件锁仍然必须实现。

## 7.3 Git GC

第一版禁止自动执行破坏性 Git GC。

设置：

```text
gc.auto=0
maintenance.auto=false
```

不实现后台 Prune。

Git 缓存可能持续增长，这是第一版明确接受的取舍。

后续如果引入仓库清理，需要确保不会回收正在生成 Workspace 所需的对象。

---

# 8. Shallow Workspace 实现

这是本项目需要重点正确实现的部分。

## 8.1 原则

控制端：

```text
Bare Repo
完整历史
```

Workspace：

```text
Shallow Repo
默认 depth=50
```

两者必须独立。

不要将 Bare Repo 自己变成 Shallow Repository。

## 8.2 固定 Commit

远端 Fetch 成功后，例如得到：

```text
Commit = abcdef123456...
```

服务在 Bare Repo 中为这次请求创建：

```text
refs/kagemusha/snapshots/<uuid>
```

该 Ref 指向本次 Fetch 的 Commit。

这一步的作用：

- 避免多个请求共享同一个 FETCH_HEAD。
- 防止远端分支移动影响本次 Workspace。
- 保证本地 Fetch 可以访问明确的 Ref。
- 为 Git Object 生命周期提供保护。

## 8.3 创建 Workspace

先初始化独立 Git 仓库：

```bash
git init /data/tmp/workspace-<uuid>
```

对于默认 `depth=50`：

```bash
git -C /data/tmp/workspace-<uuid> \
  fetch --no-tags --depth=50 \
  file:///data/projects/project-a.git \
  refs/kagemusha/snapshots/<uuid>
```

然后根据 Ref 类型和 `branch_prefix` 检出固定 Commit。例如普通 Branch：

```bash
git -C /data/tmp/workspace-<uuid> \
  checkout -B main <commit>
```

实际实现中应固定并核对完整 Commit SHA，不能在多个 Git 命令之间依赖未经保护的共享状态。

对于 `depth=0`：

```bash
git -C /data/tmp/workspace-<uuid> \
  fetch --no-tags \
  file:///data/projects/project-a.git \
  refs/kagemusha/snapshots/<uuid>
```

然后 Checkout。

这种做法不需要依赖 `git clone --branch`，因此也适用于 MR/PR 的特殊 Ref。

## 8.4 Why file://

本地 Git 仓库直接 Clone 时，Git 可能启用本地文件复制优化。

通过 `file://` 使用正常 Git 传输机制，才能可靠地应用 Depth 限制。

不得使用：

```bash
git clone --shared
```

也不得使用 alternates 引用源仓库对象。

## 8.5 验证

必须验证：

```bash
git rev-parse HEAD
```

与目标 Commit 完全一致。

对于 `depth > 0`，验证：

```bash
git rev-parse --is-shallow-repository
```

注意：如果目标 Commit 的完整历史本身不足指定深度，Git 可以返回非 Shallow Repository。这是正常情况，不应报错。

对于 `depth=0`，必须保证完整历史。

同时检查：

```text
.git/objects/info/alternates
```

不得引用服务端对象路径。

## 8.6 Git 历史的局限性

默认 Depth=50 不保证包含与目标分支的 Merge Base。

因此：

```bash
git merge-base HEAD main
```

可能失败，尤其当 Workspace 未获取 main 分支，或者共同祖先超出 Shallow History 时。

这是可接受的第一版限制。

本期不要求提供多个完整 Branch 的历史。

Agent 如需完整历史，应请求：

```json
{
  "ref": "mr:123",
  "depth": 0
}
```

---

# 9. Workspace 压缩格式

## 9.1 默认格式

使用：

```text
tar.zst
```

算法：

```text
Zstandard
```

默认参数：

```text
Compression Level = 3
Threads = 1
```

## 9.2 打包命令

使用 GNU tar + zstd：

```bash
tar \
  -C /data/tmp \
  -I 'zstd -T1 -3' \
  -cf /data/tmp/workspace-<uuid>.tar.zst \
  workspace-<uuid>
```

实际生成归档时应统一顶层目录名称为：

```text
workspace/
```

而不是暴露服务内部 UUID。

归档应包含：

- 工作目录全部文件
- `.git`
- 文件权限
- 可执行位
- 符号链接

不得将凭证相关文件打入归档。

## 9.3 解压方式

E2B 内使用：

```bash
mkdir -p /workspace/project

tar --zstd \
  -xf workspace.tar.zst \
  -C /workspace/project \
  --strip-components=1
```

E2B Template 必须包含：

```text
git
tar
zstd
```

## 9.4 压缩限制

默认最大归档大小：

```text
1 GiB
```

超过限制返回 HTTP 413。

不得为此将完整归档读取到 Python 内存。

优先生成临时归档文件，再使用 FileResponse 返回。

应避免不受限制的磁盘占用，允许配置临时空间预算。

第一版不需要支持 gzip、xz、zip 或压缩格式协商。

---

# 10. Git Token 认证

## 10.1 基本原则

用户 Git Token 由 Orchestrator 每次请求时提供。

Git Workspace Service 不持久化 Token。

不执行：

```bash
gh auth login
glab auth login
```

不依赖用户本地 Git 配置。

## 10.2 HTTPS 认证

GitHub：

```text
Username = x-access-token
Password = <token>
```

GitLab：

```text
Username = oauth2
Password = <token>
```

使用 `GIT_ASKPASS` 提供认证。

## 10.3 ASKPASS

实现一个固定的 ASKPASS 程序。

ASKPASS 脚本中不得包含硬编码 Token。

Python 根据请求设置临时执行环境：

```text
GIT_ASKPASS=/app/git-askpass
GIT_TERMINAL_PROMPT=0
GIT_USERNAME=...
GIT_TOKEN=...
```

使用：

```python
asyncio.create_subprocess_exec(...)
```

不得使用：

```python
shell=True
```

不得把用户输入拼成 Shell Command。

清空不可信的继承 Git 配置及 Credential Helper。

Git Token 不得：

- 拼进 Git URL。
- 出现在子进程命令行。
- 出现在 `.git/config`。
- 进入归档。
- 进入访问日志。
- 进入异常响应。
- 被其他请求共享。

Token 通过子进程环境传递时仍存在本机进程读取风险，因此整个 Git Service 必须被视为受信任执行环境。

## 10.4 URL 安全

Repository URL 必须：

- 使用 HTTPS。
- 不包含 URL Userinfo。
- 与 Provider 配置一致。
- Host 在受信任 Allowlist 中。
- 禁止不可信 HTTP 重定向。
- 禁止通过 Git URL Rewrite 变更目标 Host。
- 禁止访问云元数据和未授权内网地址。

需要支持企业自建 GitHub/GitLab Host，由服务配置允许的域名列表。

不要通过禁用 TLS 验证来处理企业证书问题。

可通过受信任 CA Bundle 添加企业内部 CA。

## 10.5 Git Protocol 安全

只允许预期的 HTTPS Git 访问和内部本地 `file://` Fetch。

内部 `file://` 的路径必须由服务端根据 Project ID 构造，而不是由用户直接提供。

禁用非预期的外部协议与 Git Ext Transport。

不要运行任何用户仓库中的代码、Hook 或安装脚本。

建议隔离 HOME、XDG_CONFIG_HOME、Git 系统/全局配置及 Git Template 目录。

---

# 11. 服务自身认证

Git Token 与 Git Workspace Service API Token 是两个不同的概念。

服务间认证：

```http
Authorization: Bearer <KAGEMUSHA_API_TOKEN>
```

使用环境变量：

```text
KAGEMUSHA_API_TOKEN
```

所有业务 API 必须验证该 Token。

`/healthz` 可以不要求认证，但只应提供不含敏感信息的健康状态。

默认监听：

```text
127.0.0.1:8000
```

由 Orchestrator 通过本机或安全私有网络访问。

如果跨主机部署，必须通过 TLS 或受保护的反向代理访问。

---

# 12. 并发控制

## 12.1 规模

预期：

```text
20 concurrent Sandboxes
```

不意味着每秒有 20 个 Copy 请求。

设计目标是支持多个任务同时请求 Workspace，而不是构建高吞吐 Git Hosting Service。

## 12.2 并发限制

默认：

```text
KAGEMUSHA_MAX_CONCURRENT_GIT=4
```

限制重量级 Git/Archive 操作的并发数量。

每个 Project 使用独立 Fetch Lock。

归档生成可以并行，但必须保证对象不会在生成过程中被回收。

## 12.3 进程取消

所有 Git 和 tar 子进程应：

- 有超时。
- 可以取消。
- 取消时终止子进程组。
- 避免僵尸进程。
- 在 finally 中释放资源。

客户端中断下载时仍需清理归档。

## 12.4 临时目录

所有操作使用唯一 UUID 临时目录。

启动时清理超过配置 TTL 的临时资源。

不得清理仍在被活跃请求使用的目录。

对于重启后残留的临时 Ref，也应提供安全的清理机制。

## 12.5 无分布式依赖

第一版只运行一个服务实例。

Uvicorn：

```text
workers=1
```

Docker Compose：

```text
replicas=1
```

未来如果部署多个实例，需要重新设计共享磁盘与并发协调，不能仅依赖单主机 flock。

---

# 13. 错误处理

统一错误格式：

```json
{
  "error": {
    "code": "REF_NOT_FOUND",
    "message": "Requested Git ref was not found"
  }
}
```

## 13.1 错误码

| Error | HTTP |
|---|---|
| INVALID_REQUEST | 400 |
| INVALID_REF | 400 |
| UNAUTHORIZED | 401 |
| GIT_AUTH_FAILED | 401 |
| GIT_ACCESS_DENIED | 403 |
| PROJECT_NOT_FOUND | 404 |
| REF_NOT_FOUND | 404 |
| PROJECT_ALREADY_EXISTS | 409 |
| ARCHIVE_TOO_LARGE | 413 |
| GIT_REMOTE_ERROR | 502 |
| GIT_TIMEOUT | 504 |
| INSUFFICIENT_STORAGE | 507 |
| INTERNAL_ERROR | 500 |

不要直接把未过滤的 Git stderr 返回客户端。

服务日志可以记录经过脱敏处理的 Git 错误类别。

错误信息不得包含 Token。

---

# 14. 配置

通过环境变量配置。

```text
KAGEMUSHA_DATA_DIR=/data
KAGEMUSHA_API_TOKEN=<service-secret>

KAGEMUSHA_MAX_CONCURRENT_GIT=4
KAGEMUSHA_GIT_TIMEOUT_SECONDS=300

KAGEMUSHA_DEFAULT_DEPTH=50
KAGEMUSHA_MAX_DEPTH=1000

KAGEMUSHA_ZSTD_LEVEL=3
KAGEMUSHA_ZSTD_THREADS=1

KAGEMUSHA_MAX_ARCHIVE_BYTES=1073741824
KAGEMUSHA_TEMP_TTL_SECONDS=3600
KAGEMUSHA_MAX_SUBMODULES=100
KAGEMUSHA_MAX_SUBMODULE_DEPTH=10

KAGEMUSHA_ALLOWED_GIT_HOSTS=github.com,gitlab.com
```

Host Allowlist 可以增加企业 GitLab 域名。

不允许将 `KAGEMUSHA_ALLOWED_GIT_HOSTS` 配置为空并解释为允许所有 Host。

配置启动时严格验证，错误时应 Fail Fast。

---

# 15. Dockerfile

## 15.1 镜像选择

使用多阶段构建。

Builder：

```text
ghcr.io/astral-sh/uv:python3.13-trixie-slim
```

Runtime：

```text
python:3.13-slim-trixie
```

Runtime 不需要保留 uv。

暂时不使用 Alpine。

要求构建依赖、运行依赖和 Python 环境一致。

## 15.2 Dockerfile

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock ./

RUN uv sync \
    --locked \
    --no-dev \
    --no-install-project


FROM python:3.13-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git \
       git-lfs \
       tar \
       zstd \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ /app/src/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KAGEMUSHA_DATA_DIR=/data

RUN groupadd --system app \
    && useradd --system --gid app app \
    && mkdir -p \
       /data/projects \
       /data/metadata \
       /data/locks \
       /data/tmp \
    && chown -R app:app /app /data

USER app

EXPOSE 8000

CMD ["uvicorn", "kagemusha.app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
```

此 Dockerfile 假定 Python 项目采用 `src` Layout，且运行依赖都在 `pyproject.toml` 中声明。

Agent 应确保依赖安装与实际项目布局一致。

必须提交 `uv.lock`。

## 15.3 .dockerignore

```text
.git
.venv
__pycache__
.pytest_cache
.mypy_cache
.ruff_cache
.env
.env.*
*.pyc
```

生产部署时建议固定基础镜像 Digest。

---

# 16. Docker Compose

## 16.1 要求

- 使用 Docker Named Volume。
- 不使用 Host Bind Mount。
- 单实例。
- 容器自动重启。
- 默认只暴露本机端口。
- 支持健康检查。
- `docker compose up -d --build` 可以直接运行。
- 容器重建不丢失 Git 缓存。

## 16.2 compose.yaml

```yaml
services:
  git-workspace:
    build:
      context: .
      dockerfile: Dockerfile

    image: git-workspace:local

    restart: unless-stopped

    init: true

    ports:
      - "127.0.0.1:8000:8000"

    environment:
      KAGEMUSHA_DATA_DIR: /data
      KAGEMUSHA_API_TOKEN: ${KAGEMUSHA_API_TOKEN:?required}

      KAGEMUSHA_MAX_CONCURRENT_GIT: "4"
      KAGEMUSHA_GIT_TIMEOUT_SECONDS: "300"

      KAGEMUSHA_DEFAULT_DEPTH: "50"
      KAGEMUSHA_MAX_DEPTH: "1000"

      KAGEMUSHA_ZSTD_LEVEL: "3"
      KAGEMUSHA_ZSTD_THREADS: "1"

      KAGEMUSHA_MAX_ARCHIVE_BYTES: "1073741824"
      KAGEMUSHA_TEMP_TTL_SECONDS: "3600"
      KAGEMUSHA_MAX_SUBMODULES: "100"
      KAGEMUSHA_MAX_SUBMODULE_DEPTH: "10"

      KAGEMUSHA_ALLOWED_GIT_HOSTS: ${KAGEMUSHA_ALLOWED_GIT_HOSTS:-github.com,gitlab.com}

    volumes:
      - git-workspace-data:/data

    stop_grace_period: 30s

    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  git-workspace-data:
```

## 16.3 持久化行为

```bash
docker compose down
docker compose up -d
```

数据必须保留。

```bash
docker compose down -v
```

会删除 Named Volume。

README 必须明确提示这个行为。

容器以非 Root 用户运行，需确保 Named Volume 首次初始化及再次挂载后均具有正确权限。

---

# 17. 日志

使用结构化 JSON 日志。

每个请求至少记录：

```text
request_id
project_id
operation
provider
ref
resolved_commit
depth
fetch_duration_ms
workspace_duration_ms
archive_duration_ms
archive_size_bytes
total_duration_ms
result
error_code
```

禁止记录：

- Git Token
- Authorization Header
- 完整请求体
- 含凭证的 URL
- ASKPASS 环境变量

第一版不需要 Prometheus。

为后续 Prometheus Metrics 预留清晰的操作计时边界即可。

---

# 18. 测试

使用 pytest。

需要提供可重复执行的自动化测试。

## 18.1 Git 测试

- 初始化 Bare Repository。
- 普通 Branch Fetch。
- 带 `/` 的 Branch。
- Tag Fetch。
- Annotated Tag。
- GitHub PR Ref 映射。
- GitLab MR Ref 映射。
- 远端 Branch 更新后重新 Fetch。
- 删除远端 Branch 后请求失败，不回退旧缓存。
- 请求不存在的 Ref。

## 18.2 Depth 测试

建立一个包含至少 120 个 Commit 的测试仓库。

验证：

- depth=1
- depth=10
- depth=50
- depth=100
- depth=0
- depth=-1 被拒绝
- depth=1001 被拒绝

要求：

- Shallow Workspace HEAD 与目标 SHA 一致。
- depth=50 不会意外带入完整历史。
- depth=0 包含完整目标 Commit 历史。
- 完整历史少于 depth 时能正常处理。
- 使用 PR/MR Ref 时可以生成 Shallow Workspace。
- Branch、PR/MR、Tag 及自定义 Branch Prefix 的 HEAD 状态正确。
- HTTPS Submodule 递归初始化且固定在 gitlink Commit。
- 需要认证的 Git LFS 对象在归档前完整下载。

## 18.3 Workspace 独立性

生成归档后：

1. 删除临时 Workspace。
2. 将归档复制到另一个目录或容器。
3. 解压。
4. 验证 Git Status。
5. 验证 Git Log。
6. 验证 Git Diff。
7. 验证 Git Commit。
8. 验证 Object 完整性。
9. 验证不存在外部 alternates。
10. 验证不包含服务端绝对路径。

同时验证目标仓库删除或不可访问后，已经生成的 Workspace 仍然可以正常使用。

## 18.4 并发

- 同一个 Project 并发 Fetch。
- 同一个 Project 并发 Copy。
- 不同 Project 并发 Copy。
- Fetch 过程中取消请求。
- Archive 过程中取消请求。
- 下载中途断开。
- Git Timeout。
- tar/zstd 异常退出。

验证没有死锁、损坏仓库或遗留僵尸进程。

## 18.5 凭证安全

- Token 不会写入 `.git/config`。
- Token 不会写入归档。
- Token 不会被日志输出。
- 不同并发请求使用不同 Token 不会串用。
- Invalid Token 被正确拒绝。
- Host Allowlist 生效。
- HTTP URL 被拒绝。
- 带 Userinfo 的 URL 被拒绝。
- 禁止非授权访问内部 Git Object。

## 18.6 Docker

通过 Compose 运行集成测试：

```bash
docker compose up -d --build
```

验证：

- Healthcheck 成功。
- Named Volume 正常。
- 初始化仓库。
- 创建归档。
- 重启容器。
- 缓存仍然存在。
- 非 Root 用户可以写入数据目录。
- tar、zstd、git 均可用。

---

# 19. 项目结构

```text
git-workspace/
├── Dockerfile
├── compose.yaml
├── .dockerignore
├── .env.example
├── pyproject.toml
├── uv.lock
├── README.md
│
├── src/
│   └── kagemusha/
│       ├── __init__.py
│       ├── app.py
│       ├── config.py
│       ├── models.py
│       ├── errors.py
│       ├── auth.py
│       ├── git.py
│       ├── refs.py
│       ├── repository.py
│       ├── workspace.py
│       ├── archive.py
│       └── locks.py
│
└── tests/
    ├── conftest.py
    ├── test_projects.py
    ├── test_refs.py
    ├── test_depth.py
    ├── test_workspace.py
    ├── test_credentials.py
    ├── test_concurrency.py
    └── test_api.py
```

可以适度合并文件。

不要为了目录结构引入不必要的抽象层。

---

# 20. README 要求

README 至少包含：

1. 项目介绍。
2. 技术栈。
3. 启动方式。
4. 配置参数说明。
5. Git Token 权限要求。
6. GitHub/GitLab 支持情况。
7. Project 初始化 curl 示例。
8. Workspace 下载 curl 示例。
9. Depth 参数说明。
10. E2B 解压示例。
11. Docker Volume 持久化说明。
12. 测试运行方式。
13. 已知限制。
14. 凭证安全注意事项。

示例：

```bash
docker compose up -d --build
```

初始化：

```bash
curl -X POST \
  http://127.0.0.1:8000/projects/my-project \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @init-project.json
```

创建 Workspace：

```bash
curl -X POST \
  http://127.0.0.1:8000/projects/my-project/copies \
  -H "Authorization: Bearer $KAGEMUSHA_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @create-copy.json \
  -o workspace.tar.zst
```

JSON 文件只作为 curl 输入示例，不应长期持久化真实 Token；建议使用临时文件并设置严格权限。

---

# 21. 非目标

第一版明确不实现：

- Web UI
- Git Token 持久化
- SSH Private Key
- Push
- PR/MR 创建
- Git Provider API Client
- gh/glab 集成
- E2B SDK
- 跨节点 Git 缓存
- 多实例服务
- 分布式锁
- 自动缓存 GC
- 对象存储
- 持久化 Task 状态
- Workspace 长期存储
- 多 Ref 工作区（如同时提供 main 和 MR 分支）

这些能力将来可以独立增加。

---

# 22. 验收标准

完成开发后，必须可以直接：

```bash
docker compose up -d --build
```

并满足：

- [ ] 服务正常启动，Healthcheck 通过。
- [ ] 使用 Token 初始化私有 Git 仓库。
- [ ] Project 缓存持久化到 Docker Named Volume。
- [ ] 每次 Copy 必须 Fetch 指定 Ref。
- [ ] 支持 GitHub PR。
- [ ] 支持 GitLab MR。
- [ ] 支持普通 Branch 与 Tag。
- [ ] 支持默认 depth=50。
- [ ] 支持 depth=0 完整历史。
- [ ] 不修改 Bare Repo 的完整历史属性。
- [ ] 归档使用 tar.zst。
- [ ] zstd 默认 Level 3、单线程。
- [ ] 归档包含独立、可运行的 Git 仓库。
- [ ] 归档中不存在凭证。
- [ ] 支持并发 Workspace 创建。
- [ ] 子进程异常和请求取消后正确清理。
- [ ] 错误响应清晰且没有敏感信息泄露。
- [ ] 容器重建后项目缓存保留。
- [ ] 自动化测试通过。
- [ ] README 包含完整部署和 API 示例。

---

# 23. 实施顺序

建议按照以下顺序实现。

**Phase 1 — Project & Git**

完成：

- FastAPI
- Config
- API Auth
- Project 初始化
- Bare Repo Cache
- Token ASKPASS
- Git Fetch
- Ref 解析

**Phase 2 — Workspace**

完成：

- Commit 固定
- 临时 Ref
- Shallow Fetch
- Depth 参数
- 独立 Checkout
- Git Object 验证

**Phase 3 — Archive**

完成：

- tar.zst
- FileResponse
- 文件清理
- Size Limit
- Cancellation

**Phase 4 — Deployment**

完成：

- Dockerfile
- Docker Compose
- Named Volume
- Healthcheck
- 非 Root 运行

**Phase 5 — Verification**

完成：

- 自动化测试
- 并发测试
- 安全测试
- Docker 集成测试
- README

---

# 24. 给 Coding Agent 的最终要求

请根据本 Spec 实现完整可运行的 Git Workspace Service。

要求：

1. 不要只生成脚手架，必须完成业务逻辑。
2. 以 Git CLI 为核心，不引入 GitPython。
3. 确保 Shallow Clone 正确工作，尤其是 GitHub PR 和 GitLab MR。
4. 不允许凭证进入 Workspace 或日志。
5. 不允许直接打包依赖控制机的 Git Worktree。
6. 使用可迁移的独立 Git Repository。
7. 正确处理并发 Fetch、Commit 固定和 Git Object 生命周期。
8. Docker Compose 必须可以直接启动。
9. 数据使用 Docker Named Volume，不使用本地 Bind Mount。
10. 编写并执行自动化测试。
11. 实现完成后报告测试结果和未覆盖的限制。

优先级：

**Correctness > Security > Simplicity > Performance**

避免过度工程化。

第一版只专注于两件事：

**可靠缓存 Git Repository，以及安全地生成可传入 E2B 的独立 Working Copy。**
