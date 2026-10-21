"""An empty sandbox over the engine test fixture, with one standard set of basic data.

The domain tests and the tool tests of the write primitives all need the same starting
point: a sandbox database built from the tables of the engine test fixture
(:mod:`tests.helpers.fixtures`) and a handful of basic data records to point a bill at.
:func:`engine_env` is that starting point, and it builds the records the way a sandbox
gets them - through :class:`~erp_mock.domain.engine.Engine`, one create per record - so a
test works on a sandbox no different from one a seed build produced (requirement 20.17)::

    with engine_env(tmp_path / "sandbox") as env:
        result = env.create("supply_order", {"vendor_id": env.id(VENDOR), ...})
        env.update("supply_order", "so_000001", {"remark": "改期"})
        env.transition("supply_order", "so_000001", "submit")
        env.push("supply_order_to_entry", "so_000001", [{"source_line_id": "so_000001-1", ...}])
        env.record("supply_order", "so_000001")

What the environment holds:

- :attr:`EngineEnv.store`, the open sandbox, and :attr:`EngineEnv.engine`, the engine of
  the fixture's instance; :attr:`EngineEnv.domain` and :attr:`EngineEnv.schema` are the
  resolved configuration and its tables, both built once per test session;
- :attr:`EngineEnv.ids`, the internal id of each record of :data:`BASIC_DATA` under the
  alias it was declared with, read through :meth:`EngineEnv.id`;
- :attr:`EngineEnv.operations`, how many write operations the setup ran, so a test knows
  which operation sequence number its own first write gets (requirement 4.11).

The basic data is the set of :data:`BASIC_DATA`: the organization context entity and the
user context entity, two units with a conversion record between them, two materials - one
with a purchase unit of its own, one that takes the base unit as its purchase unit through
``default_from`` - two warehouses and three partners, of which :data:`RETIRED_VENDOR` is
disabled, so a test has a reference that breaks R-GEN-006 (requirement 5.7). The records
are created in the order of the list and their internal ids and codes are therefore fixed:
the aliases of this module are the ids the fixture's own initial dataset gives the same
records (tests/fixtures/instances/fx-demo/datasets/fx-basic.yaml), so a test may be read
against either.

The environment runs its creates with no trace sequence number, as a seed build does, so
the effective calls of the sandbox stay empty until a test makes a call of its own
(requirement 18.10). :meth:`EngineEnv.run` and :meth:`EngineEnv.create` take a
``call_seq`` for the tests that need one.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import DomainModel
from erp_mock.domain.engine import (
    Arguments,
    CreateOp,
    DeleteOp,
    Engine,
    Lines,
    Operation,
    OperationContext,
    OpResult,
    PushOp,
    StateAction,
    TransitionOp,
    UpdateOp,
)
from erp_mock.domain.views import LINES, PageView, QueryView, RecordView, RefSummary
from erp_mock.state.schema import COUNTERS_TABLE, StorageSchema, build_schema
from erp_mock.state.store import Record, SandboxStore, Value
from tests.helpers.fixtures import ANCHOR_DATE, fixture_domain

__all__ = [
    "ANCHOR_DATE",
    "BASIC_DATA",
    "BOLT",
    "BOLT_PER_BOX",
    "BOX",
    "BOX_FACTOR",
    "BUYER",
    "CURRENT_USER",
    "KEEPER",
    "MAIN_BRANCH",
    "MAIN_DEPOT",
    "MAIN_VENDOR",
    "PCS",
    "RETIRED_VENDOR",
    "SPARE_BRANCH",
    "SPARE_DEPOT",
    "SPARE_VENDOR",
    "WASHER",
    "Alias",
    "BasicRecord",
    "EngineEnv",
    "arguments",
    "as_lines",
    "as_page",
    "as_record",
    "as_summary",
    "create_basic_data",
    "empty_store",
    "engine_env",
    "fixture_engine",
    "fixture_model",
    "fixture_schema",
    "line_arguments",
    "record_lines",
    "sandbox_counters",
    "summary_of",
]

# The aliases of the basic data; a test names a record by the alias and reads its internal
# id from the environment (:meth:`EngineEnv.id`).
MAIN_BRANCH: Final = "branch:main"
SPARE_BRANCH: Final = "branch:spare"
MAIN_DEPOT: Final = "depot:main"
SPARE_DEPOT: Final = "depot:spare"
PCS: Final = "measure:pcs"
BOX: Final = "measure:box"
BUYER: Final = "operator:buyer"
KEEPER: Final = "operator:keeper"
MAIN_VENDOR: Final = "vendor:main"
SPARE_VENDOR: Final = "vendor:spare"
RETIRED_VENDOR: Final = "vendor:retired"
BOLT: Final = "article:bolt"
WASHER: Final = "article:washer"
BOLT_PER_BOX: Final = "measure_link:bolt_box"

# How many base units one box of :data:`BOLT` holds, the factor of its conversion record
# (requirement 6.6).
BOX_FACTOR: Final = Decimal("12")


@dataclass(frozen=True, slots=True)
class Alias:
    """A reference to another record of the basic data, by its alias.

    The internal ids are assigned while the records are created, so a reference between
    them is written as an alias here and resolved when the record it belongs to is created.
    """

    alias: str


@dataclass(frozen=True, slots=True)
class BasicRecord:
    """One record of the basic data: its alias, its entity and what it is created with.

    ``values`` are the arguments of the create, every reference among them an
    :class:`Alias`; a field left out takes the default of the configuration
    (requirement 5.1).
    """

    alias: str
    entity: str
    values: Mapping[str, Value | Alias]


# The basic data of the environment, in the order it is created, which fixes every
# internal id and every generated code. The order is the one a seed build uses: the shared
# data of the pack first, by file path (branch, depot, measure, operator), then the records
# of a dataset (requirement 20.17). :data:`SPARE_BRANCH` is created last on purpose, so
# the ids of the records before it stay the ones the fixture's own dataset gives them.
BASIC_DATA: Final[tuple[BasicRecord, ...]] = (
    # br_000001: the default organization, the one record whose default flag is true
    # (requirement 1.8).
    BasicRecord(
        MAIN_BRANCH, "branch", {"code": "BR01", "name": "夹具核算本部", "is_primary": True}
    ),
    # dep_000001 and dep_000002, codes D001 and D002 from the entity's code rule.
    BasicRecord(MAIN_DEPOT, "depot", {"name": "夹具主料仓", "branch_id": Alias(MAIN_BRANCH)}),
    BasicRecord(SPARE_DEPOT, "depot", {"name": "夹具辅料仓", "branch_id": Alias(MAIN_BRANCH)}),
    # msr_000001 and msr_000002: the base unit and the alternative unit of the materials.
    BasicRecord(PCS, "measure", {"code": "PCS", "name": "夹具基本件"}),
    BasicRecord(BOX, "measure", {"code": "BOX", "name": "夹具标准箱"}),
    # opr_000001, the current user of the environment, and opr_000002.
    BasicRecord(
        BUYER,
        "operator",
        {"code": "OP01", "name": "夹具采购员", "branch_id": Alias(MAIN_BRANCH), "duty": "buyer"},
    ),
    BasicRecord(
        KEEPER,
        "operator",
        {"code": "OP02", "name": "夹具库管员", "branch_id": Alias(MAIN_BRANCH), "duty": "keeper"},
    ),
    # ven_000001 to ven_000003, codes V0001 to V0003. The second one takes the declared
    # defaults of pay_days and default_tax_rate, the third is disabled (requirement 5.7).
    BasicRecord(
        MAIN_VENDOR,
        "vendor",
        {
            "name": "夹具主力供货单位",
            "short_name": "夹具主力",
            "contact": "夹具联系人",
            "pay_days": 45,
        },
    ),
    BasicRecord(SPARE_VENDOR, "vendor", {"name": "夹具备用供货单位", "short_name": "夹具备用"}),
    BasicRecord(RETIRED_VENDOR, "vendor", {"name": "夹具停用供货单位", "status": "disabled"}),
    # art_000001, code A00001: base unit PCS, purchase unit BOX, tax rate 13%.
    BasicRecord(
        BOLT,
        "article",
        {
            "name": "夹具用六角螺栓",
            "spec": "M8X40",
            "grade": "plain",
            "base_measure_id": Alias(PCS),
            "buy_measure_id": Alias(BOX),
            "tax_rate": Decimal("0.13"),
        },
    ),
    # mlk_000001: one box of the bolt holds BOX_FACTOR base units (requirement 6.6).
    BasicRecord(
        BOLT_PER_BOX,
        "measure_link",
        {"article_id": Alias(BOLT), "alt_measure_id": Alias(BOX), "ratio": BOX_FACTOR},
    ),
    # art_000002, code A00002: no purchase unit of its own, so default_from takes the base
    # unit for it (requirement 5.1).
    BasicRecord(
        WASHER,
        "article",
        {
            "name": "夹具用平垫圈",
            "spec": "M8",
            "grade": "fine",
            "base_measure_id": Alias(PCS),
            "tax_rate": Decimal("0.09"),
        },
    ),
    # br_000002: a second organization, which is not the default one.
    BasicRecord(
        SPARE_BRANCH, "branch", {"code": "BR02", "name": "夹具备用本部", "is_primary": False}
    ),
)

# The id of the current user of the environment: the first operator, which is also the
# ``current_user`` of the fixture's instance configuration (requirement 1.8).
CURRENT_USER: Final = "opr_000001"


@cache
def fixture_model() -> DomainModel:
    """The resolved configuration of the fixture, built once per test session (2.4).

    The loading steps are run once here, so an environment per test costs one empty
    database and the creates of its basic data and nothing else.
    """
    return fixture_domain()


@cache
def fixture_schema() -> StorageSchema:
    """The tables of the fixture's instance, built once per test session (design.md 3.1)."""
    return build_schema(fixture_model())


