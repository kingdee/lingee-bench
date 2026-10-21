# 阶段 0：环境

目标：仓库能跑 Harbor，不依赖题集的本地检查全部通过。

## 前置（缺一项就停下，告诉用户怎么补）

| 项 | 检查 | 要求 |
|---|---|---|
| uv | `uv --version` | 已安装。依赖（含 Harbor）由 `uv.lock` 锁定，不要 `pip install` |
| Docker | `docker info` | daemon 在运行。每个 trial 都在本机 Docker 里执行 |
| Docker API | `docker version --format '{{.Server.APIVersion}}'` | ≥ 1.48（harness 镜像用 `type: image` 只读挂载） |
| 架构 | `docker version --format '{{.Server.Arch}}'` | arm64 已实测；amd64 锁文件有条目但未实测，要告诉用户这一点 |
| 系统 | — | macOS / Linux。Windows 宿主不支持 |
| 磁盘 | `docker system df` | 建议预留 10 GB 以上（任务镜像、harness 镜像、构建缓存） |

Python 3.12+ 由 `uv sync` 自动准备（见 `.python-version`），不需要系统 Python。

## 步骤

1. 安装依赖：
   ```bash
   uv sync --locked
   ```
   每次 pull 到 `pyproject.toml` / `uv.lock` 的改动后重跑。
2. 本地检查（不用 Docker、不调用模型、不需要题集）：
   ```bash
   uv run lg-bench validate                 # 校验 examples/tasks
   uv run lg-bench smoke                    # 示例题的本地参考解与评分器
   uv run python scripts/check_release.py   # 仓库层检查
   uv run pytest -q
   ```
   期望：`validate` 逐题输出 `"valid": true`；`smoke` 输出 `"local_oracle_reward": 1`；`check_release` 输出 `"status": "passed"`；pytest 全部通过，未下载题集时依赖题集的用例显示为 skipped（这是正常的）。

## 校验范围

- `validate` 检查 Harbor 原生 schema、任务环境、单轮或逐步题面及 verifier 入口，不要求参考解。
- `smoke` 只运行 `examples/tasks/ap-three-way-match`，它是唯一带本地参考解的任务。
- 正式题集的检查在阶段 1 下载后执行。

## 常见故障

- `uv: command not found`：按 https://docs.astral.sh/uv/getting-started/installation/ 安装后重试。
- `Cannot connect to the Docker daemon`：启动 Docker Desktop；Linux 上确认用户在 `docker` 组。
- pull 之后 Harbor 导入或版本报错：环境过期，重跑 `uv sync --locked`。
