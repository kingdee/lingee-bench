"""The stdio command: validate, then serve one sandbox over stdin and stdout (req 22.2).

``erp-mock-mcp stdio --instance <目录> [--profile] [--dataset] [--anchor-date YYYY-MM-DD]
[--data-dir] [--packs-dir]`` is the command an MCP client starts itself: the client spawns
the process, speaks JSON-RPC over its stdin and stdout, and the process serves exactly one
sandbox for as long as it runs (requirement 15.2). The session itself is
:func:`erp_mock.gateway.stdio.run_stdio`; this module is everything that has to be settled
before it can run, in the one order the requirements allow:

=====  ===========================================  ==================  ==================
step   what                                         what can go wrong   code
=====  ===========================================  ==================  ==================
1      read the environment once                    a value of the      2
       (:func:`~erp_mock.cli.options.read_env`,     wrong form
       requirements 23.9 and 23.10)                 (req 23.10)
2      validation step 1, which reads instance.yaml  that step found    1
       (:func:`~erp_mock.cli.loading.prepare`)       errors
3      ``--profile`` and ``--dataset`` against the   the instance has    2
       names that step read (:func:`chosen`)         no such name
                                                     (req 14.12)
4      validation steps 2 to 8 and the cross-check   a step found        1
       (requirements 2.1 and 22.7)                   errors; a Snapshot
                                                     would not build
5      assemble the pieces and serve the session     the session could   1
       (:func:`~erp_mock.gateway.stdio.run_stdio`)   not be served
=====  ===========================================  ==================  ==================

Why that order. The environment comes first because requirement 23.10 refuses a bad value
"在加载校验之前". The two names are checked between step 1 and the rest because that is the
first moment they can be: ``profiles`` and ``datasets`` are lists instance.yaml carries,
which step 1 reads, and nothing has been built or created yet - which is what makes the
"不创建沙箱" of requirement 14.12 hold without having to undo anything. A name that is not
there is a usage error, exit code 2, and stdout stays empty, as it does for a loading that
failed (requirement 22.7): the only writer of stdout in this process is the SDK's stdout
writer, and it does not exist yet.

stdout carries MCP messages and nothing else (requirement 22.2). This command writes
nothing to it at all: what it has to say - the sandbox of requirement 22.3, the error list
of a step, the log of the service - goes to stderr (:func:`~erp_mock.cli.options.`
``setup_logging``), and ``print`` is not available in this source tree (ruff rule T201).

What this module injects into the gateway, which builds neither itself (design.md 7.4), and
which is the same pair the serve command injects (:mod:`erp_mock.cli.serve`):

- the Snapshot source: :class:`~erp_mock.cli.serve.CachedSnapshots` over
  :class:`~erp_mock.seed.cache.SnapshotCache`, so the one sandbox of the process is copied
  from the Snapshot loading step 8 took up or built (requirement 20.11) and the gateway
  imports nothing of the seed layer;
- the tools layer: :class:`~erp_mock.tools.layer.ToolLayer` over the engine loading already
  built from the syntax trees of step 5 (:func:`~erp_mock.loader.step8.engine_of`), so no
  rule, writeback or query is parsed a second time.

The anchor date (requirements 16.1 and 2.14). ``--anchor-date`` is handed to both the
loading and the session, so the Snapshot step 8 took up and the Snapshot the sandbox is
copied from are the Snapshot of one date::

    --anchor-date 2026-10-09   both the step 8 anchor date and the sandbox's own
    left out                   both decided by the ``clock`` of the instance configuration

Left out, each of the two resolves that configuration for itself - the loading when it runs
and the session at its first initialize (design.md 7.3) - which is the same date for a
clock in mode fixed and the date of the run for a clock in mode today. That is what
requirement 16.1 asks for: the sandbox of a process started today is a sandbox of today,
whatever date the Snapshot cache was filled on.

The options this command has of its own are ``--profile`` and ``--dataset``, and both
select one name (requirement 14.2). The ``--dataset`` of seed is a different option with a
different meaning - it selects a subset of the datasets to build, all of them by default
(requirement 22.6) - so the two are declared where they belong rather than shared.
"""

import logging
from collections.abc import Sequence
from datetime import date
from functools import partial
from pathlib import Path
from typing import Final

import anyio
import click

from erp_mock.cli.loading import load_steps, prepare
from erp_mock.cli.options import (
    anchor_date_option,
    data_dir_option,
    instance_option,
    packs_dir_option,
    read_env,
)
from erp_mock.cli.serve import CachedSnapshots
from erp_mock.config.instance import InstanceDef
from erp_mock.gateway.clock import SystemClock
from erp_mock.gateway.handlers import SessionHandlers
from erp_mock.gateway.sandbox import SandboxManager, SnapshotSource
from erp_mock.gateway.stdio import StdioOptions, run_stdio
from erp_mock.loader.errors import CROSS
from erp_mock.loader.loader import Loader
from erp_mock.loader.step5_expr import ParsedDomain
from erp_mock.loader.step8 import engine_of
from erp_mock.seed.builder import SeedBuilder
from erp_mock.seed.cache import SnapshotCache
from erp_mock.state.schema import build_schema
from erp_mock.tools.layer import ToolLayer

__all__ = [
    "DATASET_OPTION",
    "PROFILE_OPTION",
    "chosen",
    "dataset_option",
    "profile_option",
    "stdio",
]

_LOG: Final = logging.getLogger(__name__)

