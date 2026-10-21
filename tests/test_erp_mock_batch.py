"""Batch contracts: per-case bindings, resume capability, and final-step grading."""
import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from conftest import requires_enterprise

pytestmark = requires_enterprise

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".agents/skills/erp-mock-batch-eval/scripts/batch.py"
spec = importlib.util.spec_from_file_location("erp_mock_batch", SCRIPT)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


@pytest.fixture
def release_workspace(tmp_path, monkeypatch):
    shutil.copytree(ROOT / batch.DATASET, tmp_path / batch.DATASET)
    shutil.copytree(ROOT / batch.FORMAL_JOBS, tmp_path / batch.FORMAL_JOBS)
    monkeypatch.setattr(batch, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path / batch.DATASET


def test_changed_task_is_rejected_before_configuration_generation(release_workspace):
    instruction = next((release_workspace / "tasks").glob("*/instruction.md"))
    instruction.write_text(instruction.read_text() + "\nchanged\n")
    with pytest.raises(ValueError, match="dataset hash changed"):
        batch.task_templates()


@pytest.mark.parametrize("change,message", [("added", "unversioned task file"),
                                            ("removed", "dataset hash changed")])
def test_task_files_outside_the_manifest_are_rejected(release_workspace, change, message):
    if change == "added":
        task = next(p for p in (release_workspace / "tasks").iterdir() if (p / "task.toml").is_file())
        (task / "unexpected.txt").write_text("unversioned input")
    else:
        next((release_workspace / "tasks").glob("*/instruction.md")).unlink()
    with pytest.raises(ValueError, match=message):
        batch.task_templates()


def test_manifest_cannot_silently_change_the_batch_scope(release_workspace):
    manifest = release_workspace / "dataset.toml"
    blocks = manifest.read_text().split("[[tasks]]")
    manifest.write_text("[[tasks]]".join(blocks[:-1]))
    with pytest.raises(ValueError, match="exactly the same 30 tasks"):
        batch.task_templates()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # Reuse current declared cases, but keep all generated artifacts outside the repo.
    templates = batch.task_templates()
    (tmp_path / "datasets/lg-bench-enterprise-v1.0").mkdir(parents=True)
    (tmp_path / "datasets/lg-bench-enterprise-v1.0/tasks").symlink_to(ROOT / batch.TASKS, target_is_directory=True)
    monkeypatch.setattr(batch, "ROOT", tmp_path)
    monkeypatch.setattr(batch, "task_templates", lambda: templates)
    monkeypatch.chdir(ROOT)
    return tmp_path


def prepare(workspace, harness="opencode", tasks=None):
    profile = ROOT / f"configs/jobs/{harness}-glm-5.2-smoke.yaml"
    args = SimpleNamespace(harness_job=profile, model="glm-5.3-flash", judge_model="glm-5.3",
                           output=workspace / "jobs" / harness, tasks=tasks, attempts=1)
    batch.prepare(args)
    return args, batch.load(args.output / "batch.json")


@pytest.mark.parametrize("harness", ["opencode", "claude-code", "deepseek-harness", "pi"])
def test_all_harness_profiles_preserve_each_dataset_and_explicit_resume(workspace, harness):
    args, plan = prepare(workspace, harness)
    assert plan["selected_tasks"] == plan["expected_trials"] == 30
    assert set(plan["resume_tasks"]) == {"finance-tc-02-L1", "finance-inv-007-L3", "supply-chain-tc-068-L3"}
    assert plan["supports_resume"] == (harness in {"opencode", "claude-code", "deepseek-harness"})
    for row in plan["tasks"]:
        cfg = yaml.safe_load(Path(row["config"]).read_text())
        original = batch.task_templates()[row["task"]]
        fragments = cfg["environment"]["extra_docker_compose"]
        original_binding = [p for p in original["environment"]["extra_docker_compose"]
                            if "mcp-allow-" in p]
        assert [p for p in fragments if "mcp-allow-" in p] == original_binding
        assert [p for p in fragments if p.startswith(batch.HARNESS_PREFIX)] == [
            f"configs/compose/harness/{harness}.yaml"]
        assert cfg["agents"][0]["mcp_servers"] == original["agents"][0]["mcp_servers"]
        assert cfg["agents"][0]["model_name"] == "glm-5.3-flash"
        runtime = yaml.safe_load(Path(fragments[-1]).read_text())["services"]
        assert runtime["lg-mcp-proxy"]["environment"]["LG_MCP_TARGET"] == "mock"
        assert runtime["main"]["environment"]["LG_EVAL_JUDGE_MODEL"] == "glm-5.3"
    assert not next(r for r in plan["tasks"] if r["task"] == "finance-cn-cost-006-L4")["resume"]
    with pytest.raises(ValueError, match="new directory"):
        batch.prepare(args)


def fixture_trial(path, *, steps=(), outcome="graded", exception=None, reward=0.6):
    path.mkdir(parents=True)
    batch.write(path / "result.json", {"finished_at": "2026-10-16T00:00:00Z",
                                      "exception_info": exception, "verifier_result": None})
    verifier = path / "steps" / steps[-1] / "verifier" if steps else path / "verifier"
    verifier.mkdir(parents=True)
    batch.write(verifier / "results.json", {"status": outcome})
    batch.write(verifier / "reward.json", {"content_score": 1.0, "numeric_accuracy": 0.0,
                                         "overall": reward})
    if steps:
        first = path / "steps" / steps[0] / "verifier"
        first.mkdir(parents=True, exist_ok=True)
        batch.write(first / "results.json", {"phase": "capture"})
        batch.write(first / "reward.json", {})


def test_summary_uses_final_step_and_excludes_judge_and_runtime_errors(workspace):
    args, plan = prepare(workspace, tasks=["finance-bgt-01-L2", "finance-tc-02-L1", "finance-inv-007-L3"])
    for row in plan["tasks"]:
        fixture_trial(Path(row["job_dir"]) / "trial", steps=row["steps"],
                      outcome="evaluation_error" if row["task"] == "finance-tc-02-L1" else "graded",
                      exception={"exception_type": "AgentTimeout"} if row["task"] == "finance-bgt-01-L2" else None)
    batch.summary(SimpleNamespace(batch=args.output / "batch.json"))
    report = batch.load(args.output / "summary.json")
    assert report["statuses"] == {"execution_error": 1, "evaluation_error": 1, "graded": 1}
    assert report["graded_mean"] == 0.6 and report["graded_overall_count"] == 1
    assert report["observed_trials"] == 3
    assert report["trials"][-1]["evidence"].endswith("steps/round-2/verifier/results.json")


def test_unstarted_trials_stay_pending_and_missing_rewards_are_not_zero(workspace):
    args, plan = prepare(workspace, tasks=["finance-bgt-01-L2", "finance-tc-02-L1", "finance-cas-01-0-L2"])
    fixture_trial(Path(plan["tasks"][0]["job_dir"]) / "trial", outcome="evaluation_error", reward=0)
    unscored = Path(plan["tasks"][2]["job_dir"]) / "trial"
    fixture_trial(unscored)
    (unscored / "verifier/reward.json").unlink()
    batch.summary(SimpleNamespace(batch=args.output / "batch.json"))
    report = batch.load(args.output / "summary.json")
    assert report["statuses"] == {"evaluation_error": 1, "pending": 1, "no_reward": 1}
    assert report["completed_trials"] == 2 and report["expected_trials"] == 3
    assert report["graded_mean"] is None and report["graded_overall_count"] == 0


def test_finished_harbor_job_without_trial_artifacts_is_no_reward(workspace):
    args, plan = prepare(workspace, tasks=["finance-bgt-01-L2"])
    job = Path(plan["tasks"][0]["job_dir"])
    job.mkdir(parents=True)
    batch.write(job / "result.json", {"finished_at": "2026-10-16T00:00:00Z"})
    batch.summary(SimpleNamespace(batch=args.output / "batch.json"))
    report = batch.load(args.output / "summary.json")
    assert report["statuses"] == {"no_reward": 1}
    assert report["observed_trials"] == report["completed_trials"] == 0
    assert report["graded_mean"] is None


@pytest.mark.parametrize("task", ["finance-xkfa-02", "finance-xkfa-08"])
def test_removed_finance_cases_cannot_be_selected(workspace, task):
    with pytest.raises(ValueError, match="distinct current task"):
        prepare(workspace, tasks=[task])


def test_legacy_directory_name_is_not_a_current_task(workspace):
    with pytest.raises(ValueError, match="distinct current task"):
        prepare(workspace, tasks=["finance-bgt-01"])
