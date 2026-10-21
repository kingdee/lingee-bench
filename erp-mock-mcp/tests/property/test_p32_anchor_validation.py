"""Property 32: every anchor date of the range passes the loading validation.

**Validates: Requirements 21.12**

For any anchor date of 2000-01-01 to 2099-12-31, loading ``instances/p2p-demo`` with that
date as the anchor date of step 8 passes every validation step: the build of
``purchase-basic`` at that date, INV-1, INV-2, INV-3 and INV-6 over the Snapshot the build
produced, and the current user and default organization checks (requirement 2.14), followed
by the cross-check of the descriptions against the Business_Data of that Snapshot.

What the property rules out is a dataset whose dates only work out in part of the calendar.
Every date ``purchase-basic`` writes is a relative expression over the anchor date
(requirement 20.8), so a build at an awkward date is where such an expression lands outside
the calendar or lands in an order the rules refuse: ``month(-1)`` of a January, a 29th of a
February of a non-leap year, a ``this_week`` of a date that is itself the last day of its
week, a ``today+14d`` that crosses into the next year - and R-PUR-001, which refuses an
order whose delivery date is before its business date, is evaluated at the create of the
build itself (requirement 10.9). :mod:`tests.packs.test_p2p_demo` validates the instance at
eight anchor dates picked for being awkward in exactly those ways; this property widens
those eight into the whole range requirement 21.12 gives.

A failure of a step is a failure of the property: there is no anchor date of the range the
instance may be refused at, so nothing is filtered out here and no example is dropped. The
two ways a load ends short - :class:`~erp_mock.loader.errors.ConfigErrors` from a step that
found errors and :class:`~erp_mock.seed.resolve.SeedError` from a build that could not be
carried out - are turned into an assertion naming the anchor date, so what Hypothesis prints
is the date and what went wrong at it rather than a traceback alone.

The anchor date is handed to the loader rather than taken from the clock configuration: the
clock of p2p-demo is in mode today, which writes no ``anchor_date``, and reading the current
date is the gateway's business alone (requirement 2.14) - a test never reads the system
clock (requirement 25.10). The date of every example therefore comes from the generator, and
a run of this module behaves the same whatever day it is run on.

The three dates requirement 25.4 names - 2000-01-01, the leap day 2000-02-29 and 2099-12-31
- are explicit examples, so they run at every example count, and the count itself,
:data:`EXAMPLES`, is well above the 20 random anchor dates the same requirement asks for.

The cost. One full load - the seven configuration steps, the build of the dataset and the
checks over its Snapshot - takes 0.13 to 0.55 seconds on the development machine, so the
:data:`EXAMPLES` examples of the one test of this module run in about 25 seconds.

Each example gets a data directory of its own, a :class:`tempfile.TemporaryDirectory` taken
in the test body the way :func:`tests.property.test_p31_seed_determinism.build_at` takes
one, for two reasons: the Snapshot cache below it starts out empty, so step 8 really does
build the dataset at the drawn date instead of taking up an entry an earlier example left
behind (requirement 20.12), and the directory is gone again by the end of the example, so a
run of a hundred examples leaves one cache entry on disk at a time and none behind. A
directory shared by the module would be a function-scoped fixture's or a module-scoped one's
and give up the first of the two; a function-scoped fixture would also trip Hypothesis'
``function_scoped_fixture`` health check.
"""

import tempfile
from datetime import date
from pathlib import Path
from typing import Final

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.loader.errors import ConfigErrors
from erp_mock.loader.instance import DatasetModel, LoadedInstance
from erp_mock.loader.loader import Loader
from erp_mock.seed.builder import ALIASES_FILENAME, META_FILENAME, SNAPSHOT_FILENAME
from erp_mock.seed.resolve import SeedError

# --- the instance the property loads -----------------------------------------------------

# The example instance of the repository, its business packs and its one initial dataset
# (requirement 21.2), the same three the determinism property reads.
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INSTANCE_DIR: Final = REPO_ROOT / "instances" / "p2p-demo"
PACKS_DIR: Final = REPO_ROOT / "packs"
DATASET: Final = "purchase-basic"

