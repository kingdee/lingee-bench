"""四个必选 harness 的静态预检：不启动 Docker、不调用模型。

检查项：
1. job YAML 能被 Harbor ``JobConfig`` 解析；
2. 每个 agent 能被 Harbor ``AgentFactory`` 实例化（内置名或 ``import_path`` 可导入、kwargs 合法）；
3. 任务/数据集路径存在（默认全量检查时，``datasets/`` 下未下载的题集只给 warning）；
4. 所需环境变量是否存在（只报告 set/missing，从不输出值）；
5. 默认配置集合覆盖全部四个必选 harness；
6. ``--dry-run`` 时输出自建 adapter 将执行的安装、配置文件与运行命令（密钥脱敏）；
7. 默认检查本机 harness 镜像；CI 可用 --skip-local-images 保留配置/compose 检查而跳过成品镜像。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from lg_bench.agents._image import HarnessImageMixin

REQUIRED_HARNESSES = {
    "claude-code": "Claude Code",
    "pi": "Pi",
    "deepseek-harness": "DeepSeek Harness",
    "opencode": "OpenCode",
}
# 与 docs/harness-support.md 的状态标签保持一致。
STATUS = {
    "claude-code": "已跑通（单题冒烟 reward=1）",
    "pi": "已跑通（单题冒烟 reward=1）",
    "deepseek-harness": "已跑通（单题冒烟 reward=1）",
    "opencode": "已跑通（单题冒烟 reward=1）",
}
PLACEHOLDER_PREFIX = "REPLACE_WITH"
MODEL_PROXY_COMPOSE = "configs/compose/model-proxy.yaml"
from lg_bench.agents._mcp import MCP_PROXY_COMPOSE, uses_mcp_proxy  # noqa: E402
DEFAULT_CONFIG_DIR = Path("configs/jobs")
# 正式题集不随仓库分发，按 datasets/README.md 从 Hugging Face 下载到这里。
DATASETS_DIR = Path("datasets")
DATASETS_HINT = "题集未下载，见 datasets/README.md"


def default_configs() -> list[Path]:
    # 含子目录（erp-cases/、erp-mcp-mock-smoke/ 等），CI 至少校验它们的格式、adapter 与路径；
    # 不带 --require-env 时缺凭据只报告不失败。
    return sorted(p for p in DEFAULT_CONFIG_DIR.rglob("*.yaml")
                  if not p.name.endswith(".local.yaml"))


def _env_groups(harness_id: str, agent, model_name: str | None) -> list[tuple[str, ...]]:
    """每组内任一变量存在即满足。"""
    if hasattr(agent, "required_env"):
        return [(key,) for key in agent.required_env()]
    if harness_id == "claude-code":
        groups = [("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")]
        if model_name and not model_name.startswith(("claude", PLACEHOLDER_PREFIX)):
            # 非 Anthropic 官方模型必须经 Anthropic Messages 兼容网关。
            groups.append(("ANTHROPIC_BASE_URL",))
        return groups
    return []


def _paths(job) -> list[Path]:
    paths = [Path(t.path) for t in job.tasks if getattr(t, "path", None)]
    paths += [Path(d.path) for d in job.datasets if getattr(d, "path", None)]
    return paths


def _task_mcp_urls(job) -> list[str | None]:
    """任务 task.toml 里 ``[[environment.mcp_servers]]`` 的 url。"""
    return [s.url for s in _task_mcp_servers(job)]


def _task_mcp_servers(job) -> list:
    """任务 task.toml 里 ``[[environment.mcp_servers]]``（数据集目录逐个展开）。"""
    from harbor.models.task.task import Task

    task_dirs: list[Path] = []
    for p in _paths(job):
        if (p / "task.toml").is_file():
            task_dirs.append(p)
        elif p.is_dir():
            task_dirs += [d for d in sorted(p.iterdir()) if (d / "task.toml").is_file()]
    servers: list = []
    for d in task_dirs:
        try:
            servers += list(Task(d).config.environment.mcp_servers)
        except Exception:  # 任务本身不合法时由 lg-bench validate 报告
            continue
    return servers


def _is_dataset_path(path: Path) -> bool:
    return path.parts[:1] == DATASETS_DIR.parts


def check_config(path: Path, *, dry_run: bool, logs_dir: Path,
                 check_local_images: bool = True, strict_paths: bool = True) -> list[dict]:
    """``strict_paths=False`` 时，``datasets/`` 下缺失的题集路径降为 warning（默认全量检查用）。"""
    import yaml
    from harbor.agents.factory import AgentFactory
    from harbor.models.job.config import JobConfig

    job = JobConfig.model_validate(yaml.safe_load(path.read_text()))
    missing = [p for p in _paths(job) if not p.exists()]
    pending = [] if strict_paths else [p for p in missing if _is_dataset_path(p)]
    missing_paths = [str(p) for p in missing if p not in pending]
    rows = []
    for index, agent_cfg in enumerate(job.agents):
        row: dict = {"config": str(path), "agent_index": index,
                     "name": agent_cfg.name, "import_path": agent_cfg.import_path,
                     "model_name": agent_cfg.model_name, "errors": [], "warnings": []}
        if missing_paths:
            row["errors"].append(f"missing task/dataset path: {', '.join(missing_paths)}")
        if pending:
            row["warnings"].append(f"{DATASETS_HINT}: {', '.join(map(str, pending))}")
        try:
            # 与 Harbor trial 一致：任务级 + agent 级 mcp_servers 合并（同名后者覆盖），
            # 这样 dry-run 渲染的就是运行时实际写入的 MCP 配置。
            servers = {s.name: s for s in [*_task_mcp_servers(job), *agent_cfg.mcp_servers]}
            extra = {"mcp_servers": list(servers.values())} if servers else {}
            agent = AgentFactory.create_agent_from_config(
                agent_cfg, logs_dir=logs_dir / f"{path.stem}-{index}", **extra)
        except Exception as exc:  # 导入失败、kwargs 不合法等
            row["errors"].append(f"adapter: {type(exc).__name__}: {exc}")
            rows.append(row)
            continue
        row["harbor_version"] = version("harbor")
        row["resume_trajectory"] = agent_cfg.resume_trajectory
        harness_id = getattr(agent, "HARNESS_ID", None) or agent.name()
        sidecars = [str(p) for p in job.environment.extra_docker_compose]
        if hasattr(agent, "required_env"):
            # 项目 adapter 只连代理 sidecar；缺少 sidecar 时 agent 连不上模型。
            required = [MODEL_PROXY_COMPOSE]
        else:
            required = []
        # MCP 指向代理 sidecar 时，job 必须挂上 mcp-proxy.yaml，否则 harness 连不上。
        mcp_urls = [s.url for s in agent_cfg.mcp_servers] + _task_mcp_urls(job)
        if uses_mcp_proxy(mcp_urls):
            required = [*required, MCP_PROXY_COMPOSE]
        for compose in required:
            if compose not in sidecars:
                row["errors"].append(f"environment.extra_docker_compose must include {compose}")
        if hasattr(agent, "preflight_checks"):
            # 传 job 的 compose 列表，供 adapter 检查自己需要的片段。
            if isinstance(agent, HarnessImageMixin):
                errors, warnings = agent.preflight_checks(
                    sidecars, check_local_image=check_local_images)
            else:
                errors, warnings = agent.preflight_checks(sidecars)
            row["errors"] += errors
            row["warnings"] += warnings
        row.update(harness=harness_id, adapter=f"{type(agent).__module__}:{type(agent).__name__}",
                   version=agent.version(), status=STATUS.get(harness_id, "非必选对象"))
        model = agent_cfg.model_name or ""
        if model.startswith(PLACEHOLDER_PREFIX):
            row["warnings"].append("model_name is a template placeholder; copy to *.local.yaml")
        env_report, missing = {}, []
        for group in _env_groups(harness_id, agent, model):
            present = [k for k in group if os.environ.get(k)]
            for key in group:
                env_report[key] = "set" if os.environ.get(key) else "missing"
            if not present:
                missing.append(" or ".join(group))
        row["env"] = env_report
        row["missing_env"] = missing
        if dry_run:
            if hasattr(agent, "dry_run_plan"):
                row["dry_run"] = agent.dry_run_plan()
            else:
                row["dry_run"] = "Harbor 内置 adapter：命令由 Harbor 生成，可用 harbor run --print-config 查看解析结果"
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lg-bench preflight", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, action="append",
                        help="job YAML；可重复。默认检查 configs/jobs/ 下所有 *.yaml（含子目录，不含 *.local.yaml）")
    parser.add_argument("--env-file", type=Path, help="与 harbor run --env-file 相同的 dotenv 文件")
    parser.add_argument("--require-env", action="store_true", help="缺少环境变量时返回非零")
    parser.add_argument("--dry-run", action="store_true", help="输出安装/配置/运行命令（密钥脱敏）")
    parser.add_argument("--skip-local-images", action="store_true",
                        help="CI 只校验配置与 compose 锁文件，不要求本机已有 harness 镜像")
    args = parser.parse_args(argv)

    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file():
            print(f"ERROR: env file not found: {args.env_file}", file=sys.stderr)
            return 1
        load_dotenv(args.env_file, override=True)  # 与 harbor run 行为一致

    configs = args.config or default_configs()
    failed = False
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="lg-preflight-") as tmp:
        for path in configs:
            try:
                # 显式 --config 时缺题集是错误；默认全量检查时只提醒下载。
                rows = check_config(path, dry_run=args.dry_run, logs_dir=Path(tmp),
                                    check_local_images=not args.skip_local_images,
                                    strict_paths=bool(args.config))
            except Exception as exc:
                print(json.dumps({"config": str(path), "errors": [f"{type(exc).__name__}: {exc}"]},
                                 ensure_ascii=False))
                failed = True
                continue
            for row in rows:
                seen.add(row.get("harness", ""))
                if row["errors"] or (args.require_env and row.get("missing_env")):
                    failed = True
                print(json.dumps(row, ensure_ascii=False))

    summary: dict = {"summary": True, "configs": len(configs),
                     "harnesses": {h: STATUS[h] for h in REQUIRED_HARNESSES if h in seen}}
    if not args.config:
        absent = [REQUIRED_HARNESSES[h] for h in REQUIRED_HARNESSES if h not in seen]
        summary["missing_required_harnesses"] = absent
        failed = failed or bool(absent)
    summary["ok"] = not failed
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if failed else 0
