#!/usr/bin/env python3
"""生成通用题集（lg-bench-general-v1.0）的批跑 job：任选 harness、Agent 模型和 Judge 模型。

以正式通用 job（configs/jobs/opencode-glm-5.3-flash-general-smoke.yaml）为模板，保留它的网络隔离、
Judge 注入、超时和产物收集，只替换：
- agent：取自所选 harness 的已接入 job（例如 configs/jobs/pi-glm-5.2-smoke.yaml）的 import_path 与 kwargs；
- harness 只读挂载片段：取自同一个 job；
- 模型：Agent 用 --model，Judge 用 --judge-model；另生成一个 compose 片段，让模型代理只放行这两个模型；
- 题目范围：默认全部 35 题，--tasks 可指定子集（任务目录名）。

用法（仓库根目录，不调用模型、不启动容器）：
    uv run python .agents/skills/lg-bench-setup/scripts/general_job.py \\
      --harness-job configs/jobs/pi-glm-5.2-smoke.yaml \\
      --model glm-5.3-flash --judge-model glm-5.3-flash \\
      --output configs/jobs/general-pi-glm-5.3-flash.local.yaml

输出的两个文件都以 .local.yaml 结尾，已被 .gitignore 忽略。目标文件已存在时拒绝覆盖。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = Path("configs/jobs/opencode-glm-5.3-flash-general-smoke.yaml")
TASKS = Path("datasets/lg-bench-general-v1.0/tasks")
HARNESS_PREFIX = "configs/compose/harness/"
# 模板里固定 glm-5.3-flash 的片段；生成的 job 用自己的模型白名单片段替换它。
TEMPLATE_PIN = "configs/compose/model-glm-5.3-flash.yaml"


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harness-job", type=Path, required=True, help="所选 harness 的已接入 job，提供 agent 与挂载片段")
    ap.add_argument("--model", required=True, help="Agent 模型名")
    ap.add_argument("--judge-model", required=True, help="Judge 模型名（经模型代理的 /openai 路由调用）")
    ap.add_argument("--output", type=Path, required=True, help="configs/jobs/<名字>.local.yaml")
    ap.add_argument("--tasks", nargs="+", help="只跑这些任务目录名；默认全部")
    ap.add_argument("--attempts", type=positive, default=1, help="每题尝试次数（默认 1）")
    ap.add_argument("--concurrency", type=positive, default=1, help="并发 trial 数（默认 1）")
    args = ap.parse_args()

    if Path.cwd().resolve() != ROOT:
        fail("run from the repository root")
    output = args.output
    if output.parent != Path("configs/jobs") or not output.name.endswith(".local.yaml"):
        fail("--output must be configs/jobs/<name>.local.yaml (ignored by git)")
    pin = Path("configs/compose") / f"model-{output.name.removesuffix('.local.yaml')}.local.yaml"
    for path in (output, pin):
        if path.exists():
            fail(f"{path} already exists; choose another --output")
    if not (TASKS.is_dir() and any(TASKS.glob("*/task.toml"))):
        fail(f"{TASKS} not found; download the datasets first (datasets/README.md)")
    available = sorted(p.parent.name for p in TASKS.glob("*/task.toml"))
    if args.tasks:
        unknown = sorted(set(args.tasks) - set(available))
        if unknown:
            fail(f"unknown tasks: {', '.join(unknown)}")
    for name in (args.model, args.judge_model):
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+", name):
            fail(f"invalid model name: {name!r}")

    template = yaml.safe_load(TEMPLATE.read_text())
    profile = yaml.safe_load(args.harness_job.read_text())
    if len(profile.get("agents", [])) != 1:
        fail("--harness-job must declare exactly one agent")
    harness = [c for c in profile.get("environment", {}).get("extra_docker_compose", [])
               if c.startswith(HARNESS_PREFIX)]
    if len(harness) != 1:
        fail("--harness-job must mount exactly one configs/compose/harness/*.yaml fragment")

    base_agent = template["agents"][0]
    agent = deepcopy(profile["agents"][0])
    agent["model_name"] = args.model
    for key in ("override_timeout_sec", "override_setup_timeout_sec"):
        agent[key] = base_agent[key]  # 与正式通用 job 的预算一致

    job = deepcopy(template)
    compose = [c for c in template["environment"]["extra_docker_compose"]
               if c != TEMPLATE_PIN and not c.startswith(HARNESS_PREFIX)]
    job["environment"]["extra_docker_compose"] = [*compose, str(pin), harness[0]]
    job["agents"] = [agent]
    job["verifier"]["env"]["JUDGE_MODEL"] = args.judge_model
    job.pop("tasks", None)
    dataset = {"path": str(TASKS)}
    if args.tasks:
        dataset["task_names"] = sorted(args.tasks)
    job["datasets"] = [dataset]
    job.update(n_attempts=args.attempts, n_concurrent_trials=args.concurrency,
               retry={"max_retries": 0})

    from harbor.models.job.config import JobConfig
    JobConfig.model_validate(job)

    allowed = ",".join(dict.fromkeys([args.model, args.judge_model]))
    pin.write_text(
        "# 由 general_job.py 生成：模型代理只放行本批次的 Agent 与 Judge 模型。\n"
        + yaml.safe_dump({"services": {"lg-model-proxy": {"environment": {"LG_PROXY_ALLOWED_MODELS": allowed}}}},
                         sort_keys=False))
    header = (f"# 由 .agents/skills/lg-bench-setup/scripts/general_job.py 生成，模板 {TEMPLATE}。\n"
              f"# harness 来自 {args.harness_job}；Agent={args.model}，Judge={args.judge_model}。\n"
              f"# 运行：uv run harbor run --config {output} --env-file .env.glm --job-name <名字>\n")
    output.write_text(header + yaml.safe_dump(job, allow_unicode=True, sort_keys=False))
    n_tasks = len(args.tasks or available)
    print(json.dumps({"job": str(output), "model_pin": str(pin), "harness": agent["import_path"],
                      "harness_version": agent.get("kwargs", {}).get("version"),
                      "agent_model": args.model, "judge_model": args.judge_model,
                      "tasks": n_tasks, "attempts": args.attempts, "concurrency": args.concurrency,
                      "expected_trials": n_tasks * args.attempts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
