---
name: lg-bench-setup
description: 帮外部用户从刚 clone 的 Lingee Bench 仓库一路做到出成绩：装环境、从 Hugging Face 下载题集、构建代理/harness/ERP mock 镜像、配模型凭据、选 job、预检与 oracle/nop 基线、真实运行，最后按评分状态分桶汇总成绩。覆盖示例题冒烟、通用题集 35 题批跑和 ERP 30 题批跑。用户提到安装、上手、快速开始、下载题集、配置 harness/模型/凭据、跑冒烟、跑批、评分汇总时使用。
---

# Lingee Bench：从 clone 到出成绩

把用户从未配置的仓库带到"跑完评测并拿到可报告的成绩"。本文件只做路由：进入每个阶段前，先**完整读**对应的 `references/` 文件，一次只做一个阶段，做完向用户报告结果再进入下一阶段。用户说某阶段已完成时，先跑该阶段的检查命令确认，不凭口述跳过。

## 先问清三件事

1. **评什么**：示例题冒烟（验证链路，不需要题集）、通用题集 35 题、ERP 题集 30 题，或其中几项。
2. **用哪个 harness**：Claude Code、Pi、DeepSeek Harness、OpenCode 中的哪几个。没有偏好时推荐 OpenCode（MIT，镜像最小）。
3. **用哪个模型**：Agent 模型和 Judge 模型分别是什么、端点是哪个平台。通用题和 ERP 题都要 Judge。

## 按目标选阶段

| 阶段 | 冒烟 | 通用 35 题 | ERP 30 题 | 参考文件 |
|---|:-:|:-:|:-:|---|
| 0. 环境：`uv sync`、Docker、本地检查 | ✓ | ✓ | ✓ | [`00-environment.md`](references/00-environment.md) |
| 1. 下载题集并校验 | — | ✓ | ✓ | [`01-datasets.md`](references/01-datasets.md) |
| 2. 镜像：模型代理、harness；ERP 另需 MCP 代理与 erp-mock | ✓ | ✓ | ✓ | [`02-images.md`](references/02-images.md) |
| 3. 凭据：`.env.glm`、模型白名单 | ✓ | ✓ | ✓ | [`03-credentials.md`](references/03-credentials.md) |
| 4. Job：现成 job 或生成批跑配置 | ✓ | ✓ | ✓ | [`04-job.md`](references/04-job.md) |
| 5. 预检与 oracle/nop 基线（不调模型） | ✓ | ✓ | ✓ | [`05-preflight.md`](references/05-preflight.md) |
| 6. 真实运行（付费，先征得同意） | ✓ | ✓ | ✓ | [`06-run.md`](references/06-run.md) |
| 7. 结果与评分汇总 | ✓ | ✓ | ✓ | [`07-results.md`](references/07-results.md) |

ERP 30 题的配置生成、逐题运行和汇总由相邻的 [`erp-mock-batch-eval`](../erp-mock-batch-eval/SKILL.md) 负责；本 skill 做完阶段 0–3 和阶段 5 的 ERP 基线后交给它，结果解读回到阶段 7。

第一次使用时建议顺序：先用示例题冒烟走通 0、2、3、5、6、7，确认链路和凭据没问题，再下载题集跑批。冒烟花费最小（单题约 1–2 分钟、几次模型调用）。

## 交互规则

- 用用户的语言回复。配置键、文件内容、命令保持原样。
- **以仓库现状为准**，不要信本 skill 里记下的版本号或列表。运行时发现选项：`ls configs/jobs configs/jobs/*/`、`ls examples/tasks datasets/*/tasks`、`uv run lg-bench harness status`、`uv run lg-bench preflight`。权威文档是 `README.md`、`datasets/README.md`、`docs/harness-support.md`、`docs/harness-images.md`、`.env.example`。
- 先给用户看要执行的命令或要写的文件，再执行或写入。
- 所有命令在仓库根目录执行，Python 一律走 `uv run`。
- `harbor run` 有实时进度条，耗时长：用后台进程启动或请用户在自己的终端运行，再看 `jobs/<job-name>/result.json` 判断是否结束。

## 护栏（所有阶段适用）

- **密钥只放 env 文件**（`.env.glm`、`.env.mcp`，都已被 `.gitignore` 忽略）。不读取、不回显 env 文件里的值；检查凭据只用 `lg-bench preflight --require-env` 的 set/missing 输出。用户把密钥贴进对话时，提醒他轮换。YAML 里只写变量名，不写值。
- 不要在 env 文件里设置 `ANTHROPIC_*` / `OPENAI_*`，只用带 `LG_` 前缀的变量，否则会绕过模型代理 sidecar。
- **付费或耗时操作先征得同意**，并说清题数、尝试次数、并发和预计耗时：`harness build`（联网下载上百 MB）、题集下载、真实模型运行、批跑。oracle/nop、`--install-only`、preflight 不调用模型，可以直接跑。
- 不改 `datasets/`（下载的题集）和 `examples/tasks/` 下的题目文件，不改生成的 `configs/compose/harness/*.yaml`，不改仓库自带的 job。自定义 job 一律写成 `configs/jobs/*.local.yaml`（已忽略）。
- **成绩只按评分状态分桶报告**：正式评分（graded）才进均分；执行异常、评测错误（含 Judge 故障）、未出分、未结束都单独列出，不能当作 0 分，也不能为了提高成绩改题、改评分或只挑低分题重跑。
- 不提交 `jobs/`、`datasets/` 下载内容、env 文件、`.cache/`。
