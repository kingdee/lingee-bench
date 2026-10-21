# 题集

正式题集和 ERP 评测环境发布在 Hugging Face：[KD-Lingee/lingee-bench](https://huggingface.co/datasets/KD-Lingee/lingee-bench)，不随本仓库分发。本目录只有这份说明，下载后的内容已被 `.gitignore` 忽略。

## 下载

在仓库根目录执行（需要 [huggingface_hub](https://huggingface.co/docs/huggingface_hub/guides/cli) 提供的 `hf` 命令）：

```bash
uvx --from huggingface_hub hf download KD-Lingee/lingee-bench \
  --repo-type dataset --revision <revision> --local-dir datasets \
  --include "lg-bench-*/**" --include "environments/**" --include "registry.json"
```

- 正式评测请把 `<revision>` 换成固定的提交号或 tag，并随成绩一起记录，保证可复现。
- `--include` 只拉题集与评测环境，避免 Hugging Face 仓库根目录的 README、LICENSE 覆盖本目录的说明；题集许可见 [数据集卡片](https://huggingface.co/datasets/KD-Lingee/lingee-bench)。
- 题集如果启用了 gated 访问，先执行 `uvx --from huggingface_hub hf auth login`。

下载后的目录结构：

| 路径 | 内容 | 使用方 |
| --- | --- | --- |
| `lg-bench-enterprise-v1.0/` | 企业管理 30 题，含 `dataset.toml` | `configs/jobs/erp-cases/` |
| `lg-bench-general-v1.0/` | 通用任务 35 题，含 `dataset.toml` | `configs/jobs/*-general-smoke.yaml` 等 |
| `environments/erp-mock-mcp/` | 离线 ERP 评测实例 `erp-benchmark` 及其业务包；`task-bindings.json` 记录每题的数据集、MCP 路径和工具白名单 | `docker/erp-mock-mcp/Dockerfile` 构建时打入；`check_release.py` 核对绑定 |
| `registry.json` | 题集登记 | — |

企业管理与通用任务分别报告成绩。

## 下载后检查

```bash
uv run python scripts/check_release.py --datasets   # 题量、dataset.toml 摘要、输入文件摘要、ERP 逐题绑定
uv run lg-bench validate --path datasets/lg-bench-enterprise-v1.0/tasks
uv run lg-bench validate --path datasets/lg-bench-general-v1.0/tasks
uv run pytest -q                                    # 依赖题集的用例不再跳过
```

ERP 任务还需要用下载的评测环境重新构建 mock 镜像：

```bash
docker build -f docker/erp-mock-mcp/Dockerfile -t lg-bench/erp-mock-mcp:0.1.0 .
```

## 不在这里的内容

[`examples/tasks/`](../examples/tasks) 下的示例任务随仓库提供，用于框架自测（`lg-bench smoke`、CI）和出题参考，不计正式成绩。题集划分规则见[题集划分](../docs/dataset-splits.md)。
