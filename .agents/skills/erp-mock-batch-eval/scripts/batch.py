"""Prepare configurations and summarize Harbor results for the fixed mock ERP batch."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
import tomllib
from collections import Counter
from copy import deepcopy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
DATASET = "datasets/lg-bench-enterprise-v1.0"
MANIFEST = f"{DATASET}/dataset.toml"
TASKS = "datasets/lg-bench-enterprise-v1.0/tasks"
FORMAL_JOBS = "configs/jobs/erp-cases"
HARNESS_PREFIX = "configs/compose/harness/"
# 任务目录下允许存在、但不属于Harbor任务包的辅助文件：人工单题运行脚本与续聊素材镜像。
AUXILIARY = {"run.sh", "rounds", "inputs"}


def load(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def task_templates():
    from harbor.publisher.packager import Packager

    declared = {}
    for entry in tomllib.loads((ROOT / MANIFEST).read_text())["tasks"]:
        require(entry["name"] not in declared, f"duplicate manifest entry: {entry['name']}")
        declared[entry["name"]] = entry["digest"]
    packages = {}
    for path in sorted(p for p in (ROOT / TASKS).iterdir() if (p / "task.toml").is_file()):
        package = tomllib.loads((path / "task.toml").read_text())["task"]["name"]
        require(package not in packages, f"duplicate task name: {package}")
        packages[package] = path
    names = {path.name for path in packages.values()}
    require(len(declared) == len(packages) == 30 and set(declared) == set(packages),
            "dataset.toml and task directories must declare exactly the same 30 tasks")
    for package, path in packages.items():
        digest, files = Packager.compute_content_hash(path)
        require(declared[package] == f"sha256:{digest}", f"dataset hash changed: {path.name}")
        resolved = path.resolve()
        present = {p.relative_to(resolved).as_posix() for p in resolved.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts}
        unversioned = sorted(name for name in present - {p.resolve().relative_to(resolved).as_posix() for p in files}
                             if name.split("/")[0] not in AUXILIARY)
        require(not unversioned,
                f"unversioned task file outside the dataset manifest: {path.name}: {', '.join(unversioned)}")
    templates = {}
    for path in sorted((ROOT / FORMAL_JOBS).glob("*.yaml")):
        if path.name.endswith(".local.yaml"):
            continue
        cfg = yaml.safe_load(path.read_text())
        require(len(cfg.get("tasks", [])) == 1, f"expected a per-task job: {path}")
        name = Path(cfg["tasks"][0]["path"]).name
        require(name in names and name not in templates, f"unexpected/duplicate task: {name}")
        require(Path(cfg["tasks"][0]["path"]).resolve() == ROOT / TASKS / name,
                f"formal job must bind its canonical task path: {name}")
        require(len(cfg.get("agents", [])) == 1, f"expected one agent: {path}")
        templates[name] = cfg
    require(set(templates) == names, "formal job coverage differs from the 30-task list")
    return templates


def prepare(args):
    from harbor.models.job.config import JobConfig

    templates = task_templates()
    selected = args.tasks or sorted(templates)
    require(len(selected) == len(set(selected)) and set(selected) <= templates.keys(),
            "--tasks must be distinct current task directory names")
    output = args.output.resolve()
    require(output.is_relative_to((ROOT / "jobs").resolve()) and not output.exists(),
            "output must be a new directory below jobs/")
    profile = yaml.safe_load(args.harness_job.read_text())
    require(len(profile.get("agents", [])) == 1, "harness template must have one agent")
    target = profile["agents"][0]
    module, name = target["import_path"].split(":")
    cls = getattr(importlib.import_module(module), name)
    supports_resume = bool(getattr(cls, "SUPPORTS_RESUME", False))
    runtime = output / "runtime.yaml"
    runtime_text = yaml.safe_dump({"services": {
        "lg-mcp-proxy": {"environment": {"LG_MCP_TARGET": "mock"}},
        "main": {"environment": {"LG_EVAL_JUDGE_MODEL": args.judge_model}},
    }}, allow_unicode=True, sort_keys=False)
    configs, records = {}, []
    for task in selected:
        cfg = deepcopy(templates[task])
        case_agent = cfg["agents"][0]
        agent = deepcopy(target)
        agent.update(model_name=args.model,
                     mcp_servers=deepcopy(case_agent["mcp_servers"]),
                     resume_trajectory=case_agent.get("resume_trajectory", False))
        # 运行超时由各题 task.toml [agent] timeout_sec 唯一决定：生成配置不得携带
        # override_timeout_sec（连同 harness 模板 deepcopy 带入的一并剥离），否则
        # 长题会被 job 级覆盖在 task 级超时语义之外击杀（dsh 轨迹丢失事故根因）。
        agent.pop("override_timeout_sec", None)
        if "override_setup_timeout_sec" in case_agent:
            agent["override_setup_timeout_sec"] = case_agent["override_setup_timeout_sec"]
        cfg["agents"] = [agent]
        common = [p for p in cfg["environment"]["extra_docker_compose"]
                  if not p.startswith(HARNESS_PREFIX)]
        extra = [p for p in profile.get("environment", {}).get("extra_docker_compose", [])
                 if not p.startswith(("configs/compose/mcp-allow-", "configs/compose/erp-mock"))]
        fragments = list(dict.fromkeys([*common, *extra]))
        fragments.append(str(runtime))
        cfg["environment"]["extra_docker_compose"] = fragments
        cfg.update(jobs_dir=str(output / "runs"), n_attempts=args.attempts,
                   n_concurrent_trials=1, retry={"max_retries": 0})
        JobConfig.model_validate(cfg)
        task_cfg = tomllib.loads((ROOT / TASKS / task / "task.toml").read_text())
        steps = [s["name"] for s in task_cfg.get("steps", [])]
        require(not steps or task_cfg.get("multi_step_reward_strategy") == "final",
                f"unsupported reward strategy for {task}; adapt summary before running")
        config_path = output / "configs" / f"{task}.yaml"
        configs[config_path] = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
        records.append({"task": task, "version": task_cfg["task"]["version"], "steps": steps,
                        "resume": agent["resume_trajectory"], "config": str(config_path),
                        "job_dir": str(output / "runs" / task),
                        "task_hashes": {str(p.relative_to(ROOT / TASKS / task)): sha(p)
                                        for p in sorted((ROOT / TASKS / task).rglob("*"))
                                        if p.is_file() and "__pycache__" not in p.parts}})
    (output / "configs").mkdir(parents=True)
    runtime.write_text(runtime_text)
    for path, text in configs.items():
        path.write_text(text)
    plan = {"root": str(ROOT), "harness_job": str(args.harness_job.resolve()),
            "adapter": target["import_path"], "agent_model": args.model,
            "harness_version": target.get("kwargs", {}).get("version"),
            "judge_model": args.judge_model, "supports_resume": supports_resume,
            "resume_tasks": [r["task"] for r in records if r["resume"]],
            "total_tasks": 30, "selected_tasks": len(records), "attempts": args.attempts,
            "expected_trials": len(records) * args.attempts, "tasks": records,
            "config_hashes": {str(path): sha(path) for path in [runtime, *configs]}}
    write(output / "batch.json", plan)
    print(json.dumps({k: v for k, v in plan.items() if k not in ("tasks", "config_hashes")},
                     ensure_ascii=False, indent=2))


def trial_row(trial, record):
    result = load(trial / "result.json")
    verifier = (trial / "steps" / record["steps"][-1] / "verifier"
                if record["steps"] else trial / "verifier")
    outcome_path = verifier / "results.json"
    outcome = load(outcome_path) if outcome_path.is_file() else {}
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    if not rewards and (verifier / "reward.json").is_file():
        rewards = load(verifier / "reward.json")
    exception = result.get("exception_info") or {}
    if exception:
        status = "execution_error"
    elif not result.get("finished_at"):
        status = "pending"
    elif outcome.get("status") == "evaluation_error":
        status = "evaluation_error"
    elif outcome.get("status") == "graded" and rewards:
        status = "graded"
    else:
        status = "no_reward"
    proxy_logs = sorted(str(p) for p in trial.glob("steps/*/agent/model-proxy/requests.jsonl"))
    proxy_logs += [str(p) for p in trial.glob("agent/model-proxy/requests.jsonl")]
    return {"task": record["task"], "task_version": record.get("version"),
            "trial": str(trial), "status": status,
            "content": rewards.get("content_score"), "numeric": rewards.get("numeric_accuracy"),
            "overall": rewards.get("overall", rewards.get("reward")), "rewards": rewards,
            "error": exception.get("exception_type") or outcome.get("error"),
            "evidence": str(outcome_path), "proxy_logs": proxy_logs}


def summary(args):
    plan = load(args.batch)
    rows = []
    for record in plan["tasks"]:
        job = Path(record["job_dir"])
        trials = sorted(p.parent for p in job.glob("*/result.json"))
        rows.extend(trial_row(p, record) for p in trials)
        missing = max(0, plan["attempts"] - len(trials))
        job_result = load(job / "result.json") if (job / "result.json").is_file() else {}
        for _ in range(missing):
            rows.append({"task": record["task"], "trial": None,
                         "status": "no_reward" if job_result.get("finished_at") else "pending",
                         "overall": None, "error": None})
    graded = [r for r in rows if r["status"] == "graded"]
    scored = [r["overall"] for r in graded if isinstance(r["overall"], (int, float))]
    report = {"agent_model": plan["agent_model"], "judge_model": plan["judge_model"],
              "harness_version": plan.get("harness_version"),
              "adapter": plan["adapter"], "selected_tasks": plan["selected_tasks"],
              "total_tasks": plan["total_tasks"], "attempts": plan["attempts"],
              "expected_trials": plan["expected_trials"], "observed_trials": sum(bool(r["trial"]) for r in rows),
              "completed_trials": sum(bool(r["trial"]) and r["status"] != "pending" for r in rows),
              "statuses": dict(Counter(r["status"] for r in rows)),
              "double_full": sum(r.get("content") == r.get("numeric") == 1 for r in graded),
              "graded_overall_count": len(scored), "graded_missing_overall": len(graded) - len(scored),
              "graded_mean": sum(scored) / len(scored) if scored else None, "trials": rows}
    output = args.batch.resolve().parent / "summary.json"
    write(output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "trials"}, ensure_ascii=False, indent=2))
    print(f"report: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="prepare a new batch; no model calls")
    p.add_argument("--harness-job", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--judge-model", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--tasks", nargs="+")
    p.add_argument("--attempts", type=positive, default=1)
    p.set_defaults(func=prepare)
    p = sub.add_parser("summary", help="summarize single- and multi-step trials; no model calls")
    p.add_argument("--batch", type=Path, required=True)
    p.set_defaults(func=summary)
    args = parser.parse_args()
    require(Path.cwd().resolve() == ROOT, "run from the repository root")
    return args.func(args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
