"""Property 13: input validation is deterministic, ordered, and bounded first.

**Validates: Requirements 12.2, 12.3, 12.12, 12.13**

For any tool and any arguments obtained by mutating a call it accepts - a value of the
wrong type, a missing required parameter, an unknown key, a value out of bounds, a JSON
boolean or a string of another form where a number is expected, a string longer than the
parameter takes:

- the outcome is determined by the arguments alone: validating them twice, and validating
  the same arguments with their keys written in another order, gives the same normalized
  values, the same coerced paths and the same error, details and message, in the same order
  (requirement 12.2);
- a VALIDATION_FAILED carries exactly one detail item per broken path, with the reason of
  the mutation that broke it, ordered by the declaration order of the schema, then the index
  of an array element, then the code points of an unknown key's name (requirements 12.12 and
  12.13);
- an integer ``page_size`` over its ``maximum`` or a ``lines`` array over its ``maxItems``
  ends the call with LIMIT_EXCEEDED and one item per parameter over its bound, even though
  the same arguments would otherwise be a VALIDATION_FAILED of several items
  (requirement 12.3).

The reference model is :func:`order_key`, which reads requirement 12.12 off the inputSchema
of the tool and off nothing else: a path contributes one key per level, a declared name
sorting by its position in ``properties``, an unknown one after every declared name by its
code points, and an array before each of its elements by index. :func:`in_report_order`
sorts the problems of a mutated call by that key, which is the order the details must have.

What is drawn, per example: one of the tools the engine test fixture generates, a call that
tool accepts (:func:`valid_object`, which reads the schema for the values of each
parameter), and a mutation plan over that call (:func:`plans`) - at most one mutation per
path, so each path has one expected reason. The mutations are chosen by the order the
validator checks a scalar in: the type, then ``enum``, then the bounds, then ``maxLength``,
``pattern`` and ``format``, so the drawn mutation is the first failure of its path and its
reason is the one the details carry. The keys of every object are then written in a drawn
order, and once more in the opposite order, which is what the determinism test compares.

The fixture generates no parameter with a ``pattern``, with a numeric ``enum``, with an
``integer`` that has no bound or with a ``maxLength`` of a handful of characters, so
:data:`SAMPLE_BILL` adds a bill whose header carries one field of each remaining type and
generates its create tool, the way tests/unit/tools/test_validate.py does. With it the
drawn tools cover every shape of parameter an inputSchema has (requirement 11.11) and every
reason input validation reports but ``must_be_positive``, which is about the value of one
path rather than about the order of the details.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from erp_mock.common.decimals import to_decimal
from erp_mock.common.errors import ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.fields import FieldType
from erp_mock.config.model import DomainModel, FieldModel
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import LINES_PARAM, bill_input
from erp_mock.tools.validate import (
    ENUM_REASON,
    FORMAT_REASON,
    MAX_ITEMS_REASON,
    MAX_LENGTH_REASON,
    MAXIMUM_REASON,
    MIN_ITEMS_REASON,
    MINIMUM_REASON,
    PATTERN_REASON,
    REQUIRED_REASON,
    TYPE_REASON,
    UNKNOWN_FIELD_REASON,
    ValidatedArgs,
    validate_args,
)
from tests.helpers.fixtures import load_fixture

# --- reading one schema object -----------------------------------------------------------


def properties_of(schema: Mapping[str, JsonValue]) -> Mapping[str, Mapping[str, JsonValue]]:
    """The ``properties`` of a schema object, in declaration order; empty when it has none."""
    found = schema.get("properties")
    if not isinstance(found, Mapping):
        return {}
    return {name: sub for name, sub in found.items() if isinstance(sub, Mapping)}


def items_of(schema: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """The ``items`` of an array parameter, empty for a parameter that is no array."""
    found = schema.get("items")
    return found if isinstance(found, Mapping) else {}


def required_of(schema: Mapping[str, JsonValue]) -> frozenset[str]:
    """The names in ``required``, the parameters a call must carry (requirement 11.12)."""
    found = schema.get("required")
    if isinstance(found, str) or not isinstance(found, Sequence):
        return frozenset()
    return frozenset(name for name in found if isinstance(name, str))


def enum_of(schema: Mapping[str, JsonValue]) -> tuple[JsonValue, ...] | None:
    """The allowed values of ``enum``, None for a parameter that declares none."""
    found = schema.get("enum")
    if isinstance(found, str) or not isinstance(found, Sequence):
        return None
    return tuple(found)


def count_of(value: JsonValue | None, default: int) -> int:
    """A ``maxLength``, ``minItems`` or ``maxItems`` as an int, ``default`` when absent."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


