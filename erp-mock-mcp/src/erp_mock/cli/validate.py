"""The validate command: the checks of a startup without starting anything (req 22.5).

``erp-mock-mcp validate --instance <目录> [--packs-dir] [--data-dir] [--anchor-date]`` runs
exactly what serve and stdio run before they serve: the 8 steps of requirement 2.1 and the
cross-check after them, on the instance configuration of ``--instance`` and the business
packs its instance.yaml references. Nothing is served, no port is bound and no sandbox is
created, which is what makes it the command a deployer and a CI job run (design.md
component 8).

What it answers with:

====  ================================================================================
code  what happened
0     every step passed; stdout stays empty, since requirement 22.5 asks for no output,
      and one line on stderr names what was validated and the anchor date it was
      validated under
1     a step found errors - the error list of requirement 2.2 on stderr, by the command
      group - or a Snapshot of step 8 could not be built (:class:`~erp_mock.cli.loading.`
      ``SnapshotFailed``)
2     a usage error: a missing ``--instance``, an ``--anchor-date`` that is no date
      (requirement 16.13), an option that does not exist
====  ================================================================================

``--data-dir`` is needed although nothing is served: step 8 takes up the Snapshot of every
initial dataset, and the cache of those Snapshots is below that directory
(requirement 20.11). So a validate of a dataset that has never been built builds it, and the
next command that needs it - a serve, a stdio, the next validate - takes up what this one
left (requirement 20.12). ``--anchor-date`` decides which anchor date all of that happens
under; without it, the ``clock`` of the instance configuration does (requirement 2.14,
:func:`~erp_mock.cli.loading.prepare`).
"""

import logging
from datetime import date
from pathlib import Path
from typing import Final

import click

from erp_mock.cli.loading import load_steps, prepare
from erp_mock.cli.options import (
    anchor_date_option,
    data_dir_option,
    instance_option,
    packs_dir_option,
)
from erp_mock.loader.errors import CROSS

__all__ = ["validate"]

_LOG: Final = logging.getLogger(__name__)

_PASSED: Final = "全部校验通过：业务包 %d 个，初始数据集 %d 个，锚点日期 %s"


@click.command(name="validate")
@instance_option
@packs_dir_option
@data_dir_option
@anchor_date_option
def validate(instance_dir: Path, packs_dir: Path, data_dir: Path, anchor_date: date | None) -> None:
    """校验实例配置与其引用的业务包，执行与服务启动相同的全部检查。"""
    loader = prepare(instance_dir, packs_dir, data_dir, anchor_date)
    load_steps(loader, CROSS)
    # Nothing is written to stdout: requirement 22.5 asks for the exit code alone, and the
    # stdout of a command of this service carries only what a requirement puts there
    # (requirements 22.2 and 22.10). What was validated, and the anchor date it was validated
    # under - which may have come from the clock rather than from the command line - go to
    # the log, on stderr, and only after the last check passed: the stderr of a failed
    # validate is the error list of requirement 2.2 and nothing else.
    _LOG.info(_PASSED, len(loader.raw_packs), len(loader.raw_datasets), loader.anchor().isoformat())