# The three files of the Snapshot step 8 took up, which have fixed names inside the
# directory of the cache entry (design.md data model 2).
SNAPSHOT_FILES: Final[tuple[str, ...]] = (SNAPSHOT_FILENAME, META_FILENAME, ALIASES_FILENAME)

# --- the generator -----------------------------------------------------------------------

# The anchor dates the instance has to pass the validation at (requirement 21.12), and the
# three of them requirement 25.4 names.
FIRST_DAY: Final = date(2000, 1, 1)
LEAP_DAY: Final = date(2000, 2, 29)
LAST_DAY: Final = date(2099, 12, 31)
ANCHORS: Final = st.dates(min_value=FIRST_DAY, max_value=LAST_DAY)

# How many anchor dates the property draws, well above the 20 random ones requirement 25.4
# asks for and the count a property of this suite declares by default (tests/conftest.py);
# at about 0.2 seconds a load that is some 25 seconds of wall clock (module docstring).
EXAMPLES: Final = 100


# --- one validation ----------------------------------------------------------------------


def validate_at(anchor: date, data_dir: Path) -> LoadedInstance:
    """Run every validation step of p2p-demo at ``anchor``; return the configuration.

    The eight steps and the cross-check that follows them (requirement 2.1), with the
    Snapshot of ``purchase-basic`` cached below ``data_dir``, which is what the ``validate``
    command of the CLI runs for ``--anchor-date <anchor>`` (requirement 22.5). Raises
    :class:`~erp_mock.loader.errors.ConfigErrors` at the first step that finds an error and
    :class:`~erp_mock.seed.resolve.SeedError` where the dataset cannot be built at that
    date, which is what requirement 21.12 rules out.
    """
    return Loader(INSTANCE_DIR, PACKS_DIR, anchor, data_dir).load()


def missing_files(dataset: DatasetModel) -> tuple[str, ...]:
    """The names of the files that are not in the cache entry of ``dataset``, or are empty.

    Step 8 leaves the directory of the entry it took up behind
    (:attr:`~erp_mock.loader.instance.DatasetModel.snapshot`), and a Snapshot is those three
    files (requirement 20.10); None means the step did not run, which no load that returned
    may leave behind, and is reported as all three being missing.
    """
    directory = dataset.snapshot
    if directory is None:
        return SNAPSHOT_FILES
    return tuple(
        name
        for name in SNAPSHOT_FILES
        if not (directory / name).is_file() or (directory / name).stat().st_size == 0
    )


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 32: 任意锚点日期都能通过加载校验
@pytest.mark.req("21.12")
@settings(max_examples=EXAMPLES, deadline=None)
@given(anchor=ANCHORS)
@example(anchor=FIRST_DAY)
@example(anchor=LEAP_DAY)
@example(anchor=LAST_DAY)
def test_p2p_demo_passes_the_whole_validation_at_any_anchor_date(anchor: date) -> None:
    # A data directory of its own, emptied again at the end of the example, so the build
    # runs at this anchor date rather than hitting a cache entry (module docstring). The
    # ``deadline=None`` of the settings above is the profile's as well (tests/conftest.py);
    # it is written out here because one example is a whole load and far past any deadline.
    with tempfile.TemporaryDirectory() as work:
        try:
            loaded = validate_at(anchor, Path(work) / "data")
        except (ConfigErrors, SeedError) as error:
            raise AssertionError(
                f"锚点日期 {anchor} 下 p2p-demo 没有通过加载校验：{error}"
            ) from error

        # The load went through at this date, and it went through step 8: the anchor date of
        # the configuration object is the one that was passed in, and the Snapshot of the
        # dataset was taken up under it (requirement 2.14).
        dataset = loaded.default_dataset
        assert dataset.name == DATASET, f"缺省初始数据集是 {dataset.name}，不是 {DATASET}"
        assert loaded.step8_anchor == anchor, (
            f"第 8 步锚点日期是 {loaded.step8_anchor}，不是传入的 {anchor}"
        )
        missing = missing_files(dataset)
        assert not missing, f"锚点日期 {anchor} 下取用的 Snapshot 缺少文件：{missing}"
