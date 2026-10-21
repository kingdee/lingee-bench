"""scripts/perf.py: one run at a small scale, and that it is no step of the local check.

Requirement 25.11 asks for two things of the performance command, and this file is one test
for each:

- it measures and reports the three figures of engineering.md section 6 on p2p-demo. The
  run here is the real command - a subprocess over ``instances/p2p-demo``, which starts a
  real ``erp-mock-mcp serve``, opens real sessions and reads the trace over the Admin API -
  with ``--initializes`` and ``--calls`` turned down to a handful, which is what those two
  options are for (:mod:`scripts.perf`). The assertions are about the shape of the output
  and not about any duration: a figure has to be there, be positive and carry the sample
  count that was asked for, and a threshold on a measurement would make this suite fail on
  a loaded machine, which requirement 25.11 explicitly does not want;
- the measurement is not a pass condition of the local check command, so no step of
  :data:`~scripts.check.STEPS` runs it (requirement 25.7 lists the steps that do).

The three samples of one run are therefore two initializes and five calls, which keeps the
whole file to a few seconds of the suite. ``--data-dir`` is a ``tmp_path``, so the Snapshot
cache, the sandboxes and the trace of the service go where pytest removes them rather than
into a temporary directory of the script's own.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from scripts.check import STEPS
from scripts.perf import (
    CALL_P95,
    PASSED_THROUGH,
    REPO_ROOT,
    SANDBOX_P95,
    STARTUP,
    UNIT,
)

PERF_SCRIPT: Final = REPO_ROOT / "scripts" / "perf.py"

# The scale of the one run: enough samples for a p95 to be a sample of the right run, few
# enough to stay within a few seconds (module docstring).
INITIALIZES: Final = 2
CALLS: Final = 5

# The deadline of that run. It takes a few seconds; a test that reaches this has failed.
RUN_TIMEOUT_S: Final = 180.0

# The three lines of stdout, and nothing else there (:meth:`~scripts.perf.Metric.line`).
METRICS: Final[tuple[str, ...]] = (STARTUP, SANDBOX_P95, CALL_P95)


def metric_line(name: str) -> re.Pattern[str]:
    """The pattern of the reported line of the metric ``name``: its value and its ``n``."""
    return re.compile(rf"^{re.escape(name)} +(\d+\.\d+) {re.escape(UNIT)} +n=(\d+)", re.MULTILINE)


def measured(stdout: str, name: str) -> tuple[float, int]:
    """The value and the sample count the run reported for ``name``."""
    found = metric_line(name).search(stdout)
    assert found is not None, f"输出中没有指标 {name}：\n{stdout}"
    return float(found.group(1)), int(found.group(2))


@pytest.mark.req("25.11")
def test_a_small_run_reports_the_three_metrics(tmp_path: Path) -> None:
    assert PERF_SCRIPT.is_file(), f"没有找到性能测量命令：{PERF_SCRIPT}"
    # The environment the script passes on to the service it starts, so that no ERP_MOCK_*
    # of the machine the tests run on decides the limits of this run (requirement 23.9).
    env = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    completed = subprocess.run(
        [
            sys.executable,
            str(PERF_SCRIPT),
            "--instance",
            str(REPO_ROOT / "instances" / "p2p-demo"),
            "--packs-dir",
            str(REPO_ROOT / "packs"),
            "--data-dir",
            str(tmp_path / "var"),
            "--initializes",
            str(INITIALIZES),
            "--calls",
            str(CALLS),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
        check=False,
    )

    # The measurement was made: the service came up, every call was performed and none of
    # them was refused by a resource limit (:func:`~scripts.perf.call_latencies`).
    assert completed.returncode == 0, f"stderr:\n{completed.stderr}\nstdout:\n{completed.stdout}"

    # stdout carries the three figures, in report order, and nothing else; the script's own
    # lines are on stderr, as in scripts/check.py.
    lines = completed.stdout.splitlines()
    assert [line.split(maxsplit=1)[0] for line in lines] == list(METRICS), f"stdout:\n{lines}"

    # Each figure is a positive duration in milliseconds, and the two that were sampled
    # carry the sample counts the command line asked for.
    startup, startup_n = measured(completed.stdout, STARTUP)
    sandbox, sandbox_n = measured(completed.stdout, SANDBOX_P95)
    call, call_n = measured(completed.stdout, CALL_P95)
    assert startup > 0.0
    assert sandbox > 0.0
    assert call >= 0.0
    assert (startup_n, sandbox_n, call_n) == (1, INITIALIZES, CALLS)


@pytest.mark.req("25.11")
def test_the_local_check_does_not_run_the_performance_command() -> None:
    """The measurement is no pass condition of the check command (requirement 25.11)."""
    assert not [step for step in STEPS if any(PERF_SCRIPT.stem in part for part in step)]
