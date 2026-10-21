"""Property 25: the negative stock check and the order of the lines.

**Validates: Requirements 9.4, 9.5, 9.9**

For any bill whose pack declares an inventory linkage and any stock on hand, the approve is
refused over R-GEN-007 exactly when some (material, warehouse) would be left below 0 - the
quantity on hand the operation started from, 0 where the pair has no balance record, plus
the net change of the whole bill on that pair - and the details list those pairs by material
id and then warehouse id, each carrying that quantity on hand; for any permutation of the
lines, the verdict, its details and the stock on hand a successful approve leaves are the
same (requirements 9.4, 9.5 and 9.9).

The reference model is written here and knows nothing of
:mod:`erp_mock.domain.inventory`: it reads the direction of each of the fixture's four
linkages off :func:`line_deltas` - a table of what one line adds to which warehouse - sums
those per (material, warehouse) as exact :class:`fractions.Fraction` values, adds the
quantity on hand the setup left on the pair and compares the sum against 0. It is therefore
a sum per pair and nothing else: no line is looked at on its own and no order is kept, which
is what requirement 9.5 asks of the check and requirement 9.9 of its result. The quantity
of a line is ``round(qty × factor, 4)``, the base unit quantity of requirement 6.6, with the
factor of the fixture's one conversion record for the lines drawn in the alternative unit,
so the model never reads ``qty_base`` back from the engine.

How the order is compared: every example is run twice, each time in a sandbox of its own
built from the same basic data, and the two runs differ only in the order in which the
lines of the setup entry and of the bill under test are written - the first run writes them
as they were drawn, the second in a drawn permutation. The internal ids of the basic data
are fixed by the order :data:`~tests.helpers.engine_env.BASIC_DATA` is created in, so the
two sandboxes name the same materials and warehouses and their verdicts compare directly.
Both verdicts are then compared with each other, which is the confluence of requirement 9.9,
and with the reference model, which is the ``if and only if`` of requirement 9.4. A verdict
holds whether the approve was refused, the pairs the error reported in report order and the
quantity on hand of every pair that has a balance record afterwards, so the comparison
covers the details and the stock on hand at once; the quantities are read back as exact
rationals, their text being property 1's business.

What is drawn, per example: one of the four bills of the engine test fixture, which is one
of the four directions a linkage may declare (``goods_entry`` is ``in`` and names its
warehouse on the line, ``issue_note`` is ``out`` and names it on the header, ``stock_move``
is ``transfer`` between two header fields, and ``count_sheet`` is ``adjust`` by the enum
field of its header), its header warehouses and, for the count sheet, whether it is a gain
or a loss; 1 to 4 lines of it, each naming one of the two materials of the fixture, one of
the two warehouses and a quantity; and the stock on hand to start from, as 0 to 5 lines of
one goods entry that is approved first. Quantities come from a small pool of values that
combine, and a line of the bolt may be written in its alternative unit, which is 12 base
units - so a pair that is left at exactly 0, which is no violation, and a pair that is left
just below it come up often, in both units. The permutation and the quantities are what the
property turns on; the materials and warehouses are what makes the pairs collide.

Note the two things a single bill of this fixture cannot do, which the unit tests of the
primitive cover instead: it cannot move one (material, warehouse) in both directions at
once - an ``out`` bill only takes stock off a pair and a transfer's two warehouses must
differ (R-FXT-001) - so an intermediate quantity below 0 under a net change that is not
(requirement 9.5) is pinned by
``tests/unit/domain/test_inventory.py::test_an_intermediate_quantity_below_zero_is_no_violation``;
and it cannot leave two warehouses short at the same time, since the only bill that names a
warehouse per line is the one that adds stock, so the second key of the details order is
pinned by ``test_net_changes_are_ordered_by_material_then_warehouse`` there.
"""

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import Final