@cache
def fixture_engine() -> Engine:
    """The engine of the fixture's instance, built once per test session."""
    return Engine(fixture_model())


@dataclass(frozen=True, slots=True)
class EngineEnv:
    """An open sandbox with the basic data of :data:`BASIC_DATA` (see the module docstring)."""

    engine: Engine
    store: SandboxStore
    ids: FrozenMap[str, str]
    operations: int
    anchor_date: date = ANCHOR_DATE
    current_user_id: str | None = CURRENT_USER

    @property
    def domain(self) -> DomainModel:
        """The resolved configuration of the fixture's instance (design.md 2.4)."""
        return self.engine.domain

    @property
    def schema(self) -> StorageSchema:
        """The tables of the sandbox (design.md 3.1)."""
        return self.store.schema

    def id(self, alias: str) -> str:
        """The internal id of the basic data record ``alias`` (:data:`BASIC_DATA`)."""
        return self.ids[alias]

    def context(self, call_seq: int | None = None) -> OperationContext:
        """The operation context of one call: the anchor date and the current user."""
        return OperationContext(
            anchor_date=self.anchor_date,
            current_user_id=self.current_user_id,
            call_seq=call_seq,
        )

    def run(self, op: Operation, call_seq: int | None = None) -> OpResult:
        """Run ``op`` against the sandbox and return its result.

        ``call_seq`` is the trace sequence number the records it writes are recorded under;
        None leaves the effective calls of the sandbox alone (requirement 18.10).
        """
        return self.engine.run(self.store, self.context(call_seq), op)

    def run_with(self, engine: Engine, op: Operation, call_seq: int | None = None) -> OpResult:
        """Run ``op`` against this sandbox through ``engine`` rather than its own.

        For a test that varies the configuration of the instance without varying its
        tables - a business pack rule the fixture does not declare, for one: an engine over
        such a model stores its records in the same schema, so the sandbox of the
        environment serves it as it stands (``Engine(replace(env.domain, rules=...))``).
        """
        return engine.run(self.store, self.context(call_seq), op)

    def create(
        self,
        target: str,
        values: Mapping[str, Value] = FrozenMap(),
        lines: Sequence[Mapping[str, Value]] | None = None,
        call_seq: int | None = None,
    ) -> OpResult:
        """Create one record of ``target`` with ``values`` and ``lines`` (requirement 5.1)."""
        return self.run(CreateOp(target, arguments(values), line_arguments(lines)), call_seq)

    def update(
        self,
        target: str,
        record_id: str,
        values: Mapping[str, Value] = FrozenMap(),
        lines: Sequence[Mapping[str, Value]] | None = None,
        call_seq: int | None = None,
    ) -> OpResult:
        """Change the record ``record_id`` of ``target`` (requirements 5.2 and 5.3).

        ``lines`` is None for a call that carries no lines, which leaves the lines of a
        bill as they are, and a sequence for one that replaces them as a whole.
        """
        return self.run(
            UpdateOp(target, record_id, arguments(values), line_arguments(lines)), call_seq
        )

    def delete(self, target: str, record_id: str, call_seq: int | None = None) -> OpResult:
        """Delete the record ``record_id`` of ``target`` (requirement 5.5)."""
        return self.run(DeleteOp(target, record_id), call_seq)

    def transition(
        self,
        target: str,
        record_id: str,
        action: StateAction,
        call_seq: int | None = None,
    ) -> OpResult:
        """Run the state operation ``action`` on the bill ``record_id`` (req 7.1, 7.2)."""
        return self.run(TransitionOp(target, record_id, action), call_seq)

    def push(
        self,
        conversion: str,
        source_id: str,
        lines: Sequence[Mapping[str, Value]] = (),
        call_seq: int | None = None,
        source_name: str | None = None,
    ) -> OpResult:
        """Push ``source_id`` through ``conversion`` and return the draft (req 8.1).

        ``source_name`` is the short name the push tool of the conversion names its source
        bill under, which the field path of that parameter is built from
        (requirement 11.13); None takes the name of the source bill itself.
        """
        return self.run(
            PushOp(conversion, source_id, tuple(arguments(line) for line in lines), source_name),
            call_seq,
        )

    def record(self, table: str, record_id: str) -> Record | None:
        """The record ``record_id`` of ``table`` as storage holds it, None when absent."""
        with self.store.transaction() as uow:
            return uow.get(table, record_id)

    def records(self, table: str) -> list[Record]:
        """Every record of ``table``, by id (:meth:`~erp_mock.state.store.UnitOfWork.select`)."""
        with self.store.transaction() as uow:
            return uow.select(table)

    def counters(self) -> dict[tuple[str, str], int]:
        """The counters the sandbox holds, keyed by (kind, key) (requirement 4.6)."""
        return sandbox_counters(self.store)


