"""The engine test fixture: the business packs and the instance config below tests/fixtures.

The fixture is a second, self-contained business configuration next to the example packs
of ``packs/``: the pack :data:`BASE_PACK` holds the basic data and :data:`TRADE_PACK`,
which depends on it, the bills. Every entity name and bill name differs from the ones the
example packs use (docs/packs/example-p2p.md), so a test that passes on both shows that
the engine takes those names from the configuration alone (requirement 1.10). What the
fixture covers, beyond the example packs' M0 subset:

- the org and the user context entity, a material-like entity with a base unit, a
  purchase unit and conversion records, a partner entity with ``has_status`` and a
  warehouse entity;
- both preset state machines, a bill with pricing and a bill that only declares quantity
  semantics;
- a push whose limit_by is qty (with ``auto_close_source_when``) and one whose limit_by is
  amount, from a simple bill, writing back ``linked_amount`` on the source header;
- the four inventory directions in, out, transfer and adjust;
- one enforced and one described rule, a declarative query with a filter, the inventory
  balance query, and ``exclude``, ``rename`` and ``source_short_names``.

:func:`load_fixture` runs the validation steps on it and returns the loader, so a test
reads the loaded configuration from the attributes the steps leave behind::

    loader = load_fixture()                  # steps 1 to 7
    [pack.name for pack in loader.raw_packs]
    load_fixture(CROSS, data_dir=tmp_path)   # step 8 and the cross-check as well

:func:`fixture_domain` gives the resolved domain model of the fixture, what step 5 builds
out of those documents (design.md 2.4)::

    domain = fixture_domain()
    domain.bills["supply_order"].lines["qty_base"].computed

:func:`fixture_loaded` gives the read-only configuration object of the fixture, what the
steps amount to (:class:`~erp_mock.loader.instance.LoadedInstance`)::

    loaded = fixture_loaded()
    loaded.default_profile.tool_names

The fixture is validated by tests/unit/loader/test_fixtures.py; the property tests of
requirement 25.2 and the tool generation tests use it as their second starting point.
"""

from datetime import date
from pathlib import Path
from typing import Final

from erp_mock.config.model import DomainModel
from erp_mock.loader.errors import CROSS, Step
from erp_mock.loader.instance import LoadedInstance
from erp_mock.loader.loader import STEPS, Loader

__all__ = [
    "ANCHOR_DATE",
    "BASE_PACK",
    "DATASET",
    "FIXTURES_DIR",
    "IMPLEMENTED_STEPS",
    "INSTANCE_DIR",
    "INSTANCE_NAME",
    "PACKS_DIR",
    "PACK_NAMES",
    "PROFILE",
    "TRADE_PACK",
    "fixture_domain",
    "fixture_loaded",
    "fixture_loader",
    "load_fixture",
]

# tests/fixtures/ and the two directories a Loader is given.
FIXTURES_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures"
PACKS_DIR: Final = FIXTURES_DIR / "packs"
INSTANCE_DIR: Final = FIXTURES_DIR / "instances" / "fx-demo"

# The instance config, its one profile and its one initial dataset, as instance.yaml
# names them, and the packs it references, in its order.
INSTANCE_NAME: Final = "fx-demo"
PROFILE: Final = "semantic"
DATASET: Final = "fx-basic"
BASE_PACK: Final = "fx_base"
TRADE_PACK: Final = "fx_trade"
PACK_NAMES: Final = (BASE_PACK, TRADE_PACK)

# The anchor date of the fixture: its clock mode is fixed, so every run of every step
# works on this date (requirement 16.3).
ANCHOR_DATE: Final = date(2026, 10, 9)

# The steps :func:`load_fixture` runs by default, in the order they run. Step 8 and the
# cross-check after it are not among them: they take up or build the Snapshot of every
# dataset and therefore need a data directory, which a caller passes as ``data_dir``
# (tests/unit/loader/test_step8.py runs them on a ``tmp_path``). Every other caller of the
# fixture works on the configuration alone, and loading it stays a matter of milliseconds.
IMPLEMENTED_STEPS: Final[tuple[Step, ...]] = (1, 2, 3, 4, 5, 6, 7)


def fixture_loader(anchor_date: date | None = None, data_dir: Path | None = None) -> Loader:
    """A loader for the fixture, with no step run yet.

    ``anchor_date`` is the date step 8 takes up the Snapshots under; None takes it from the
    clock configuration, which is the fixed :data:`ANCHOR_DATE`. ``data_dir`` is the data
    directory step 8 caches them below, which the steps before it do not need.
    """
    return Loader(INSTANCE_DIR, PACKS_DIR, anchor_date, data_dir)


def load_fixture(
    through: Step = IMPLEMENTED_STEPS[-1],
    anchor_date: date | None = None,
    data_dir: Path | None = None,
) -> Loader:
    """Run the validation steps on the fixture up to ``through`` and return the loader.

    The steps run in the order of :data:`~erp_mock.loader.loader.STEPS`, so the loader
    comes back with what each of them left behind (``instance``, ``packs``,
    ``raw_packs``). Raises :class:`~erp_mock.loader.errors.ConfigErrors` at the first step
    that finds an error, which is a broken fixture; ``through`` must be a step, and running
    step 8 or the cross-check needs ``data_dir``.
    """
    if through not in STEPS:
        raise ValueError(f"{through!r} 不是加载步骤")
    loader = fixture_loader(anchor_date, data_dir)
    for step in STEPS:
        loader.run_step(step)
        if step == through:
            break
    return loader


def fixture_domain() -> DomainModel:
    """The resolved domain model of the fixture, as step 5 builds it (design.md 2.4).

    Raises :class:`~erp_mock.loader.errors.ConfigErrors` at the first step that finds an
    error, which is a broken fixture.
    """
    domain = load_fixture(5).domain
    if domain is None:
        raise AssertionError("第 5 步没有产出领域模型")
    return domain


def fixture_loaded(anchor_date: date | None = None, data_dir: Path | None = None) -> LoadedInstance:
    """The read-only configuration object of the fixture (design.md 2.4).

    The steps of :data:`IMPLEMENTED_STEPS` are run and their results assembled, so the
    object is what the framework serves the fixture from, short of the datasets step 8 takes
    up; passing ``data_dir`` runs that step and the cross-check as well, and the object then
    carries one :class:`~erp_mock.loader.instance.DatasetModel` per dataset.
    ``anchor_date`` is passed to the loader as the CLI would pass ``--anchor-date``; it is
    the date the Snapshots are taken up under (``step8_anchor``) and nothing else the object
    carries depends on it. Raises :class:`~erp_mock.loader.errors.ConfigErrors` at the first
    step that finds an error, which is a broken fixture.
    """
    through: Step = IMPLEMENTED_STEPS[-1] if data_dir is None else CROSS
    return load_fixture(through, anchor_date=anchor_date, data_dir=data_dir).assemble()
