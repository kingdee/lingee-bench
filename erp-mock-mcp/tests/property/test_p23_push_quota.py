"""Property 23: the quota of a push.

**Validates: Requirements 8.5, 8.7, 8.12**

For any state an approved source bill has been brought to and any lines of a push - lines
that name the same source line among them - the push goes through exactly when, for every
source line, the sum of the ``qty_base`` of the lines that name it is no more than what that
source line had left before the push; where it does not, the error is R-GEN-005 and its
details list every source line the push takes too much of, by the index of the line that
names it first, each carrying what was left; and a push that goes through leaves every field
of the source bill header and of every source line as it found them (requirements 8.5, 8.7
and 8.12).

The reference model is written here and knows nothing of
:mod:`erp_mock.domain.conversion`: it keeps one number per source line, the quantity the
drafts of the conversion occupy, and adds to it what each push that goes through takes and
takes off it what each deleted draft gave back. What a source line has left is its own
``qty_base`` minus that number, which is the Pushable_Qty of the glossary. Every quantity is
an exact :class:`fractions.Fraction`: a source line of ``qty`` in its unit is
``round(qty × factor, 4)`` base units, the factor being the one conversion record of the
fixture for the lines drawn in boxes, and a pushed line is the same over the quantity the
caller sent - so the model never reads a ``qty_base``, an occupied quantity or a
``pushable_qty`` back from the engine to decide anything.

What each step of an example is compared against, after the step ran:

- the verdict: refused with the details the model says, or the draft the model says goes
  through, which is the ``if and only if`` of requirement 8.5. The details are compared as
  numbers - the field path, the source line and what was left - since the text a quantity
  is written as is property 1's business;
- the occupied quantity of every source line, read off the sandbox as the sum of the
  ``qty_base`` of every stored line of the conversion's target that names it, against the
  model's number. A refused push adds nothing to it, which is requirement 12.7, and a draft
  counts as much as any other bill, which is what the glossary asks;
- the set of drafts the source bill has, so a refusal writes no bill at all;
- the source bill as the approve left it, header and lines, field by field and internal
  columns included: ``status``, the link field and ``updated_seq`` among them
  (requirement 8.12).

After the drawn steps, each source line is probed twice, which pins what it has left from
both sides (requirement 8.5): a push of one quantity unit more than the model says is left
is refused and reports exactly that quantity, and a push of exactly what is left goes
through - the ``合计等于 Pushable_Qty 时允许下推`` of requirement 8.5. The second probe is
skipped for a source line with nothing left, since a push of 0 is not a quantity a tool
accepts (requirement 12.6).

What is drawn, per example: a supply order of 1 to 3 lines, each naming one of the two
materials of the fixture and a quantity, and a line of the bolt may be ordered in boxes,
which are :data:`~tests.helpers.engine_env.BOX_FACTOR` base units each; and 1 to 5 steps
over the order once it is approved, each either a push of 1 to 3 lines - each line naming
one of the source lines, a quantity and one of the two warehouses - or the delete of one of
the drafts the earlier pushes generated. The quantities come from two small pools that
combine, so a push that fits a source line exactly, one that is a unit over and one that is
far over all come up often, and two lines of one push name the same source line about as
often as not (requirement 8.7).

The conversion under test is ``supply_order_to_entry``, the one of the fixture whose
``limit_by`` is ``qty``, which is what the property is about: the quota per source line and
the ``pushable_qty`` of its details. The quota of a conversion limited by amount
(requirement 8.6) is a quota per source bill and is pinned by the unit tests of the
primitive instead (``tests/unit/domain/test_conversion.py``).

No draft a push generates is ever approved: the approve of a pushed bill runs the writebacks
and the automatic close of its source bill (requirements 8.8 and 8.13), which is property
24's business. The drafts therefore only ever occupy the quota, which is exactly what the
glossary counts, and the source bill of an example stays in the state its approve left it in
for the whole walk.

Every example runs on the same sandbox, built once for the module with the standard basic
data, and creates a source order of its own in it: a quota is per source line, so the orders
of two examples never meet, and the occupied quantity each example reads off the sandbox is
read under its own source line ids.
"""

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Final, assert_never

