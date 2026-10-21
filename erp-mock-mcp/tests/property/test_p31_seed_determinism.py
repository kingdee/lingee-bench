"""Property 31: the determinism of a dataset build.

**Validates: Requirements 20.3, 20.13, 20.14**

For any anchor date of 2000-01-01 to 2099-12-31, the three files of a build are decided by
the dataset definition and the anchor date alone, and nothing else:

- two builds of one dataset at one anchor date write byte for byte the same Snapshot,
  metadata and alias map (requirement 20.13);
- the alias map of any two anchor dates is byte for byte the same, since an internal id
  comes from the creation order and not from a date (requirement 20.14);
- a build that differs only in ``random_seed`` keeps the field values the dataset writes
  itself, every internal id and the alias map, so the seed moves drawn values and nothing
  else (requirement 20.3).

The three dates requirement 25.4 names - 2000-01-01, the leap day 2000-02-29 and
2099-12-31 - are explicit examples, so they run at every example count.

One example builds three times: twice at the drawn anchor date with the dataset's own seed,
for the first clause, and once more with a seed of its own, for the third. The second
clause compares the alias map against :data:`REFERENCE_ANCHOR`, built once per dataset, so
every example compares two different anchor dates without a fourth build. Each build goes
into a temporary directory of its own, as :meth:`~erp_mock.seed.builder.SeedBuilder.build`
asks, and the directory is thrown away once the three files and the records of the Snapshot
have been read into a :class:`Built`.

What a Snapshot holds is read here through a read-only connection of its own rather than
through :class:`~erp_mock.state.store.SandboxStore`, so the comparison of the third clause
does not go through the same decoding the build wrote the values with, and the bytes just
read stay the bytes on disk. Which values the dataset writes itself and which ones a
generator draws is read off the plan of the definition
(:func:`~erp_mock.seed.resolve.resolve`), whose ``Drawn`` is exactly "a value the build
draws from its random sequence".

Requirement 20.13 is about determinism and not about which anchor dates a dataset can be
built at, which is property 32 (requirement 21.12): where a build fails, the property asks
that the two builds fail the same way, and the example is then dropped from the clauses
about the files. A date that cannot be built is reported as a Hypothesis event, and a
dataset that fails at many dates makes the ``filter_too_much`` health check fail.

:data:`CASES` is the two datasets the property runs on, one parametrized run each: the
engine test fixture's own ``fx-basic`` and ``purchase-basic`` of ``instances/p2p-demo``, the
initial dataset of the example instance (requirement 21.2). An example builds three times,
which is about 0.3 seconds for either dataset on the development machine: the two runs
together take about a minute of wall clock (29 seconds for ``fx-basic`` and 32 for
``purchase-basic``) at the 100 examples a property of this suite declares by default
(tests/conftest.py), so both stay on that count.
"""

import json
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Final

import pytest
from hypothesis import assume, event, example, given, settings
from hypothesis import strategies as st

from erp_mock.config.instance import DatasetDef
from erp_mock.domain.numbering import FIRST_LINE_NO, line_id
from erp_mock.loader.errors import Step
from erp_mock.loader.loader import Loader
from erp_mock.seed.builder import (
    ALIASES_FILENAME,
    META_FILENAME,
    SNAPSHOT_FILENAME,
    SeedBuilder,
    SnapshotRef,
)
from erp_mock.seed.resolve import Drawn, PlanField, SeedError, SeedPlan, resolve
from erp_mock.state.schema import ID_COLUMN
from tests.helpers.fixtures import DATASET, INSTANCE_DIR, PACKS_DIR

