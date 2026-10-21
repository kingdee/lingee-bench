"""The seed command: build the Snapshots of the initial datasets again (req 22.6, 22.10).

``erp-mock-mcp seed --instance <目录> [--dataset <名称>] [--anchor-date] [--data-dir]
[--packs-dir]`` is the command that fills the Snapshot cache on purpose: a CI job runs it to
build what a later serve or stdio will take up, and a pack author runs it to see what their
dataset amounts to. It does three things, in this order (requirement 22.6):

1. the validation steps 1 to 7 (:data:`~erp_mock.cli.loading.CONFIG_STEP`) - everything that
   reads documents, and nothing that builds, takes up or creates anything;
2. the Snapshot, the metadata and the alias map of each selected dataset at the anchor date,
   built with the cache ignored (:meth:`~erp_mock.seed.cache.SnapshotCache.rebuild`): the
   entry that is there is not looked at, and what the build wrote replaces it;
3. the checks of step 8 over the datasets it just built (:func:`~erp_mock.loader.step8.check`):
   INV-1, INV-2, INV-3 and INV-6, the current user and the one default organization
   (requirements 2.10 and 2.14).

Why the cache is ignored rather than consulted: an entry is taken up when the six values of
requirement 20.12 still fit, which is what makes a serve fast, but the point of this command
is to produce the three files, and "they are there already" would make a build that was asked
for not happen. Everything else about the build is the cache's own - it is written into a
temporary directory and only a build that got through replaces the entry, so a seed that
failed leaves the cache exactly as it was (requirement 20.11).

What a build cannot change is what it produces: two builds of one dataset at one anchor date
write the same bytes (requirement 20.13), so running this over a cache that was filled
already replaces each entry with an identical one.

Which datasets:

=============================  =========================================================
``--dataset`` left out         every dataset of instance.yaml, in its order (req 22.6)
``--dataset purchase-basic``   that one dataset
``--dataset Purchase-Basic``   a usage error: the comparison is character for character,
                               exit code 2, nothing built (requirements 22.8 and 22.9)
=============================  =========================================================

Step 1 is where the ``--dataset`` check happens, as it is for the ``--profile`` and
``--dataset`` of stdio: instance.yaml has just been read, so the names are known, and nothing
has been built yet (:func:`~erp_mock.cli.loading.prepare`, requirement 22.9).

What a run that got through writes on stdout: one line per dataset, in the order instance.yaml
lists them, with the four things requirement 22.10 asks for - the dataset as ``<名称>@<版本>``,
the anchor date, the Snapshot and the alias map - separated by tabs::

    purchase-basic@1.0.0<TAB>2026-10-09<TAB>/srv/var/snapshots/...snapshot.sqlite<TAB>...

Tabs rather than spaces because two of the four fields are paths and a path may contain a
space: with tabs the line stays one record of four fields for ``cut -f`` and for whatever CI
script reads it. Everything else the command has to say - the anchor date it settled, the
error list of a step, a build that failed - goes to stderr, and the lines are written after
the last check passed, so stdout is the whole report or nothing at all.

The exit codes (requirement 22.8): 0 for a run that built and checked everything, 1 for a step
that found errors (the error list of requirement 2.2, by the command group) or a build that
failed (:class:`~erp_mock.cli.loading.SnapshotFailed`), 2 for a usage error, the ``--dataset``
of requirement 22.9 included.
"""

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Final

import click

from erp_mock.cli.loading import (
    CONFIG_STEP,
    SNAPSHOT_STEP,
    load_steps,
    prepare,
    snapshot_build,
)
from erp_mock.cli.options import (
    anchor_date_option,
    data_dir_option,
    instance_option,
    packs_dir_option,
)
from erp_mock.config.instance import DatasetDef, InstanceDef
from erp_mock.loader.errors import StepErrors
from erp_mock.loader.loader import Loader
from erp_mock.loader.step4 import RawDoc
from erp_mock.loader.step5_expr import ParsedDomain
from erp_mock.loader.step8 import check as check_step8
from erp_mock.loader.step8 import engine_of
from erp_mock.seed.builder import SeedBuilder, SnapshotRef
from erp_mock.seed.cache import SnapshotCache

__all__ = ["DATASET_OPTION", "dataset_option", "seed", "selected"]

# The option an error about it names, so the message and the declaration cannot drift apart
# (requirement 22.9).
DATASET_OPTION: Final = "--dataset"

# One line of stdout: the dataset, the anchor date, the Snapshot and the alias map
# (requirement 22.10, module docstring).
_LINE: Final = "{dataset}\t{anchor}\t{snapshot}\t{aliases}"

_UNKNOWN_DATASET: Final = (
    "{option} 的值 {value} 不在实例配置的 datasets 中（区分大小写）；该实例的初始数据集：{known}"
)
_NO_INSTANCE: Final = "第 1 步没有报错也没有读入 instance.yaml"
_NO_DOMAIN: Final = "第 5 步没有报错也没有产出领域模型"
_MISSING_DEFINITION: Final = "第 4 步没有读入初始数据集 {name} 的定义"

