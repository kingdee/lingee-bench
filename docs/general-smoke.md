# 通用任务单题冒烟（Mac / Linux）

此入口复现 OpenCode 1.19.0 + `glm-5.3-flash` 的通用任务验证，默认选择
`SCH-meeting-date-policy-check-L1`。Agent 和 Judge 都使用同一模型；默认单题、一次尝试、无重试。

## 准备环境

在仓库根目录执行。需要按 `uv.lock` 安装依赖、Docker API ≥ 1.48，以及本机已有模型代理和 OpenCode 镜像：

```bash
uv sync --locked
docker build -f docker/model-proxy/Dockerfile -t lg-bench/model-proxy:0.1.0 .
uv run lg-bench harness build opencode
```

已有镜像时跳过构建。首次构建会联网下载依赖，实际模型运行会调用付费 API。
按 `.env.example` 配置 `.env.glm` 中的 `LG_OPENAI_BASE_URL` 和 `LG_OPENAI_API_KEY`。
真实端点与密钥只进入模型代理容器。

## 直接运行正式 Job

```bash
# 检查镜像、路径和凭据；凭据只输出 set/missing
uv run lg-bench preflight \
  --config configs/jobs/opencode-glm-5.3-flash-general-smoke.yaml \
  --env-file .env.glm --require-env

# 核对实际运行配置
uv run harbor run \
  --config configs/jobs/opencode-glm-5.3-flash-general-smoke.yaml --print-config

# 实际模型评测，结果保存在 jobs/；完成后自动清理容器
uv run harbor run \
  --config configs/jobs/opencode-glm-5.3-flash-general-smoke.yaml \
  --env-file .env.glm \
  --job-name "general-opencode-flash-$(date +%Y%m%d-%H%M%S)"
```

Job 直接读取 `datasets/lg-bench-general-v1.0/tasks/SCH-meeting-date-policy-check-L1`，
不生成任务副本。可用 Harbor 的目录和筛选参数选择另一道题，例如：

```bash
uv run harbor run \
  --config configs/jobs/opencode-glm-5.3-flash-general-smoke.yaml \
  --env-file .env.glm \
  --path datasets/lg-bench-general-v1.0/tasks \
  --include-task-name SCH-cross-timezone-meeting-conversion-L2
```

`.env.glm` 和 `jobs/` 均不提交。正式 Job 与 Compose 随仓库提交，按[题集](../datasets/README.md)下载题集后即可使用。

## 网络边界

正式Job用`configs/compose/general-isolation.yaml`将任务容器限制在Docker内部网络；模型代理同时连接内部与外部网络。Agent和verifier经`lg-model-proxy`访问指定模型，真实密钥只进入代理容器。

任务使用`network_mode = "public"`让Harbor不再另起网络控制sidecar，实际出网边界由Compose执行。运行时必须引用正式Job；单独使用`--path`不会自动带上该隔离配置。

## 查看结果

```bash
uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py \
  jobs/<本次job名> --env-file .env.glm
```

检查 trial 无异常、`verifier/score.json` 为 `dual_channel`，并阅读规则及 Judge 的逐项结果。
Judge故障记为评测错误，单独统计，不计入正常业务得分。产物收集到 trial 的 `artifacts/workspace/output/`。
