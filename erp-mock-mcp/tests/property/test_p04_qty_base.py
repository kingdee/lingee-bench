"""Property 4: the base unit quantity of a line.

**Validates: Requirements 6.6**

For any material with a random base unit and random conversion records, any unit of a line
and any quantity, the ``qty_base`` a write stores is ``round(qty × factor, 4)``, where the
factor is 1 when the unit of the line is the base unit of its material and the factor of
the material's conversion record for that unit otherwise; a line whose material, unit or
quantity is empty keeps ``qty_base`` empty.

The reference keeps the factor and the quantity as ``fractions.Fraction``, so the product
is exact and the only rounding is the one to the four decimals of a quantity, ROUND_HALF_UP
- independently of how :func:`~erp_mock.domain.unit_conversion.qty_base` reaches it, which
multiplies in a wide Decimal context. The two are compared as exact rationals, and every
value a write stores carries the four decimals the rounding leaves.

What is drawn, per example: a world of up to three materials, each with a base unit out of
a small pool of units or none at all, and with conversion records for any subset of that
pool, each with its own factor; then the lines of one bill, each naming a material of the
world or none, a unit of the pool or none, and a quantity or none. The pool is small on
purpose, so the cases where the factor is decided come up often: a unit that is the base
unit of its material, a unit the material converts, a unit it does neither with - the line
requirement 6.7 rejects the write over, whose ``qty_base`` stays empty - and the same unit
reading differently for two materials. A material may also carry a record for its own base
unit, which requirement 6.11 keeps a write from storing and which the base unit overrides
either way, as requirement 6.6 orders the two clauses.

Quantities and factors are both quantity fields: 0.0001 to 100000000 with at most 4
decimals (the range of requirement 6.5), drawn at that precision and with the trailing
zeros of the fraction dropped, which is the same value written differently. Half the draws
aim at the rounding: an odd multiple of 0.5 times an odd multiple of 0.0001 is an odd
multiple of 0.00005, which lies exactly half a unit above the quantity that is kept.

The bill is one of the two measured bills of the engine test fixture: the pricing bill,
whose quantity is the standard ``qty``, and the bill without pricing, whose quantity field
is one of its own, so the property covers both halves of what requirement 6.6 calls the
quantity. Both take their material and unit from fields of the fixture's own naming, which
the test never spells out: it reads them from the resolved configuration
(:func:`~erp_mock.domain.unit_conversion.resolve_line_quantity`, requirement 6.8).

The world is written straight into a real sandbox rather than created through the engine,
for two reasons: a create would default or reject part of what is drawn (the base unit
field is required and a record for the base unit is rejected by requirement 6.11), and one
transaction per example keeps a hundred examples quick where a few thousand engine writes
would not. The database is a real one over the tables of the fixture, built once for the
module, and every example undoes its own inserts before the transaction ends, so the
examples do not see each other. The unit records themselves are never written: the factor
is read from the material and its conversion records alone, and whether the references of a
line exist is the references slot's business (requirement 5.6).
"""

import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Final

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.decimals import PLACES
from erp_mock.common.frozen import FrozenMap
from erp_mock.domain.unit_conversion import (
    QTY_BASE,
    LineQuantity,
    assign_qty_base,
    resolve_line_quantity,
)
from erp_mock.state.schema import ID_COLUMN
from erp_mock.state.store import SandboxStore, UnitOfWork, Value
from tests.helpers.engine_env import empty_store, fixture_model

# The two bills of the engine test fixture whose lines measure something: the pricing bill,
# whose quantity is the standard qty, and the bill without pricing, whose quantity field is
# its own (requirement 6.6, tests/fixtures).
ORDER: Final = "supply_order"
ENTRY: Final = "goods_entry"
MEASURED_BILLS: Final = (ORDER, ENTRY)

# The decimal places of a quantity, which qty_base is rounded to (requirement 4.7).
QTY_PLACES: Final = PLACES["qty"]

# The units a line may name and the materials a world may hold. Both pools are small, so
# the same unit comes up as the base unit of one material and as a converted or unconverted
# unit of another.
UNITS: Final = ("u1", "u2", "u3", "u4")
MATERIAL_IDS: Final = ("m1", "m2", "m3")

# How many lines one bill gets.
MAX_LINES: Final = 6

# The largest quantity and factor of requirement 6.5: 100000000 is 10 ** 8.
LIMIT_EXPONENT: Final = 8