def arguments(values: Mapping[str, Value]) -> Arguments:
    """``values`` as the arguments of one level of an operation (design.md 4.1)."""
    return FrozenMap(values)


def line_arguments(lines: Sequence[Mapping[str, Value]] | None) -> Lines | None:
    """``lines`` as the arguments of the lines of a bill, None when a call carries none."""
    return None if lines is None else tuple(arguments(line) for line in lines)


def empty_store(sandbox_dir: Path, schema: StorageSchema | None = None) -> SandboxStore:
    """An open, empty sandbox with the tables of the fixture; every counter is 0 (req 4.6).

    The caller closes it, which :func:`engine_env` does for a test that needs the basic
    data as well.
    """
    store = SandboxStore(sandbox_dir, schema if schema is not None else fixture_schema())
    store.open()
    try:
        store.create_tables()
    except Exception:
        store.close()
        raise
    return store


def create_basic_data(
    engine: Engine,
    store: SandboxStore,
    *,
    anchor_date: date = ANCHOR_DATE,
    records: Sequence[BasicRecord] = BASIC_DATA,
) -> FrozenMap[str, str]:
    """Create ``records`` through ``engine`` and return their ids by alias (req 20.17).

    One create per record, in the order of the list, each in a transaction of its own as a
    write operation is; a reference written as an :class:`Alias` is resolved to the id of
    the record it names, which was created earlier in the list.
    """
    ctx = OperationContext(anchor_date=anchor_date)
    ids: dict[str, str] = {}
    for basic in records:
        values = arguments(
            {
                name: ids[value.alias] if isinstance(value, Alias) else value
                for name, value in basic.values.items()
            }
        )
        view = as_record(engine.run(store, ctx, CreateOp(basic.entity, values)).view)
        ids[basic.alias] = _record_id(basic, view)
    return FrozenMap(ids)


