---
name: erp-mock-batch-eval
description: 对当前固定30道ERP题使用离线mock和指定harness、Agent模型、Judge模型做批量评测，生成逐题配置、预检、直接使用Harbor执行并汇总单轮及多轮结果。支持全批和明确指定的子集。安装环境用lg-bench-setup，修改题目或mock数据不属于本skill。
---

# ERP 30 题 × mock × 指定 harness

在仓库根目录工作，Python 使用 `uv run`。题集与 ERP 评测环境不在本仓库，先按
[`datasets/README.md`](../../../datasets/README.md) 从 Hugging Face 下载并重建 mock 镜像。首次使用先读
[`datasets/lg-bench-enterprise-v1.0/README.md`](../../../datasets/lg-bench-enterprise-v1.0/README.md) 和所选 harness 在
[`docs/harness-support.md`](../../../docs/harness-support.md) 的说明。

## 确定本次运行

从用户上下文取得 harness、Agent 模型、Judge 模型、尝试次数和并发；只询问仍缺少的必要选择。
默认每题一次、逐题串行；用户指定并发时直接组织相应 Harbor 进程，不额外封装调度器，
不要擅自增加付费尝试。`batch.py` 只提供配置生成和结果汇总，不启动评测或读取凭据。
用户已经明确要求真实运行时，准备与预检通过后直接执行，不重复确认。仅要求建配置、写 skill
或查看成绩时，不调用真实模型；未授权的镜像构建和真实运行先说明具体动作与成本再征求同意。

固定范围由 `datasets/lg-bench-enterprise-v1.0/dataset.toml`、`tasks/` 和正式逐题job共同校验，必须恰好30题。
清单按`task.name`记录各题Harbor包摘要，逐题重算比对；任务目录中除`run.sh`、`rounds/`、`inputs/`外
不得出现未纳入任务包的文件。变更后按题集README刷新摘要。
`--tasks`使用清单中的完整目录名，例如`finance-bgt-01-L2`；仅用于用户明确要求的冒烟、子集或复跑，报告中必须说明是子集。目录与task.name后缀和metadata.difficulty一致；case_id、mock数据集与白名单仍沿用原业务标识。
不从历史 `jobs/flash-*` 或mock数据集数量推断本批范围。

## 准备配置

选一个仓库内、所选 harness 的已接入 job 作为 **Agent 配置模板**。例如
`configs/jobs/pi-glm-5.2-smoke.yaml`、`deepseek-harness-glm-5.2-smoke.yaml`、
`claude-code-glm-5.2-smoke.yaml`、
`opencode-glm-5.2-smoke.yaml`。
模板决定 adapter、CLI 版本、kwargs 和 harness 专属 compose；模型由参数指定，运行超时沿用各题。
新 harness 可传自己的已接入模板，不通过改名假装已支持。

**运行超时由各题 `task.toml` 唯一决定，不允许 job 级覆盖**：`harbor run` 的配置
（生成配置与手写配置 alike）一律不得携带 `override_timeout_sec`，`batch.py` 生成时会
强制剥离该键（含 harness 模板带入的值）。Harbor 的取值优先级是
`job override_timeout_sec OR task.toml [agent] timeout_sec`——只要 job 配置带了
该键，task.toml 就被架空。某题超时不足时，唯一正确动作是按发布流程调整该题
task.toml（在题集中修改、递增任务版本并更新校验和，随新的题集版本发布），不在job配置里打补丁。背景：
dsh 批次曾因 job 级 1200s 覆盖击杀长题，ATIF 轨迹只在正常收尾落盘，导致
br-01/fa-16/qc-010 三题轨迹整丢。`override_setup_timeout_sec`（镜像/环境安装阶段）
不在此限，task.toml 无对应字段。

```bash
uv run python .agents/skills/erp-mock-batch-eval/scripts/batch.py prepare \
  --harness-job configs/jobs/opencode-glm-5.2-smoke.yaml \
  --model glm-5.3-flash --judge-model glm-5.3 \
  --output jobs/erp30-opencode-20261016
```

输出全在新的 `jobs/<批次>/`：`batch.json`、30 份配置、固定 mock/Judge 的 compose。
目录已存在时拒绝覆盖。实际题名、版本、任务/配置哈希和预期 trial 数均写入清单。
默认不改原模板、任务、金标、mock 数据或现有 adapter。

每题单独运行，保留其 `mcp-allow-*.yaml` 的白名单和数据集绑定；不能将 30 题合并成共用
一个数据集的 job。Agent 的 ERP MCP 固定指向 `lg-mcp-proxy`，末尾 compose 强制
`LG_MCP_TARGET=mock`，Judge 模型也固定在本批配置中。Agent 与 Judge 的模型须都被模型代理允许。