import pytest
from hypothesis import example, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.decimals import PLACES
from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.domain.engine import OpResult
from erp_mock.domain.inventory import (
    NEGATIVE_STOCK_REASON,
    NEGATIVE_STOCK_RULE,
    balance_id,
)
from erp_mock.state.schema import (
    ID_COLUMN,
    MATERIAL_COLUMN,
    QTY_BASE_COLUMN,
    STOCK_BALANCE_TABLE,
    WAREHOUSE_COLUMN,
)
from erp_mock.state.store import Value
from tests.helpers.engine_env import (
    BOLT,
    BOX,
    BOX_FACTOR,
    MAIN_DEPOT,
    MAIN_VENDOR,
    SPARE_DEPOT,
    WASHER,
    EngineEnv,
    as_record,
    engine_env,
)

# The four bills of the engine test fixture that move stock, one per direction of
# requirement 9.2 (tests/fixtures/packs/fx_trade).
ENTRY: Final = "goods_entry"
ISSUE: Final = "issue_note"
MOVE: Final = "stock_move"
COUNT: Final = "count_sheet"
KINDS: Final[tuple[str, ...]] = (ENTRY, ISSUE, MOVE, COUNT)

# The quantity field of each of them, which the bills do not share: the quantity semantics
# of a bill name their own (requirement 6.6).
QTY_FIELDS: Final[Mapping[str, str]] = {
    ENTRY: "entry_qty",
    ISSUE: "issue_qty",
    MOVE: "move_qty",
    COUNT: "diff_qty",
}

# The two values of the enum field the adjust direction of the count sheet reads: a gain
# takes +qty_base and a loss -qty_base (requirement 9.2).
GAIN: Final = "gain"
LOSS: Final = "loss"
COUNT_KINDS: Final[tuple[str, ...]] = (GAIN, LOSS)

# The materials and the warehouses a line may name. Both pools are small on purpose, so the
# lines of one bill land on the same (material, warehouse) often and the sum per pair is
# what decides (requirement 9.5).
MATERIALS: Final[tuple[str, ...]] = (BOLT, WASHER)
DEPOTS: Final[tuple[str, ...]] = (MAIN_DEPOT, SPARE_DEPOT)

# The quantities a line may carry: values that combine, so the stock on hand a setup leaves
# and the quantity a bill takes off it are equal, one apart or far apart about as often. A
# line of the bolt may be written in boxes, which are :data:`BOX_FACTOR` base units each, so
# 0.5 and 1 box meet 6 and 12 base units (requirement 6.6).
QUANTITIES: Final[tuple[Decimal, ...]] = tuple(
    Decimal(text) for text in ("0", "0.5", "1", "2", "3", "6", "12")
)

# How many lines the bill under test and the setup entry get; the bill has at least one,
# since a standard bill without lines cannot be submitted (R-GEN-002, requirement 7.5). The
# setup gets one more, so a bill that takes stock off a warehouse is about as often within
# what is on hand as beyond it.
MAX_LINES: Final = 4
MAX_SETUP_LINES: Final = 5

# The decimal places of a quantity, which a base unit quantity is rounded to (4.7, 6.6).
QTY_PLACES: Final = PLACES["qty"]

# The factor of a line written in the base unit of its material (requirement 6.6), and the
# quantity on hand of a (material, warehouse) without a balance record (requirement 9.5).
ONE: Final = Fraction(1)
ZERO: Final = Fraction(0)

# The keys of one detail item of R-GEN-007, in order (design.md data model 6): a reason that
# names no input field, the two ids and the quantity on hand before the operation.
REASON_KEY: Final = "reason"
DETAIL_KEYS: Final[tuple[str, ...]] = (
    REASON_KEY,
    MATERIAL_COLUMN,
    WAREHOUSE_COLUMN,
    QTY_BASE_COLUMN,
)

# One (material, warehouse), by the aliases of the basic data; the reference model keys its
# sums by it and resolves the two ids only to order and to report them (requirement 9.4).
type Place = tuple[str, str]