import pytest
from hypothesis import example, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.decimals import PLACES
from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.config.model import LINE_SUFFIX
from erp_mock.domain.conversion import (
    EXCEEDS_QTY_REASON,
    QUOTA_RULE,
    SOURCE_BILL_ID,
    SOURCE_LINE_ID,
)
from erp_mock.domain.unit_conversion import QTY_BASE
from erp_mock.domain.views import LINES, RecordView
from erp_mock.state.schema import BILL_ID_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, Value
from tests.helpers.engine_env import (
    BOLT,
    BOX,
    BOX_FACTOR,
    MAIN_DEPOT,
    MAIN_VENDOR,
    PCS,
    SPARE_DEPOT,
    WASHER,
    EngineEnv,
    as_record,
    engine_env,
    record_lines,
)

# The conversion under test and the two bills it runs between: the pricing, measured
# ``supply_order`` and the ``goods_entry`` it generates, whose quota is the base unit
# quantity of each source line (tests/fixtures/packs/fx_trade/conversions).
ORDER: Final = "supply_order"
ENTRY: Final = "goods_entry"
ORDER_TO_ENTRY: Final = "supply_order_to_entry"
ORDER_LINES: Final = f"{ORDER}{LINE_SUFFIX}"
ENTRY_LINES: Final = f"{ENTRY}{LINE_SUFFIX}"

# The short name the pack names the source bill of the push under, which the field path of
# that parameter is built from (requirement 11.13).
ORDER_SHORT: Final = "order"

# The fields a line of either bill carries: the material and the unit of a source line, and
# the quantity and the warehouse a pushed line takes from the caller.
ARTICLE: Final = "article_id"
MEASURE: Final = "measure_id"
DEPOT: Final = "depot_id"
ENTRY_QTY: Final = "entry_qty"
QTY: Final = "qty"
PRICE: Final = "price"

# The materials a source line may order and the warehouses a pushed line may receive into.
# Both pools are small on purpose, so the lines of one push name the same source line often
# and the sum per source line is what decides (requirement 8.7).
MATERIALS: Final[tuple[str, ...]] = (BOLT, WASHER)
DEPOTS: Final[tuple[str, ...]] = (MAIN_DEPOT, SPARE_DEPOT)

# The quantities a source line is ordered with and the ones a pushed line takes: values that
# combine, so a push that uses a source line up exactly, one that is a unit over and one
# that is far over all come up often (requirement 8.5). ``0.0625`` is a quantity of four
# decimals, which is 0.75 base units in boxes.
SOURCE_QUANTITIES: Final[tuple[Decimal, ...]] = tuple(
    Decimal(text) for text in ("1", "2", "2.5", "4")
)
PUSH_QUANTITIES: Final[tuple[Decimal, ...]] = tuple(
    Decimal(text) for text in ("0.0625", "0.5", "1", "1.5", "2", "2.5", "4")
)

# The price of a source line: a pricing bill needs one to leave draft (requirement 7.5), and
# the quota of this conversion is counted in quantities alone.
UNIT_PRICE: Final = Decimal("1")

# How many lines an order and a push get, and how many steps one walk runs; a source line is
# always there, since a standard bill without lines cannot be submitted (requirement 7.5).
MAX_SOURCE_LINES: Final = 3
MAX_PUSH_LINES: Final = 3
MAX_STEPS: Final = 5

# How often a step deletes a draft rather than pushing: one draw in four, which gives the
# quota a draft gave back often enough and still leaves the walk mostly pushes.
DROPS: Final = st.sampled_from([False, False, False, True])

# The decimal places of a quantity, which a base unit quantity is rounded to (4.7, 6.6), and
# the smallest quantity a field of that precision holds, which a probe goes over a quota by.
QTY_PLACES: Final = PLACES["qty"]
UNIT: Final = Fraction(1, 10**QTY_PLACES)
ONE: Final = Fraction(1)
ZERO: Final = Fraction(0)

# The keys of one detail item of R-GEN-005 by quantity, in order (design.md data model 6):
# the quantity field path of the line that names the source line first, the reason, the
# source line and what it had left.
FIELD_KEY: Final = "field"
REASON_KEY: Final = "reason"
PUSHABLE_QTY_KEY: Final = "pushable_qty"
DETAIL_KEYS: Final[tuple[str, ...]] = (FIELD_KEY, REASON_KEY, SOURCE_LINE_ID, PUSHABLE_QTY_KEY)