# --- the reference model of the detail order (requirement 12.12) -------------------------

# One step of a field path: where a name sits among the keys of its object, or which element
# of an array a path goes through. The three parts keep every step the same type, so a key
# of steps compares as a tuple: a declared name by its position in ``properties``, an
# unknown one after every declared one by its code points, and an element by its index.
type Step = tuple[int, int, str]

_DECLARED: Final = 0
_UNKNOWN: Final = 1
# The step of a path that names no element: before the first element of the array, so the
# ``min_items`` of ``lines`` comes before the problems of ``lines[0]``.
_NO_INDEX: Final[Step] = (0, 0, "")


def name_step(names: Sequence[str], name: str) -> Step:
    """Where ``name`` sits among the keys of one object (requirement 12.12)."""
    if name in names:
        return (_DECLARED, names.index(name), "")
    return (_UNKNOWN, 0, name)


def order_key(schema: Mapping[str, JsonValue], path: str) -> tuple[Step, ...]:
    """The position of the detail item of ``path`` among the details of its error.

    Requirement 12.12 reads as one pair of steps per level of the path: the declared
    parameters of an object in declaration order, its unknown keys after them by code
    point, and the elements of an array by index. ``path`` is written the way an error
    reports it: ``remark``, ``lines``, ``lines[2]`` or ``lines[2].qty``.
    """
    steps: list[Step] = []
    level = schema
    for segment in path.split("."):
        name, bracket, index = segment.partition("[")
        declared = properties_of(level)
        steps.append(name_step(tuple(declared), name))
        steps.append((int(index.removesuffix("]")) + 1, 0, "") if bracket else _NO_INDEX)
        level = declared.get(name, {})
        if bracket:
            level = items_of(level)
    return tuple(steps)


def in_report_order(
    schema: Mapping[str, JsonValue], problems: Iterable[tuple[str, str]]
) -> list[tuple[str, str]]:
    """``problems`` as the details of their error list them (requirement 12.12).

    Each problem is the ``(field, reason)`` of one detail item; no two carry the same path,
    so the order is decided by :func:`order_key` alone.
    """
    return sorted(problems, key=lambda problem: order_key(schema, problem[0]))


# --- the tools the examples are drawn from ----------------------------------------------

# A bill of this module alone: the header of the fixture's ``count_sheet`` with one field of
# each type its generated tools have no parameter of, so a ``pattern``, a numeric ``enum``,
# an unbounded ``integer`` and a short ``maxLength`` are drawn as well.
SAMPLE_BILL: Final = "count_sheet_sample"
EXTRA_FIELDS: Final[tuple[tuple[str, FieldType, Mapping[str, Any]], ...]] = (
    ("fee", "money", {}),
    ("term", "int", {}),
    ("urgent", "bool", {}),
    ("cycle", "period", {}),
    ("tag", "string", {"max_length": 4}),
    ("levy", "rate", {"values": ("0", "0.06", "0.130")}),
)