# --- what one example works on -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a bill: what it measures, how much, in which unit and where.

    ``in_boxes`` writes the line in the alternative unit of the bolt, the one material of
    the fixture that has a conversion record, so its base unit quantity is the drawn
    quantity times :data:`BOX_FACTOR` (requirement 6.6); ``depot`` is read only by the
    goods entry, the one bill of the fixture whose warehouse field is on the line
    (requirement 9.11).
    """

    material: str
    qty: Decimal
    in_boxes: bool
    depot: str


@dataclass(frozen=True, slots=True)
class Order:
    """A permutation of the lines of the setup entry and of the bill under test (req 9.9).

    Both are permutations of ``range(len(...))``: the i-th line written is the line the
    case drew at ``order[i]``, so a run writes the same lines in another order and nothing
    else about it differs.
    """

    setup: tuple[int, ...]
    lines: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Case:
    """One example: the stock to start from, the bill under test and a permutation.

    ``setup`` are the lines of one goods entry that is approved first, which is what leaves
    the quantity on hand the check of the bill under test starts from (requirement 9.5);
    ``kind`` is the bill under test, ``depot`` its warehouse - the warehouse the stock leaves
    for a transfer - ``target_depot`` the warehouse a transfer moves stock to and
    ``count_kind`` the value the adjust direction of a count sheet reads (requirement 9.2).
    """

    setup: tuple[Line, ...]
    kind: str
    depot: str
    target_depot: str
    count_kind: str
    lines: tuple[Line, ...]
    order: Order


@dataclass(frozen=True, slots=True)
class Verdict:
    """What one approve left behind: the R-GEN-007 verdict and the stock on hand.

    ``refused`` is whether the approve ended in the rule, ``details`` the pairs the error
    reported in report order - the two internal ids and the quantity on hand before the
    operation - and ``balances`` the quantity on hand of every (material, warehouse) that
    has a balance record afterwards, by the id of that record. Quantities are exact
    rationals, so a verdict compares as a number and not as text.

    Two runs that differ only in the order of the lines produce the same verdict
    (requirement 9.9), and so does the reference model (requirements 9.4 and 9.5).
    """

    refused: bool
    details: tuple[tuple[str, str, Fraction], ...]
    balances: tuple[tuple[str, Fraction], ...]


# --- the reference model (requirements 9.2, 9.4 and 9.5) ---------------------------------


def round_qty(value: Fraction) -> Fraction:
    """``value`` rounded to the 4 decimals of a quantity, ROUND_HALF_UP (req 4.7).

    ``floor(|value| × 10000 + 1/2)`` is one more than the kept units exactly when what is
    dropped is at least half a unit; the sign goes back on afterwards, so a half moves away
    from zero.
    """
    units = math.floor(abs(value) * 10**QTY_PLACES + Fraction(1, 2))
    return Fraction(-units if value < 0 else units, 10**QTY_PLACES)


def base_qty(line: Line) -> Fraction:
    """The base unit quantity of ``line``: ``round(qty × factor, 4)`` (req 6.6, 9.2).

    The factor is 1 for a line written in the base unit of its material and
    :data:`BOX_FACTOR` for one written in boxes, the fixture's one conversion record.
    """
    factor = Fraction(BOX_FACTOR) if line.in_boxes else ONE
    return round_qty(Fraction(line.qty) * factor)


def line_deltas(case: Case, line: Line) -> tuple[tuple[Place, Fraction], ...]:
    """What one line of the bill under test adds to each pair it touches (req 9.2).

    The direction of the linkage of the bill decides both the warehouse and the sign: the
    goods entry adds the quantity to the warehouse of the line, the issue note takes it off
    the warehouse of the header, the stock move takes it off one header warehouse and adds
    it to the other, and the count sheet adds or takes it off by the value of its enum
    field.
    """
    quantity = base_qty(line)
    if case.kind == ENTRY:
        return (((line.material, line.depot), quantity),)
    if case.kind == ISSUE:
        return (((line.material, case.depot), -quantity),)
    if case.kind == MOVE:
        return (
            ((line.material, case.depot), -quantity),
            ((line.material, case.target_depot), quantity),
        )
    sign = ONE if case.count_kind == GAIN else -ONE
    return (((line.material, case.depot), sign * quantity),)


def summed(deltas: Sequence[tuple[Place, Fraction]]) -> dict[Place, Fraction]:
    """``deltas`` added up per (material, warehouse), which no order changes (req 9.5, 9.9)."""
    totals: dict[Place, Fraction] = {}
    for place, quantity in deltas:
        totals[place] = totals.get(place, ZERO) + quantity
    return totals


def net_change(case: Case) -> dict[Place, Fraction]:
    """The net change of the whole bill under test on each pair it touches (req 9.5)."""
    return summed([delta for line in case.lines for delta in line_deltas(case, line)])


def quantity_before(case: Case) -> dict[Place, Fraction]:
    """The quantity on hand the setup entry leaves on each pair it touches (req 9.1).

    The setup is a goods entry, so every one of its lines adds its base unit quantity to the
    warehouse the line names; a pair it does not touch has no balance record and counts as 0
    (requirements 9.5 and 9.10).
    """
    return summed([((line.material, line.depot), base_qty(line)) for line in case.setup])


def place_ids(ids: Mapping[str, str], place: Place) -> tuple[str, str]:
    """The internal ids of one pair, which the details are ordered by (requirement 9.4)."""
    material, warehouse = place
    return (ids[material], ids[warehouse])


def balances_of(
    ids: Mapping[str, str], quantities: Mapping[Place, Fraction]
) -> tuple[tuple[str, Fraction], ...]:
    """``quantities`` as one entry per balance record, by the id of the record (9.10, 5.14).

    Every pair that has a transaction has a record, a quantity on hand of 0 included, so
    each pair of ``quantities`` is one entry.
    """
    return tuple(
        sorted(
            (
                (balance_id(*place_ids(ids, place)), quantity)
                for place, quantity in quantities.items()
            ),
            key=lambda entry: entry[0],
        )
    )


def expected(ids: Mapping[str, str], case: Case) -> Verdict:
    """The verdict requirements 9.4 and 9.5 give ``case``, whatever order its lines come in.

    The approve is refused exactly when some pair's quantity on hand before the operation
    plus the net change of the whole bill on it is below 0, with one detail item per such
    pair by material id and then warehouse id, each carrying that quantity on hand; a
    refusal rolls the whole operation back, so the stock on hand is the one the setup left,
    and an approve that goes through leaves every pair at the sum of the two
    (requirement 9.10 keeps a record for each one).
    """
    before = quantity_before(case)
    delta = net_change(case)
    short = tuple(
        place
        for place in sorted(delta, key=lambda place: place_ids(ids, place))
        if before.get(place, ZERO) + delta[place] < 0
    )
    if short:
        return Verdict(
            refused=True,
            details=tuple((*place_ids(ids, place), before.get(place, ZERO)) for place in short),
            balances=balances_of(ids, before),
        )
    after = {place: before.get(place, ZERO) + delta.get(place, ZERO) for place in {*before, *delta}}
    return Verdict(refused=False, details=(), balances=balances_of(ids, after))


# --- running one case against a sandbox of its own ---------------------------------------


def header_values(env: EngineEnv, case: Case) -> dict[str, Value]:
    """The required header values of the bill under test (requirements 7.5 and 9.2).

    The warehouses the linkage reads at header level and, for the count sheet, the enum
    field its adjust direction reads; the issue note's taker defaults to the current user.
    """
    if case.kind == ENTRY:
        return {"vendor_id": env.id(MAIN_VENDOR)}
    if case.kind == ISSUE:
        return {"depot_id": env.id(case.depot)}
    if case.kind == MOVE:
        return {
            "from_depot_id": env.id(case.depot),
            "to_depot_id": env.id(case.target_depot),
        }
    return {"depot_id": env.id(case.depot), "count_kind": case.count_kind}


def line_values(env: EngineEnv, kind: str, line: Line) -> dict[str, Value]:
    """One line of the bill ``kind`` as a write carries it (requirements 5.1 and 9.11).

    The material, the quantity in the field the bill names for it and, for a line written in
    boxes, the unit; the unit of every other line defaults to the base unit of its material.
    The warehouse is written only for the goods entry, whose linkage names it on the line.
    """
    values: dict[str, Value] = {
        "article_id": env.id(line.material),
        QTY_FIELDS[kind]: line.qty,
    }
    if line.in_boxes:
        values["measure_id"] = env.id(BOX)
    if kind == ENTRY:
        values["depot_id"] = env.id(line.depot)
    return values


def record_id(result: OpResult) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    bill_id = as_record(result.view).fields[ID_COLUMN]
    assert isinstance(bill_id, str), "a create returned no id"
    return bill_id


def approved_bill(
    env: EngineEnv, kind: str, header: Mapping[str, Value], lines: Sequence[Mapping[str, Value]]
) -> None:
    """Create the bill ``kind`` with ``lines``, submit it and approve it (req 7.1, 9.1).

    The approve is the operation the linkage runs on, so what it writes is one transaction
    per line and warehouse and the new quantity on hand of each pair; an error of the
    approve reaches the caller.
    """
    bill_id = record_id(env.create(kind, dict(header), [dict(line) for line in lines]))
    env.transition(kind, bill_id, "submit")
    env.transition(kind, bill_id, "approve")


def seed_stock(env: EngineEnv, case: Case, order: Sequence[int]) -> None:
    """Approve the setup entry, which leaves the stock the bill under test starts from.

    A goods entry only adds stock, so this approve never breaks R-GEN-007; a case whose
    setup is empty writes no entry at all and leaves every pair without a balance record
    (requirement 9.10).
    """
    if not case.setup:
        return
    lines = [line_values(env, ENTRY, case.setup[index]) for index in order]
    approved_bill(env, ENTRY, {"vendor_id": env.id(MAIN_VENDOR)}, lines)


def detail_quantity(value: object) -> Fraction:
    """The quantity on hand a detail item carries, read back as an exact rational (4.9).

    A quantity is reported as the text of the decimal formatter, which reads back as the
    value it was written from; what that text looks like is property 1's business.
    """
    assert isinstance(value, str), f"the quantity of a detail item is text, got {value!r}"
    return Fraction(Decimal(value))


def reported(error: ErpError) -> tuple[tuple[str, str, Fraction], ...]:
    """The pairs ``error`` reports, in report order (requirement 9.4, design.md 6).

    Every item carries the reason of R-GEN-007, which names no input field, the two
    internal ids and the quantity on hand before the operation, in that key order.
    """
    items: list[tuple[str, str, Fraction]] = []
    for item in error.details:
        assert tuple(item) == DETAIL_KEYS, f"unexpected detail keys: {tuple(item)}"
        assert item[REASON_KEY] == NEGATIVE_STOCK_REASON
        material, warehouse = item[MATERIAL_COLUMN], item[WAREHOUSE_COLUMN]
        assert isinstance(material, str) and isinstance(warehouse, str)
        items.append((material, warehouse, detail_quantity(item[QTY_BASE_COLUMN])))
    return tuple(items)


def stored_balances(env: EngineEnv) -> tuple[tuple[str, Fraction], ...]:
    """The quantity on hand of every pair that has a balance record, by its id (req 9.10)."""
    found: list[tuple[str, Fraction]] = []
    for record in env.records(STOCK_BALANCE_TABLE):
        quantity = record[QTY_BASE_COLUMN]
        assert isinstance(quantity, Decimal), f"a balance holds no quantity: {record}"
        record_key = record[ID_COLUMN]
        assert isinstance(record_key, str)
        found.append((record_key, Fraction(quantity)))
    return tuple(found)


def approve_bill(env: EngineEnv, case: Case, order: Sequence[int]) -> Verdict:
    """Approve the bill under test with its lines in ``order`` and read the verdict off it.

    A refusal is BUSINESS_RULE_VIOLATION with the rule id R-GEN-007 (requirement 9.4); any
    other error ends the example, since the drawn bill breaks no other rule.
    """
    lines = [line_values(env, case.kind, case.lines[index]) for index in order]
    try:
        approved_bill(env, case.kind, header_values(env, case), lines)
    except ErpError as error:
        assert (error.code, error.rule_id) == (
            ErrorCode.BUSINESS_RULE_VIOLATION,
            NEGATIVE_STOCK_RULE,
        ), f"the approve ended in {error.code} {error.rule_id}: {error.message}"
        return Verdict(refused=True, details=reported(error), balances=stored_balances(env))
    return Verdict(refused=False, details=(), balances=stored_balances(env))


def run(root: Path, ids: Mapping[str, str], case: Case, order: Order) -> Verdict:
    """Build ``case`` in a sandbox of its own and approve its bill with ``order``.

    Each run gets an empty sandbox with the standard basic data, so the two runs of one
    example start from the same stock and the same internal ids - which the order the basic
    data is created in fixes, and which the run checks against ``ids``.
    """
    # A space in the directory name: the sandbox database is read through a URI (design 3.4).
    sandbox_dir = Path(mkdtemp(dir=root, prefix="sb "))
    try:
        with engine_env(sandbox_dir) as env:
            assert dict(env.ids) == dict(ids), "a sandbox named the basic data differently"
            seed_stock(env, case, order.setup)
            return approve_bill(env, case, order.lines)
    finally:
        rmtree(sandbox_dir, ignore_errors=True)


# --- what is drawn -----------------------------------------------------------------------


@st.composite
def stock_lines(draw: st.DrawFn) -> Line:
    """Draw one line: a material, a quantity, a unit and a warehouse (see the module doc)."""
    material = draw(st.sampled_from(MATERIALS))
    boxes = draw(st.booleans())
    return Line(
        material=material,
        qty=draw(st.sampled_from(QUANTITIES)),
        # Only the bolt has a conversion record, so only its lines may be written in boxes.
        in_boxes=boxes and material == BOLT,
        depot=draw(st.sampled_from(DEPOTS)),
    )


@st.composite
def orders(draw: st.DrawFn, setup: int, lines: int) -> Order:
    """Draw the order the second run writes the lines of a case in (requirement 9.9)."""
    return Order(
        setup=tuple(draw(st.permutations(range(setup)))),
        lines=tuple(draw(st.permutations(range(lines)))),
    )


@st.composite
def cases(draw: st.DrawFn) -> Case:
    """Draw one example: a bill of one of the four directions, its lines and the stock."""
    depot = draw(st.sampled_from(DEPOTS))
    setup = tuple(draw(st.lists(stock_lines(), max_size=MAX_SETUP_LINES)))
    lines = tuple(draw(st.lists(stock_lines(), min_size=1, max_size=MAX_LINES)))
    return Case(
        setup=setup,
        kind=draw(st.sampled_from(KINDS)),
        depot=depot,
        # The two warehouses of a transfer must differ, which R-FXT-001 of the fixture asks
        # of every stock move (requirement 10.7).
        target_depot=draw(st.sampled_from([other for other in DEPOTS if other != depot])),
        count_kind=draw(st.sampled_from(COUNT_KINDS)),
        lines=lines,
        order=draw(orders(len(setup), len(lines))),
    )


def line(material: str, qty: str, *, boxes: bool = False, depot: str = MAIN_DEPOT) -> Line:
    """One line written out, in the base unit of its material unless ``boxes`` says so."""
    assert not boxes or material == BOLT, "only the bolt has a conversion record"
    return Line(material=material, qty=Decimal(qty), in_boxes=boxes, depot=depot)


def identity(case: Case) -> Order:
    """The order the lines of ``case`` were drawn in, which the first run writes them in."""
    return Order(setup=tuple(range(len(case.setup))), lines=tuple(range(len(case.lines))))


def case(
    kind: str,
    lines: Sequence[Line],
    *,
    setup: Sequence[Line] = (),
    depot: str = MAIN_DEPOT,
    target_depot: str = SPARE_DEPOT,
    count_kind: str = LOSS,
) -> Case:
    """One example written out; the second run writes both sets of lines back to front."""
    return Case(
        setup=tuple(setup),
        kind=kind,
        depot=depot,
        target_depot=target_depot,
        count_kind=count_kind,
        lines=tuple(lines),
        order=Order(
            setup=tuple(reversed(range(len(setup)))),
            lines=tuple(reversed(range(len(lines)))),
        ),
    )


@pytest.fixture(scope="module")
def sandbox_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each run is created below."""
    return tmp_path_factory.mktemp("p25-sandboxes")


