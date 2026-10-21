"""One starting point of the random operation sequences: a snapshot file and its instance.

A :class:`Start` is everything the sequences of requirement 25.2 need before the first
call: the Snapshot file every example is copied from, the loaded configuration of the
instance it belongs to, the :class:`~erp_mock.tools.layer.ToolLayer` the calls go through,
and the write tools of that configuration sorted into what the generator picks from
(:class:`BillTools`, :class:`PushTools`). It is built once per test module and shared by
every example, which copies a sandbox of its own from the snapshot
(:func:`~tests.property.opseq.session.open_sandbox`)::

    start = fixture_start(tmp_path_factory.mktemp("opseq"))   # once per module
    with open_sandbox(start, parent) as session:              # once per example
        ...

Two factories. :func:`start_from` takes a snapshot file and a
:class:`~erp_mock.loader.instance.LoadedInstance` and nothing else, so any Snapshot of any
instance is a starting point: task 19.8 passes the ``purchase-basic`` Snapshot of
``p2p-demo`` and the second starting point requirement 25.2 asks for.
:func:`fixture_start` is the starting point of this task: it writes the standard basic data
of :mod:`tests.helpers.engine_env` out as a snapshot file - built the way the Snapshot
cache builds one, through a build database and ``VACUUM INTO`` (design.md 6.1) - and
returns the :class:`Start` over it.

Why a snapshot file rather than a sandbox with the data created in it: an example that
needs its own sandbox gets it by copying pages (:func:`~erp_mock.state.snapshot.copy_snapshot`,
the backup API), not by running the creates again, so 500 sequences cost one build and 500
copies; and two sandboxes copied from one file start in exactly the same state, which is
what property 19 compares error results in (task 16.3).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import BillModel, ConversionModel, DomainModel
from erp_mock.domain.engine import Engine
from erp_mock.domain.state_machine import TRANSITIONS, StateAction
from erp_mock.loader.instance import LoadedInstance
from erp_mock.state.schema import StorageSchema, build_schema
from erp_mock.state.snapshot import build_snapshot_db, finalize_snapshot
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import ID_PARAM
from erp_mock.tools.layer import STATE_ACTIONS, ToolLayer, source_short_name
from tests.helpers.engine_env import ANCHOR_DATE, BASIC_DATA, BasicRecord, create_basic_data
from tests.helpers.fixtures import fixture_loaded

__all__ = [
    "BUILD_DB_NAME",
    "FIXTURE_START",
    "SNAPSHOT_NAME",
    "BillTools",
    "PushTools",
    "Start",
    "fixture_start",
    "instance_anchor_date",
    "start_from",
    "write_snapshot",
]

# The name :func:`fixture_start` gives its starting point, which a test reports in the
# parametrisation of a property (design.md property 17).
FIXTURE_START: Final = "fx-basic"

# The two files :func:`write_snapshot` works with, below the working directory it is given:
# the build database the records are created in and the snapshot written out of it
# (design.md 6.1).
BUILD_DB_NAME: Final = "build.sqlite"
SNAPSHOT_NAME: Final = "snapshot.sqlite"


# --- the write tools of one bill and of one conversion (requirement 11.1) ----------------


@dataclass(frozen=True, slots=True)
class BillTools:
    """The write tools one bill of the profile exposes, by the operation they perform.

    Every field is None, or empty, when the profile exposes no such tool: a pack may leave
    an operation out of its ``operations`` and a profile may ``exclude`` a generated tool,
    so the generator offers exactly the operations the surface actually has
    (requirements 11.1 and 11.10). ``states`` holds the state operations of the bill's own
    machine, keyed by the action (:data:`~erp_mock.domain.state_machine.TRANSITIONS`).
    """

    bill: BillModel
    create: ToolSpec | None = None
    update: ToolSpec | None = None
    delete: ToolSpec | None = None
    get: ToolSpec | None = None
    states: FrozenMap[StateAction, ToolSpec] = FrozenMap()

    @property
    def name(self) -> str:
        """The name of the bill, which is the name of its table (design.md 3.1)."""
        return self.bill.name

    def pre_state(self, action: StateAction) -> str | None:
        """The state ``action`` starts from in this bill's machine, None when it has none."""
        found = TRANSITIONS.get((self.bill.state_machine, action))
        return None if found is None else found.pre


