import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from lg_bench.cli import discover, smoke, validate, validate_seed

from conftest import requires_enterprise, requires_general

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "examples/tasks"
TASK = TASKS / "ap-three-way-match"
spec = importlib.util.spec_from_file_location("seed_verifier", TASK / "tests/verify.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.fixture
def workspace(tmp_path):
    shutil.copytree(TASK / "environment/data", tmp_path / "data")
    (tmp_path / "output").mkdir()
    subprocess.run([sys.executable, str(TASK / "solution/solve.py"), str(tmp_path)], check=True)
    return tmp_path


def mutate(workspace, change):
    path = workspace / "output/reconciliation.json"
    result = json.loads(path.read_text())
    change(result)
    path.write_text(json.dumps(result))


def test_task_and_harbor_schema():
    assert discover(TASK) == [TASK]
    assert TASK in discover(TASKS)
    assert validate(TASK)["valid"]
    assert smoke(TASK)["local_oracle_reward"] == 1


def test_oracle_matches_independent_expectations(workspace):
    assert verifier.grade(workspace)["success"] == 1


def test_nop_gets_zero(workspace):
    (workspace / "output/reconciliation.json").unlink()
    assert verifier.grade(workspace)["success"] == 0


@pytest.mark.parametrize("change", [
    lambda r: r["decisions"].pop(),
    lambda r: r["decisions"].__setitem__(1, r["decisions"][0].copy()),
    lambda r: r["decisions"][0].update(amount_cents=1),
    lambda r: r["decisions"][0].update(amount_cents=True),
    lambda r: r["decisions"][0].update(amount_cents=50000.0),
    lambda r: r["decisions"][1].update(receipt_ids=["R003", "R004"]),
    lambda r: r["decisions"][0].update(receipt_ids=["R001", "R002", "R002"]),
    lambda r: r["decisions"][0].update(po_id="PO404"),
    lambda r: r["decisions"][5].update(decision="ready_for_review", reason_code="matched"),
    lambda r: r["decisions"][6].update(decision="ready_for_review", reason_code="matched"),
    lambda r: r["decisions"][7].update(decision="needs_approval", reason_code="amount_requires_approval"),
    lambda r: r["summary"].update(ready_amount_cents=1050001),
    lambda r: r["summary"].update(approval_count=True),
    lambda r: r.update(payments=[{"invoice_id": "I001", "amount_cents": 50000}]),
    lambda r: r.update(batch_id="WRONG"),
    lambda r: r.update(extra="unexpected"),
    lambda r: [d.update(decision="ready_for_review", reason_code="matched") for d in r["decisions"]],
    lambda r: [d.update(decision="blocked", reason_code="missing_po") for d in r["decisions"]],
], ids=["missing", "duplicate", "amount", "bool-amount", "float-amount", "cancelled-receipt",
        "duplicate-evidence", "wrong-po", "duplicate-paid", "approval-bypass", "threshold-boundary",
        "summary", "bool-summary", "payment", "batch", "extra-field", "approve-all", "block-all"])
def test_incorrect_artifacts_fail(workspace, change):
    mutate(workspace, change)
    assert verifier.grade(workspace)["success"] == 0


@pytest.mark.parametrize("text", ["{}", "null", "[]", "{broken", '{"batch_id":"x","batch_id":"y"}', '{"value":NaN}'])
def test_invalid_json_fails_closed(workspace, text):
    (workspace / "output/reconciliation.json").write_text(text)
    assert verifier.grade(workspace)["success"] == 0


def test_order_is_not_semantic(workspace):
    def change(result):
        result["decisions"].reverse()
        for row in result["decisions"]:
            row["receipt_ids"].reverse()
    mutate(workspace, change)
    assert verifier.grade(workspace)["success"] == 1


@pytest.mark.parametrize("name", ["state.json", "invoices.json", "policy.md"])
def test_original_inputs_cannot_be_modified(workspace, name):
    path = workspace / "data" / name
    path.write_text(path.read_text() + "\n")
    result = verifier.grade(workspace)
    assert result["success"] == 0
    assert not result["checks"]["input_unchanged"]


def test_symlink_output_rejected(workspace):
    path = workspace / "output/reconciliation.json"
    target = workspace / "result.json"
    path.rename(target)
    path.symlink_to(target)
    assert verifier.grade(workspace)["success"] == 0


def test_unknown_input_file_rejected(workspace):
    (workspace / "data/payment.json").write_text("{}")
    assert verifier.grade(workspace)["success"] == 0


def test_invalid_metadata_is_rejected(tmp_path):
    task = tmp_path / TASK.name
    shutil.copytree(TASK, task)
    config = task / "task.toml"
    config.write_text(config.read_text().replace('domain = "finance"', 'domain = "unknown"'))
    with pytest.raises(ValueError, match="unsupported domain"):
        validate_seed(task)


@pytest.mark.parametrize("split", ["public_eval", "heldout_eval"])
def test_supported_evaluation_splits_are_accepted(tmp_path, split):
    task = tmp_path / TASK.name
    shutil.copytree(TASK, task)
    config = task / "task.toml"
    config.write_text(config.read_text().replace('split = "dev"', f'split = "{split}"'))
    assert validate_seed(task)["valid"]


@pytest.mark.parametrize("split", ["public-test", "private-test"])
def test_legacy_split_names_are_rejected(tmp_path, split):
    task = tmp_path / TASK.name
    shutil.copytree(TASK, task)
    config = task / "task.toml"
    config.write_text(config.read_text().replace('split = "dev"', f'split = "{split}"'))
    with pytest.raises(ValueError, match="unsupported split"):
        validate_seed(task)


def test_empty_dataset_does_not_silently_pass(tmp_path):
    with pytest.raises(ValueError, match="No tasks"):
        discover(tmp_path)


@pytest.mark.parametrize("task", discover(TASKS), ids=lambda p: p.name)
def test_native_tasks_validate_without_requiring_oracles(task):
    assert validate(task)["valid"]


ENTERPRISE_TASKS = ROOT / "datasets/lg-bench-enterprise-v1.0/tasks"
GENERAL_TASK = ROOT / "datasets/lg-bench-general-v1.0/tasks/SCH-meeting-date-policy-check-L1"
MULTI_STEP_TASK = ENTERPRISE_TASKS / "finance-tc-02-L1"


@requires_enterprise
@pytest.mark.parametrize("task", discover(ENTERPRISE_TASKS) if ENTERPRISE_TASKS.is_dir() else [],
                         ids=lambda p: p.name)
def test_enterprise_tasks_validate_without_requiring_oracles(task):
    assert validate(task)["valid"]


@requires_general
@pytest.mark.parametrize("missing", ["instruction.md", "tests/test.sh", "environment"])
def test_native_validation_still_rejects_missing_runtime_files(tmp_path, missing):
    task = tmp_path / "imported-task"
    shutil.copytree(GENERAL_TASK, task)
    path = task / missing
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    with pytest.raises((ValueError, FileNotFoundError), match=missing):
        validate(task)


@requires_enterprise
@pytest.mark.parametrize("relative", ["instruction.md", "tests/test.sh"])
def test_native_validation_checks_every_declared_step(tmp_path, relative):
    task = tmp_path / "multi-step"
    shutil.copytree(MULTI_STEP_TASK, task)
    (task / "steps/round-2" / relative).unlink()
    with pytest.raises(FileNotFoundError):
        validate(task)


@requires_general
def test_imported_task_is_valid_but_has_no_local_oracle():
    task = GENERAL_TASK
    assert validate(task)["valid"]
    with pytest.raises(ValueError, match="missing solution/solve.sh"):
        smoke(task)