def with_sample_bill(domain: DomainModel) -> tuple[DomainModel, ToolSpec]:
    """``domain`` with :data:`SAMPLE_BILL` added to it, and the create tool of that bill.

    The bill is a copy of ``count_sheet`` under a name of its own, so the tools of the
    fixture keep working against the same resolved configuration.
    """
    source = domain.bills["count_sheet"]
    header = FrozenMap(
        {
            **source.header,
            **{
                name: FieldModel(
                    name=name, type=field_type, origin="declared", writable=True, **overrides
                )
                for name, field_type, overrides in EXTRA_FIELDS
            },
        }
    )
    sample = replace(
        source,
        name=SAMPLE_BILL,
        header=header,
        header_declared_fields=(
            *source.header_declared_fields,
            *(name for name, _, _ in EXTRA_FIELDS),
        ),
    )
    built = bill_input(sample, "create")
    spec = ToolSpec(
        name=f"{SAMPLE_BILL}_create",
        generated_name=f"{SAMPLE_BILL}_create",
        pack=sample.pack,
        kind="entity_op",
        action="create",
        target=SAMPLE_BILL,
        input_schema=built.schema,
        param_paths=built.param_paths,
    )
    return replace(domain, bills=FrozenMap({**domain.bills, SAMPLE_BILL: sample})), spec


def fixture_tools() -> tuple[DomainModel, tuple[ToolSpec, ...]]:
    """The resolved configuration of the engine test fixture and every tool validated here."""
    loader = load_fixture(6)
    domain, generated = loader.domain, loader.tools
    assert domain is not None and generated is not None, "the fixture must load through step 6"
    model, sample = with_sample_bill(domain)
    return model, (*generated.tools, sample)


DOMAIN, TOOLS = fixture_tools()

# One tool of each shape an inputSchema has (requirement 11.4): the sample bill, whose
# header carries every type of parameter, the create and the update of a bill with an array,
# a push, a search with a keyword, an enum filter and a date range, and the stock balance
# query. Most of the tools a profile exposes take an ``id`` alone, so a drawn tool is one of
# these as often as it is any of them; the orders the property is about need several paths.
SHAPE_NAMES: Final[tuple[str, ...]] = (
    f"{SAMPLE_BILL}_create",
    "supply_order_create",
    "supply_order_update",
    "pay_slip_create_from_fee",
    "supply_order_search",
    "stock_level_query",
)
SHAPES: Final[tuple[ToolSpec, ...]] = tuple(spec for spec in TOOLS if spec.name in SHAPE_NAMES)
assert len(SHAPES) == len(SHAPE_NAMES), "the fixture renamed one of the tools drawn by name"

SPECS: Final = st.one_of(st.sampled_from(TOOLS), st.sampled_from(SHAPES))

# The upper bound each type of parameter carries, and the reason reported for a value over
# it: the ``maximum`` of ``page_size`` and the ``maxItems`` of ``lines`` (requirement 11.6).
BOUNDS: Final[Mapping[str, tuple[str, str]]] = {
    "integer": ("maximum", MAXIMUM_REASON),
    "array": ("maxItems", MAX_ITEMS_REASON),
}


def bounds_of(schema: Mapping[str, JsonValue]) -> tuple[tuple[str, str, int], ...]:
    """The top-level parameters with an upper bound: the name, the reason and the bound.

    The two bounds requirement 12.3 ends a call over. A bound further down a path is
    neither of them: the validator weighs the top level alone.
    """
    found: list[tuple[str, str, int]] = []
    for name, sub in properties_of(schema).items():
        bound = BOUNDS.get(str(sub.get("type")))
        if bound is not None and bound[0] in sub:
            found.append((name, bound[1], count_of(sub[bound[0]], 0)))
    return tuple(found)


BOUNDED: Final[tuple[ToolSpec, ...]] = tuple(spec for spec in TOOLS if bounds_of(spec.input_schema))

# --- a call every tool accepts -----------------------------------------------------------

# The text of a string parameter: short enough for any ``maxLength`` and free of the lone
# surrogate code points that are a type error of their own (design.md 5.2).
ALPHABET: Final = "abXY09_-中"
MAX_TEXT: Final = 8

# The dates a date parameter is given, all of them dates of the calendar, and the periods a
# period parameter is given, all of them matching ``PERIOD_PATTERN``.
DATES: Final = st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
PERIODS: Final = DATES.map(lambda value: f"{value.year:04d}-{value.month:02d}")