@pytest.fixture(scope="module")
def ids(sandbox_root: Path) -> Iterator[Mapping[str, str]]:
    """The internal id of each record of the basic data, by its alias (engine_env).

    Built once for the module from one sandbox of its own: the ids are fixed by the order
    the basic data is created in, so they are the ids every run of every example gets, which
    :func:`run` checks.
    """
    with engine_env(sandbox_root / "aliases") as env:
        yield dict(env.ids)


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 25: 负库存判定与分录顺序无关
@pytest.mark.req("9.4", "9.5", "9.9")
@settings(max_examples=100)
@given(case=cases())
# Two lines that take the stock on hand to exactly 0: the net change is not below 0, so the
# approve goes through and the pair keeps a balance record of 0 (requirements 9.5 and 9.10).
@example(case=case(ISSUE, [line(BOLT, "6"), line(BOLT, "6")], setup=[line(BOLT, "1", boxes=True)]))
# Each line on its own stays within the stock on hand and their sum does not: only the sum
# decides, and the detail item carries the quantity on hand before the operation (9.4, 9.5).
@example(
    case=case(
        ISSUE,
        [line(BOLT, "6"), line(BOLT, "6"), line(BOLT, "1")],
        setup=[line(BOLT, "1", boxes=True)],
    )
)
# Two materials short at once, reported by material id whatever order the lines come in: the
# bolt is partly in stock and the washer is not in stock at all (requirement 9.4).
@example(case=case(ISSUE, [line(WASHER, "3"), line(BOLT, "2")], setup=[line(BOLT, "1")]))
# A transfer out of a warehouse that holds nothing: the pair the stock leaves is short, the
# pair it arrives at is not, so one of the two is reported (requirements 9.2 and 9.4).
@example(case=case(MOVE, [line(BOLT, "2")]))
# A transfer that empties both pairs of its source warehouse exactly, which goes through and
# leaves four balance records, two of them at 0 (requirements 9.2 and 9.10).
@example(
    case=case(
        MOVE,
        [line(BOLT, "2"), line(WASHER, "1")],
        setup=[line(BOLT, "2"), line(WASHER, "1")],
    )
)
# A count sheet writing a loss off a pair with no balance record at all: the quantity on
# hand before the operation is 0 and the loss breaks the rule (requirements 9.2 and 9.5).
@example(case=case(COUNT, [line(WASHER, "0.5")], count_kind=LOSS))
# The same count sheet as a gain, which only adds stock and therefore goes through (9.2).
@example(case=case(COUNT, [line(BOLT, "3")], count_kind=GAIN))
# Two units on the same pair: 1 box and half a box are 18 base units, which one issue note
# of 1.5 boxes takes back off exactly, so the quantity the check reads is the base unit one
# and not what any line was written with (requirements 9.2 and 9.11).
@example(
    case=case(
        ISSUE,
        [line(BOLT, "1.5", boxes=True)],
        setup=[line(BOLT, "1", boxes=True), line(BOLT, "0.5", boxes=True)],
    )
)
# A goods entry, the one direction that only adds stock, with a line of 0 and a line in the
# other warehouse: both pairs take a balance record (requirements 9.1 and 9.10).
@example(case=case(ENTRY, [line(BOLT, "0"), line(WASHER, "2", depot=SPARE_DEPOT)]))
def test_the_negative_stock_check_is_the_net_change_per_place_in_any_order(
    sandbox_root: Path, ids: Mapping[str, str], case: Case
) -> None:
    model = expected(ids, case)
    note(f"expected: {model}")

    drawn = run(sandbox_root, ids, case, identity(case))
    permuted = run(sandbox_root, ids, case, case.order)

    # The verdict, its details and the stock on hand the approve left do not depend on the
    # order of the lines (requirement 9.9).
    assert permuted == drawn
    # And both are the ones the net change of each (material, warehouse) decides: refused
    # exactly when a pair would be left below 0, with those pairs by material id and then
    # warehouse id (requirements 9.4 and 9.5).
    assert drawn == model