# --- the datasets the property runs on ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One initial dataset of one instance configuration, built by this property.

    ``dataset`` is the name of the definition below ``<instance_dir>/datasets/``, and the
    two directories are the ones a :class:`~erp_mock.loader.loader.Loader` takes.
    """

    dataset: str
    instance_dir: Path
    packs_dir: Path


# The two datasets the property runs on, one parametrized run each (module docstring): the
# dataset of the engine test fixture and the one of the example instance of the repository.
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURE_CASE: Final = Case(dataset=DATASET, instance_dir=INSTANCE_DIR, packs_dir=PACKS_DIR)
P2P_CASE: Final = Case(
    dataset="purchase-basic",
    instance_dir=REPO_ROOT / "instances" / "p2p-demo",
    packs_dir=REPO_ROOT / "packs",
)
CASES: Final[tuple[Case, ...]] = (FIXTURE_CASE, P2P_CASE)

# The loading steps a build needs: step 7 is the last one whose result the read-only
# configuration object carries, and step 8 is the one that builds Snapshots itself, which
# this property does not go through (requirement 2.14).
BUILD_STEPS: Final[tuple[Step, ...]] = (1, 2, 3, 4, 5, 6, 7)

# The anchor date the read-only configuration object of a case is assembled with. Any date
# would do: it is written into the ``step8_anchor`` of that object, which no build reads -
# the anchor date of a build is the one handed to
# :meth:`~erp_mock.seed.builder.SeedBuilder.build`, which is the date an example draws. It
# is passed in rather than left out because an instance whose ``clock.mode`` is today, as
# p2p-demo's is, writes no anchor date of its own, and the current date is read by the
# gateway alone (requirements 2.14 and 25.10).
ASSEMBLY_ANCHOR: Final = date(2000, 1, 1)


@dataclass(frozen=True, slots=True)
class Target:
    """What building one dataset takes, assembled once per case.

    ``plan`` is the definition as the build walks it, which says of every written value
    whether a generator draws it (:class:`~erp_mock.seed.resolve.Drawn`) or the dataset
    writes it (requirement 20.3).
    """

    builder: SeedBuilder
    definition: DatasetDef
    file: str
    plan: SeedPlan

    @property
    def tables(self) -> tuple[str, ...]:
        """The tables of the instance that hold records, in table name order."""
        return tuple(table.name for table in self.builder.schema.diffable_tables)

    def line_table(self, bill: str) -> str:
        """The table the lines of the bill ``bill`` are in."""
        return self.builder.domain.bills[bill].line_name

    def seeded(self, random_seed: int | None) -> DatasetDef:
        """The definition with ``random_seed`` written into it, the dataset's own for None."""
        if random_seed is None:
            return self.definition
        return self.definition.model_copy(update={"random_seed": random_seed})


@cache
def target(case: Case) -> Target:
    """The configuration of ``case`` and its dataset definition, loaded once per case.

    The steps of :data:`BUILD_STEPS` run and their results are assembled under
    :data:`ASSEMBLY_ANCHOR`, which decides nothing about a build. Raises
    :class:`~erp_mock.loader.errors.ConfigErrors` at the first step that finds an error,
    which is a broken instance configuration, and StopIteration when the instance declares
    no dataset of that name.
    """
    loader = Loader(case.instance_dir, case.packs_dir, ASSEMBLY_ANCHOR)
    for step in BUILD_STEPS:
        loader.run_step(step)
    loaded = loader.assemble()
    document = next(doc for doc in loader.raw_datasets if doc.document.dataset == case.dataset)
    return Target(
        builder=SeedBuilder(loaded, case.instance_dir),
        definition=document.document,
        file=document.file,
        plan=resolve(
            document.document,
            loaded.domain,
            loaded.packs,
            file=document.file,
            instance_user=loaded.instance.current_user,
        ),
    )


# --- generators --------------------------------------------------------------------------

# The anchor dates a dataset has to be buildable at (requirement 21.12), which is the range
# the property draws from, and the three of them requirement 25.4 names.
ANCHORS: Final = st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31))
FIRST_DAY: Final = date(2000, 1, 1)
LEAP_DAY: Final = date(2000, 2, 29)
LAST_DAY: Final = date(2099, 12, 31)

# The anchor date the alias map of every other one is compared against (requirement 20.14):
# a date in the middle of the range and none of the three above, so every example of the
# property compares two anchor dates that differ.
REFERENCE_ANCHOR: Final = date(2050, 7, 15)

# The second ``random_seed`` of the third clause, drawn as the distance from the dataset's
# own seed so that it is never that seed again (requirement 20.3). Any value of the range
# is a seed a dataset could declare: ``random_seed`` is a non-negative integer.
SEED_SPACE: Final = 2**32
SEED_OFFSETS: Final = st.integers(min_value=1, max_value=SEED_SPACE - 1)


# --- one build ---------------------------------------------------------------------------

# One stored value of a Snapshot, as the sqlite3 driver hands it over: a decimal and a date
# are TEXT there, so two builds are compared on the text that is in the file.
type CellValue = str | int | float | bytes | None

