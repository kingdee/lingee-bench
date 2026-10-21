# 阶段 2：镜像

目标：所选 job 需要的本地镜像都已构建。仓库不分发任何镜像和 harness 二进制，都在本机构建。

| 镜像 | 谁需要 | 构建 |
|---|---|---|
| `lg-bench/model-proxy:0.1.0` | 所有 job | `docker build -f docker/model-proxy/Dockerfile -t lg-bench/model-proxy:0.1.0 .` |
| harness 只读挂载镜像 | 所有真实运行 | `uv run lg-bench harness build <name>` |
| `lg-bench/mcp-proxy:0.1.0` | ERP 题、ERP mock 示例题 | `docker build -f docker/mcp-proxy/Dockerfile -t lg-bench/mcp-proxy:0.1.0 .` |
| `lg-bench/erp-mock-mcp:0.1.0` | ERP 题、ERP mock 示例题 | 先完成阶段 1，再 `docker build -f docker/erp-mock-mcp/Dockerfile -t lg-bench/erp-mock-mcp:0.1.0 .` |

镜像标签以 `configs/compose/*.yaml` 里的 `image:` 为准。已存在时用 `docker image inspect <tag> --format '{{.Id}}'` 确认，不必重建。

## 1. 模型代理

每个 trial 会追加一个 `lg-model-proxy` sidecar（`configs/compose/model-proxy.yaml`，`pull_policy: never`）。真实密钥只进这个容器，Agent 和 Judge 都经它访问模型。

## 2. harness 只读挂载镜像

官方 job 都是 `install_mode: image`：trial 内只做挂载校验和软链，秒级完成；镜像没构建就直接报错，不回退在线安装。

```bash
uv run lg-bench harness status
```

每行一个 harness。`exists: true`、`errors: []`、`compose_current: true` 表示就绪。缺哪个就构建哪个，只构建用户选定的：

```bash
uv run lg-bench harness build opencode     # 可写多个，或 all
uv run lg-bench harness status
```

- 这一步会联网下载 harness 制品：按 `docker/harness/harness-lock.json` 下载并校验哈希，缓存到 `.cache/lg-harness/`。Claude Code 约 232 MB，慢网会很久。执行前告诉用户。
- **许可**：Claude Code 是商业 CLI。构建前提醒用户自行阅读并遵守上游条款（见 `docker/harness/<name>/files/NOTICE`），由用户决定是否构建。Pi、DeepSeek Harness、OpenCode 为 MIT。
- 本机已有校验过的制品时，可用 `uv run lg-bench harness seed <file>` 放进缓存（哈希必须在锁文件里），省掉下载。
- `configs/compose/harness/<name>.yaml` 由工具生成，不要手改。
- 构建输出里的 `InvalidDefaultArgInFrom` 警告可以忽略：基础镜像由构建参数传入。

harness 名称以 `ls docker/harness/` 为准（目录名即名称）。

## 3. ERP：MCP 代理与离线 mock

ERP 题经 `lg-mcp-proxy` 访问离线 ERP mock（`erp-mock`），代理按题过滤工具。两个镜像都要构建：

- erp-mock 镜像在构建时把 `datasets/environments/erp-mock-mcp/` 里的评测实例和业务包打进去，所以**必须先下载题集**。目录不存在时构建会报 `not found` 失败，这是预期行为。
- 换题集 revision 后必须重建 erp-mock 镜像。
- 校验镜像内的实例：
  ```bash
  docker run --rm --entrypoint sh lg-bench/erp-mock-mcp:0.1.0 -c \
    'erp-mock-mcp validate --instance instances/erp-benchmark --packs-dir packs --data-dir /tmp/v'
  ```
  期望最后一行是"全部校验通过"。

## 4. 运行器镜像（可选）

在容器里跑 Harbor 的部署方式见 `docs/harness-support.md`。它挂载 `/var/run/docker.sock`，等同宿主机 root 权限，只在专用评测机上使用。上手阶段直接在宿主机跑 Harbor 即可。

## 常见故障

- `harness status` 报 lockhash 不符或镜像 ID 与构建记录不符：标签被改指或锁文件已更新，重新 `harness build <name>`。
- 构建时 sha256/sha512 不一致：下载损坏或被篡改，删掉 `.cache/lg-harness/blobs/` 下对应文件后重试，不要绕过校验。
- 拉基础镜像超时（`DeadlineExceeded`、`context deadline exceeded`）：网络问题，重试；任务镜像首次构建也要拉 Docker Hub 的基础镜像。
