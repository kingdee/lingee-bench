# Lingee Bench

[English](README.md) | **简体中文**

Lingee Bench 是基于 [Harbor](https://github.com/harbor-framework/harbor) 的 Agent 评测框架，提供 harness 适配、模型与 MCP 代理、离线 ERP mock 服务、批跑与评分汇总工具。题目、输入数据和评分器随题集发布在 Hugging Face：[KD-Lingee/lingee-bench](https://huggingface.co/datasets/KD-Lingee/lingee-bench)。

| 题集 | 题量 | 获取方式 |
| --- | ---: | --- |
| 企业管理 | 30 | 从 Hugging Face 下载到 `datasets/lg-bench-enterprise-v1.0/`；财务、制造、供应链 |
| 通用任务 | 35 | 从 Hugging Face 下载到 `datasets/lg-bench-general-v1.0/`；数据分析、文档、检索、图像及日程 |
| 示例任务 | 2 | 随仓库提供，位于 [examples/tasks](examples/tasks)；用于框架自测和出题参考，不计正式成绩 |

企业管理与通用任务分别报告成绩。发布版本由本仓库的 Git 提交或 tag、题集的 Hugging Face revision、题目清单和文件摘要共同标识。

支持 Claude Code、Pi、DeepSeek Harness、OpenCode。运行要求和配置见[文档目录](docs/README.md)。

## 用 coding agent 带你跑

仓库在 `.agents/skills/` 下提供了 skill，支持 skills 的 coding agent 可以照着它从零做到出成绩，每一步都会先给你看命令，付费操作会先征得同意：

- [lg-bench-setup](.agents/skills/lg-bench-setup/SKILL.md)：安装、下载题集、构建镜像、配凭据、预检与基线、冒烟、通用题集批跑、评分汇总。
- [erp-mock-batch-eval](.agents/skills/erp-mock-batch-eval/SKILL.md)：ERP 30 题批跑与汇总。

在仓库根目录打开 agent，直接说明目标即可，例如：

> 用 lg-bench-setup 帮我把 OpenCode + glm-5.3-flash 跑通，先跑示例题冒烟，再跑通用题集 35 题并汇总成绩。

密钥由你自己写进 `.env.glm`，不要贴进对话。下面是同样流程的手动步骤。

## 1. 安装

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 及可用的 Docker（API ≥ 1.48，macOS 或 Linux）。

```bash
uv sync --locked
cp .env.example .env.glm
```

在 `.env.glm` 填写模型端点和密钥：harness 用 OpenAI 兼容协议时填 `LG_OPENAI_*`，Claude Code 填 `LG_ANTHROPIC_*`。通用题和 ERP 题的 Judge 走 OpenAI 兼容协议，跑这两类题时 `LG_OPENAI_*` 必填。凭据文件不提交；真实凭据只进入模型代理。

构建模型代理和所用 harness 镜像，以 OpenCode 为例：

```bash
docker build -f docker/model-proxy/Dockerfile -t lg-bench/model-proxy:0.1.0 .
uv run lg-bench harness build opencode
```

## 2. 示例题冒烟（不需要题集）

先确认链路和凭据可用：

```bash
uv run lg-bench preflight --config configs/jobs/opencode-glm-5.2-smoke.yaml --env-file .env.glm --require-env
uv run harbor run --config configs/jobs/opencode-glm-5.2-smoke.yaml --env-file .env.glm --job-name smoke-opencode
uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py jobs/smoke-opencode --env-file .env.glm
```

这个 job 使用 `glm-5.2`。代理默认只放行 `glm-5.2`；换模型时改 `.env.glm` 的 `LG_PROXY_ALLOWED_MODELS`，并在 job 的 `*.local.yaml` 副本里改 `model_name`。

## 3. 下载题集

固定 revision 的方法见[题集下载](datasets/README.md)：

```bash
uvx --from huggingface_hub hf download KD-Lingee/lingee-bench \
  --repo-type dataset --revision <revision> --local-dir datasets \
  --include "lg-bench-*/**" --include "environments/**" --include "registry.json"
uv run python scripts/check_release.py --datasets
```

## 4. 通用题集批跑与评分

生成批跑配置：任选 harness、Agent 模型和 Judge 模型，网络隔离与 Judge 注入沿用正式口径。脚本同时生成模型白名单片段。

```bash
uv run python .agents/skills/lg-bench-setup/scripts/general_job.py \
  --harness-job configs/jobs/opencode-glm-5.2-smoke.yaml \
  --model glm-5.3-flash --judge-model glm-5.3-flash \
  --output configs/jobs/general-opencode-glm-5.3-flash.local.yaml
uv run lg-bench preflight --config configs/jobs/general-opencode-glm-5.3-flash.local.yaml --env-file .env.glm --require-env
uv run harbor run --config configs/jobs/general-opencode-glm-5.3-flash.local.yaml \
  --env-file .env.glm --job-name general-opencode-flash
uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py jobs/general-opencode-flash --env-file .env.glm
```

默认跑全部 35 题，`--tasks` 可选子集。单题运行和网络边界见[通用任务运行](docs/general-smoke.md)。

## 5. ERP 题集批跑与评分

先构建 MCP 代理与离线 ERP mock。mock 镜像会打入下载的评测实例 `datasets/environments/erp-mock-mcp/`，题集更新后需要重新构建：

```bash
docker build -f docker/mcp-proxy/Dockerfile -t lg-bench/mcp-proxy:0.1.0 .
docker build -f docker/erp-mock-mcp/Dockerfile -t lg-bench/erp-mock-mcp:0.1.0 .
```

在 `.env.glm` 设置 `LG_PROXY_ALLOWED_MODELS`，包含本次的 Agent 与 Judge 模型，例如 `glm-5.3-flash,glm-5.3`。然后生成 30 题配置、逐题运行并汇总：

```bash
B=jobs/erp30-opencode
uv run python .agents/skills/erp-mock-batch-eval/scripts/batch.py prepare \
  --harness-job configs/jobs/opencode-glm-5.2-smoke.yaml \
  --model glm-5.3-flash --judge-model glm-5.3 --output $B
python3 -c "import json,sys; [print(t['task'], t['config'], t['job_dir']) for t in json.load(open(sys.argv[1]))['tasks']]" $B/batch.json |
while read -r task cfg dir; do
  [ -e "$dir" ] && continue   # 已有结果的题跳过，中断后可重复执行
  uv run harbor run --config "$cfg" --env-file .env.glm --job-name "$task" < /dev/null
done
uv run python .agents/skills/erp-mock-batch-eval/scripts/batch.py summary --batch $B/batch.json
```

每题绑定自己的 mock 数据集和工具白名单，所以每题一个 job。评测使用随题集发布的冻结 mock，不需要连接真实 ERP。评分约定见 [ERP 评分](docs/erp-scoring.md)。

## 成绩怎么读

汇总按评分状态分桶：只有正式评分（graded）进入均分；执行异常、评测错误（含 Judge 故障）、未出分、未结束单独列出，不当作 0 分。报告成绩时写明仓库提交、题集 revision、harness 与版本、Agent 与 Judge 模型、尝试次数，以及均分的分母。通用题集与 ERP 题集分别报告。

## 检查与开发

不需要题集：

```bash
uv run lg-bench validate          # 校验 examples/tasks
uv run lg-bench smoke             # 示例任务的本地参考解与评分器
uv run python scripts/check_release.py
uv run pytest -q                  # 依赖题集的用例显示为 skipped
```

下载题集后：

```bash
uv run python scripts/check_release.py --datasets
uv run lg-bench validate --path datasets/lg-bench-enterprise-v1.0/tasks
uv run lg-bench validate --path datasets/lg-bench-general-v1.0/tasks
uv run pytest -q
```

## 目录

```text
examples/tasks/                  示例任务（框架自测、出题参考）
datasets/                        题集下载位置（内容不入库，见 datasets/README.md）
erp-mock-mcp/                    离线业务服务引擎、示例实例和人工建模业务包
configs/                         Harbor Job及Compose配置
src/lg_bench/                    harness适配、代理、校验与预检
scripts/                         发布检查与批跑脚本
.agents/skills/                  上手、批跑与评分归因 skill
docs/                            使用与评分文档
```

## 许可

自有代码与文档采用[Apache-2.0](LICENSE)。第三方组件保留原有作者、版权及许可声明，范围见[NOTICE](NOTICE)。题集的许可见 Hugging Face 数据集卡片。