# The values a number parameter is given: 1 to 999 with at most 2 decimals, which is within
# the precision of every number field (``PLACES`` of erp_mock.common.decimals) and stays
# greater than 0 after rounding, as the quantity of a line must be (requirement 12.2). The
# same value as a JSON number or as the decimal string requirement 12.5 accepts.
AMOUNTS: Final = st.decimals(
    min_value=1, max_value=999, places=2, allow_nan=False, allow_infinity=False
)
NUMBERS: Final = st.one_of(AMOUNTS.map(float), AMOUNTS.map(str))

# The most elements a drawn call writes, well inside the ``maxItems`` of 200.
MAX_BASE_LINES: Final = 3


def valid_value(schema: Mapping[str, JsonValue]) -> st.SearchStrategy[Any]:
    """A value the scalar parameter ``schema`` accepts (requirement 11.11)."""
    allowed = enum_of(schema)
    match schema.get("type"):
        case "string":
            if allowed is not None:
                return st.sampled_from(list(allowed))
            if schema.get("format") == "date":
                return DATES.map(lambda value: value.isoformat())
            if isinstance(schema.get("pattern"), str):
                return PERIODS
            size = min(MAX_TEXT, count_of(schema.get("maxLength"), MAX_TEXT))
            return st.text(alphabet=ALPHABET, max_size=size)
        case "integer":
            low = count_of(schema.get("minimum"), 0)
            return st.integers(min_value=low, max_value=count_of(schema.get("maximum"), low + 49))
        case "number":
            return st.sampled_from(list(allowed)) if allowed is not None else NUMBERS
        case "boolean":
            return st.booleans()
    raise AssertionError(f"no generated parameter has the type {schema.get('type')!r}")


@st.composite
def valid_object(draw: st.DrawFn, schema: Mapping[str, JsonValue]) -> dict[str, Any]:
    """An object ``schema`` accepts: every required parameter, and some of the others.

    The array of a bill is always written, so the mutations below have elements to break;
    every other optional parameter is drawn in or left out.
    """
    required = required_of(schema)
    values: dict[str, Any] = {}
    for name, sub in properties_of(schema).items():
        if sub.get("type") == "array":
            values[name] = draw(valid_array(sub))
        elif name in required or draw(st.booleans()):
            values[name] = draw(valid_value(sub))
    return values


@st.composite
def valid_array(draw: st.DrawFn, schema: Mapping[str, JsonValue]) -> list[dict[str, Any]]:
    """The elements of an array parameter, between its ``minItems`` and a handful."""
    low = max(count_of(schema.get("minItems"), 1), 1)
    count = draw(st.integers(min_value=low, max_value=max(low, MAX_BASE_LINES)))
    element = items_of(schema)
    return [draw(valid_object(element)) for _ in range(count)]


# --- the mutations of one call (requirements 12.2, 12.12 and 12.13) ---------------------

# Values the validator rejects as a type error, by the type the parameter declares: a JSON
# boolean is no number, ``1e3``, `` 5``, ``1,000``, ``+5``, ``.5`` and ``5.`` are no decimal
# strings (requirement 12.13), a value of more significant digits than the engine computes
# with is no number either (requirement 12.4), a float with a fraction is no integer, and a
# lone surrogate is no text the sandbox could write out (design.md 5.2).
WRONG_TYPE: Final[Mapping[str, tuple[Any, ...]]] = {
    "string": (7, 2.5, True, None, [1], {"a": 1}, "\ud800"),
    "integer": ("5", 2.5, True, None, [], {}),
    "number": (True, False, "1e3", " 5", "1,000", "+5", ".5", "5.", "", "9" * 85, None, [], {}),
    "boolean": ("true", 1, 0, None, [], {}),
    "array": ("many", 7, True, None, {}),
}

# A value no ``enum`` of a generated parameter allows: the codes of an enum field are
# identifiers and the declared values of a number field are decimals (requirement 11.11).
OUTSIDE_ENUM: Final[Mapping[str, Any]] = {"string": "__none__", "number": 7.5}

