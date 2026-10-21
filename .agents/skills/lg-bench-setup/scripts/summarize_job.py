#!/usr/bin/env python3
"""汇总一次 Harbor job 的结果：job 统计、逐 trial 成绩/异常/耗时/token、模型代理请求、
评分状态分桶与正式评分均分，以及可选的密钥泄露检查。

用法（仓库根目录）：
    uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py jobs/<job-name>
    uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py jobs/<job-name> --env-file .env.glm

适用于示例题冒烟和通用题集批跑（一个 job 多道题）。ERP 30 题批次每题一个 job，
用 erp-mock-batch-eval 的 batch.py summary 汇总。

只读 job 目录，不写任何文件。--env-file 只用来取密钥值做全文搜索，输出里只出现变量名和命中文件数，从不输出值。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# 视为密钥、需要做泄露检查的变量名
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|HEADERS|PASSWORD)", re.I)


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def seconds(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "-"
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    return f"{(b - a).total_seconds():.0f}s"


def proxy_summary(trial: Path) -> str:
    log = trial / "agent" / "model-proxy" / "requests.jsonl"
    if not log.is_file():
        return "无代理日志"
    events, models, bad = Counter(), Counter(), Counter()
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        events[e.get("event")] += 1
        if e.get("event") == "request":
            models[e.get("upstream_model")] += 1
            if e.get("status") != 200:
                bad[e.get("status")] += 1
    parts = [f"请求 {events['request']}", f"模型 {dict(models)}"]
    if bad:
        parts.append(f"非200 {dict(bad)}")
    for name in ("rejected", "misconfigured"):
        if events[name]:
            parts.append(f"{name} {events[name]}")
    return "，".join(parts)


def verifier_dir(trial: Path) -> Path:
    """单轮题读 verifier/；多轮题读末步（steps/ 按名称排序的最后一轮）的 verifier/。"""
    steps = sorted(p for p in (trial / "steps").glob("*/verifier") if p.is_dir())
    return steps[-1] if steps and not (trial / "verifier").is_dir() else trial / "verifier"


def grade(trial: Path, r: dict) -> tuple[str, float | None, str]:
    """返回 (评分状态, 主分, 说明)。

    评分状态：graded（正式评分，计入均分）、evaluation_error（评分器/Judge/证据采集失败）、
    execution_error（Harbor 记录的运行异常）、pending（未结束）、no_reward（结束但没有评分）。
    主分：通用题与示例题取 reward，ERP 题取 overall（无则 reward）。
    """
    if r.get("exception_info"):
        return "execution_error", None, (r["exception_info"].get("exception_type") or "")
    if not r.get("finished_at"):
        return "pending", None, ""
    rewards = (r.get("verifier_result") or {}).get("rewards") or {}
    score = rewards.get("overall", rewards.get("reward"))
    score = score if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    vdir = verifier_dir(trial)
    for name in ("score.json", "results.json"):  # 通用题 / ERP 题的评分明细
        path = vdir / name
        if not path.is_file():
            continue
        detail = load(path)
        status = str(detail.get("status", ""))
        # 通用题：dual_channel 为规则+Judge 正式合分；core_threshold_failed 为核心产物缺失，正式记 0 分。
        # ERP 题：graded 为正式评分。其余（evaluation_error、invalid_*、judge_unavailable_*）均不是正式成绩。
        if status in ("dual_channel", "core_threshold_failed", "graded"):
            return ("graded", score, status) if score is not None else ("no_reward", None, status)
        reason = detail.get("error") or detail.get("error_kind") or ""
        note = f"{status or 'unknown'}: {str(reason)[:120]}" if reason else (status or "unknown")
        return "evaluation_error", None, note
    if score is not None:  # 示例题等只写 reward 的评分器
        return "graded", score, ""
    return "no_reward", None, ""


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if SECRET_NAME.search(name) and len(value) >= 8:
            values[name] = value
    return values


def leak_check(job: Path, env_file: Path) -> None:
    secrets = parse_env(env_file)
    if not secrets:
        print(f"\n泄露检查：{env_file} 中没有可检查的密钥变量")
        return
    hits = Counter()
    needles = {name: value.encode() for name, value in secrets.items()}
    for f in job.rglob("*"):
        if not f.is_file():
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        for name, needle in needles.items():
            if needle in data:
                hits[name] += 1
    print("\n泄露检查（只列变量名与命中文件数，期望全部为 0）：")
    for name in sorted(secrets):
        print(f"  {name}: {hits[name]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir", type=Path, help="<jobs_dir>/<job-name>，含 result.json 的那一层")
    ap.add_argument("--env-file", type=Path, help="运行时用的 env 文件，用于密钥泄露检查")
    args = ap.parse_args()

    job: Path = args.job_dir
    if not (job / "result.json").is_file():
        subs = sorted(p.name for p in job.iterdir() if (p / "result.json").is_file()) if job.is_dir() else []
        print(f"{job} 不是 job 目录（缺 result.json）。", file=sys.stderr)
        if subs:
            print("该目录下的 job：" + "、".join(subs), file=sys.stderr)
        return 2

    stats = load(job / "result.json").get("stats", {})
    print(f"job: {job}")
    print("trial 状态：" + "，".join(
        f"{k.removeprefix('n_').removesuffix('_trials')}={stats.get(k, 0)}"
        for k in ("n_completed_trials", "n_errored_trials", "n_running_trials",
                  "n_pending_trials", "n_cancelled_trials")))
    if stats.get("n_running_trials") or stats.get("n_pending_trials"):
        print("注意：仍有 running/pending trial，job 尚未结束或被中断，以下是阶段性结果。")
    for key, ev in (stats.get("evals") or {}).items():
        exc = {k: len(v) for k, v in (ev.get("exception_stats") or {}).items()}
        print(f"- {key}: n_trials={ev.get('n_trials')} errors={ev.get('n_errors')} "
              f"metrics={ev.get('metrics')} exceptions={exc or '无'}")
    print(f"token：input={stats.get('n_input_tokens')} cache={stats.get('n_cache_tokens')} "
          f"output={stats.get('n_output_tokens')} cost_usd={stats.get('cost_usd')}")

    print("\n逐 trial：")
    buckets: Counter = Counter()
    scores: list[float] = []
    problems: list[str] = []
    for trial in sorted(p for p in job.iterdir() if (p / "result.json").is_file()):
        r = load(trial / "result.json")
        info = r.get("agent_info") or {}
        model = (info.get("model_info") or {}).get("name")
        rewards = (r.get("verifier_result") or {}).get("rewards")
        exc = (r.get("exception_info") or {}).get("exception_type")
        ar = r.get("agent_result") or {}
        status, score, note = grade(trial, r)
        buckets[status] += 1
        if status == "graded" and score is not None:
            scores.append(score)
        elif status != "graded":
            problems.append(f"{trial.name}: {status}{f' ({note})' if note else ''}")
        print(f"* {trial.name}")
        print(f"    agent={info.get('name')} {info.get('version')} model={model} "
              f"耗时={seconds(r.get('started_at'), r.get('finished_at'))}")
        print(f"    评分={status}{f' [{note}]' if note else ''} rewards={rewards} exception={exc or '无'} "
              f"tokens(in/cache/out)={ar.get('n_input_tokens')}/{ar.get('n_cache_tokens')}/{ar.get('n_output_tokens')}")
        print(f"    代理：{proxy_summary(trial)}")

    total = sum(buckets.values())
    print("\n评分汇总：")
    print(f"  trial {total}：" + "，".join(f"{k}={buckets[k]}" for k in
          ("graded", "evaluation_error", "execution_error", "no_reward", "pending")))
    if scores:
        print(f"  正式评分均分 = {sum(scores) / len(scores):.4f}（分母 {len(scores)} 个 graded trial；"
              f"满分 {sum(s == 1 for s in scores)}，零分 {sum(s == 0 for s in scores)}）")
    else:
        print("  没有正式评分的 trial，不计算均分。")
    if problems:
        print("  未计入均分（单独报告，不能当作 0 分）：")
        for line in problems:
            print(f"    - {line}")

    if args.env_file:
        leak_check(job, args.env_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