# One compared value of the third clause: the column of one record of one table.
type Cell = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Built:
    """What one build produced, read before its directory is thrown away.

    ``db``, ``meta`` and ``aliases`` are the bytes of the three files, which the first two
    clauses compare, and ``rows`` the records of the Snapshot by table, internal id and
    column, which the third clause is read from.
    """

    db: bytes
    meta: bytes
    aliases: bytes
    rows: Mapping[str, Mapping[str, Mapping[str, CellValue]]]

    @property
    def alias_map(self) -> Mapping[str, str]:
        """The ``aliases`` of the alias map file: the internal id of each alias (20.10)."""
        aliases = json.loads(self.aliases)["aliases"]
        return {str(alias): str(record_id) for alias, record_id in aliases.items()}

    @property
    def ids(self) -> Mapping[str, tuple[str, ...]]:
        """Every internal id of the Snapshot, by table and in id order (requirement 20.3)."""
        return {table: tuple(records) for table, records in self.rows.items()}


def build_at(case: Case, anchor: date, random_seed: int | None = None) -> Built:
    """Build the dataset of ``case`` at ``anchor`` and read what the build wrote.

    ``random_seed`` replaces the seed of the definition; None builds the dataset as it is
    written. The build goes into a temporary directory of its own, which is removed once
    the three files and the records of the Snapshot have been read, so an example of this
    property leaves nothing behind. Raises :class:`~erp_mock.seed.resolve.SeedError` where
    the build fails.
    """
    at = target(case)
    with tempfile.TemporaryDirectory() as work:
        ref = at.builder.build(at.seeded(random_seed), anchor, Path(work) / "build", file=at.file)
        return read_built(ref, at.tables)


def build_or_error(case: Case, anchor: date, random_seed: int | None = None) -> Built | SeedError:
    """Build as :func:`build_at` does, returning the error of a build that failed.

    Which anchor dates a dataset can be built at is property 32; this property compares
    two builds, whether they got through or not (module docstring).
    """
    try:
        return build_at(case, anchor, random_seed)
    except SeedError as error:
        return error


def read_built(ref: SnapshotRef, tables: Sequence[str]) -> Built:
    """Read the three files of ``ref`` and the records of its Snapshot into memory."""
    return Built(
        db=ref.db.read_bytes(),
        meta=ref.meta.read_bytes(),
        aliases=ref.aliases.read_bytes(),
        rows=read_rows(ref.db, tables),
    )


def read_rows(db: Path, tables: Sequence[str]) -> dict[str, dict[str, dict[str, CellValue]]]:
    """Every record of each of ``tables`` of the Snapshot ``db``, by id and column.

    A read-only connection of the test's own (module docstring), so the file the bytes were
    read from is not written to and no sidecar file of a sandbox appears beside it.
    """
    uri = f"file:{db}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return {table: _records(connection, table) for table in tables}


def _records(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, CellValue]]:
    """Every record of ``table``, by internal id and in id order."""
    rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY {ID_COLUMN}').fetchall()
    return {str(row[ID_COLUMN]): dict(row) for row in rows}


@cache
def reference_aliases(case: Case) -> bytes:
    """The alias map file of ``case`` at :data:`REFERENCE_ANCHOR`, built once per case.

    What every other anchor date of that dataset is compared against (requirement 20.14).
    """
    return build_at(case, REFERENCE_ANCHOR).aliases


# --- what the dataset writes itself (requirement 20.3) -----------------------------------


def written_values(at: Target, built: Built) -> dict[Cell, CellValue]:
    """The value of every field the dataset of ``at`` writes itself, by table, id and column.

    The header fields and the line fields of the records of ``entities`` whose value the
    definition writes - a literal, a date expression, an alias or a code - leaving out the
    ones a generator draws, which are the values a second seed is allowed to move
    (requirement 20.3). The records of ``bulk`` write no value of their own and are left to
    the comparison of the internal ids.

    Raises KeyError where a record or a column the plan names is not in the Snapshot, which
    is this reader and the build disagreeing about what was created.
    """
    values: dict[Cell, CellValue] = {}
    aliases = built.alias_map
    for record in at.plan.records:
        record_id = aliases[record.alias]
        values |= _written(built, record.kind, record_id, record.fields)
        if not record.lines:
            continue
        table = at.line_table(record.kind)
        for number, line in enumerate(record.lines, start=FIRST_LINE_NO):
            values |= _written(built, table, line_id(record_id, number), line.fields)
    return values