# --- what one example works on -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Material:
    """One material of the world: its base unit and the factors of its records (req 6.6).

    ``base_unit`` is None for a material without a base unit, and ``factors`` holds one
    entry per conversion record of the material, keyed by its conversion unit; a key that
    is the base unit is a record requirement 6.11 keeps a write from storing.
    """

    id: str
    base_unit: str | None
    factors: FrozenMap[str, Decimal]


@dataclass(frozen=True, slots=True)
class World:
    """The materials and conversion records the sandbox holds for one example."""

    materials: tuple[Material, ...]

    def material(self, material_id: str | None) -> Material | None:
        """The material ``material_id``, None for an empty id (requirement 6.6)."""
        found = [item for item in self.materials if item.id == material_id]
        return found[0] if found else None


@dataclass(frozen=True, slots=True)
class Line:
    """The three fields of a line the primitive reads, any of them possibly empty."""

    material_id: str | None
    unit_id: str | None
    qty: Decimal | None


@dataclass(frozen=True, slots=True)
class Case:
    """One example: the bill written, the world it is written against and its lines."""

    bill: str
    world: World
    lines: tuple[Line, ...]


# --- the reference model -----------------------------------------------------------------


def round_qty(value: Fraction) -> Fraction:
    """``value`` rounded to the 4 decimals of a quantity, ROUND_HALF_UP (req 4.7).

    ``floor(|value| × 10000 + 1/2)`` is one more than the kept units exactly when what is
    dropped is at least half a unit; the sign goes back on afterwards, so a half moves away
    from zero.
    """
    units = math.floor(abs(value) * 10**QTY_PLACES + Fraction(1, 2))
    return Fraction(-units if value < 0 else units, 10**QTY_PLACES)


def ref_factor(material: Material, unit_id: str) -> Fraction | None:
    """How many base units one ``unit_id`` of ``material`` is (requirement 6.6).

    1 when the unit is the base unit of the material, the factor of its conversion record
    for that unit otherwise, and None when it has none - the line requirement 6.7 rejects
    the write over, which leaves no quantity to store.
    """
    if unit_id == material.base_unit:
        return Fraction(1)
    factor = material.factors.get(unit_id)
    return None if factor is None else Fraction(factor)


def ref_qty_base(world: World, line: Line) -> Fraction | None:
    """The ``qty_base`` requirement 6.6 gives ``line``: round(qty × factor, 4).

    Empty when the material, the unit or the quantity of the line is empty, and when the
    unit has no factor (:func:`ref_factor`).
    """
    material = world.material(line.material_id)
    if material is None or line.unit_id is None or line.qty is None:
        return None
    factor = ref_factor(material, line.unit_id)
    return None if factor is None else round_qty(Fraction(line.qty) * factor)


def exact(value: Decimal | None) -> Fraction | None:
    """``value`` as an exact rational, None where the quantity is empty."""
    return None if value is None else Fraction(value)


# --- generators --------------------------------------------------------------------------


def scaled(units: int, places: int) -> Decimal:
    """``units`` counts of the last of ``places`` decimal places, as a Decimal value."""
    return Decimal(f"{units}E-{places}")