# Texts that are no date of the calendar, which a date parameter reports as a format error
# rather than a type error (requirement 12.2), and a text the period pattern rejects.
NO_DATES: Final[tuple[str, ...]] = (
    "2026-02-30",
    "2026-13-01",
    "2026-2-3",
    "20261009",
    "2026-10-09T00:00:00",
    "0000-01-01",
)
NO_PERIOD: Final = "2026-13"

# What an element of the lines array is replaced by to make it no object at all.
NOT_OBJECTS: Final[tuple[Any, ...]] = (1, "x", True, None, [])

# The names an unknown key is drawn from: no generated tool declares a parameter of any of
# them, and their code points are spread out, so the order the details put them in shows
# (requirement 12.12). The value under an unknown key is never read.
UNKNOWN_NAMES: Final[tuple[str, ...]] = ("0x", "A", "Ab", "_x", "aa", "z", "zz", "中")
UNKNOWN_VALUE: Final = 1


@dataclass(frozen=True, slots=True)
class Change:
    """One mutation of one parameter: what is sent instead of a value it accepts.

    ``reason`` is what the detail item of its path must say; ``absent`` leaves the
    parameter out, which a required one is reported missing for.
    """

    reason: str
    value: Any = None
    absent: bool = False


@dataclass(frozen=True, slots=True)
class LineChange:
    """What one element of the lines array is given: a scalar, or mutated parameters."""

    scalar: Any = None
    not_object: bool = False
    params: tuple[tuple[str, Change], ...] = ()
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan:
    """The mutations of one call: at most one per path, so each path has one reason.

    ``lines`` is empty whenever ``top`` breaks the array itself: a ``lines`` that is left
    out, is no array or is empty has no elements to report problems of.
    """

    top: tuple[tuple[str, Change], ...] = ()
    unknown: tuple[str, ...] = ()
    lines: tuple[tuple[int, LineChange], ...] = ()


def outside_enum(json_type: object, allowed: Sequence[JsonValue]) -> Any:
    """A value ``allowed`` does not hold; ``enum`` compares numbers by value (req 12.4)."""
    value = OUTSIDE_ENUM[str(json_type)]
    if json_type == "number":
        assert all(to_decimal(value) != to_decimal(str(item)) for item in allowed)
    else:
        assert value not in allowed
    return value


def changes(schema: Mapping[str, JsonValue], *, required: bool) -> tuple[Change, ...]:
    """Every mutation of ``schema`` that breaks its path in exactly one, known way.

    Which mutations apply follows the order the validator checks a path in (design.md 5.2):
    the type first, then ``enum``, then the bounds, then ``maxLength``, ``pattern`` and
    ``format``. Only the first failure of a path is reported, so a parameter with an
    ``enum`` gets no bound and no length mutation - either would be reported as an ``enum``
    problem - and a string gets whichever of its three text checks comes first.

    A value over an upper bound is no mutation of this table: it ends the call in the stage
    before the traversal, which :func:`over_bounds` draws instead (requirement 12.3).
    """
    json_type = str(schema.get("type"))
    found: list[Change] = [Change(TYPE_REASON, value) for value in WRONG_TYPE[json_type]]
    if required:
        found.append(Change(REQUIRED_REASON, absent=True))
    allowed = enum_of(schema)
    if allowed is not None:
        found.append(Change(ENUM_REASON, outside_enum(json_type, allowed)))
        return tuple(found)
    match json_type:
        case "string":
            limit = schema.get("maxLength")
            if isinstance(limit, int):
                found.append(Change(MAX_LENGTH_REASON, "长" * (limit + 1)))
            elif isinstance(schema.get("pattern"), str):
                found.append(Change(PATTERN_REASON, NO_PERIOD))
            elif schema.get("format") == "date":
                found.extend(Change(FORMAT_REASON, value) for value in NO_DATES)
        case "integer" | "number":
            low = schema.get("minimum")
            if isinstance(low, int | float) and not isinstance(low, bool):
                found.append(Change(MINIMUM_REASON, low - 1))
        case "array":
            if count_of(schema.get("minItems"), 0) > 0:
                found.append(Change(MIN_ITEMS_REASON, []))
    return tuple(found)