dataset_option = click.option(
    "--dataset",
    "dataset",
    default=None,
    help="只构建这一个初始数据集；缺省构建 instance.yaml 的 datasets 中的全部。",
)


@click.command(name="seed")
@instance_option
@dataset_option
@anchor_date_option
@data_dir_option
@packs_dir_option
def seed(
    instance_dir: Path,
    dataset: str | None,
    anchor_date: date | None,
    data_dir: Path,
    packs_dir: Path,
) -> None:
    """忽略缓存重新构建所选初始数据集的 Snapshot、元数据与别名映射。"""
    loader = prepare(instance_dir, packs_dir, data_dir, anchor_date)
    names = selected(_instance_of(loader), dataset)
    load_steps(loader, CONFIG_STEP)

    loaded = loader.assemble()
    engine = engine_of(_parsed_of(loader))
    cache = SnapshotCache(data_dir, SeedBuilder(loaded, instance_dir, engine))
    definitions = _definitions(loader, names)
    built = _rebuild(cache, definitions, loaded.step8_anchor)

    # The checks of step 8 over the Snapshots just built: the cache has exactly what this
    # command wrote, so the step takes those entries up and builds nothing
    # (requirements 20.12 and 22.6).
    errors = StepErrors(SNAPSHOT_STEP)
    with snapshot_build(loaded.step8_anchor):
        check_step8(loaded, definitions, instance_dir, data_dir, errors, engine=engine)
    errors.raise_if_any(loader.pack_order)

    for ref in built:
        click.echo(_line(ref))


def selected(instance: InstanceDef, dataset: str | None) -> tuple[str, ...]:
    """The datasets to build, in the order instance.yaml lists them (requirement 22.6).

    ``dataset`` is the ``--dataset`` of the command: None selects every dataset of
    instance.yaml, a name the one dataset that carries it. The comparison is character for
    character, so a name that differs only in case is a name the instance does not have.

    Raises :class:`click.UsageError` - :data:`~erp_mock.cli.options.EXIT_USAGE`, a line on
    stderr naming the option and its value, nothing on stdout - for a name the instance does
    not list (requirement 22.9). Called right after step 1 and before anything is built, which
    is what makes the "不构建 Snapshot" of that requirement hold.
    """
    names = tuple(instance.datasets)
    if dataset is None:
        return names
    if dataset not in names:
        raise click.UsageError(
            _UNKNOWN_DATASET.format(option=DATASET_OPTION, value=dataset, known="、".join(names))
        )
    return (dataset,)


def _rebuild(
    cache: SnapshotCache, definitions: Sequence[RawDoc[DatasetDef]], anchor: date
) -> tuple[SnapshotRef, ...]:
    """Build each dataset of ``definitions`` at ``anchor``, ignoring the cache (req 22.6).

    One build per dataset, in the order of ``definitions``, each replacing the entry of its
    key; raises :class:`~erp_mock.cli.loading.SnapshotFailed` at the first build that failed,
    which leaves the entries of that build and of the datasets after it as they were
    (requirement 20.11).
    """
    with snapshot_build(anchor):
        return tuple(cache.rebuild(raw.document, anchor, file=raw.file) for raw in definitions)


def _line(ref: SnapshotRef) -> str:
    """The stdout line of one built dataset (requirement 22.10, module docstring)."""
    return _LINE.format(
        dataset=ref.key.dataset,
        anchor=ref.key.anchor.isoformat(),
        snapshot=ref.db,
        aliases=ref.aliases,
    )


def _definitions(loader: Loader, names: Sequence[str]) -> tuple[RawDoc[DatasetDef], ...]:
    """The definitions step 4 read for ``names``, in the order instance.yaml lists them.

    Step 4 matches a definition to a name by its ``dataset`` field rather than by its file
    name (requirement 2.13) and reads them in the order of instance.yaml, so filtering what it
    left is what keeps the order requirement 22.10 asks for.
    """
    chosen = frozenset(names)
    found = tuple(raw for raw in loader.raw_datasets if raw.document.dataset in chosen)
    if len(found) != len(chosen):
        missing = sorted(chosen - {raw.document.dataset for raw in found})
        raise AssertionError(_MISSING_DEFINITION.format(name=missing[0]))
    return found


def _instance_of(loader: Loader) -> InstanceDef:
    """The instance.yaml of ``loader``; step 1 has run and reported nothing."""
    instance = loader.instance
    if instance is None:
        raise AssertionError(_NO_INSTANCE)
    return instance


def _parsed_of(loader: Loader) -> ParsedDomain:
    """The domain model and expression trees of ``loader``; step 5 has run and passed.

    The engine of the build is built from those trees rather than from the model alone, so no
    rule, writeback or query is parsed a second time (:func:`~erp_mock.loader.step8.engine_of`).
    """
    parsed = loader.parsed
    if parsed is None:
        raise AssertionError(_NO_DOMAIN)
    return parsed
