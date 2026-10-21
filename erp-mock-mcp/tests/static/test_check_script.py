"""scripts/check.py: the order of its steps, its stderr output and stopping at a failure.

The steps are the checks of requirements 25.7 and 25.8, the domain coverage gate of
requirement 25.1, which is a second pytest run over tests/unit and tests/property alone,
and the requirement tracing check of requirement 25.9.

A stub runner stands in for run_command, so no test here starts the real tools (the
pytest step would run this suite again). Only run_command itself and one start of the
script, in which no tool can be found, use real processes.
"""

import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.check import CANNOT_START, REPO_ROOT, STEPS, run_command, run_steps

CHECK_SCRIPT = REPO_ROOT / "scripts" / "check.py"

EXPECTED_COMMANDS = [
    "uv lock --check",
    "ruff format --check",
    "ruff check",
    "mypy",
    # The load validation of the example instance and the pack level tests (req 25.7).
    "erp-mock-mcp validate --instance instances/p2p-demo",
    "pytest tests/packs",
    "pytest tests/",
    # The coverage gate of erp_mock.domain (requirement 25.1).
    "pytest tests/unit tests/property --cov=erp_mock.domain --cov-fail-under=90",
    # The requirement tracing check (requirement 25.9).
    "python scripts/req_trace.py",
]
TOTAL = len(EXPECTED_COMMANDS)


class StubRunner:
    """Stands in for run_command: records each command and returns a scripted exit code.

    Like a real tool it also writes a line to stderr, which shows where the check's own
    lines fall relative to each step.
    """

    def __init__(self, exit_codes: dict[str, int] | None = None) -> None:
        self.exit_codes = exit_codes or {}
        self.commands: list[str] = []

    def __call__(self, argv: Sequence[str]) -> int:
        command = shlex.join(argv)
        self.commands.append(command)
        sys.stderr.write(f"(output of {command})\n")
        return self.exit_codes.get(command, 0)


@pytest.mark.req("25.7", "25.8")
def test_steps_run_in_order_each_announced_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    runner = StubRunner()
    assert run_steps(STEPS, runner) == 0
    assert runner.commands == EXPECTED_COMMANDS
    expected_err: list[str] = []
    for number, command in enumerate(EXPECTED_COMMANDS, start=1):
        expected_err += [f"==> [{number}/{TOTAL}] {command}", f"(output of {command})"]
    expected_err.append(f"==> check passed: all {TOTAL} steps")
    captured = capsys.readouterr()
    assert captured.err.splitlines() == expected_err
    assert captured.out == ""


@pytest.mark.req("25.7", "25.8")
@pytest.mark.parametrize("exit_code", [1, 2, -9])
@pytest.mark.parametrize("failing", range(TOTAL))
def test_check_stops_at_the_first_failing_step(
    failing: int, exit_code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    command = EXPECTED_COMMANDS[failing]
    runner = StubRunner({command: exit_code})
    assert run_steps(STEPS, runner) == 1
    assert runner.commands == EXPECTED_COMMANDS[: failing + 1]
    assert capsys.readouterr().err.splitlines()[-3:] == [
        f"==> [{failing + 1}/{TOTAL}] {command}",
        f"(output of {command})",
        f"==> check failed at [{failing + 1}/{TOTAL}] {command}: exit code {exit_code}",
    ]


@pytest.mark.req("25.7")
def test_run_command_runs_from_the_repository_root_and_returns_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    script = (
        "import os, sys\n"
        "sys.stdout.write('tool output\\n')\n"
        "sys.exit(3 if os.path.samefile(os.getcwd(), sys.argv[1]) else 4)\n"
    )
    assert run_command([sys.executable, "-c", script, str(REPO_ROOT)]) == 3
    assert capfd.readouterr().out == "tool output\n"


@pytest.mark.req("25.7", "25.8")
def test_script_exits_non_zero_when_the_first_step_cannot_start(tmp_path: Path) -> None:
    # tmp_path is an empty directory: with it as PATH no tool can be found.
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0] == f"==> [1/{TOTAL}] uv lock --check"
    assert lines[1].startswith("==> cannot start uv: ")
    assert lines[2:] == [
        f"==> check failed at [1/{TOTAL}] uv lock --check: exit code {CANNOT_START}"
    ]
