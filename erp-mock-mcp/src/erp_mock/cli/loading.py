"""What every command does before it does its own work: load the instance configuration.

serve, stdio, validate and seed all start by validating one instance configuration against
the business packs it references (requirement 2.1), and three things about that are the same
whichever command is running: which steps run, where the anchor date of step 8 comes from,
and what happens when a Snapshot cannot be built. That is this module; the options those
commands share are :mod:`erp_mock.cli.options`.

The anchor date, which is settled between step 1 and the steps that read it
(requirement 2.14):

=====================================  ==================================================
``--anchor-date 2026-10-09``           that date, whatever the configuration says
``clock.mode`` fixed                   ``clock.anchor_date`` of the configuration
``clock.mode`` today                   the current date of :class:`SystemClock` in
                                       ``clock.timezone``
=====================================  ==================================================

Why between step 1 and the rest: the clock configuration is in instance.yaml, which step 1
reads, and the current date may only be read by the gateway's :class:`SystemClock`
(requirement 24.7) - so the loader cannot work it out itself and the CLI cannot work it out
before step 1. :func:`prepare` therefore runs step 1, resolves the date through
:func:`~erp_mock.gateway.clock.resolve_anchor` and leaves it on the loader, where step 8 and
the read-only configuration object pick it up. Step 1 is also the point at which a command
may look at a ``--profile`` or a ``--dataset`` it was given: those are names instance.yaml
lists (requirements 14.12 and 22.9), and nothing has been built or started yet.

Which steps, per command (requirements 2.1 and 22.6):

=========================  =============================  ==============================
command                    steps                          :func:`load_steps` is called
=========================  =============================  ==============================
serve, stdio, validate     1 to 8 and the cross-check     ``load_steps(loader, CROSS)``
seed                       1 to 7, then its own rebuild    ``load_steps(loader, 7)``
=========================  =============================  ==============================

What can go wrong, and who answers it:

====================================  ==========================================  ====
what                                  answer                                      code
====================================  ==========================================  ====
a step found errors (ConfigErrors)    the error list of requirement 2.2 on         1
                                      stderr, by :class:`~erp_mock.cli.main.`
                                      ``ConfigErrorsReport`` of the command group
a Snapshot could not be built         :class:`SnapshotFailed`, the reason on       1
(SeedError, OSError)                  stderr
``--dataset`` names no dataset of     :class:`click.UsageError` of the command     2
the instance                          (requirements 22.8 and 22.9)
====================================  ==========================================  ====

The first of the three is caught by the command group rather than here, since every command
loads and every step may raise it (:mod:`erp_mock.cli.main`); the second is caught where a
build can happen, which is step 8 of a loading (:func:`load_steps`) and the rebuild of the
seed command.

Loading itself logs nothing, so that the stderr of a command that failed is the error list of
requirement 2.2 and nothing else, which is what a CI job reads. What a command has to say
about a run that got through - the anchor date it settled, what it validated, what it built -
it says itself.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Final

import click

from erp_mock.cli.options import EXIT_FAILED
from erp_mock.gateway.clock import SystemClock, resolve_anchor
from erp_mock.loader.errors import Step
from erp_mock.loader.loader import STEPS, Loader
from erp_mock.seed.resolve import SeedError

__all__ = [
    "CONFIG_STEP",
    "SNAPSHOT_STEP",
    "SnapshotFailed",
    "load_steps",
    "prepare",
    "snapshot_build",
]

# The last step that reads documents alone: through step 7 nothing is built, taken up or
# created, which is where the seed command stops before it rebuilds the Snapshots itself
# (requirement 22.6).
CONFIG_STEP: Final[Step] = 7

# The one step that reads data rather than documents, and therefore the one step that may
# build something: step 8 takes up the Snapshot of every initial dataset and builds the ones
# the cache has nothing to offer for (requirement 2.14). The seed command runs the checks of
# this step itself, over the Snapshots it rebuilt (requirement 22.6).
SNAPSHOT_STEP: Final[Step] = 8

_NO_INSTANCE: Final = "第 1 步没有报错也没有读入 instance.yaml"
_UNKNOWN_STEP: Final = "{step!r} 不是加载步骤"
_BUILD_FAILED: Final = "锚点日期 {anchor} 下的 Snapshot 构建失败：{error}"


class SnapshotFailed(click.ClickException):
    """A Snapshot that could not be built: the reason on stderr, exit code 1 (req 22.8).

    A :class:`click.ClickException` so that click does the exiting, as
    :class:`~erp_mock.cli.main.ConfigErrorsReport` does for a failed validation step. The
    message names the anchor date and what the build reported, which for a
    :class:`~erp_mock.seed.resolve.SeedError` is the dataset definition file, the item of it
    and the reason (requirements 20.6 and 20.15).
    """

    exit_code = EXIT_FAILED

    def __init__(self, error: Exception, anchor: date) -> None:
        super().__init__(_BUILD_FAILED.format(anchor=anchor.isoformat(), error=error))
        self.error = error


def prepare(
    instance_dir: Path,
    packs_dir: Path,
    data_dir: Path | None = None,
    anchor_date: date | None = None,
    *,
    clock: SystemClock | None = None,
) -> Loader:
    """A loader with step 1 run and the anchor date of the loading settled (req 2.14).

    ``instance_dir``, ``packs_dir`` and ``data_dir`` are the options of the command, already
    absolute (:func:`~erp_mock.cli.options.resolve_path`), and ``anchor_date`` its
    ``--anchor-date``, None where it was not given. ``clock`` is the system clock the current
    date is read from for a clock in mode today, :class:`SystemClock` itself by default; a
    test passes one over instants of its own and so reads no clock (requirement 25.10).

    Step 1 runs here rather than in :func:`load_steps` because what comes after it depends on
    what it read: the anchor date on ``clock``, and a ``--profile`` or ``--dataset`` of the
    command on the names instance.yaml lists (module docstring). Raises
    :class:`~erp_mock.loader.errors.ConfigErrors` where step 1 found errors - an instance
    directory that is not there included (requirement 2.12) - which the command group reports.
    """
    loader = Loader(instance_dir, packs_dir, anchor_date, data_dir)
    loader.run_step(1)
    instance = loader.instance
    if instance is None:
        raise AssertionError(_NO_INSTANCE)
    # The loader takes the anchor date as an argument because only the caller can read a
    # clock (requirement 24.7); the configuration that decides it is what step 1 just read,
    # so the date is put in place now. Step 8 is the only step that reads it.
    loader.anchor_date = resolve_anchor(
        anchor_date, instance.clock, SystemClock() if clock is None else clock
    )
    return loader


def load_steps(loader: Loader, through: Step) -> None:
    """Run the steps of ``loader`` after step 1, up to and including ``through``.

    ``through`` is :data:`~erp_mock.loader.errors.CROSS` for a command that validates
    everything (requirement 2.1) and :data:`CONFIG_STEP` for the seed command, which stops
    before step 8 and rebuilds the Snapshots itself (requirement 22.6). :func:`prepare` has
    run step 1 already, so it is not run again.

    Raises :class:`~erp_mock.loader.errors.ConfigErrors` at the first step that found errors,
    which the command group reports, and :class:`SnapshotFailed` where step 8 had to build a
    Snapshot and the build failed.
    """
    if through not in STEPS:
        raise ValueError(_UNKNOWN_STEP.format(step=through))
    for step in STEPS:
        if step != 1:
            _run_step(loader, step)
        if step == through:
            return


def _run_step(loader: Loader, step: Step) -> None:
    """Run one step of ``loader``, reporting a build of step 8 that failed (req 22.8).

    Only step 8 builds anything, so only its errors can be a Snapshot that could not be
    produced; an OSError of any other step is a configuration file that cannot be read,
    which that step reports as a configuration error.
    """
    if step != SNAPSHOT_STEP:
        loader.run_step(step)
        return
    with snapshot_build(loader.anchor()):
        loader.run_step(step)


@contextmanager
def snapshot_build(anchor: date) -> Iterator[None]:
    """Turn a build that failed inside the block into exit code 1 (requirement 22.8).

    ``anchor`` is the anchor date the Snapshot is of, which the message names.
    :class:`~erp_mock.seed.resolve.SeedError` is a dataset the build cannot carry out
    (requirements 20.6, 20.15 and 20.16) and OSError a data directory the cache cannot be
    written in; both are a Snapshot the command could not produce, which is neither a
    configuration error of requirement 2.2 nor anything the command line can mend.
    """
    try:
        yield
    except (SeedError, OSError) as error:
        raise SnapshotFailed(error, anchor) from error
