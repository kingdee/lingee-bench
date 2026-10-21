# 阶段 1：下载题集

目标：正式题集和 ERP 评测环境已下载到 `datasets/`，并通过完整性校验。只跑示例题冒烟时跳过本阶段。

题集发布在 Hugging Face：`KD-Lingee/lingee-bench`。权威说明是 `datasets/README.md`，先读它。

## 1. 下载

先向用户确认要用的 revision（提交号或 tag）。正式评测必须固定 revision，并随成绩记录；用户只是试用时可以用 `main`，但要告诉他这样的结果不可复现。

```bash
uvx --from huggingface_hub hf download KD-Lingee/lingee-bench \
  --repo-type dataset --revision <revision> --local-dir datasets \
  --include "lg-bench-*/**" --include "environments/**" --include "registry.json"
```

- `--include` 不能省：省了会用 Hugging Face 根目录的 README 覆盖仓库里的 `datasets/README.md`。如果已经被覆盖，用 `git checkout datasets/README.md` 恢复。
- 题集启用 gated 访问时，先请用户在自己的终端执行 `uvx --from huggingface_hub hf auth login`。不要让用户把 HF token 贴进对话。
- 下载后应有：`datasets/lg-bench-enterprise-v1.0/`、`datasets/lg-bench-general-v1.0/`、`datasets/environments/erp-mock-mcp/`、`datasets/registry.json`。缺任何一个都先停下：多半是 revision 写错或下载被中断。

## 2. 校验

```bash
uv run python scripts/check_release.py --datasets
uv run lg-bench validate --path datasets/lg-bench-enterprise-v1.0/tasks
uv run lg-bench validate --path datasets/lg-bench-general-v1.0/tasks
uv run pytest -q
```

期望：

- `check_release` 的 `layers` 含 `datasets`，`status` 为 `passed`。它核对题量（30/35）、每题 Harbor 包摘要、输入文件摘要，以及 ERP 每题的数据集、MCP 路径和工具白名单与题集的 `task-bindings.json` 一致。
- 两组 `validate` 逐题 `"valid": true`。
- pytest 不再有因题集缺失而跳过的用例。

校验失败时不要修改题目或配置去"凑通过"：

- `package digest mismatch`、`declared input digest mismatch`：下载不完整或文件被改过，删掉对应目录重新下载。
- `tools differ from ... task-bindings.json`、`not bound by any task`：本仓库的 ERP job/白名单片段与这个题集版本不匹配。确认仓库版本与题集 revision 是配套发布的。

## 3. 题集更新后

换 revision 后重跑本阶段的校验，并在阶段 2 **重新构建 erp-mock 镜像**（镜像构建时打入评测实例，不会自动更新）。新旧 revision 的成绩分开报告。