@st.composite
def param_changes(
    draw: st.DrawFn, schema: Mapping[str, JsonValue]
) -> tuple[tuple[str, Change], ...]:
    """A mutation of some of the declared parameters of one object, in declaration order."""
    required = required_of(schema)
    chosen: list[tuple[str, Change]] = []
    for name, sub in properties_of(schema).items():
        if draw(st.booleans()):
            options = changes(sub, required=name in required)
            chosen.append((name, draw(st.sampled_from(list(options)))))
    return tuple(chosen)


@st.composite
def unknown_keys(draw: st.DrawFn, limit: int) -> tuple[str, ...]:
    """Up to ``limit`` names no object of an inputSchema declares (requirement 12.12)."""
    drawn = draw(st.lists(st.sampled_from(UNKNOWN_NAMES), unique=True, max_size=limit))
    return tuple(drawn)


@st.composite
def line_changes(draw: st.DrawFn, schema: Mapping[str, JsonValue]) -> LineChange:
    """What one element of the lines array is given: a scalar once in four draws."""
    if draw(st.integers(min_value=0, max_value=3)) == 0:
        return LineChange(scalar=draw(st.sampled_from(NOT_OBJECTS)), not_object=True)
    return LineChange(params=draw(param_changes(schema)), unknown=draw(unknown_keys(2)))


@st.composite
def plans(draw: st.DrawFn, schema: Mapping[str, JsonValue], base: Mapping[str, Any]) -> Plan:
    """A mutation plan over the call ``base``, which ``schema`` accepts."""
    top = draw(param_changes(schema))
    unknown = draw(unknown_keys(3))
    array = properties_of(schema).get(LINES_PARAM)
    broken = any(name == LINES_PARAM for name, _ in top)
    if array is None or broken:
        return Plan(top, unknown)
    count = len(base[LINES_PARAM])
    indexes = draw(
        st.lists(st.integers(min_value=0, max_value=count - 1), unique=True, max_size=count)
    )
    element = items_of(array)
    lines = tuple((index, draw(line_changes(element))) for index in sorted(indexes))
    return Plan(top, unknown, lines)


def changed(base: Mapping[str, Any], plan: Plan) -> dict[str, Any]:
    """``base`` with ``plan`` applied to it: what the client sends instead."""
    raw = dict(base)
    for name, change in plan.top:
        if change.absent:
            raw.pop(name, None)
        else:
            raw[name] = change.value
    for name in plan.unknown:
        raw[name] = UNKNOWN_VALUE
    if plan.lines:
        array = list(raw[LINES_PARAM])
        for index, line in plan.lines:
            array[index] = changed_line(array[index], line)
        raw[LINES_PARAM] = array
    return raw


def changed_line(base: Mapping[str, Any], change: LineChange) -> Any:
    """One element of the lines array with its mutations applied."""
    if change.not_object:
        return change.scalar
    element = dict(base)
    for name, param in change.params:
        if param.absent:
            element.pop(name, None)
        else:
            element[name] = param.value
    for name in change.unknown:
        element[name] = UNKNOWN_VALUE
    return element


def problems_of(plan: Plan) -> list[tuple[str, str]]:
    """The ``(field, reason)`` of every detail item ``plan`` must produce."""
    found = [(name, change.reason) for name, change in plan.top]
    found += [(name, UNKNOWN_FIELD_REASON) for name in plan.unknown]
    for index, change in plan.lines:
        path = f"{LINES_PARAM}[{index}]"
        if change.not_object:
            found.append((path, TYPE_REASON))
            continue
        found += [(f"{path}.{name}", param.reason) for name, param in change.params]
        found += [(f"{path}.{name}", UNKNOWN_FIELD_REASON) for name in change.unknown]
    return found


