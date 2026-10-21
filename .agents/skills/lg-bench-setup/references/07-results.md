# 阶段 7：结果与评分汇总

目标：分开说清"链路是否跑通"和"成绩是多少"，并按评分状态分桶报告。job 目录只读，不改任何结果文件。

## 1. 汇总

**示例题冒烟、通用题集批跑**（一个 job）：

```bash
uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py <jobs_dir>/<job-name> --env-file .env.glm
```

输出 job 统计、逐 trial 的 agent/版本/模型/耗时/评分状态/reward/异常/token、模型代理请求统计、评分汇总（分桶计数与正式评分均分），以及密钥泄露检查（只输出变量名和命中文件数，不输出密钥值）。传入的必须是含 `result.json` 的 job 目录本身；传父目录时脚本会列出其中的 job 名，让用户选，不要自己猜。

**ERP 题集批跑**（每题一个 job）：

```bash
uv run python .agents/skills/erp-mock-batch-eval/scripts/batch.py summary --batch jobs/erp30-<...>/batch.json
uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py jobs/erp30-<...>/runs/<task> --env-file .env.glm   # 抽查单题、做泄露检查
```

`summary` 写出 `summary.json`，含完成数、分桶、双满分数、graded 均值和逐 trial 证据路径。多轮题按末步评分，解读规则见 `erp-mock-batch-eval`。

## 2. 评分状态

| 状态 | 含义 | 是否进均分 |
|---|---|---|
| `graded` | 正式评分。通用题 `score.json` 为 `dual_channel`（规则 + Judge 合分）或 `core_threshold_failed`（核心产物缺失，记 0 分）；ERP 题 `results.json` 为 `graded`；示例题写出 reward | 是 |
| `evaluation_error` | 评分器、证据采集或 Judge 失败（含 `invalid_*`、`judge_unavailable_*`），即使 reward 写了 0 | 否，单独列出 |
| `execution_error` | Harbor 记录的运行异常（超时、容器、镜像、网络） | 否，单独列出 |
| `no_reward` | 结束了但没有评分 | 否，单独列出 |
| `pending` | 还没结束 | 否；批次未完成时成绩只是阶段性的 |

主分：通用题和示例题取 `reward`（0–1），ERP 题取 `overall`；ERP 还要看 `content_score` 与 `numeric_accuracy` 两个分项。

## 3. 判读

**链路跑通**（逐 trial 都要满足）：

- 没有执行异常；
- 代理请求数 > 0，模型全部是预期的 Agent/Judge 模型，没有非 200、`rejected`、`misconfigured`；
- `artifacts/` 下有产物，`verifier/` 下有评分输出；
- 泄露检查全部为 0。

注意：

- 链路正常而分数低，是模型 + harness 这次没解对，不要因此改配置、改题或重装。
- `evaluation_error` 多半是 Judge 问题：先看该 trial 的 `verifier/score.json` 或 `results.json` 里的 `status`/`error`，再查 Judge 模型是否在白名单内、`LG_OPENAI_*` 是否可用。修好后换 job-name 复跑受影响的题，并在报告里说明。
- 单次运行不能下稳定性结论。要比较 harness 或模型，需要同配置多次运行（`n_attempts` ≥ 3），并且题集 revision、仓库版本、Judge 模型一致。

## 4. 报告模板

向用户报告时至少包含：

- 题集与 revision、仓库提交；harness 与版本、Agent 模型、Judge 模型；题数（是否子集）、尝试次数、并发；
- 预期 trial 数、实际完成数，以及各状态的计数；
- 正式评分均分和它的分母（graded trial 数）；ERP 另报双满分（content 与 numeric 都为 1）的题数；
- 未计入均分的 trial 逐条列出原因；
- 泄露检查结果。

通用题集和 ERP 题集分别报告，不合并成一个分数。

## 5. 想深入看某个 trial

`<jobs_dir>/<job-name>/<task>__<id>/` 下：

| 路径 | 内容 |
|---|---|
| `result.json` | 该 trial 的 agent 信息、reward、异常、token、各阶段耗时 |
| `verifier/` | 示例题 `details.json`；通用题 `score.json`（规则与 Judge 逐项明细）；ERP 题 `results.json`；以及 `reward.json`、`test-stdout.txt` |
| `steps/<轮次>/` | 多轮题每轮的 agent、代理日志和 verifier |
| `agent/` | harness 原始日志与轨迹（如 `opencode.txt`、`trajectory.json`） |
| `agent/model-proxy/requests.jsonl` | 每次模型请求的路由、模型、状态码、耗时（无密钥、无正文） |
| `agent/harness-provenance.json` | 实际安装方式、版本、lockhash、镜像 ID |
| `artifacts/` | Harbor 收集的产物 |

图形化浏览轨迹：请用户在自己的终端运行 `uv run harbor view jobs`（启动本地 Web 服务，默认 127.0.0.1:8080，会一直占用终端）。

## 6. 收尾

- 结果不进 Git（`jobs/` 已忽略）。对外分享前再检查一遍日志里有没有凭据、请求内容或内部地址。
- 各题集和运行版本分别报告，评分政策以对应任务为准（ERP 见 `docs/erp-scoring.md`）。