def _written(
    built: Built, table: str, record_id: str, fields: Sequence[PlanField]
) -> dict[Cell, CellValue]:
    """The values of the fields of one record that are not drawn from the seed."""
    row = built.rows[table][record_id]
    return {
        (table, record_id, field.field.column): row[field.field.column]
        for field in fields
        if not isinstance(field.value, Drawn)
    }


# --- what a failed comparison reports ----------------------------------------------------


def difference(name: str, first: bytes, second: bytes) -> str:
    """Where two versions of the file ``name`` differ, as a message about them."""
    offset = next(
        (
            index
            for index, (one, other) in enumerate(zip(first, second, strict=False))
            if one != other
        ),
        min(len(first), len(second)),
    )
    return f"{name} 的两份内容不同：长度 {len(first)} 与 {len(second)}，第一个不同的字节在 {offset}"


def changes(first: Mapping[Cell, CellValue], second: Mapping[Cell, CellValue]) -> str:
    """The cells two builds disagree on, as a message about them."""
    both = dict(first) | dict(second)
    differing = {
        cell: (first.get(cell), second.get(cell)) for cell in both if _differs(first, second, cell)
    }
    return f"{len(differing)} 个字段值不同：{differing}"


def _differs(first: Mapping[Cell, CellValue], second: Mapping[Cell, CellValue], cell: Cell) -> bool:
    """Whether the two builds hold a different value for ``cell``."""
    return cell not in first or cell not in second or first[cell] != second[cell]


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 31: 数据集构建的确定性
@pytest.mark.req("20.3", "20.13", "20.14")
@pytest.mark.parametrize("case", CASES, ids=[case.dataset for case in CASES])
@settings(max_examples=100)
@given(anchor=ANCHORS, offset=SEED_OFFSETS)
@example(anchor=FIRST_DAY, offset=1)
@example(anchor=LEAP_DAY, offset=1)
@example(anchor=LAST_DAY, offset=1)
def test_a_build_is_decided_by_the_dataset_and_the_anchor_date(
    case: Case, anchor: date, offset: int
) -> None:
    at = target(case)
    first = build_or_error(case, anchor)
    second = build_or_error(case, anchor)

    # A dataset that cannot be built at this anchor date has to fail the same way twice;
    # the clauses about the files are about the dates that build (module docstring).
    if isinstance(first, SeedError) or isinstance(second, SeedError):
        assert isinstance(first, SeedError) and isinstance(second, SeedError), (
            f"锚点日期 {anchor} 下两次构建一次成功一次失败：{first}；{second}"
        )
        assert str(first) == str(second), f"两次构建的错误不同：{first}；{second}"
        event(f"{case.dataset} 在锚点日期 {anchor} 下构建失败")
        # The example is dropped here, so a dataset that fails at many anchor dates makes
        # the ``filter_too_much`` health check fail rather than passing unnoticed.
        assume(False)
        return

    # The same dataset at the same anchor date: the three files byte for byte (20.13).
    assert first.db == second.db, difference(SNAPSHOT_FILENAME, first.db, second.db)
    assert first.meta == second.meta, difference(META_FILENAME, first.meta, second.meta)
    assert first.aliases == second.aliases, difference(
        ALIASES_FILENAME, first.aliases, second.aliases
    )

    # Any two anchor dates: the alias map byte for byte (requirement 20.14).
    reference = reference_aliases(case)
    assert first.aliases == reference, difference(ALIASES_FILENAME, first.aliases, reference)

    # Only ``random_seed`` changed: the written field values, every internal id and the
    # alias map stay as they were (requirement 20.3). The requirement is about two builds
    # that both got through, so a seed this dataset cannot be built with is passed over.
    other = build_or_error(case, anchor, (at.definition.random_seed + offset) % SEED_SPACE)
    if isinstance(other, SeedError):
        event(f"{case.dataset} 在 random_seed 偏移 {offset} 下构建失败")
        return
    assert other.aliases == first.aliases, difference(
        ALIASES_FILENAME, first.aliases, other.aliases
    )
    assert other.ids == first.ids, f"内部 ID 变了：{first.ids} 与 {other.ids}"
    written, other_written = written_values(at, first), written_values(at, other)
    assert other_written == written, changes(written, other_written)
