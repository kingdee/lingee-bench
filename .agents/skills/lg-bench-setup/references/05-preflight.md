# 阶段 5：预检与基线

目标：在花钱之前确认配置、凭据、镜像和 Docker 链路都没问题。本阶段所有命令都**不调用模型**，可以直接执行。

## 1. 静态预检（不启动容器）

对本次要跑的每个 job 运行（ERP 批跑抽查一两份生成的配置即可，配置结构相同）：

```bash
uv run lg-bench preflight --config <job.yaml> --env-file .env.glm --require-env
```

每个 agent 输出一行 JSON，最后一行是汇总。要求：`errors` 为空、`missing_env` 为空、汇总 `"ok": true`。加 `--dry-run` 会打印 adapter 将执行的安装、配置文件和运行命令（密钥已脱敏），排查配置时用。

| 报错 | 处理 |
|---|---|
| `missing_env` 非空 | 回阶段 3 补变量 |
| 镜像不存在 / lockhash 不符 / 没有本机构建记录 | 回阶段 2 `harness build <name>` |
| `missing task/dataset path` | 题集未下载（回阶段 1），或 job 里的路径写错 |
| `environment.extra_docker_compose must include ...` | job 漏了必需的 compose 片段，对照仓库自带 job |
| `AgentFactory` 实例化失败 | `import_path` 或 `kwargs` 写错，对照仓库自带 job |

不带 `--config` 时会检查 `configs/jobs/` 下全部 job；没下载题集时，指向 `datasets/` 的路径只给"题集未下载"warning，这是正常的。

## 2. 解析结果

```bash
uv run harbor run --config <job.yaml> --print-config
```

核对 `agents[]` 的 `import_path`、`model_name`、`kwargs.version`、超时和题目范围。

## 3. Docker 链路基线

用 Harbor 内置的 `oracle`（参考解）和 `nop`（什么都不做）agent 跑，不调用模型。按目标选择：

| 目标 | 命令 | 期望 |
|---|---|---|
| 所有目标（必做） | `uv run harbor run --path examples/tasks/ap-three-way-match --agent oracle -n 1 --job-name baseline-oracle` | reward 1 |
| 所有目标（必做） | `uv run harbor run --path examples/tasks/ap-three-way-match --agent nop -n 1 --job-name baseline-nop` | reward 0 |
| ERP | `uv run harbor run --config configs/jobs/erp-mcp-mock-smoke/opencode-glm-5.2-erp-mock-smoke.yaml --env-file .env.glm --agent oracle --job-name baseline-erp-mock` | reward 1，`details.json` 里 `matches_live_erp` 为 true |
| 通用题（可选） | 对生成的通用 job 加 `--agent nop -p datasets/lg-bench-general-v1.0/tasks -i SCH-meeting-date-policy-check-L1 --env-file .env.glm --job-name baseline-general-nop`（`-i` 必须和 `-p` 一起用） | 无异常；评分状态 `core_threshold_failed`、reward 0（不调用 Judge） |

- ERP 基线的 oracle 经 MCP 代理真实查询 erp-mock，能证明 mcp-proxy、erp-mock 镜像和题集评测实例配套。
- 每个基线用阶段 7 的汇总脚本看结果。oracle 不是 1、或者有异常，先修环境，别往下走。
- 任务镜像首次构建要拉 Docker Hub 的基础镜像，慢一些；之后每个基线十几秒。
- 基线 job 目录验证完可以删除。

## 4. 只装 harness（可选）

```bash
uv run harbor run --config <job.yaml> --install-only
```

只执行 agent setup，不解题、不调用模型。image 模式下每个 harness 应在几秒内完成。页面上 mean=0 是因为没运行 verifier，不是模型得 0 分。setup 报错多半是镜像未构建或片段未引用，回阶段 2。