@contextmanager
def engine_env(
    sandbox_dir: Path,
    *,
    anchor_date: date = ANCHOR_DATE,
    current_user_id: str | None = CURRENT_USER,
    records: Sequence[BasicRecord] = BASIC_DATA,
) -> Iterator[EngineEnv]:
    """An open sandbox with the basic data, closed again when the block returns.

    ``sandbox_dir`` is where the database file goes, a directory below a test's
    ``tmp_path``; ``records`` is the basic data to create, :data:`BASIC_DATA` by default
    and an empty sequence for a test that wants the tables alone.
    """
    engine = fixture_engine()
    store = empty_store(sandbox_dir)
    with closing(store):
        ids = create_basic_data(engine, store, anchor_date=anchor_date, records=records)
        yield EngineEnv(
            engine=engine,
            store=store,
            ids=ids,
            operations=len(records),
            anchor_date=anchor_date,
            current_user_id=current_user_id,
        )


def sandbox_counters(store: SandboxStore) -> dict[tuple[str, str], int]:
    """The counters ``store`` holds, keyed by (kind, key) (requirement 4.6)."""
    with store.transaction() as uow:
        rows = uow.select(COUNTERS_TABLE)
    return {(str(row["kind"]), str(row["key"])): int(str(row["value"])) for row in rows}