# The two options an error about them names, so the message and the declaration below
# cannot drift apart (requirement 14.12).
PROFILE_OPTION: Final = "--profile"
DATASET_OPTION: Final = "--dataset"

_UNKNOWN_NAME: Final = "{option} 的值 {value} 不在实例配置中（区分大小写）；该实例的可选值：{known}"
_NO_INSTANCE: Final = "第 1 步没有报错也没有读入 instance.yaml"
_NO_DOMAIN: Final = "第 5 步没有报错也没有产出领域模型"
_SERVING: Final = "stdio 会话开始：profile %s，初始数据集 %s"

profile_option = click.option(
    "--profile",
    "profile",
    default=None,
    help="会话使用的 profile；缺省取 instance.yaml 的 default_profile。",
)

dataset_option = click.option(
    "--dataset",
    "dataset",
    default=None,
    help="沙箱使用的初始数据集；缺省取 instance.yaml 的 default_dataset。",
)


@click.command(name="stdio")
@instance_option
@profile_option
@dataset_option
@anchor_date_option
@data_dir_option
@packs_dir_option
def stdio(
    instance_dir: Path,
    profile: str | None,
    dataset: str | None,
    anchor_date: date | None,
    data_dir: Path,
    packs_dir: Path,
) -> None:
    """校验实例配置，然后经 stdin 与 stdout 服务一个沙箱，直到 stdin 结束或收到信号。"""
    # The five steps of the module docstring, in the order the requirements fix.
    env = read_env()
    loader = prepare(instance_dir, packs_dir, data_dir, anchor_date)
    instance = _instance_of(loader)
    profile_name = chosen(PROFILE_OPTION, profile, instance.profiles, instance.default_profile)
    dataset_name = chosen(DATASET_OPTION, dataset, instance.datasets, instance.default_dataset)
    load_steps(loader, CROSS)
    loaded = loader.assemble()
    # The engine of the loading, which both the Snapshot builder of a dataset the cache has
    # nothing for and the tools layer of every call work through (module docstring).
    engine = engine_of(_parsed_of(loader))
    schema = build_schema(loaded.domain)
    # Annotated so that what the gateway asks for and what the CLI hands it are checked
    # against each other here rather than at the first initialize.
    snapshots: SnapshotSource = CachedSnapshots(
        SnapshotCache(data_dir, SeedBuilder(loaded, instance_dir, engine))
    )
    manager = SandboxManager(
        data_dir=data_dir,
        loaded=loaded,
        schema=schema,
        snapshots=snapshots,
        clock=SystemClock(),
        limits=env.sandbox,
    )
    layer = ToolLayer(loaded, engine, limits=env.resources)
    options = StdioOptions(
        profile=loaded.profiles[profile_name],
        dataset=loaded.datasets[dataset_name],
        anchor_date=anchor_date,
    )
    _LOG.info(_SERVING, profile_name, dataset_name)
    try:
        # The one event loop of the process, started here and left when the session has
        # ended and its diff is written (requirement 22.4); ``anyio.run`` passes positional
        # arguments alone, hence the partial. No janitor is started: the sandbox of a stdio
        # process lives as long as its process (design.md 7.3).
        anyio.run(
            partial(
                run_stdio,
                loaded=loaded,
                schema=schema,
                manager=manager,
                handlers=SessionHandlers(manager, layer),
                options=options,
            )
        )
    finally:
        # The database and the trace of the sandbox are closed by the exit of the session
        # already; this closes the thread pool behind them, and whatever a session that
        # failed left open. The sandbox directory stays where it is, with its trace and its
        # diff in it (requirement 22.3).
        manager.close()
    # Returning is exit code 0, which is what requirement 22.4 asks of a process whose
    # stdin reached EOF or which was asked to stop.


def chosen(option: str, value: str | None, known: Sequence[str], default: str) -> str:
    """The name ``option`` selected, or ``default`` where it was not given (req 14.12).

    ``value`` is the ``--profile`` or ``--dataset`` of the command and ``known`` the names
    instance.yaml lists for it, which step 1 has just read; ``default`` is that section's
    ``default_profile`` or ``default_dataset``, which step 5 checks is one of them
    (requirement 2.10) and which a session that asks for neither gets (requirement 15.1).
    The comparison is character for character, so a name that differs only in case is a
    name the instance does not have.

    Raises :class:`click.UsageError` - :data:`~erp_mock.cli.options.EXIT_USAGE`, a line on
    stderr naming the option and its value, nothing on stdout - for a name the instance
    does not list. Called right after step 1 and before anything is built or created, which
    is what makes the "不创建沙箱" of requirement 14.12 hold.
    """
    if value is None:
        return default
    if value not in known:
        raise click.UsageError(
            _UNKNOWN_NAME.format(option=option, value=value, known="、".join(known))
        )
    return value


def _instance_of(loader: Loader) -> InstanceDef:
    """The instance.yaml of ``loader``; step 1 has run and reported nothing."""
    instance = loader.instance
    if instance is None:
        raise AssertionError(_NO_INSTANCE)
    return instance


def _parsed_of(loader: Loader) -> ParsedDomain:
    """The domain model and expression trees of ``loader``; step 5 has run and passed.

    The engine of the session is built from those trees rather than from the model alone, so
    no rule, writeback or query is parsed a second time
    (:func:`~erp_mock.loader.step8.engine_of`).
    """
    parsed = loader.parsed
    if parsed is None:
        raise AssertionError(_NO_DOMAIN)
    return parsed
