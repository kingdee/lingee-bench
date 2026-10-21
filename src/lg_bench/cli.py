"""Small development helpers; all actual agent evaluation goes through Harbor."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DOMAINS = {"finance", "supply_chain", "manufacturing"}
SPLITS = {"dev", "public_eval", "heldout_eval"}
# 随仓库提供的示例任务：框架自测与出题参考，不计正式成绩。正式题集见 datasets/README.md。
EXAMPLES = Path("examples/tasks")
SEED_TASK = EXAMPLES / "ap-three-way-match"
SEED_REQUIRED_FILES = ("instruction.md", "task.toml", "environment/Dockerfile",
                       "solution/solve.sh", "tests/test.sh")


def discover(path: Path) -> list[Path]:
    if (path / "task.toml").is_file():
        return [path.resolve()]
    tasks = sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not tasks:
        raise ValueError(f"No tasks found: {path}")
    return [p.resolve() for p in tasks]


def validate(path: Path) -> dict:
    """Validate Harbor's native layout and schema; reference solutions are optional."""
    from harbor.models.task.task import Task

    if not (path / "environment").is_dir():
        raise ValueError(f"{path.name}: missing environment/")
    task = Task(path)  # Includes config-aware verifier checks for every declared step.
    if task.has_steps:
        for step in task.config.steps:
            if not task.step_instruction(step.name).strip():
                raise ValueError(f"{path.name}/{step.name}: empty instruction")
    elif not task.instruction.strip():
        raise ValueError(f"{path.name}: empty instruction")
    return {"task": path.name, "domain": task.config.metadata.get("domain"), "valid": True}


def validate_seed(path: Path) -> dict:
    """Check the stricter project contract used by the local oracle smoke."""
    from harbor.models.task.task import Task

    result = validate(path)
    for name in SEED_REQUIRED_FILES:
        if not (path / name).is_file():
            raise ValueError(f"{path.name}: missing {name}")
    metadata = Task(path).config.metadata
    for key in ("task_id", "task_version", "domain", "role", "difficulty", "interaction",
                "split", "scenario_family", "data_source"):
        if not isinstance(metadata.get(key), str) or not metadata[key]:
            raise ValueError(f"{path.name}: missing metadata.{key}")
    if metadata["task_id"] != path.name:
        raise ValueError(f"{path.name}: task_id must equal directory name")
    if metadata["domain"] not in DOMAINS:
        raise ValueError(f"{path.name}: unsupported domain")
    if metadata["split"] not in SPLITS:
        raise ValueError(f"{path.name}: unsupported split")
    if metadata["interaction"] not in {"files", "api", "ui"}:
        raise ValueError(f"{path.name}: unsupported interaction")
    return result


def smoke(path: Path) -> dict:
    validate_seed(path)
    with tempfile.TemporaryDirectory(prefix="lg-bench-") as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        shutil.copytree(path / "environment/data", workspace / "data")
        (workspace / "output").mkdir()
        logs = Path(tmp) / "logs"
        env = dict(os.environ, BENCH_WORKSPACE=str(workspace), BENCH_LOG_DIR=str(logs),
                   PYTHONDONTWRITEBYTECODE="1",
                   PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
        for script in ("solution/solve.sh", "tests/test.sh"):
            subprocess.run(["bash", str(path / script)], env=env, cwd=workspace,
                           check=True, timeout=60, capture_output=True, text=True)
        reward = (logs / "reward.txt").read_text().strip()
        if reward != "1":
            raise ValueError(f"{path.name}: oracle failed: {(logs / 'details.json').read_text()}")
        return {"task": path.name, "local_oracle_reward": 1, "docker_tested": False}


def main() -> int:
    if sys.argv[1:2] == ["harness"]:
        # harness 只读挂载镜像的构建与检查，见 lg_bench/harness_images.py。
        from lg_bench.harness_images import main as harness_main
        return harness_main(sys.argv[2:])
    if sys.argv[1:2] == ["preflight"]:
        # harness 静态预检有独立参数，见 lg_bench/preflight.py。
        from lg_bench.preflight import main as preflight_main
        return preflight_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "smoke", "preflight"])
    parser.add_argument("--path", type=Path,
                        help="task or dataset; defaults to examples/tasks for validate, the seed for smoke")
    args = parser.parse_args()
    path = args.path or (SEED_TASK if args.command == "smoke" else EXAMPLES)
    try:
        for task_path in discover(path):
            print(json.dumps((validate if args.command == "validate" else smoke)(task_path)))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