@st.composite
def permuted(draw: st.DrawFn, value: Any) -> Any:
    """``value`` with the keys of every object in it written in a drawn order.

    A client's arguments are a JSON object, whose keys arrive in the order the client wrote
    them; nothing the validator reports may depend on that order (requirement 12.12).
    """
    if isinstance(value, Mapping):
        keys = draw(st.permutations(list(value)))
        return {key: draw(permuted(value[key])) for key in keys}
    if isinstance(value, list):
        return [draw(permuted(item)) for item in value]
    return value


def reversed_keys(value: Any) -> Any:
    """``value`` with the keys of every object in it written in the opposite order."""
    if isinstance(value, Mapping):
        return {key: reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(item) for item in value]
    return value


# --- the examples ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One example: a tool, a call it accepts, and that call mutated, written twice."""

    spec: ToolSpec
    base: Mapping[str, Any]
    raw: Mapping[str, Any]
    again: Mapping[str, Any]
    problems: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class BoundCase:
    """One example of requirement 12.3: a call over an upper bound, and the same within it.

    ``within`` carries every mutation of the call but the ones over a bound, so it ends in
    the VALIDATION_FAILED the bound outranks; ``raw`` is ``within`` with each parameter of
    ``problems`` put over its bound.
    """

    spec: ToolSpec
    within: Mapping[str, Any]
    raw: Mapping[str, Any]
    problems: tuple[tuple[str, str], ...]


@st.composite
def mutated(draw: st.DrawFn) -> Case:
    """A tool, a call it accepts and the mutations of that call, keys in a drawn order."""
    spec = draw(SPECS)
    schema = spec.input_schema
    base = draw(valid_object(schema))
    plan = draw(plans(schema, base))
    raw = draw(permuted(changed(base, plan)))
    return Case(
        spec=spec,
        base=base,
        raw=raw,
        again=reversed_keys(raw),
        problems=tuple(in_report_order(schema, problems_of(plan))),
    )


@st.composite
def over_bounds(draw: st.DrawFn) -> BoundCase:
    """A mutated call with at least one parameter over its upper bound (requirement 12.3)."""
    spec = draw(st.sampled_from(BOUNDED))
    schema = spec.input_schema
    base = draw(valid_object(schema))
    within = changed(base, draw(plans(schema, base)))
    # One unknown key, so the call has a problem of requirement 12.2 whatever was drawn.
    within[draw(st.sampled_from(UNKNOWN_NAMES))] = UNKNOWN_VALUE
    bounds = bounds_of(schema)
    over = draw(st.lists(st.sampled_from(bounds), unique_by=lambda bound: bound[0], min_size=1))
    raw = dict(within)
    for name, _, bound in over:
        extra = draw(st.integers(min_value=1, max_value=3))
        raw[name] = beyond(properties_of(schema)[name], base.get(name), bound + extra)
    return BoundCase(
        spec=spec,
        within=within,
        raw=raw,
        problems=tuple(in_report_order(schema, [(name, reason) for name, reason, _ in over])),
    )


def beyond(schema: Mapping[str, JsonValue], base: Any, size: int) -> Any:
    """A value of the parameter ``schema`` of size ``size``, which is over its bound.

    An integer parameter gets the number ``size``; an array parameter gets ``size``
    elements: the ones the call already carried, padded with objects that miss every
    required parameter of an element, so the arguments are wrong in several ways besides
    being too long.
    """
    if schema.get("type") == "integer":
        return size
    elements: list[Any] = list(base) if isinstance(base, list) else []
    return [*elements, *({} for _ in range(size - len(elements)))]


# --- reading one outcome -----------------------------------------------------------------


def shape(value: Any) -> Any:
    """``value`` as a structure whose comparison covers the key order of every object."""
    if isinstance(value, Mapping):
        return [(key, shape(item)) for key, item in value.items()]
    if isinstance(value, list | tuple):
        return [shape(item) for item in value]
    return value


def outcome(args: ValidatedArgs) -> Any:
    """Everything input validation reports, as a value two calls can be compared by."""
    error = args.error
    return (
        shape(args.values),
        args.coerced,
        None if error is None else (error.code, error.message, error.rule_id, shape(error.details)),
    )