@dataclass(frozen=True, slots=True)
class PushTools:
    """The push tool of one conversion, with the two bills it runs between (req 8.1).

    ``source_param`` is the ``<short name>_id`` parameter the tool names its source bill
    under, which the generator sends the source bill in and which an error of that
    parameter reports (requirement 11.13).
    """

    conversion: ConversionModel
    source: BillModel
    target: BillModel
    spec: ToolSpec
    source_param: str


# --- one starting point ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Start:
    """A snapshot file, the instance it belongs to and the tools layer of that instance.

    - ``name``: how a test reports the starting point, the name of the initial dataset
      (:data:`FIXTURE_START`, ``purchase-basic``);
    - ``snapshot``: the Snapshot file every example is copied from (module docstring);
    - ``loaded``: the read-only configuration of the instance (design.md 2.4);
    - ``layer``: the tools layer every call goes through, with the resource bounds of a
      plain process - the sequences are let through by the probe instead
      (:class:`~tests.property.opseq.session.BlankProbe`);
    - ``schema``: the tables of the instance, which the snapshot and every sandbox hold;
    - ``anchor_date`` and ``current_user_id``: the business date and the current user of
      every call (requirements 16.2 and 1.8);
    - ``tools``: every tool the default profile exposes, by the name tools/list returns
      (requirement 11.9);
    - ``bills`` and ``pushes``: the write tools of the surface, sorted by what they
      perform (:class:`BillTools`, :class:`PushTools`). The M0 tool list gives an entity
      ``search`` and ``get`` and nothing else (docs/packs/example-p2p.md section 8), so
      every write tool of every instance is a bill's or a push, and an instance that
      exposed the create of an entity record would need candidates of its own here;
    - ``aliases``: the internal id of each record of the starting point under the alias it
      was declared with, which is what ``aliases.json`` of a Snapshot holds
      (requirement 20.10); a test that wants to name a record of the start reads it here.
    """

    name: str
    snapshot: Path
    loaded: LoadedInstance
    layer: ToolLayer
    schema: StorageSchema
    anchor_date: date
    current_user_id: str | None
    tools: FrozenMap[str, ToolSpec] = FrozenMap()
    bills: tuple[BillTools, ...] = ()
    pushes: tuple[PushTools, ...] = ()
    aliases: FrozenMap[str, str] = FrozenMap()

    @property
    def domain(self) -> DomainModel:
        """The resolved configuration of the instance (design.md 2.4)."""
        return self.layer.domain

    @property
    def engine(self) -> Engine:
        """The engine the domain steps of every call run through (design.md 4.1)."""
        return self.layer.engine

    def spec(self, tool: str) -> ToolSpec:
        """The tool ``tool`` of the profile; raises KeyError when it exposes no such tool."""
        return self.tools[tool]

    def id(self, alias: str) -> str:
        """The internal id the starting point gives the record ``alias`` (req 20.10)."""
        return self.aliases[alias]


def start_from(
    snapshot: Path,
    loaded: LoadedInstance,
    *,
    name: str,
    anchor_date: date | None = None,
    current_user_id: str | None = None,
    aliases: Mapping[str, str] = FrozenMap(),
) -> Start:
    """The starting point of the sequences over ``snapshot`` and the instance ``loaded``.

    ``anchor_date`` is the date the snapshot was built under, which every call then works
    on (requirement 16.2); None takes the one the instance configures
    (:func:`instance_anchor_date`). ``current_user_id`` is the current user of the calls;
    None takes the ``current_user`` of instance.yaml, which is what a sandbox of that
    instance has (requirement 1.8). ``aliases`` is the alias map of the snapshot, empty
    for a starting point whose records no test names.

    The snapshot file itself is not read here, so the path may be written before the file
    is: :func:`fixture_start` builds the start first and then writes the snapshot it names.
    """
    layer = ToolLayer(loaded)
    specs = loaded.default_profile.tools
    return Start(
        name=name,
        snapshot=snapshot,
        loaded=loaded,
        layer=layer,
        schema=build_schema(loaded.domain),
        anchor_date=anchor_date if anchor_date is not None else instance_anchor_date(loaded),
        current_user_id=(
            current_user_id if current_user_id is not None else loaded.instance.current_user
        ),
        tools=FrozenMap((spec.name, spec) for spec in specs),
        bills=_bill_tools(loaded.domain, specs),
        pushes=_push_tools(loaded.domain, specs),
        aliases=FrozenMap(aliases),
    )