def trimmed(value: Decimal) -> Decimal:
    """``value`` with the trailing zeros of its fraction dropped ("0.85" for "0.850000")."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text)


# A quantity field as a write stores it: 0.0001 to 100000000 with at most 4 decimals
# (requirement 6.5). Half the draws keep that precision, the other half write the same
# value with fewer decimals.
AT_PRECISION: Final = st.integers(min_value=1, max_value=10 ** (LIMIT_EXPONENT + QTY_PLACES)).map(
    lambda count: scaled(count, QTY_PLACES)
)
QTY_VALUES: Final = st.one_of(AT_PRECISION, AT_PRECISION.map(trimmed))

# Odd multiples of 0.5 and of 0.0001: their product is an odd multiple of 0.00005, which
# lies exactly half a unit of the last kept place above the quantity that is kept.
ODD_HALVES: Final = st.integers(min_value=1, max_value=10**6).map(
    lambda n: scaled(5 * (2 * n - 1), 1)
)
ODD_STEPS: Final = st.integers(min_value=1, max_value=10**4).map(
    lambda n: scaled(2 * n - 1, QTY_PLACES)
)

QUANTITIES: Final = st.one_of(QTY_VALUES, ODD_HALVES)
FACTORS: Final = st.one_of(QTY_VALUES, ODD_STEPS)

# How often a material, a unit or a quantity of a line is empty: one draw in four
# (requirement 6.6).
EMPTY: Final = st.sampled_from([False, False, False, True])


@st.composite
def materials(draw: st.DrawFn, material_id: str) -> Material:
    """Draw one material: a random base unit and random conversion records (req 6.6).

    The base unit is a unit of the pool or none at all, and the conversion records cover
    any subset of the pool, the base unit included, each with a factor of its own.
    """
    base_unit = draw(st.sampled_from([*UNITS, None]))
    units = draw(st.lists(st.sampled_from(UNITS), unique=True, max_size=len(UNITS)))
    factors = {unit: draw(FACTORS) for unit in units}
    return Material(id=material_id, base_unit=base_unit, factors=FrozenMap(factors))


@st.composite
def lines(draw: st.DrawFn, material_ids: Sequence[str]) -> Line:
    """Draw one line: a material of the world, a unit of the pool and a quantity.

    Each of the three is empty in one draw out of four, which is what leaves ``qty_base``
    empty (requirement 6.6).
    """
    return Line(
        material_id=None if draw(EMPTY) else draw(st.sampled_from(material_ids)),
        unit_id=None if draw(EMPTY) else draw(st.sampled_from(UNITS)),
        qty=None if draw(EMPTY) else draw(QUANTITIES),
    )


@st.composite
def cases(draw: st.DrawFn) -> Case:
    """Draw one example: a measured bill, the world it is written against and its lines."""
    count = draw(st.integers(min_value=1, max_value=len(MATERIAL_IDS)))
    material_ids = MATERIAL_IDS[:count]
    world = World(tuple(draw(materials(material_id)) for material_id in material_ids))
    length = draw(st.integers(min_value=1, max_value=MAX_LINES))
    drawn = draw(st.lists(lines(material_ids), min_size=length, max_size=length))
    return Case(bill=draw(st.sampled_from(MEASURED_BILLS)), world=world, lines=tuple(drawn))


# --- the sandbox the factors are read from -----------------------------------------------


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SandboxStore]:
    """An open, empty sandbox over the tables of the fixture, one for the whole module."""
    with closing(empty_store(tmp_path_factory.mktemp("p04") / "sandbox")) as sandbox:
        yield sandbox


def write_world(
    uow: UnitOfWork, quantity: LineQuantity, world: World
) -> tuple[tuple[str, str], ...]:
    """Write ``world`` into the sandbox and return the (table, id) of every record.

    The tables and fields are those the configuration declared, which the primitive reads
    through as well (requirement 6.8): the measured entity with its base unit field, and
    the conversion records with their material, unit and factor fields.
    """
    conversion = quantity.conversion
    written: list[tuple[str, str]] = []
    for material in world.materials:
        uow.insert(
            quantity.material.name,
            {ID_COLUMN: material.id, conversion.base_unit_field: material.base_unit},
        )
        written.append((quantity.material.name, material.id))
        for ordinal, (unit_id, factor) in enumerate(material.factors.items(), start=1):
            record_id = f"{material.id}c{ordinal}"
            uow.insert(
                conversion.entity,
                {
                    ID_COLUMN: record_id,
                    conversion.material_field: material.id,
                    conversion.unit_field: unit_id,
                    conversion.factor_field: factor,
                },
            )
            written.append((conversion.entity, record_id))
    return tuple(written)


@contextmanager
def world_in(store: SandboxStore, quantity: LineQuantity, world: World) -> Iterator[UnitOfWork]:
    """One transaction of ``store`` holding ``world``, which leaves the sandbox empty again.

    The records of the world are inserted, the block reads them through the unit of work,
    and the inserts are undone before the transaction ends: by the deletes when the block
    returns and by the rollback of the transaction when it raises (requirement 12.7). The
    next example therefore starts from the same empty database and reuses the same ids.
    """
    with store.transaction() as uow:
        written = write_world(uow, quantity, world)
        try:
            yield uow
        finally:
            for table, record_id in written:
                uow.delete(table, record_id)


def candidate_line(quantity: LineQuantity, line: Line) -> dict[str, Value]:
    """``line`` as the line of a candidate record: the three fields of the bill it names."""
    return {
        quantity.material_field: line.material_id,
        quantity.unit_field: line.unit_id,
        quantity.qty_field: line.qty,
    }


def as_qty(value: Value) -> Decimal | None:
    """The ``qty_base`` of one line as the primitive left it: a quantity or empty."""
    assert value is None or isinstance(value, Decimal), f"qty_base is {value!r}"
    return value


def stored_qty_base(store: SandboxStore, case: Case) -> tuple[Decimal | None, ...]:
    """The ``qty_base`` step 5 of a write gives the lines of ``case`` (requirement 6.6).

    :func:`~erp_mock.domain.unit_conversion.assign_qty_base` is that step: it takes the
    lines the write will store and fills the field of each of them in place. The resolved
    configuration is read here rather than taken as a test argument, so a falsifying
    example reports the case and not the whole model.
    """
    domain = fixture_model()
    bill = domain.bills[case.bill]
    quantity = resolve_line_quantity(domain, bill)
    assert quantity is not None, f"{case.bill} declares no quantity semantics"
    candidates = [candidate_line(quantity, line) for line in case.lines]
    with world_in(store, quantity, case.world) as uow:
        assign_qty_base(uow, domain, bill, candidates)
    return tuple(as_qty(line[QTY_BASE]) for line in candidates)


def world_of(*factors: Mapping[str, str]) -> World:
    """A world whose materials all take the first unit as their base unit (req 6.6).

    One material per argument, in the order of :data:`MATERIAL_IDS`, each converting the
    units of its argument; a factor is written as text.
    """
    return World(
        tuple(
            Material(
                id=MATERIAL_IDS[index],
                base_unit=UNITS[0],
                factors=FrozenMap({unit: Decimal(text) for unit, text in entry.items()}),
            )
            for index, entry in enumerate(factors)
        )
    )


def line(material_id: str | None, unit_id: str | None, qty: str | None) -> Line:
    """One line whose quantity is written as text, None where a value is empty."""
    return Line(material_id=material_id, unit_id=unit_id, qty=None if qty is None else Decimal(qty))


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 4: 基本单位数量
@pytest.mark.req("6.6")
@settings(max_examples=100)
@given(case=cases())
# The example of requirement 6.6: one box of the material is 100 base units, so 20 boxes
# are 2000 of them.
@example(case=Case(ORDER, world_of({"u2": "100"}), (line("m1", "u2", "20"),)))
# A line in the base unit takes a factor of 1, and a conversion record for the base unit
# does not change that (requirements 6.6 and 6.11).
@example(case=Case(ENTRY, world_of({"u1": "100"}), (line("m1", "u1", "2.5"),)))
# The product is rounded ROUND_HALF_UP: 0.00015 and 0.00005 move away from zero, 0.00014
# does not (requirement 4.7).
@example(
    case=Case(
        ENTRY,
        world_of({"u2": "0.0001"}),
        (line("m1", "u2", "1.5"), line("m1", "u2", "0.5"), line("m1", "u2", "1.4")),
    )
)
# An empty material, an empty unit and an empty quantity each leave qty_base empty.
@example(
    case=Case(
        ORDER,
        world_of({"u2": "12"}),
        (line(None, "u2", "20"), line("m1", None, "20"), line("m1", "u2", None)),
    )
)
# A unit that is neither the base unit nor a unit the material converts: the line
# requirement 6.7 rejects the write over, which stores no quantity.
@example(case=Case(ORDER, world_of({"u2": "12"}), (line("m1", "u3", "20"),)))
# The same unit read for two materials, one converting it and one taking it as its base
# unit, and a material without a conversion record at all.
@example(
    case=Case(
        ENTRY,
        World(
            (
                Material("m1", "u1", FrozenMap({"u2": Decimal("12")})),
                Material("m2", "u2", FrozenMap({"u1": Decimal("0.5")})),
                Material("m3", "u1", FrozenMap()),
            )
        ),
        (
            line("m1", "u2", "1"),
            line("m2", "u2", "1"),
            line("m2", "u1", "1"),
            line("m3", "u1", "1"),
        ),
    )
)
# Both ends of the range of requirement 6.5 in one bill: the smallest product rounds to 0,
# the largest is 10 ** 16.
@example(
    case=Case(
        ORDER,
        world_of({"u2": "0.0001", "u3": "100000000"}),
        (line("m1", "u2", "0.0001"), line("m1", "u3", "100000000")),
    )
)
# A material without a base unit: nothing is 1 and only its conversion records convert.
@example(
    case=Case(
        ORDER,
        World((Material("m1", None, FrozenMap({"u2": Decimal("7")})),)),
        (line("m1", "u1", "3"), line("m1", "u2", "3")),
    )
)
def test_qty_base_is_the_rounded_product_of_the_quantity_and_the_factor(
    store: SandboxStore, case: Case
) -> None:
    found = stored_qty_base(store, case)

    expected = [ref_qty_base(case.world, item) for item in case.lines]
    for number, (value, reference) in enumerate(zip(found, expected, strict=True), start=1):
        assert exact(value) == reference, f"line {number} of {case}"
        # round(qty × factor, 4): a stored quantity carries the four decimals it was
        # rounded to (requirement 4.7).
        assert value is None or value.as_tuple().exponent == -QTY_PLACES
