"""Property 14: the equivalence of a JSON number and the decimal string of the same value.

**Validates: Requirements 12.4, 12.5, 17.11**

For any parameter declared as number and any decimal value of at most 15 significant digits,
sending the value as a JSON number and sending it as a decimal string give the same
normalized argument and the same outcome; the only difference is the coerced paths, which
list exactly the parameters that arrived as strings, in the order of the details
(requirements 12.4, 12.5 and 17.11).

Where the two are compared. Input validation is the one step of a call that sees the two
forms apart (:func:`~erp_mock.tools.validate.validate_args`, design.md 5.3): everything
after it works on the normalized arguments alone, so two calls whose
:class:`~erp_mock.tools.validate.ValidatedArgs` agree in ``values`` and in ``error`` run the
same way and return the same result. Each example therefore builds the same call twice - once
with every number as a JSON number, once with the drawn mix of JSON numbers and decimal
strings - and compares:

- the normalized arguments, by sign, digits and exponent, so 0.13 and 0.1300 would not pass
  as the same value, and key by key, so the lines are compared as well (:func:`shape`);
- the error, by its code, message, rule and detail items, which is the whole error result
  (:func:`error_shape`, requirement 13.6);
- the coerced paths: none for the call of JSON numbers, and the paths of the strings for the
  other one, whether validation failed or not (requirement 17.11).

The premise of the property is that the two writings denote the same number, and
:func:`assert_one_value` asserts it of every argument drawn rather than assuming it: the
decimal string matches :data:`DECIMAL_STRING`, the form requirement 12.5 accepts, and both
it and the JSON number read back as the drawn value - the JSON number through ``repr``, the
shortest text it reads back as, which is the decimal it was written as for at most
:data:`MAX_DIGITS` significant digits (requirement 12.4).

The reference model is :func:`coerced_paths`, which reads the coerced paths of a call off the
inputSchema and the arguments alone: the parameters of an object in the order the schema
declares them, the elements of an array by index, the parameters of an element in the order
the element declares them - the order requirement 12.12 gives the details - and a path of the
list exactly where the schema declares a number and the call sent a string (requirement
12.5). The expected normalized value is :func:`reference_round`, ROUND_HALF_UP in exact
rational arithmetic, so the shared value is pinned to the field precision of its parameter
and not just to the other form of itself.

What is drawn, per example: one of two tools, 1 to 3 lines, and a value, two writings and a
form for each number parameter of the header and of every line, the optional ones sometimes
left out. The values are non-negative: whole numbers of units, values exactly at the
precision of their field, and values with 1 to 8 digits past it that sit at half a unit of
the last kept place or one in the last digit away from it, so a value is as often at the
boundary of ROUND_HALF_UP as in the middle of a step (:func:`values`). The string of a value
is sometimes written with leading zeros before the integer part or trailing zeros after the
fraction, which is where ``0.130`` for ``0.13`` comes from, and a value that is a whole
number goes as a JSON integer as well as a JSON float. The second test adds one fault to the
call - an unknown key, a missing required parameter, an invalid date, a value below its
``minimum`` of 0, or a line quantity that rounds to 0 - so that the coerced paths of a call
that failed validation are drawn too (requirement 17.11).

The two tools: the create tool of ``supply_order`` of the engine test fixture, whose lines
carry a rate, a quantity and a price, and the create tool of its ``count_sheet`` with one
number field of each of the four types added to the header, one of them with allowed values
(:data:`EXTRA_FIELDS`), which is how ``enum`` and ``minimum`` get compared as Decimals
(requirement 12.4). Both schemas are generated from the model the way step 6 generates them
(:func:`~erp_mock.tools.input_schema.bill_input`). Nothing is stored, so the ids the calls
name need not exist in any sandbox.

Each test runs at least 100 examples.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from functools import cache
from math import floor
from typing import Any, Final

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.decimals import PLACES, DecimalKind
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.model import DomainModel, FieldModel
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import LINES_PARAM, bill_input
from erp_mock.tools.validate import ValidatedArgs, validate_args
from tests.helpers.fixtures import fixture_domain

# A JSON number denotes exactly the decimal it is written as up to this many significant
# digits (requirement 12.4), which is what makes the two writings of a value comparable.
MAX_DIGITS: Final = 15

# The decimal strings a number parameter accepts, written out from requirement 12.5. Used
# with fullmatch, as "$" would also accept a trailing newline.
DECIMAL_STRING: Final = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")

# The JSON type a parameter must have for its string to be converted (requirement 12.5),
# and the keywords of a schema object the reference model reads.
NUMBER_TYPE: Final = "number"
ARRAY_TYPE: Final = "array"
_TYPE: Final = "type"
_PROPERTIES: Final = "properties"
_ITEMS: Final = "items"

# The two tools, by the name each one is called under below.
ORDER_TOOL: Final = "supply_order_create"
SAMPLE_TOOL: Final = "count_sheet_create"

# What one faulty call carries beyond its arguments: a key no tool declares, and a text
# that is no date of the calendar (requirement 12.2).
UNKNOWN_KEY: Final = "zz"
BIZ_DATE: Final = "biz_date"
BAD_DATE: Final = "2026-02-30"

# Leading or trailing zeros added to the decimal string of a value.
ZERO_COUNTS: Final = st.integers(min_value=1, max_value=3)


# --- what one call of the property looks like --------------------------------------------


@dataclass(frozen=True, slots=True)
class Sent:
    """One number argument: its value and the two ways a call may send it.

    ``text`` is the decimal string of ``value`` (requirement 12.5) and ``number`` the JSON
    number of the same value (requirement 12.4); ``as_text`` says which of the two the
    mixed call sends, where the call of JSON numbers always sends ``number``.
    """

    value: Decimal
    text: str
    number: int | float
    as_text: bool = True


@dataclass(frozen=True, slots=True)
class Plan:
    """One call of a tool: its number arguments, and what else it sends.

    ``header`` and each element of ``lines`` name the number parameters the call carries;
    the other arguments come from the :class:`Case` of ``tool``. ``extra`` adds arguments
    beyond it, which is how a faulty call sends an unknown key or an invalid date, and
    ``dropped`` is a required parameter the call leaves out.
    """

    tool: str
    lines: tuple[tuple[tuple[str, Sent], ...], ...]
    header: tuple[tuple[str, Sent], ...] = ()
    extra: tuple[tuple[str, JsonValue], ...] = ()
    dropped: str | None = None


@dataclass(frozen=True, slots=True)
class Slot:
    """One number parameter a call may carry, and what it has to satisfy.

    ``allowed`` are the declared values of its field, which become the numeric ``enum`` of
    the schema, ``positive`` marks the quantity of a line, which must be greater than 0
    after rounding, and ``required`` the ones the schema requires (requirements 11.11
    and 12.2).
    """

    name: str
    kind: DecimalKind
    allowed: tuple[str, ...] = ()
    positive: bool = False
    required: bool = False


@dataclass(frozen=True, slots=True)
class Case:
    """One tool of the property: a valid call of it, and its number parameters.

    ``header`` and ``line`` are the arguments of a valid call that are no numbers, which
    every call of the tool carries, and ``required_header`` those of them the schema
    requires, one of which a faulty call leaves out.
    """

    bill: str
    header: Mapping[str, JsonValue]
    line: Mapping[str, JsonValue]
    line_slots: tuple[Slot, ...]
    header_slots: tuple[Slot, ...] = ()
    required_header: tuple[str, ...] = ()


# One number field of each of the four types, added to the header of the sample bill. ``levy``
# declares allowed values, which the schema writes as the JSON numbers 0, 0.06 and 0.13 and a
# parameter is compared with by value, so 0.130 is one of them (requirements 11.11 and 12.4).
EXTRA_FIELDS: Final[tuple[tuple[str, DecimalKind, tuple[str, ...]], ...]] = (
    ("fee", "money", ()),
    ("levy", "rate", ("0", "0.06", "0.130")),
    ("unit_price", "price", ()),
    ("net_qty", "qty", ()),
)

CASES: Final[Mapping[str, Case]] = {
    ORDER_TOOL: Case(
        bill="supply_order",
        header={"vendor_id": "ven_000001", "need_date": "2026-10-20"},
        line={"article_id": "art_000001"},
        line_slots=(
            Slot("tax_rate", "rate"),
            Slot("qty", "qty", positive=True, required=True),
            Slot("price", "price", required=True),
        ),
        required_header=("vendor_id", "need_date"),
    ),
    SAMPLE_TOOL: Case(
        bill="count_sheet",
        header={"depot_id": "dep_000001", "count_kind": "gain"},
        line={"article_id": "art_000001"},
        line_slots=(Slot("diff_qty", "qty", positive=True, required=True),),
        header_slots=tuple(
            Slot(name, kind, allowed=allowed) for name, kind, allowed in EXTRA_FIELDS
        ),
        required_header=("depot_id", "count_kind"),
    ),
}


@cache
def tool_of(case: str) -> tuple[DomainModel, ToolSpec]:
    """The resolved model and the create tool of ``case``, built once per session.

    The schema is generated from the bill the way step 6 generates it, and the model is
    passed along with it: the precision a number parameter is rounded to and the quantity
    of a line are properties of the fields behind the parameters (requirement 11.3).
    """
    domain = fixture_domain()
    if case == SAMPLE_TOOL:
        domain = with_number_fields(domain, CASES[case].bill)
    bill = domain.bills[CASES[case].bill]
    built = bill_input(bill, "create")
    spec = ToolSpec(
        name=case,
        generated_name=case,
        pack=bill.pack,
        kind="entity_op",
        action="create",
        target=bill.name,
        input_schema=built.schema,
        param_paths=built.param_paths,
    )
    return domain, spec


def with_number_fields(domain: DomainModel, name: str) -> DomainModel:
    """``domain`` with :data:`EXTRA_FIELDS` added to the header of the bill ``name``."""
    bill = domain.bills[name]
    added = tuple(
        FieldModel(
            name=field,
            type=kind,
            origin="declared",
            writable=True,
            values=allowed,
        )
        for field, kind, allowed in EXTRA_FIELDS
    )
    extended = replace(
        bill,
        header=FrozenMap({**bill.header, **{field.name: field for field in added}}),
        header_declared_fields=(
            *bill.header_declared_fields,
            *(field.name for field in added),
        ),
    )
    return replace(domain, bills=FrozenMap({**domain.bills, name: extended}))


# --- the reference model (requirements 12.5, 12.12 and 12.6) -----------------------------


def coerced_paths(
    schema: Mapping[str, JsonValue], raw: Mapping[str, Any], prefix: str = ""
) -> tuple[str, ...]:
    """The paths of the number parameters ``raw`` sends as decimal strings (reqs 12.5, 12.12).

    The declared parameters of an object in the order of the schema, the elements of an
    array by index and the parameters of an element in the order the element declares them,
    which is the order of the details (requirement 12.12); a path is in the list exactly
    where the schema declares a number and the call sent a string, whatever its form.
    ``prefix`` is what the paths of this object start with.
    """
    found: list[str] = []
    for name, declared in sub_objects(schema.get(_PROPERTIES)).items():
        if name not in raw:
            continue
        value = raw[name]
        path = f"{prefix}{name}"
        json_type = declared.get(_TYPE)
        if json_type == NUMBER_TYPE and isinstance(value, str):
            found.append(path)
        elif json_type == ARRAY_TYPE and isinstance(value, list):
            element = sub_schema(declared.get(_ITEMS))
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    found.extend(coerced_paths(element, item, f"{path}[{index}]."))
    return tuple(found)


def sub_schema(value: JsonValue | None) -> Mapping[str, JsonValue]:
    """``value`` as a schema object, empty where the keyword is absent."""
    return value if isinstance(value, Mapping) else {}


def sub_objects(value: JsonValue | None) -> Mapping[str, Mapping[str, JsonValue]]:
    """The ``properties`` of a schema object, in declaration order."""
    return {name: sub_schema(declared) for name, declared in sub_schema(value).items()}


def reference_round(value: Decimal, places: int) -> Fraction:
    """``value`` rounded to ``places`` decimal places, ROUND_HALF_UP, in exact arithmetic.

    floor(|value| × 10**places + 1/2) is one more than the kept units exactly when the
    discarded part is at least half a unit; the sign is reapplied afterwards, so a half
    moves away from zero (requirement 4.7).
    """
    exact = Fraction(value)
    magnitude = floor(abs(exact) * 10**places + Fraction(1, 2))
    return Fraction(-magnitude if exact < 0 else magnitude, 10**places)


# --- building the two calls of one plan --------------------------------------------------


def arguments(plan: Plan, *, mixed: bool) -> dict[str, Any]:
    """The arguments of ``plan`` as one of the two calls sends them.

    ``mixed`` sends the decimal string of every argument drawn that way and the JSON number
    of the others; without it every number goes as a JSON number, which is the call the
    mixed one is compared against.
    """
    case = CASES[plan.tool]
    args: dict[str, Any] = {
        name: value for name, value in case.header.items() if name != plan.dropped
    }
    args.update({name: written(arg, mixed=mixed) for name, arg in plan.header})
    args.update(plan.extra)
    args[LINES_PARAM] = [
        {**case.line, **{name: written(arg, mixed=mixed) for name, arg in line}}
        for line in plan.lines
    ]
    return args


def written(arg: Sent, *, mixed: bool) -> str | int | float:
    """How one of the two calls writes the number argument ``arg``."""
    return arg.text if mixed and arg.as_text else arg.number


def drawn_numbers(plan: Plan) -> tuple[tuple[str, Slot, Sent], ...]:
    """Every number argument of ``plan``: its path, its parameter and its two writings."""
    case = CASES[plan.tool]
    header = {slot.name: slot for slot in case.header_slots}
    element = {slot.name: slot for slot in case.line_slots}
    found = [(name, header[name], arg) for name, arg in plan.header]
    for index, line in enumerate(plan.lines):
        found.extend((f"{LINES_PARAM}[{index}].{name}", element[name], arg) for name, arg in line)
    return tuple(found)


def value_at(args: ValidatedArgs, path: str) -> object:
    """The normalized argument at ``path``, which the call must have carried."""
    if not path.startswith(f"{LINES_PARAM}["):
        return args.values[path]
    index, _, name = path[len(LINES_PARAM) + 1 :].partition("].")
    lines = args.lines
    assert lines is not None, "expected the call to carry lines"
    return lines[int(index)][name]


# --- the assertions ----------------------------------------------------------------------


def shape(value: object) -> object:
    """``value`` as what tells one normalized argument from another.

    A Decimal is compared by sign, digits and exponent, so 0.13 and 0.1300 are not the same
    normalized value; a mapping is compared key by key in order and a tuple element by
    element, so the lines and the keys of a line are compared as well. Any other value is
    compared with its type next to it, so 1 and True are not the same argument either.
    """
    if isinstance(value, Decimal):
        return value.as_tuple()
    if isinstance(value, Mapping):
        return tuple((name, shape(item)) for name, item in value.items())
    if isinstance(value, tuple):
        return tuple(shape(item) for item in value)
    return (type(value).__name__, value)


def error_shape(args: ValidatedArgs) -> tuple[object, ...]:
    """The error result of a rejected call: its code, message, rule and details (req 13.6)."""
    error = args.error
    assert error is not None, "expected the arguments to be rejected"
    return (
        error.code,
        error.message,
        error.rule_id,
        tuple(dict(item) for item in error.details),
    )


def assert_one_value(arg: Sent) -> None:
    """Assert that both writings of ``arg`` denote its value (requirements 12.4 and 12.5).

    The premise of the property: the decimal string has the form requirement 12.5 accepts
    and reads back as the value, and so does the JSON number through ``repr``, the shortest
    text it reads back as, which is the decimal it was written as at this many significant
    digits.
    """
    assert DECIMAL_STRING.fullmatch(arg.text) is not None, f"not a decimal string: {arg.text!r}"
    assert Decimal(arg.text) == arg.value
    assert Decimal(arg.number if isinstance(arg.number, int) else repr(arg.number)) == arg.value


def assert_normalized(result: object, value: Decimal, places: int) -> None:
    """Assert that ``result`` is ``value`` rounded to ``places`` decimal places (req 12.6).

    Value, exponent and sign bit together fix ``result.as_tuple()``; the sign bit may be set
    only on a negative result, so a result of zero must be +0.
    """
    assert isinstance(result, Decimal), f"not a Decimal: {result!r}"
    expected = reference_round(value, places)
    sign, _, exponent = result.as_tuple()
    assert Fraction(result) == expected
    assert exponent == -places
    assert sign == (1 if expected < 0 else 0)


def assert_same_outcome(
    plan: Plan, raw: Mapping[str, Any], plain: ValidatedArgs, mixed: ValidatedArgs
) -> None:
    """Assert the two calls of ``plan`` differ in their coerced paths and nothing else.

    ``plain`` is the outcome of the call that sent every number as a JSON number and
    ``mixed`` the one of the call ``raw``, which sent the drawn mix of the two forms. The
    normalized arguments and the error are the whole outcome of input validation, which is
    everything the rest of the call sees (design.md 5.3); the paths of the strings are the
    mixed call's alone, in the order of the details (requirements 12.5 and 12.12).
    """
    _, spec = tool_of(plan.tool)
    assert shape(plain.values) == shape(mixed.values)
    assert (plain.error is None) == (mixed.error is None)
    if plain.error is not None:
        assert error_shape(plain) == error_shape(mixed)
    expected = coerced_paths(spec.input_schema, raw)
    # The reference walks the schema; the count is what the plan drew as strings.
    assert len(expected) == sum(1 for _, _, arg in drawn_numbers(plan) if arg.as_text)
    assert plain.coerced == ()
    assert mixed.coerced == expected


# --- generators --------------------------------------------------------------------------


@st.composite
def values(draw: st.DrawFn, slot: Slot, *, nonzero: bool = False, tiny: bool = False) -> Decimal:
    """A non-negative value for the parameter ``slot``, at most 15 significant digits.

    A value of the declared ``enum`` where the field has one, which is compared before
    rounding, so only a value of it passes (requirement 12.4). Otherwise a quarter of the
    values are whole numbers of units, which a client may write as a JSON integer; the rest
    sit exactly at the precision of the field, with nothing to discard, or carry 1 to 8
    digits past it, half of which are exactly half a unit of the last kept place or one in
    the last digit away from it, so the boundary of ROUND_HALF_UP is drawn as often as the
    middle of a step. ``nonzero`` keeps the value above 0, which the quantity of a line
    needs after rounding, and ``tiny`` draws a value below half a unit of the precision
    instead, which rounds to 0 (requirement 12.2).
    """
    places = PLACES[slot.kind]
    if tiny:
        return Decimal(f"{draw(st.integers(min_value=1, max_value=4))}E{-(places + 1)}")
    if slot.allowed:
        return Decimal(draw(st.sampled_from(slot.allowed)))
    least = 1 if nonzero or slot.positive else 0
    if draw(st.sampled_from((True, False, False, False))):
        return Decimal(draw(st.integers(min_value=least, max_value=10**9)))
    extra = draw(st.integers(min_value=0, max_value=8))
    kept = draw(st.integers(min_value=least, max_value=10 ** (MAX_DIGITS - extra) - 1))
    if extra == 0:
        return Decimal(f"{kept}E{-places}")
    half = 5 * 10 ** (extra - 1)  # half a unit in the last kept place
    discarded = draw(
        st.one_of(
            st.sampled_from((half, half - 1, half + 1)),
            st.integers(min_value=0, max_value=10**extra - 1),
        )
    )
    return Decimal(f"{kept * 10**extra + discarded}E{-(places + extra)}")


@st.composite
def texts(draw: st.DrawFn, value: Decimal) -> str:
    """``value`` as a decimal string, sometimes with zeros that do not change it.

    Plain notation, which is the only form requirement 12.5 accepts, sometimes with 1 to 3
    zeros before the integer part or after the fraction: ``0.130`` for 0.13 and ``007.5``
    for 7.5 are the same number written differently (requirement 12.4 compares by value).
    """
    plain = format(value, "f")
    sign, digits = ("-", plain[1:]) if plain.startswith("-") else ("", plain)
    if draw(st.booleans()):
        digits = "0" * draw(ZERO_COUNTS) + digits
    if draw(st.booleans()):
        digits = digits if "." in digits else f"{digits}."
        digits = digits + "0" * draw(ZERO_COUNTS)
    return f"{sign}{digits}"


def json_numbers(value: Decimal) -> st.SearchStrategy[int | float]:
    """``value`` as the JSON number a client writes: a JSON integer where it is integral."""
    if value != value.to_integral_value():
        return st.just(float(value))
    forms: list[int | float] = [int(value), float(value)]
    return st.sampled_from(forms)


@st.composite
def sents(draw: st.DrawFn, slot: Slot, *, nonzero: bool = False, tiny: bool = False) -> Sent:
    """One number argument of ``slot``: a value, its two writings and the form to send."""
    value = draw(values(slot, nonzero=nonzero, tiny=tiny))
    return Sent(value, draw(texts(value)), draw(json_numbers(value)), draw(st.booleans()))


@st.composite
def slot_values(draw: st.DrawFn, slots: tuple[Slot, ...]) -> tuple[tuple[str, Sent], ...]:
    """The number arguments of one object: every required parameter and some of the rest."""
    found: list[tuple[str, Sent]] = []
    for slot in slots:
        if slot.required or draw(st.booleans()):
            found.append((slot.name, draw(sents(slot))))
    return tuple(found)


@st.composite
def plans(draw: st.DrawFn) -> Plan:
    """A call every number argument of which is good: ``error`` is None either way."""
    tool = draw(st.sampled_from(sorted(CASES)))
    case = CASES[tool]
    count = draw(st.integers(min_value=1, max_value=3))
    lines = tuple(draw(slot_values(case.line_slots)) for _ in range(count))
    return Plan(tool, lines, draw(slot_values(case.header_slots)))


@st.composite
def faulty_plans(draw: st.DrawFn) -> Plan:
    """A call that fails validation, whose decimal strings are converted all the same (17.11).

    One fault on top of a good call: a key the tool does not declare, a required parameter
    left out, a text that is no date of the calendar, a number below its ``minimum`` of 0,
    or a line quantity that is not greater than 0 after rounding. The faults that are no
    numbers leave every drawn path converted, and the two that are numbers show that the
    path of a value that was rejected is converted as well (requirements 12.5 and 17.11).
    """
    plan = draw(plans())
    case = CASES[plan.tool]
    match draw(st.sampled_from(("unknown", "missing", "date", "minimum", "positive"))):
        case "unknown":
            return replace(plan, extra=(*plan.extra, (UNKNOWN_KEY, 1)))
        case "missing":
            return replace(plan, dropped=draw(st.sampled_from(case.required_header)))
        case "date":
            return replace(plan, extra=(*plan.extra, (BIZ_DATE, BAD_DATE)))
        case "minimum":
            return draw(below_minimum(plan))
        case _:
            return draw(tiny_quantity(plan))


@st.composite
def below_minimum(draw: st.DrawFn, plan: Plan) -> Plan:
    """``plan`` with one number argument below the ``minimum`` of 0 of its parameter.

    A parameter with declared values is left out: a negative value of one of those fails
    the ``enum`` instead, which the examples of the second test cover.
    """
    case = CASES[plan.tool]
    # Where the value goes: the header, which is None, or the line of that index.
    targets: list[tuple[int | None, Slot]] = [
        (None, slot) for slot in case.header_slots if not slot.allowed
    ]
    targets.extend(
        (index, slot)
        for index in range(len(plan.lines))
        for slot in case.line_slots
        if not slot.allowed
    )
    index, slot = draw(st.sampled_from(targets))
    negative = negated(draw(sents(slot, nonzero=True)))
    return (
        with_header(plan, slot.name, negative)
        if index is None
        else with_line(plan, index, slot.name, negative)
    )


@st.composite
def tiny_quantity(draw: st.DrawFn, plan: Plan) -> Plan:
    """``plan`` with a line quantity that is not greater than 0 after rounding (req 12.2)."""
    case = CASES[plan.tool]
    slot = next(slot for slot in case.line_slots if slot.positive)
    index = draw(st.integers(min_value=0, max_value=len(plan.lines) - 1))
    return with_line(plan, index, slot.name, draw(sents(slot, tiny=True)))


def negated(arg: Sent) -> Sent:
    """``arg`` negated, both of its writings with it: a value below the ``minimum`` of 0.

    ``arg`` is drawn away from zero, so the result is below 0 and no writing of it is -0.
    """
    assert arg.value > 0, f"expected a value above zero: {arg.value}"
    return Sent(-arg.value, f"-{arg.text}", -arg.number, arg.as_text)


def with_header(plan: Plan, name: str, arg: Sent) -> Plan:
    """``plan`` with the header argument ``name`` set to ``arg``."""
    rest = tuple((key, found) for key, found in plan.header if key != name)
    return replace(plan, header=(*rest, (name, arg)))


def with_line(plan: Plan, index: int, name: str, arg: Sent) -> Plan:
    """``plan`` with the argument ``name`` of the line at ``index`` set to ``arg``."""
    rest = tuple((key, found) for key, found in plan.lines[index] if key != name)
    lines = list(plan.lines)
    lines[index] = (*rest, (name, arg))
    return replace(plan, lines=tuple(lines))


def sent(text: str, *, string: str | None = None, integral: bool = False) -> Sent:
    """The argument ``text`` denotes, as a decimal string and as a JSON number.

    ``string`` writes the decimal string another way - with leading or trailing zeros -
    and ``integral`` sends the JSON number as a JSON integer rather than a JSON float.
    """
    value = Decimal(text)
    return Sent(value, text if string is None else string, int(value) if integral else float(value))


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 14: 数值宽进的等价性
@pytest.mark.req("12.4", "12.5")
@settings(max_examples=100)
@given(plan=plans())
# A value past the precision of its field, rounded half up to it (requirement 12.6).
@example(
    plan=Plan(ORDER_TOOL, ((("qty", sent("20", integral=True)), ("price", sent("0.1234565"))),))
)
# The same values written with leading and trailing zeros (requirement 12.4).
@example(
    plan=Plan(
        ORDER_TOOL,
        (
            (
                ("tax_rate", sent("0.13", string="0.1300")),
                ("qty", sent("20", string="0020.0000")),
                ("price", sent("0.85", string="00.850")),
            ),
        ),
    )
)
# Two lines, every number a decimal string: the coerced paths in the order of the details.
@example(
    plan=Plan(
        ORDER_TOOL,
        (
            (("tax_rate", sent("0.09")), ("qty", sent("2")), ("price", sent("1.5"))),
            (("qty", sent("3")), ("price", sent("2.25"))),
        ),
    )
)
# 0.130 is the allowed value 0.13 (requirement 12.4), and money 1.005 rounds up to 1.01.
@example(
    plan=Plan(
        SAMPLE_TOOL,
        ((("diff_qty", sent("3")),),),
        header=(("levy", sent("0.13", string="0.130")), ("fee", sent("1.005"))),
    )
)
# A value of 0, which the ``minimum`` of 0 allows, and one far below one unit.
@example(
    plan=Plan(
        SAMPLE_TOOL,
        ((("diff_qty", sent("0.0001")),),),
        header=(("fee", sent("0", integral=True)), ("unit_price", sent("0.000001"))),
    )
)
def test_a_decimal_string_and_a_json_number_normalize_the_same(plan: Plan) -> None:
    domain, spec = tool_of(plan.tool)
    numbers = arguments(plan, mixed=False)
    strings = arguments(plan, mixed=True)
    for _, _, arg in drawn_numbers(plan):
        assert_one_value(arg)  # the premise: both writings denote the same number

    plain = validate_args(domain, spec, numbers)
    coerced = validate_args(domain, spec, strings)

    assert plain.error is None, f"unexpected error: {plain.error}"
    assert coerced.error is None, f"unexpected error: {coerced.error}"
    assert_same_outcome(plan, strings, plain, coerced)
    for path, slot, arg in drawn_numbers(plan):
        assert_normalized(value_at(plain, path), arg.value, PLACES[slot.kind])


# Feature: erp-mock-mcp-m0, Property 14: 数值宽进的等价性
@pytest.mark.req("12.5", "17.11")
@settings(max_examples=100)
@given(plan=faulty_plans())
# A price below its ``minimum`` of 0: the path of the rejected value is converted all the same.
@example(plan=Plan(ORDER_TOOL, ((("qty", sent("20")), ("price", sent("-1.5"))),)))
# A quantity that rounds to 0, which no line may carry (requirement 12.2).
@example(plan=Plan(ORDER_TOOL, ((("qty", sent("0.00004")), ("price", sent("0.85"))),)))
# A value no allowed value equals, compared as a Decimal before rounding (requirement 12.4).
@example(plan=Plan(SAMPLE_TOOL, ((("diff_qty", sent("3")),),), header=(("levy", sent("0.07")),)))
# A key the tool does not declare, a required parameter left out, and an invalid date.
@example(
    plan=Plan(
        ORDER_TOOL,
        ((("qty", sent("20")), ("price", sent("0.85"))),),
        extra=((UNKNOWN_KEY, 1),),
    )
)
@example(
    plan=Plan(
        ORDER_TOOL,
        ((("tax_rate", sent("0.13")), ("qty", sent("20")), ("price", sent("0.85"))),),
        dropped="vendor_id",
    )
)
@example(
    plan=Plan(
        SAMPLE_TOOL,
        ((("diff_qty", sent("3")),),),
        header=(("fee", sent("1.23")),),
        extra=((BIZ_DATE, BAD_DATE),),
    )
)
def test_the_coerced_paths_are_the_same_when_validation_fails(plan: Plan) -> None:
    domain, spec = tool_of(plan.tool)
    numbers = arguments(plan, mixed=False)
    strings = arguments(plan, mixed=True)
    for _, _, arg in drawn_numbers(plan):
        assert_one_value(arg)  # the premise: both writings denote the same number

    plain = validate_args(domain, spec, numbers)
    coerced = validate_args(domain, spec, strings)

    assert plain.error is not None, "expected the arguments to be rejected"
    assert_same_outcome(plan, strings, plain, coerced)
