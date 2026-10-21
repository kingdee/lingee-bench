# 阶段 6：真实运行

目标：用户确认后，用真实模型跑冒烟或批跑。

## 1. 先确认

真实运行会调用付费模型（Agent 和 Judge 都要），并长时间占用本机 Docker。执行前告诉用户：

- 跑哪个 job、哪个 harness、Agent 与 Judge 模型、几道题、`n_attempts` 和并发数，以及预期 trial 数；
- 大致耗时：示例题冒烟约 1–2 分钟；通用题和 ERP 题单题一般几分钟到十几分钟，批次按"题数 × 尝试次数 × 单题耗时 ÷ 并发"估算，30–35 题串行通常要数小时；
- 首次运行时每道题还要构建任务镜像。多数几十秒，个别题更久，例如 `finance-bgt-05-L3` 要安装 Playwright 和 Chromium，慢网下可能超过半小时。之后会走 Docker 缓存。这段时间算在环境准备里，不消耗模型额度；构建被中断时该 trial 记为执行异常；
- 费用以模型服务实际计价为准。Harbor 里的 `cost_usd` 不是账单（Claude Code 按 Anthropic 价格估算，其余多为空）；
- 正式成绩要固定题集 revision（阶段 1），并在报告里写明。

用户明确同意后再运行。只要求"配置好"或"看成绩"时不要启动真实运行。

## 2. 运行

通用要求：

- 总是带 `--job-name`，结果落在 `<jobs_dir>/<job-name>/`。同一配置复跑换一个 job-name，不要只挑低分题重跑。
- 用后台进程启动，或请用户在自己的终端运行；前台同步执行容易卡住。看 `<jobs_dir>/<job-name>/job.log` 跟进度，`result.json` 里 running/pending 归零才算结束。
- 跑之前先按阶段 5 预检通过。

### 示例题冒烟

```bash
uv run harbor run --config configs/jobs/<harness>-glm-5.2-smoke.yaml \
  --env-file .env.glm --job-name smoke-<harness>-$(date +%Y%m%d-%H%M)
```

### 通用题集批跑

```bash
uv run harbor run --config configs/jobs/general-<harness>-<model>.local.yaml \
  --env-file .env.glm --job-name general-<harness>-<model>-$(date +%Y%m%d-%H%M)
```

一个 job 内含全部所选题目，并发由生成时的 `--concurrency` 决定。

### ERP 题集批跑

按 `erp-mock-batch-eval` 逐题运行 `batch.json` 里的配置，`--job-name` 必须用条目的 `task`，结果才会落在清单声明的目录：

```bash
B=jobs/erp30-<harness>-<日期>
python3 -c "import json,sys; [print(t['task'], t['config'], t['job_dir']) for t in json.load(open(sys.argv[1]))['tasks']]" $B/batch.json |
while read -r task cfg dir; do
  if [ -e "$dir" ]; then echo "skip $task：已有结果目录 $dir"; continue; fi
  uv run harbor run --config "$cfg" --env-file .env.glm --job-name "$task" < /dev/null
done
```

- 默认逐题串行；要跨题并发时自行分组启动多个进程，并在报告里注明。
- 循环会跳过已有结果目录的题，所以中断后重新执行同一段命令即可接着跑。被中断那一题的目录可能不完整，汇总时会显示为 `pending` 或 `execution_error`；要补跑它，先向用户说明，再把它的目录改名留档后重跑，不要直接删除。
- 运行超时由各题 `task.toml` 决定，生成的配置不带 `override_timeout_sec`，不要手动加。

## 3. 运行中的常见故障

| 现象 | 检查 |
|---|---|
| `AgentSetupTimeoutError` | 多半是镜像未构建、走了在线安装且出网慢。确认 job 是 `install_mode: image`，回阶段 2 |
| 401 / 403 | 凭据或端点不匹配。不要输出密钥做诊断，让用户核对 `.env.glm` |
| 404 / 模型不存在 | 账号是否有该模型权限、网关路由名、YAML 的 `model_name` |
| 代理返回 400 | 请求的模型（Agent 或 Judge）不在白名单内，见阶段 3 第 3 节 |
| 代理返回 503 `not set` | 该路由的 `LG_*_BASE_URL` / `LG_*_API_KEY` 没传进来：是不是漏了 `--env-file` |
| `No such image: lg-bench/erp-mock-mcp:0.1.0` | erp-mock 镜像未构建，回阶段 2 |
| 拉基础镜像超时 | 网络问题，该 trial 记为执行异常；网络恢复后换 job-name 复跑这一题，并在报告里说明 |
| `NonZeroAgentExitCodeError` | 看 trial 的 `agent/` 下 stderr 和 harness 日志，属于运行异常，不算成绩 |
| 轮数或预算耗尽 | 保留为系统失败；要放宽预算就另建一个配置明确不同的实验 |

更多 harness 专属问题见 `docs/harness-support.md` 对应小节和 `docs/claude-code.md`。
