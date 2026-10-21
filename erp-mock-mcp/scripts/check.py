"""Local check command (requirements 25.7, 25.8, 25.9).

Run it from the repository root::

    uv run --locked python scripts/check.py

``uv run --locked`` stops with an error when uv.lock does not match pyproject.toml;
otherwise it installs the versions locked in uv.lock into .venv and puts .venv/bin first
on PATH, where the commands of STEPS are found. The commands run one after another from
the repository root, each announced on stderr. The first one that exits with a non-zero
code, or cannot be started, ends the check with exit code 1 and the remaining steps do
not run; exit code 0 means that every step passed. This script writes only to stderr and
leaves stdout to the tools.

The steps are the ones requirement 25.7 lists, in the order design.md writes them: the
formatting, lint and type checks, the load validation of the example instance, the pack
level tests, the whole suite with the coverage gate of requirement 25.1, and the
requirement tracing check of requirement 25.9. Exit code 0 of the whole command in a
fresh checkout is the M0 exit criterion (requirement 25.10), which is why no step reads
anything outside the repository: the tracing check works off ``tests/req_index.json``
(scripts/req_trace.py), not off the spec.
"""

import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# Exit code given to a step whose command cannot be started, the code a shell reports
# for a command it cannot find.
CANNOT_START: Final = 127

Command = tuple[str, ...]

# Runs one command and returns its exit code. main() uses run_command; tests pass a stub.
Runner = Callable[[Sequence[str]], int]

# The steps of the check, in the order they run (module docstring).
STEPS: Final[tuple[Command, ...]] = (
    # uv.lock matches pyproject.toml (25.8). `uv run --locked` refuses to start otherwise;
    # this step keeps the check independent of how the script was started.
    ("uv", "lock", "--check"),
    ("ruff", "format", "--check"),
    ("ruff", "check"),
    # Strict mode for erp_mock.domain is set in [tool.mypy] of pyproject.toml.
    ("mypy",),
    # The load validation of the example instance: every step of requirement 2.1 over
    # instances/p2p-demo and the packs it references, the Snapshot of its initial dataset
    # included (requirements 25.7 and 25.10). No --anchor-date is given, so the date is
    # the instance's own business (its clock is in mode today).
    ("erp-mock-mcp", "validate", "--instance", "instances/p2p-demo"),
    # The pack level tests: the declarative cases in each business pack's own tests/
    # directory, which tests/packs/test_pack_cases.py collects and replays (requirement
    # 21.9). Requirement 25.7 names them beside the whole suite, so they are their own
    # step: a configuration error of an example pack is then reported before the suite
    # runs, rather than somewhere inside it.
    ("pytest", "tests/packs"),
    ("pytest", "tests/"),
    # The coverage gate of erp_mock.domain: the unit tests and the property tests together
    # cover at least 90% of its lines, and this step fails below that (requirement 25.1).
    # The tests run a second time here because the gate counts those two directories alone.
    (
        "pytest",
        "tests/unit",
        "tests/property",
        "--cov=erp_mock.domain",
        "--cov-fail-under=90",
    ),
    # The requirement tracing check: every acceptance criterion of requirements 1 to 24 has
    # at least one test marked with it (requirement 25.9). It collects the suite once more
    # (collection only, no test runs) and compares the markers with tests/req_index.json.
    ("python", "scripts/req_trace.py"),
)


def _say(message: str) -> None:
    """Write one line of the check's own output to stderr."""
    sys.stderr.write(f"==> {message}\n")
    sys.stderr.flush()


def run_command(argv: Sequence[str]) -> int:
    """Run ``argv`` from the repository root with inherited stdio; return its exit code.

    A command that cannot be started, for example because its program is not on PATH,
    is reported on stderr and counts as exiting with CANNOT_START.
    """
    try:
        return subprocess.run(argv, cwd=REPO_ROOT, check=False).returncode
    except OSError as error:
        _say(f"cannot start {argv[0]}: {error}")
        return CANNOT_START


def run_steps(steps: Sequence[Sequence[str]], runner: Runner) -> int:
    """Run ``steps`` in order with ``runner`` and stop at the first one that fails.

    Each step is announced on stderr before it runs. Returns the exit code of the check:
    0 when every step exits with 0, otherwise 1.
    """
    total = len(steps)
    for number, argv in enumerate(steps, start=1):
        command = shlex.join(argv)
        _say(f"[{number}/{total}] {command}")
        exit_code = runner(argv)
        if exit_code != 0:
            _say(f"check failed at [{number}/{total}] {command}: exit code {exit_code}")
            return 1
    _say(f"check passed: all {total} steps")
    return 0


def main() -> int:
    """Run the full check and return the exit code of the process."""
    return run_steps(STEPS, run_command)


if __name__ == "__main__":
    sys.exit(main())