# --- reading a view (design.md 4.1) ------------------------------------------------------


def as_record(view: RecordView | PageView | QueryView) -> RecordView:
    """The view as the record it is: a read or a write of one record returns a RecordView."""
    assert isinstance(view, RecordView)
    return view


def as_summary(value: object) -> RefSummary:
    """The value as the reference summary it is (requirement 13.3)."""
    assert isinstance(value, RefSummary)
    return value


def as_page(view: RecordView | PageView | QueryView) -> PageView:
    """The view as the page it is: a search and the stock balance query return one (5.11)."""
    assert isinstance(view, PageView)
    return view


def as_lines(value: object) -> tuple[RecordView, ...]:
    """The value as the array of line objects it is (requirement 13.2)."""
    assert isinstance(value, tuple)
    for line in value:
        assert isinstance(line, RecordView)
    return value


def summary_of(view: RecordView, key: str) -> dict[str, Value]:
    """The nested object the reference ``key`` of ``view`` returns (requirement 13.3)."""
    return dict(as_summary(view.fields[key]).fields)


def record_lines(view: RecordView) -> tuple[RecordView, ...]:
    """The lines of the bill ``view`` returns, by line number (requirement 5.12)."""
    return as_lines(view.fields[LINES])


def _record_id(basic: BasicRecord, view: RecordView) -> str:
    """The internal id the create of ``basic`` assigned (requirement 4.1)."""
    record_id = view.fields["id"]
    assert isinstance(record_id, str), f"the create of {basic.alias} returned no id"
    return record_id
