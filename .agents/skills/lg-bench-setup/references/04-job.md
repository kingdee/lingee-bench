# 阶段 4：Job

目标：确定这次要跑的 Harbor job 配置。优先用仓库自带的 job；批跑用脚本生成 `*.local.yaml`，不要手改自带 job。

## 1. 现成 job（不要背列表，现场看）

```bash
ls configs/jobs configs/jobs/*/
```

每个 YAML 开头都有注释说明用途。当前约定：

| 类型 | 文件 | 内容 | 需要题集 |
|---|---|---|---|
| 示例题冒烟 | `configs/jobs/<harness>-glm-5.2-smoke.yaml` | 1 个 harness × `examples/tasks/ap-three-way-match`，0/1 评分 | 否 |
| ERP mock 冒烟 | `configs/jobs/erp-mcp-mock-smoke/<harness>-glm-5.2-erp-mock-smoke.yaml` | 1 个 harness × `examples/tasks/erp-mock-budget-scheme-smoke`，验证 MCP 代理与 erp-mock 链路 | 要 erp-mock 镜像（阶段 1、2） |
| 通用题正式 job | `configs/jobs/opencode-glm-5.3-flash-general-smoke.yaml` | OpenCode + glm-5.3-flash（Agent 与 Judge 同模型）× 1 道通用题；带网络隔离与 Judge 注入 | 是 |
| ERP 正式 job | `configs/jobs/erp-cases/*.yaml` | 每题一个 job，OpenCode + glm-5.3-flash，Judge glm-5.3 | 是 |

`office-smoke-glm-5.2.yaml` 是早期的多 harness 文档题冒烟，没有配置 Judge（`verifier.env`），按通用题评分器的约定会记为评测错误，不要用于出成绩。

## 2. 通用题集批跑：生成配置

用脚本以正式通用 job 为模板生成，任选 harness、Agent 模型和 Judge 模型，网络隔离、Judge 注入、超时都保持正式口径：

```bash
uv run python .agents/skills/lg-bench-setup/scripts/general_job.py \
  --harness-job configs/jobs/<harness>-glm-5.2-smoke.yaml \
  --model <agent-model> --judge-model <judge-model> \
  --output configs/jobs/general-<harness>-<model>.local.yaml \
  [--tasks <任务目录名> ...] [--attempts N] [--concurrency N]
```

- `--harness-job` 只提供 adapter、CLI 版本和 harness 挂载片段，用所选 harness 的示例题冒烟 job 即可。
- 默认跑全部 35 题；`--tasks` 指定子集时，报告里必须注明是子集。
- 脚本同时生成 `configs/compose/model-<名字>.local.yaml`，让代理只放行本次的两个模型；不用再改 `LG_PROXY_ALLOWED_MODELS`。
- 两个输出文件都已被 `.gitignore` 忽略。目标已存在时脚本拒绝覆盖，换一个名字。
- 生成后把输出的 JSON 摘要（harness、版本、模型、题数、预期 trial 数）给用户看。

## 3. ERP 题集批跑：交给 erp-mock-batch-eval

ERP 30 题每题绑定自己的 mock 数据集和工具白名单，必须每题一个 job。配置生成、逐题运行和汇总都按 [`erp-mock-batch-eval`](../../erp-mock-batch-eval/SKILL.md) 执行：

```bash
uv run python .agents/skills/erp-mock-batch-eval/scripts/batch.py prepare \
  --harness-job configs/jobs/<harness>-glm-5.2-smoke.yaml \
  --model <agent-model> --judge-model <judge-model> \
  --output jobs/erp30-<harness>-$(date +%Y%m%d)
```

`prepare` 不调用模型。它校验题集恰好 30 题且摘要未变，生成 `batch.json` 和 30 份配置。

## 4. 只跑部分题或改参数

- 通用题：用 `general_job.py --tasks`，或对生成的 job 在命令行加 `-p datasets/lg-bench-general-v1.0/tasks -i '<glob>'`（`-x` 排除，`-l N` 最多 N 题）。加 `--print-config` 只打印解析结果，不运行。
- 其他改动：复制一份 `*.local.yaml` 再改。常改字段是 `n_attempts`（开发基线建议 3，发布建议 5）和 `n_concurrent_trials`（受本机 CPU/内存限制）。
- 换模型改 `agents[].model_name`。当前锁定的 Harbor 里，`harbor run -m` **不会**覆盖 job 里已写的 `model_name`，必须改 YAML（或用上面的生成脚本）。换模型后结果不能再标成原模型。
- 不要改：`kwargs.version`、`install_mode`、`import_path`、`extra_docker_compose`（镜像片段与锁文件、白名单与题集绑定）、`verifier.env`（Judge 接线），以及 `override_timeout_sec` 这类预算。改了预算就是另一个实验，必须单独标记。

写之前把完整 YAML 给用户看。写完用 `uv run harbor run --config <job> --print-config` 核对 `agents[0]` 的 `import_path`、`model_name`、`kwargs.version`，以及 `datasets`/`tasks` 的范围。

注意：`lg-bench preflight` 默认跳过 `*.local.yaml`，检查时要用 `--config` 显式指定。