# --- what one example works on -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """One line of the source order: what it orders, in which unit and how much.

    ``in_boxes`` orders the line in the alternative unit of the bolt, the one material of
    the fixture that has a conversion record, so the quota the line gives is the ordered
    quantity times :data:`~tests.helpers.engine_env.BOX_FACTOR` base units; every other line
    is ordered in the base unit of its material (requirement 6.6).
    """

    material: str
    in_boxes: bool
    qty: Decimal

    @property
    def factor(self) -> Fraction:
        """How many base units one unit of this line is (requirement 6.6)."""
        return Fraction(BOX_FACTOR) if self.in_boxes else ONE

    @property
    def quantity(self) -> Fraction:
        """The quota this source line gives: its own ``qty_base`` (requirement 8.5)."""
        return round_qty(Fraction(self.qty) * self.factor)


@dataclass(frozen=True, slots=True)
class Take:
    """One line of a push: the source line it names, how much it takes and where to.

    ``source`` is the index of the source line in the order, which the run resolves to the
    ``source_line_id`` the line is sent with; a push whose lines carry the same ``source``
    twice is the one requirement 8.7 is about. The unit of a pushed line is not the caller's
    to choose: the conversion maps it from the source line, so the base unit quantity of
    this line is ``qty`` times the factor of the source line it names.
    """

    source: int
    qty: Decimal
    depot: str


@dataclass(frozen=True, slots=True)
class Push:
    """One step of a walk: a push of these lines, in this order (requirement 8.1)."""

    lines: tuple[Take, ...]


@dataclass(frozen=True, slots=True)
class Drop:
    """One step of a walk: delete one of the drafts the earlier pushes generated.

    ``which`` picks it out of the drafts that are still there, by its remainder; a step that
    comes before any push has nothing to delete and does nothing. A deleted draft gives the
    quota it occupied back, since the occupied quantity counts the bills the sandbox still
    holds (requirement 8.5, glossary).
    """

    which: int


type Step = Push | Drop


@dataclass(frozen=True, slots=True)
class Case:
    """One example: the source order to approve and the steps to run over it."""

    source: tuple[Source, ...]
    steps: tuple[Step, ...]


@dataclass(frozen=True, slots=True)
class Item:
    """One detail item R-GEN-005 is expected to report (requirement 8.5).

    ``field`` is the quantity field path of the line that names the source line first,
    ``source`` the index of that source line and ``pushable`` what it had left before the
    push.
    """

    field: str
    source: int
    pushable: Fraction


@dataclass(frozen=True, slots=True)
class Refused:
    """What the model says of a push that takes too much: R-GEN-005 with these items."""

    items: tuple[Item, ...]


@dataclass(frozen=True, slots=True)
class Accepted:
    """What the model says of a push that fits: it takes ``taken`` from these source lines."""

    taken: tuple[tuple[int, Fraction], ...]


type Verdict = Refused | Accepted


@dataclass(frozen=True, slots=True)
class Draft:
    """One draft a push generated: its id and what it took from each source line."""

    id: str
    taken: tuple[tuple[int, Fraction], ...]


@dataclass(frozen=True, slots=True)
class Order:
    """The approved source order one example works on, and the sandbox it lives in.

    ``lines`` are the ids of its lines in line order, which a push names its source lines
    by; ``own`` the quota each of them gives, as the model computes it; and ``before`` the
    bill as the approve left it - the header and the lines as storage holds them, which
    requirement 8.12 asks a push to leave alone.
    """

    env: EngineEnv
    case: Case
    id: str
    lines: tuple[str, ...]
    own: tuple[Fraction, ...]
    before: tuple[Record, tuple[Record, ...]]


# --- the reference model (requirements 8.5 and 8.7) --------------------------------------


def round_qty(value: Fraction) -> Fraction:
    """``value`` rounded to the 4 decimals of a quantity, ROUND_HALF_UP (req 4.7).

    ``floor(|value| × 10000 + 1/2)`` is one more than the kept units exactly when what is
    dropped is at least half a unit; the sign goes back on afterwards, so a half moves away
    from zero.
    """
    units = math.floor(abs(value) * 10**QTY_PLACES + Fraction(1, 2))
    return Fraction(-units if value < 0 else units, 10**QTY_PLACES)


def taken_per_source(case: Case, push: Push) -> dict[int, Fraction]:
    """What ``push`` takes from each source line it names (requirements 8.5 and 8.7).

    The base unit quantity of a pushed line is ``round(qty × factor, 4)``, the factor being
    the one of the source line the line names, since the conversion maps the unit of a
    pushed line from its source line (requirement 6.6). Lines that name the same source line
    are one entry, the sum of their quantities, and the entries are in the order the source
    lines are first named, which is the order requirement 8.5 reports them in.
    """
    totals: dict[int, Fraction] = {}
    for take in push.lines:
        quantity = round_qty(Fraction(take.qty) * case.source[take.source].factor)
        totals[take.source] = totals.get(take.source, ZERO) + quantity
    return totals


def first_paths(push: Push) -> dict[int, str]:
    """The quantity field path of the line that names each source line first (req 8.5)."""
    paths: dict[int, str] = {}
    for index, take in enumerate(push.lines):
        paths.setdefault(take.source, f"{LINES}[{index}].{ENTRY_QTY}")
    return paths


def verdict(case: Case, push: Push, left: Sequence[Fraction]) -> Verdict:
    """What requirements 8.5 and 8.7 say of ``push`` against what each source line has left.

    The push goes through exactly when no source line is asked for more than it has left -
    an exact fit is allowed - and is refused with one item per source line that is, in the
    order the lines of the push name those source lines first, each item carrying the
    quantity field path of the line that names it first and what it had left.
    """
    totals = taken_per_source(case, push)
    paths = first_paths(push)
    exceeded = tuple(
        Item(field=paths[index], source=index, pushable=left[index])
        for index, total in totals.items()
        if total > left[index]
    )
    if exceeded:
        return Refused(items=exceeded)
    return Accepted(taken=tuple(totals.items()))


def left_of(order: Order, occupied: Sequence[Fraction]) -> list[Fraction]:
    """What each source line has left: its own quota minus what is occupied (glossary)."""
    return [own - taken for own, taken in zip(order.own, occupied, strict=True)]


def decimal_of(value: Fraction) -> Decimal:
    """``value`` as the quantity a caller sends; it must fit the 4 decimals of one (4.7)."""
    units = value * 10**QTY_PLACES
    assert units.denominator == 1, f"{value} is not a quantity of {QTY_PLACES} decimals"
    return Decimal(units.numerator).scaleb(-QTY_PLACES)


# --- building the source order ------------------------------------------------------------


def source_values(env: EngineEnv, ordered: Source) -> dict[str, Value]:
    """One line of the source order as a create carries it (requirements 5.1 and 6.1).

    The material, the unit it is ordered in - written out rather than left to the default of
    the field, so the factor of the model is the one the line is measured with - and the
    quantity and the price a pricing bill needs to leave draft (requirement 7.5); the tax
    rate defaults from the material.
    """
    return {
        ARTICLE: env.id(ordered.material),
        MEASURE: env.id(BOX if ordered.in_boxes else PCS),
        QTY: ordered.qty,
        PRICE: UNIT_PRICE,
    }


def record_id(view: RecordView) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    value = view.fields[ID_COLUMN]
    assert isinstance(value, str), "a write operation returned no id"
    return value


def stored_lines(env: EngineEnv, table: str, bill_id: str) -> tuple[Record, ...]:
    """The lines of the bill ``bill_id`` as storage holds them, internal columns included."""
    return tuple(line for line in env.records(table) if line.get(BILL_ID_COLUMN) == bill_id)


def stored_bill(env: EngineEnv, bill_id: str) -> tuple[Record, tuple[Record, ...]]:
    """The source bill as storage holds it: the header and every line (requirement 8.12)."""
    header = env.record(ORDER, bill_id)
    assert header is not None, f"{ORDER} {bill_id} is not in the sandbox"
    return header, stored_lines(env, ORDER_LINES, bill_id)


def stored_source(order: Order) -> tuple[Record, tuple[Record, ...]]:
    """The source bill of ``order`` as storage holds it (requirement 8.12)."""
    return stored_bill(order.env, order.id)


def build_order(env: EngineEnv, case: Case) -> Order:
    """Create the source order of ``case``, submit it and approve it (requirements 7.1, 8.3).

    The push requires an approved source bill, so the order goes through the two state
    operations of the standard machine first; what the approve left behind is the state
    requirement 8.12 asks every later push to preserve.
    """
    header: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "need_date": env.anchor_date}
    rows = [source_values(env, ordered) for ordered in case.source]
    view = as_record(env.create(ORDER, header, rows).view)
    order_id = record_id(view)
    env.transition(ORDER, order_id, "submit")
    env.transition(ORDER, order_id, "approve")
    return Order(
        env=env,
        case=case,
        id=order_id,
        lines=tuple(record_id(line) for line in record_lines(view)),
        own=tuple(ordered.quantity for ordered in case.source),
        before=stored_bill(env, order_id),
    )


# --- running one step and comparing it with the model ------------------------------------


def detail_quantity(value: object) -> Fraction:
    """The quantity a detail item carries, read back as an exact rational (req 4.9).

    A quantity is reported as the text of the decimal formatter, which reads back as the
    value it was written from; what that text looks like is property 1's business.
    """
    assert isinstance(value, str), f"the quantity of a detail item is text, got {value!r}"
    return Fraction(Decimal(value))


def reported(error: ErpError) -> tuple[tuple[str, str, Fraction], ...]:
    """The source lines ``error`` reports, in report order (req 8.5, design.md 6).

    Every item carries the quantity field path of a line, the reason of the quantity quota,
    the source line that is over it and what that source line had left, in that key order.
    """
    items: list[tuple[str, str, Fraction]] = []
    for item in error.details:
        assert tuple(item) == DETAIL_KEYS, f"unexpected detail keys: {tuple(item)}"
        assert item[REASON_KEY] == EXCEEDS_QTY_REASON
        path, source_line = item[FIELD_KEY], item[SOURCE_LINE_ID]
        assert isinstance(path, str) and isinstance(source_line, str)
        items.append((path, source_line, detail_quantity(item[PUSHABLE_QTY_KEY])))
    return tuple(items)


def expected_details(order: Order, refusal: Refused) -> tuple[tuple[str, str, Fraction], ...]:
    """The details the model expects, with its source line indexes resolved to ids."""
    return tuple((item.field, order.lines[item.source], item.pushable) for item in refusal.items)


def run_push(order: Order, push: Push, expected: Verdict) -> str | None:
    """Run ``push`` and pin what it did against ``expected``; the draft's id, or None.

    A push the model refuses must end in BUSINESS_RULE_VIOLATION with the rule id
    R-GEN-005 and exactly the details the model gives (requirement 8.5); a push the model
    accepts must return the draft it generated. Any other error ends the example: the drawn
    pushes name an approved source bill, lines of that bill and references that are there
    and enabled, so R-GEN-005 is the only rule they can break.
    """
    lines: list[dict[str, Value]] = [
        {
            SOURCE_LINE_ID: order.lines[take.source],
            ENTRY_QTY: take.qty,
            DEPOT: order.env.id(take.depot),
        }
        for take in push.lines
    ]
    if isinstance(expected, Refused):
        with pytest.raises(ErpError) as raised:
            order.env.push(ORDER_TO_ENTRY, order.id, lines, source_name=ORDER_SHORT)
        error = raised.value
        assert (error.code, error.rule_id) == (
            ErrorCode.BUSINESS_RULE_VIOLATION,
            QUOTA_RULE,
        ), f"the push ended in {error.code} {error.rule_id}: {error.message}"
        assert reported(error) == expected_details(order, expected)
        return None
    pushed = order.env.push(ORDER_TO_ENTRY, order.id, lines, source_name=ORDER_SHORT)
    return record_id(as_record(pushed.view))


def stored_occupied(order: Order) -> list[Fraction]:
    """What the drafts of the conversion occupy of each source line, off the sandbox (8.5).

    The sum of the ``qty_base`` of every stored line of the target bill that names the source
    line, which the glossary counts whatever state the bill holding it is in; a line of
    another example's order is not one of them.
    """
    index_of = {line_id: index for index, line_id in enumerate(order.lines)}
    totals = [ZERO] * len(order.lines)
    for line in order.env.records(ENTRY_LINES):
        source_line = line.get(SOURCE_LINE_ID)
        index = index_of.get(source_line) if isinstance(source_line, str) else None
        if index is None:
            continue
        quantity = line.get(QTY_BASE)
        assert isinstance(quantity, Decimal), f"a pushed line holds no quantity: {line}"
        totals[index] += Fraction(quantity)
    return totals


def stored_drafts(order: Order) -> set[str]:
    """The bills of the target that the sandbox holds for this source bill (requirement 8.1)."""
    return {
        str(header[ID_COLUMN])
        for header in order.env.records(ENTRY)
        if header.get(SOURCE_BILL_ID) == order.id
    }


def check_sandbox(order: Order, occupied: Sequence[Fraction], drafts: Sequence[Draft]) -> None:
    """What holds after every step: the occupancy, the drafts and the untouched source bill.

    The occupied quantity of each source line is what the model accumulated, so a refused
    push added nothing (requirement 12.7) and a deleted draft gave its quantity back; the
    drafts of the source bill are the ones the model holds; and the source bill is field for
    field the one its approve left behind - its state, its link field and its ``updated_seq``
    included (requirement 8.12).
    """
    assert stored_occupied(order) == list(occupied)
    assert stored_drafts(order) == {draft.id for draft in drafts}
    assert stored_source(order) == order.before


def run_step(order: Order, step: Step, occupied: list[Fraction], drafts: list[Draft]) -> None:
    """Run one step of a walk, pin its outcome and carry the model along with it."""
    match step:
        case Push():
            left = left_of(order, occupied)
            expected = verdict(order.case, step, left)
            note(f"{step} against {left} -> {expected}")
            pushed = run_push(order, step, expected)
            if isinstance(expected, Accepted):
                assert pushed is not None
                drafts.append(Draft(id=pushed, taken=expected.taken))
                for index, quantity in expected.taken:
                    occupied[index] += quantity
        case Drop(which=which):
            if drafts:
                draft = drafts.pop(which % len(drafts))
                note(f"delete the draft {draft}")
                order.env.delete(ENTRY, draft.id)
                for index, quantity in draft.taken:
                    occupied[index] -= quantity
        case _:
            assert_never(step)
    check_sandbox(order, occupied, drafts)


def probe_quotas(order: Order, occupied: list[Fraction], drafts: list[Draft]) -> None:
    """Pin what every source line has left from both sides (requirement 8.5).

    One quantity unit more than the model says is left is refused and the item reports
    exactly that quantity; exactly what is left goes through, which is the exact fit
    requirement 8.5 allows. A source line with nothing left gets only the first probe: a
    push of 0 is not a quantity a tool accepts (requirement 12.6).
    """
    for index, ordered in enumerate(order.case.source):
        left = left_of(order, occupied)[index]
        over = decimal_of(left / ordered.factor + UNIT)
        run_step(order, Push((Take(source=index, qty=over, depot=MAIN_DEPOT),)), occupied, drafts)
        if left > ZERO:
            exact = decimal_of(left / ordered.factor)
            run_step(
                order, Push((Take(source=index, qty=exact, depot=MAIN_DEPOT),)), occupied, drafts
            )


# --- what is drawn -----------------------------------------------------------------------


@st.composite
def source_lines(draw: st.DrawFn) -> Source:
    """Draw one line of the source order: a material, a unit and a quantity."""
    material = draw(st.sampled_from(MATERIALS))
    boxes = draw(st.booleans())
    return Source(
        material=material,
        # Only the bolt has a conversion record, so only its lines may be ordered in boxes.
        in_boxes=boxes and material == BOLT,
        qty=draw(st.sampled_from(SOURCE_QUANTITIES)),
    )


@st.composite
def steps(draw: st.DrawFn, sources: int) -> Step:
    """Draw one step: a push of 1 to 3 lines, or the delete of one of the drafts."""
    if draw(DROPS):
        return Drop(which=draw(st.integers(min_value=0, max_value=MAX_STEPS)))
    count = draw(st.integers(min_value=1, max_value=MAX_PUSH_LINES))
    lines: list[Take] = []
    for _ in range(count):
        lines.append(
            Take(
                source=draw(st.integers(min_value=0, max_value=sources - 1)),
                qty=draw(st.sampled_from(PUSH_QUANTITIES)),
                depot=draw(st.sampled_from(DEPOTS)),
            )
        )
    return Push(lines=tuple(lines))


@st.composite
def cases(draw: st.DrawFn) -> Case:
    """Draw one example: the source order and the steps to run over it."""
    # The counts are drawn rather than left to the shape of a list strategy, so an order of
    # three lines - the shape two source lines over their quota at once needs - comes up as
    # often as one of a single line (requirements 8.5 and 8.7).
    ordered = tuple(
        draw(source_lines())
        for _ in range(draw(st.integers(min_value=1, max_value=MAX_SOURCE_LINES)))
    )
    count = draw(st.integers(min_value=1, max_value=MAX_STEPS))
    walk = [draw(steps(len(ordered))) for _ in range(count)]
    return Case(source=ordered, steps=tuple(walk))


def source(material: str, qty: str, *, boxes: bool = False) -> Source:
    """One source line written out, in the base unit of its material unless ``boxes``."""
    assert not boxes or material == BOLT, "only the bolt has a conversion record"
    return Source(material=material, in_boxes=boxes, qty=Decimal(qty))


def take(index: int, qty: str, *, depot: str = MAIN_DEPOT) -> Take:
    """One pushed line written out: the source line it names and how much it takes."""
    return Take(source=index, qty=Decimal(qty), depot=depot)


def push(*lines: Take) -> Push:
    """One push step written out, its lines in the order they are sent."""
    return Push(lines=lines)


def case(ordered: Sequence[Source], *walk: Step) -> Case:
    """One example written out: the source order and the steps to run over it."""
    return Case(source=tuple(ordered), steps=tuple(walk))


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data, one for the whole module."""
    with engine_env(tmp_path_factory.mktemp("p23") / "sandbox") as environment:
        yield environment


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 23: 下推额度
@pytest.mark.req("8.5", "8.7", "8.12")
@settings(max_examples=100)
@given(case=cases())
# A push that uses a source line up exactly, which is allowed (requirement 8.5).
@example(case=case([source(BOLT, "2", boxes=True)], push(take(0, "2"))))
# Two lines of one push on one source line, a unit over between them: the source line yields
# one item, which names the line that came first (requirements 8.5 and 8.7).
@example(
    case=case(
        [source(BOLT, "2", boxes=True)],
        push(take(0, "1"), take(0, "1.5", depot=SPARE_DEPOT)),
    )
)
# Three lines of one push on one source line that use it up exactly between them (8.7).
@example(case=case([source(WASHER, "4")], push(take(0, "1"), take(0, "1"), take(0, "2"))))
# Two source lines over their quota at once, reported by the line that names each of them
# first: the washer through the first and third line, the bolt through the second (8.5, 8.7).
@example(
    case=case(
        [source(BOLT, "1", boxes=True), source(WASHER, "1")],
        push(take(1, "2"), take(0, "2"), take(1, "1")),
    )
)
# A draft already takes from the quota, so the next push has the rest of it: the second push
# is one unit of the pool over what is left and the third fits it exactly (requirement 8.5).
@example(
    case=case(
        [source(WASHER, "4")],
        push(take(0, "2.5")),
        push(take(0, "2")),
        push(take(0, "1.5")),
    )
)
# A deleted draft gives its quota back: the second push is refused while the first draft is
# there and the fourth goes through once it is gone (requirement 8.5, glossary).
@example(
    case=case(
        [source(WASHER, "2")],
        push(take(0, "2")),
        push(take(0, "0.5")),
        Drop(which=0),
        push(take(0, "2")),
    )
)
# Two source lines of the same material in different units: the quota of each is counted in
# base units, so 0.0625 of a box is 0.75 of them (requirements 6.6 and 8.5).
@example(
    case=case(
        [source(BOLT, "1", boxes=True), source(BOLT, "1")],
        push(take(0, "0.0625"), take(1, "1")),
    )
)
# A delete that comes before any push has nothing to give back (requirement 8.5).
@example(case=case([source(WASHER, "1")], Drop(which=0), push(take(0, "1"))))
def test_a_push_fits_exactly_when_every_source_line_has_the_quota_left(
    env: EngineEnv, case: Case
) -> None:
    order = build_order(env, case)
    occupied = [ZERO] * len(case.source)
    drafts: list[Draft] = []

    for step in case.steps:
        run_step(order, step, occupied, drafts)

    # What every source line has left, from both sides: a unit more is refused and reports
    # exactly that quantity, and exactly that quantity goes through (requirement 8.5).
    probe_quotas(order, occupied, drafts)