def instance_anchor_date(loaded: LoadedInstance) -> date:
    """The anchor date the clock of ``loaded`` configures (requirement 16.3).

    Raises ValueError for an instance that configures none, whose sandboxes take the date
    of the system clock: a test of requirement 25.10 runs on any system date, so its
    starting point must name the date it was built under instead.
    """
    text = loaded.instance.clock.anchor_date
    if text is None:
        raise ValueError(f"实例 {loaded.instance.instance} 没有配置锚点日期，请显式传入")
    return date.fromisoformat(text)


# --- the fixture starting point (tests.helpers.engine_env) -------------------------------


def fixture_start(workdir: Path, *, records: Sequence[BasicRecord] = BASIC_DATA) -> Start:
    """The starting point over the standard basic data, written out below ``workdir``.

    The records of ``records`` are created through the engine of the fixture's instance,
    one create per record as a seed build runs them (requirement 20.17), and the snapshot
    is written out of the build database. ``workdir`` is a directory of a test's own, a
    ``tmp_path_factory.mktemp`` of the module that uses the start; call it once per module
    and copy a sandbox per example (module docstring).

    Task 19.8 replaces this with the Snapshot of the ``purchase-basic`` dataset and the one
    of a test business pack, both through :func:`start_from`; nothing but this function and
    its ``records`` knows the fixture.
    """
    start = start_from(
        workdir / "snapshots" / SNAPSHOT_NAME,
        fixture_loaded(),
        name=FIXTURE_START,
        anchor_date=ANCHOR_DATE,
    )
    aliases = write_snapshot(start, workdir / "build" / BUILD_DB_NAME, records=records)
    return replace(start, aliases=aliases)


def write_snapshot(
    start: Start, build_db: Path, *, records: Sequence[BasicRecord] = BASIC_DATA
) -> FrozenMap[str, str]:
    """Create ``records`` in a build database and write the snapshot of ``start`` out of it.

    The two steps of a Snapshot build (design.md 6.1): the records go into a database that
    asserts the pragmas the output's bytes depend on
    (:func:`~erp_mock.state.snapshot.build_snapshot_db`) and ``VACUUM INTO`` writes the
    file every example is copied from (:func:`~erp_mock.state.snapshot.finalize_snapshot`).
    Returns the internal id of each record under the alias it was declared with, which
    becomes the ``aliases`` of the start.
    """
    store = build_snapshot_db(build_db, start.schema)
    try:
        aliases = create_basic_data(
            start.engine, store, anchor_date=start.anchor_date, records=records
        )
    finally:
        store.close()
    finalize_snapshot(build_db, start.snapshot)
    return aliases


# --- sorting the tools of the surface (requirement 11.1) ---------------------------------


def _bill_tools(domain: DomainModel, specs: Sequence[ToolSpec]) -> tuple[BillTools, ...]:
    """The write tools of each bill of ``domain``, in the model order of the bills.

    A bill the profile exposes no write tool of is left out: nothing of the sequences can
    be done to it (requirement 11.10).
    """
    found: list[BillTools] = []
    for bill in domain.bills.values():
        of_bill = [spec for spec in specs if spec.kind == "entity_op" and spec.target == bill.name]
        tools = BillTools(
            bill=bill,
            create=_action_of(of_bill, "create"),
            update=_action_of(of_bill, "update"),
            delete=_action_of(of_bill, "delete"),
            get=_action_of(of_bill, "get"),
            states=FrozenMap(
                (action, spec)
                for spec in of_bill
                if (action := STATE_ACTIONS.get(spec.action)) is not None
            ),
        )
        if _writes_anything(tools):
            found.append(tools)
    return tuple(found)


def _push_tools(domain: DomainModel, specs: Sequence[ToolSpec]) -> tuple[PushTools, ...]:
    """The push tool of each conversion the profile exposes one of (requirement 11.1)."""
    found: list[PushTools] = []
    for spec in specs:
        conversion = domain.conversions.get(spec.target) if spec.kind == "push" else None
        if conversion is None:
            continue
        found.append(
            PushTools(
                conversion=conversion,
                source=domain.bills[conversion.source],
                target=domain.bills[conversion.target],
                spec=spec,
                source_param=f"{source_short_name(spec)}_{ID_PARAM}",
            )
        )
    return tuple(found)


def _action_of(specs: Sequence[ToolSpec], action: str) -> ToolSpec | None:
    """The tool of ``specs`` whose action is ``action``, None when there is none."""
    return next((spec for spec in specs if spec.action == action), None)


def _writes_anything(tools: BillTools) -> bool:
    """Whether the profile exposes a write tool of the bill (requirement 25.2)."""
    return any([tools.create, tools.update, tools.delete, tools.states])