续聊开关沿用正式 job：TC-02、TC-068、INV-007 开启，cn-COST-006 的独立场景保持关闭。
查看 `batch.json` 的 `supports_resume` 和 `resume_tasks`。模板不支持要求的原生续聊时，
先说明能力缺口并处理，不把重开会话当续聊，不静默关闭开关或少跑题目。
此检查依据已有显式 job 配置，不按 metadata 新加强制预检。
## 预检与执行

准备后检查所选 harness 镜像和 mock 镜像；依赖或镜像缺失时按 `lg-bench-setup` 对应阶段处理。
源码取数通过不等于运行镜像一致：题集版本或 mock 引擎变动后，重新构建 mock 镜像，
并用 `scripts/check_release.py --datasets` 核对白名单片段与评测实例。30 题没有统一参考解，
不能要求它们全部 oracle=1；种子题的 oracle/nop 只证明工程链路。

对生成的 **每份** job 运行静态预检，使用本次运行相同的 env 文件。例如：

```bash
uv run lg-bench preflight \
  --config jobs/erp30-opencode-20261016/configs/finance-bgt-01-L2.yaml \
  --env-file .env.glm --require-env
```

检查 `errors`、`missing_env`、实际 Harbor/CLI 版本、MCP、超时、尝试次数与续聊能力。
超时核对以各题 `task.toml [agent] timeout_sec` 为准：生成的 job 配置里不应出现
`override_timeout_sec`（出现即本规则被违反，删掉再预检）。需要完整解析时用
`uv run harbor run --config <配置> --print-config`。
凭据只进入 env 文件；不回显值，不把真实密钥写进生成配置或报告。
已获真实运行授权且预检通过后：

```bash
uv run harbor run \
  --config jobs/erp30-opencode-20261016/configs/finance-bgt-01-L2.yaml \
  --env-file .env.glm --job-name finance-bgt-01-L2
```

全批按 `batch.json` 的 `tasks` 逐题调用 Harbor。`--config` 使用该条目的 `config`，
`--job-name` 使用其 `task`，这样结果才落在清单声明的 `job_dir`（`runs/<任务名>/`），
汇总无需猜测目录。`prepare --attempts N` 控制每题尝试次数；同一 job 内的并发由
Harbor 的 `n_concurrent_trials` 控制，跨题并发需在运行记录中单独注明。
使用可持续跟踪的终端会话启动，定期查看 Harbor 的 `job.log`、`result.json` 和逐 trial 归档。
按清单的 `supports_resume`/`resume_tasks` 先确认适配器能力。运行前检查配置与任务版本仍与
准备时一致、目标 job 目录尚不存在；追加实验或复跑使用新批次目录，明确选题原因。

## 汇总与解释

```bash
uv run python .agents/skills/erp-mock-batch-eval/scripts/batch.py summary \
  --batch jobs/erp30-opencode-20261016/batch.json
```

生成 `summary.json`，逐 trial 保留证据路径、原始 rewards、content、numeric、overall 和状态。
单轮读 `verifier/`；多轮依任务声明的末步读 `steps/<末步>/verifier/`，不把首轮快照当最终评分。
当前任务均为末步评分；策略改变时先调整汇总方法，不自行平均各轮。

- `graded`：有正式评分；均值只以该桶为分母，并报告分母和缺失 overall 的情况。
- `evaluation_error`：评分器、证据采集或 Judge 协议失败；即使 reward 写零也单独列出。
- `execution_error`：Harbor trial 记录的执行异常；与正常业务低分分开。CLI 非零但没有
  trial 结果时，从 Harbor 日志另行说明，不凭退出码伪造逐 trial 成绩。
- `no_reward` / `pending`：分别表示结束后缺评分、尚无完成结果；不按零分补齐。

CLI exit=0 不代表评分有效，取数完整不代表评分全通过。报告完成数/预期数、上述分桶、
双满分数、graded 均值、任务与模型版本、尝试次数、并发和逐题失败原因。
多轮的 Agent、proxy、trajectory 和产物也在 `steps/<轮次>/`；逐轮查证会话与取证，
不能因根 `agent/` 无日志而断言没调用模型。代理日志可能累计，先检查是否为前缀再去重计数，
不要简单相加重复事件。泄露检查可复用 `lg-bench-setup/scripts/summarize_job.py --env-file`，
其根目录代理摘要不作为多轮完整日志的证据。

低分与失败保留原档，不为提高成绩改题面、数据、金标或重评分；需要修复时另开明确范围的任务。
历史成绩与任务升级后的成绩分开解释。`jobs/`、凭据和本机缓存不提交 Git。