def reasons(args: ValidatedArgs) -> list[tuple[str, str]]:
    """The ``(field, reason)`` of each detail item, in report order (requirement 12.12)."""
    error = args.error
    assert error is not None, "expected the arguments to be rejected"
    return [(str(item["field"]), str(item["reason"])) for item in error.details]


def code(args: ValidatedArgs) -> ErrorCode:
    """The error code of a rejected call."""
    error = args.error
    assert error is not None, "expected the arguments to be rejected"
    return error.code


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 13: 入参校验的确定性、排序与上限优先
@pytest.mark.req("12.2")
@settings(max_examples=100)
@given(case=mutated())
def test_the_same_arguments_always_give_the_same_outcome(case: Case) -> None:
    first = validate_args(DOMAIN, case.spec, case.raw)
    second = validate_args(DOMAIN, case.spec, case.raw)
    # The same arguments with their keys written in another order (requirement 12.12).
    shuffled = validate_args(DOMAIN, case.spec, case.again)

    assert outcome(first) == outcome(second)
    assert outcome(first) == outcome(shuffled)


# Feature: erp-mock-mcp-m0, Property 13: 入参校验的确定性、排序与上限优先
@pytest.mark.req("12.2", "12.12", "12.13")
@settings(max_examples=100)
@given(case=mutated())
def test_the_details_hold_one_item_per_broken_path_in_schema_order(case: Case) -> None:
    # The mutations start from a call the tool accepts, so every detail item below is one
    # of them and nothing else.
    accepted = validate_args(DOMAIN, case.spec, case.base)
    assert accepted.error is None, f"the base call was rejected: {accepted.error}"

    args = validate_args(DOMAIN, case.spec, case.raw)

    if not case.problems:
        # Nothing was broken, so the call is the one the tool accepts.
        assert args.error is None, f"unexpected error: {args.error}"
        return
    # One item per mutated path, each with the reason of its mutation, in the order of the
    # reference model: no path appears twice and no path the mutations left alone appears.
    assert code(args) == ErrorCode.VALIDATION_FAILED
    assert reasons(args) == list(case.problems)


# Feature: erp-mock-mcp-m0, Property 13: 入参校验的确定性、排序与上限优先
@pytest.mark.req("12.3")
@settings(max_examples=100)
@given(case=over_bounds())
def test_an_upper_bound_outranks_every_other_error(case: BoundCase) -> None:
    # The same call within its bounds ends in the error the bound outranks.
    assert code(validate_args(DOMAIN, case.spec, case.within)) == ErrorCode.VALIDATION_FAILED

    args = validate_args(DOMAIN, case.spec, case.raw)

    # One item per parameter over its bound, in declaration order, and nothing else: the
    # problems of requirement 12.2 the call also has are not reported here.
    assert code(args) == ErrorCode.LIMIT_EXCEEDED
    assert reasons(args) == list(case.problems)
    # The call ended before the traversal, so no argument was normalized or converted.
    assert dict(args.values) == {}
    assert args.coerced == ()


@pytest.mark.req("12.12")
def test_the_order_model_reads_the_declaration_order_of_the_schema() -> None:
    """The reference model of the property, pinned to one schema the fixture generates.

    ``supply_order_create`` declares vendor_id, need_date, price_includes_tax, biz_date,
    remark and lines, and its elements article_id, measure_id, tax_rate, qty and price.
    """
    schema = next(spec for spec in TOOLS if spec.name == "supply_order_create").input_schema
    paths = ("zz", "lines[1].article_id", "Ab", "lines", "lines[0].aa", "remark", "lines[0]")

    assert [path for path, _ in in_report_order(schema, [(path, "") for path in paths])] == [
        # The declared parameters in declaration order, the array before its elements, the
        # elements by index, and the unknown keys of each level after its declared ones by
        # code point: "Ab" before "zz" (requirement 12.12).
        "remark",
        "lines",
        "lines[0]",
        "lines[0].aa",
        "lines[1].article_id",
        "Ab",
        "zz",
    ]
    assert order_key(schema, "lines[0].price") < order_key(schema, "lines[0].aa")
