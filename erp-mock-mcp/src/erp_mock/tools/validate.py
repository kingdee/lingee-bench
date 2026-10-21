"""Input validation and argument normalization of one tool call (design.md 5.2).

Steps 2 and 3 of the pipeline (requirement 12.1): what a client sent is checked against
the inputSchema of the tool it called (:mod:`erp_mock.tools.input_schema`) and turned into
the typed, rounded arguments the engine runs on (``Arguments`` of
:mod:`erp_mock.domain.engine`). Both run outside the transaction, so a call that ends here
left the sandbox database untouched (design.md 5.3).

Why the schema is walked here instead of by a JSON Schema library (design.md issue 7):

- a number parameter also accepts a decimal string, and every path converted that way goes
  into the trace (requirements 12.5 and 17.11);
- ``enum`` and ``minimum`` compare Decimals, so 0.130 is 0.13 (requirement 12.4);
- the details of one error carry one item per field path, ordered by the schema
  (requirement 12.12), and a ``page_size`` or a ``lines`` over its upper bound outranks
  every other error (requirement 12.3);
- the arguments come out in the Python types the storage layer writes: a Decimal rounded to
  the precision of its field, a ``datetime.date``, an int, a bool or a str
  (requirement 12.6, design.md data model 3).

The order of the checks (design.md 5.2):

1. the upper bounds: a ``page_size`` greater than its ``maximum`` or a ``lines`` with more
   elements than its ``maxItems`` ends the call with LIMIT_EXCEEDED and one detail item per
   parameter over its bound, whatever else is wrong with the arguments (requirement 12.3).
2. the declared parameters, in the order the schema declares them, then the unknown ones by
   the code point order of their names; the elements of an array by index, each element the
   same way (requirement 12.12). One path contributes at most one detail item: a scalar is
   checked for its type, then ``enum``, ``minimum`` and ``maximum``, then ``maxLength``,
   ``pattern`` and ``format``, and the first failure is the one reported.
3. rounding and the line quantity: a qty, price, money or rate value is rounded ROUND_HALF_UP
   to the precision of its field, and a line quantity that is not greater than 0 after
   rounding is reported in the same pass as the schema problems, so the details stay in one
   order (requirements 12.2 and 12.6).

What comes back is a :class:`ValidatedArgs`: the normalized arguments, the coerced paths and
the error, if any. The error is returned rather than raised because requirement 17.11 wants
the coerced paths of a call that *failed* validation as well - only a call that ends before
schema validation, such as one over a resource limit (requirement 23.8), reports none.

``validate_args`` takes the resolved domain model next to the spec: the precision a number
parameter is rounded to and the line quantity of a bill are properties of the fields behind
the parameters, which the 14 keywords of an inputSchema do not carry (requirement 11.3).

The reasons of the detail items are those of design.md data model 6: the schema reasons of
its table and :data:`POSITIVE_REASON`, all of them in :data:`REASONS`. The message of an
error names the field path of its first item, in simplified Chinese (requirement 13.7).
"""

import decimal
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

from erp_mock.common.decimals import PLACES, DecimalKind, round_half_up, to_decimal
from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.fields import FieldType
from erp_mock.config.model import BillModel, DomainModel, FieldModel
from erp_mock.domain.engine import Arguments, Lines
from erp_mock.state.store import Value
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import LINES_PARAM

__all__ = [
    "ENUM_REASON",
    "FORMAT_REASON",
    "MAXIMUM_REASON",
    "MAX_ITEMS_REASON",
    "MAX_LENGTH_REASON",
    "MINIMUM_REASON",
    "MIN_ITEMS_REASON",
    "PATTERN_REASON",
    "POSITIVE_REASON",
    "REASONS",
    "REQUIRED_REASON",
    "TYPE_REASON",
    "UNKNOWN_FIELD_REASON",
    "ArgValue",
    "ValidatedArgs",
    "quantity_field",
    "validate_args",
]

# One normalized argument: the typed value of a scalar parameter, or the lines array as the
# tuple of maps an operation takes (``Lines`` of erp_mock.domain.engine). A parameter the
# call did not carry contributes no argument at all, so None is only ever the value of a
# parameter that may be empty - which no inputSchema declares (requirement 11.3).
type ArgValue = Value | Lines

# The reasons of design.md data model 6 that input validation reports: the schema ones, and
# the line quantity that is not positive after rounding (requirement 12.2).
REQUIRED_REASON: Final = "required"
UNKNOWN_FIELD_REASON: Final = "unknown_field"
TYPE_REASON: Final = "type"
ENUM_REASON: Final = "enum"
MINIMUM_REASON: Final = "minimum"
MAXIMUM_REASON: Final = "maximum"
MIN_ITEMS_REASON: Final = "min_items"
MAX_ITEMS_REASON: Final = "max_items"
MAX_LENGTH_REASON: Final = "max_length"
PATTERN_REASON: Final = "pattern"
FORMAT_REASON: Final = "format"
POSITIVE_REASON: Final = "must_be_positive"

# The keys of one detail item, in the order they are written (design.md data model 6).
_FIELD_KEY: Final = "field"
_REASON_KEY: Final = "reason"
_EXPECTED_KEY: Final = "expected"

# The decimal places of a quantity, which a line quantity is rounded to before it is
# compared with 0 (requirements 4.7 and 12.2).
_QTY_PLACES: Final = PLACES["qty"]

# How each reason reads in the message of the error (requirement 13.7): ``field`` is the
# path of the first detail item, ``expected`` what the schema allows there.
_MESSAGES: Final[Mapping[str, str]] = {
    REQUIRED_REASON: "缺少必填参数 {field}",
    UNKNOWN_FIELD_REASON: "参数 {field} 不是本工具的参数",
    TYPE_REASON: "参数 {field} 的类型必须是 {expected}",
    ENUM_REASON: "参数 {field} 的取值必须是 {expected} 之一",
    MINIMUM_REASON: "参数 {field} 不能小于 {expected}",
    MAXIMUM_REASON: "参数 {field} 不能大于 {expected}",
    MIN_ITEMS_REASON: "参数 {field} 至少要有 {expected} 个元素",
    MAX_ITEMS_REASON: "参数 {field} 最多 {expected} 个元素",
    MAX_LENGTH_REASON: "参数 {field} 的长度不能超过 {expected} 个字符",
    PATTERN_REASON: "参数 {field} 的写法不符合要求",
    FORMAT_REASON: "参数 {field} 不是有效的日期",
    POSITIVE_REASON: f"参数 {{field}} 舍入到 {_QTY_PLACES} 位小数后必须大于 0",
}

# Every reason this module reports, in the order of design.md data model 6.
REASONS: Final[tuple[str, ...]] = tuple(_MESSAGES)

# What the rest of the detail items is called in the message, one phrase per error code.
_VALIDATION_REST: Final = "处参数有误"
_LIMIT_REST: Final = "处超过上限"

# The JSON types an inputSchema writes (requirement 11.11).
_ARRAY: Final = "array"
_BOOLEAN: Final = "boolean"
_INTEGER: Final = "integer"
_NUMBER: Final = "number"
_OBJECT: Final = "object"
_STRING: Final = "string"

# The ``format`` of a date parameter, whose text is also checked against the calendar
# (requirement 12.2).
_DATE_FORMAT: Final = "date"

# The standard quantity field of a bill with pricing (meta-model.md section 4).
_QTY: Final = "qty"

# ``YYYY-MM-DD``, the only form a date parameter takes (requirement 11.11). date.fromisoformat
# alone would also read ``20261009`` and ``2026-W41-5``, which no client may send.
_DATE_TEXT: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# A surrogate code point. A string decoded from JSON holds one only where the client wrote a
# lone ``\udXXX`` escape: the decoder combines a high surrogate followed by a low one into the
# supplementary character they spell, so any surrogate left in a Python string is lone, and
# UTF-8 cannot encode it (design.md 5.2, erp_mock.common.jsonio).
_SURROGATE: Final = re.compile(r"[\ud800-\udfff]")


@dataclass(frozen=True, slots=True)
class ValidatedArgs:
    """The outcome of input validation: the arguments, the coerced paths and the error.

    - ``values``: the normalized arguments under the names of the parameters that carried
      them, in declaration order: a Decimal rounded to the precision of its field, a
      ``datetime.date``, an int, a bool, a str, and the lines array as a tuple of maps
      (:data:`ArgValue`). A parameter the call did not carry is absent, so the engine sees
      the difference between an update that replaces the lines and one that leaves them
      alone (requirement 5.2). After an error the map holds what was normalized before it,
      which no caller reads.
    - ``coerced``: the paths of the number parameters that arrived as decimal strings, in
      the order of the details (requirements 12.5 and 17.11). It is filled whether or not
      validation failed; a call that ends in the upper-bound stage reports none, since
      nothing was converted there.
    - ``error``: the VALIDATION_FAILED or LIMIT_EXCEEDED of this call, None when the
      arguments are good (requirements 12.2 and 12.3).
    """

    values: FrozenMap[str, ArgValue] = FrozenMap()
    coerced: tuple[str, ...] = ()
    error: ErpError | None = None

    @property
    def scalars(self) -> Arguments:
        """Every argument but the lines: the values of a record, a header or a read."""
        return FrozenMap(
            (name, value)
            for name, value in self.values.items()
            if name != LINES_PARAM and not isinstance(value, tuple)
        )

    @property
    def lines(self) -> Lines | None:
        """The lines array, None where the call carried none (requirements 5.2, 5.3)."""
        found = self.values.get(LINES_PARAM)
        return found if isinstance(found, tuple) else None


def validate_args(
    domain: DomainModel, spec: ToolSpec, raw: Mapping[str, object] | None = None
) -> ValidatedArgs:
    """Check ``raw`` against the inputSchema of ``spec`` and normalize it (req 12.2 to 12.6).

    ``raw`` is what the client sent, parsed JSON and nothing else, None for a call that
    carried no arguments at all; ``domain`` is the resolved configuration, read for the
    precision of each number parameter and for the quantity field of a bill's lines.

    Returns the arguments, the coerced paths and the error of this call
    (:class:`ValidatedArgs`); it raises nothing of its own, so the coerced paths of a call
    that failed validation reach the trace as well (requirement 17.11).
    """
    args: Mapping[str, object] = {} if raw is None else raw
    plan = _plan(domain, spec)
    exceeded = _over_bounds(plan, args)
    if exceeded:
        return ValidatedArgs(error=_error(ErrorCode.LIMIT_EXCEEDED, exceeded, _LIMIT_REST))
    found = _Found()
    values = _object_values(plan, args, "", found)
    error = (
        None
        if not found.problems
        else _error(ErrorCode.VALIDATION_FAILED, found.problems, _VALIDATION_REST)
    )
    return ValidatedArgs(FrozenMap(values), tuple(found.coerced), error)


def quantity_field(bill: BillModel) -> str | None:
    """The name of the quantity of ``bill``'s lines, None where they measure none.

    The standard ``qty`` of a bill with pricing and the ``qty_field`` of its quantity
    semantics otherwise, which is how requirement 6.6 reads the quantity of a line; a
    parameter of that name must be greater than 0 after rounding (requirement 12.2).
    :attr:`BillModel.qty_field <erp_mock.config.model.BillModel.qty_field>` is the same
    name for every bill that declares quantity semantics, and None for a bill with pricing
    that declares none, whose lines carry the standard ``qty`` all the same.
    """
    if bill.pricing:
        return _QTY
    return None if bill.line_quantity is None else bill.line_quantity.qty_field


# --- the plan of one tool, which the walk reads (design.md 5.2) --------------------------


@dataclass(frozen=True, slots=True)
class _Param:
    """One declared parameter: its schema, read once, and what its field adds.

    ``minimum`` and ``maximum`` are the bounds as the schema writes them, for the detail
    item, and ``at_least`` and ``at_most`` the same bounds as Decimals, for the comparison
    (requirement 12.4); ``enum`` and ``numbers`` are the allowed values the same two ways.
    ``kind`` is the precision a number is rounded to and ``positive`` marks the quantity of
    a line (requirements 12.2 and 12.6). ``element`` is the object of an array's elements.
    """

    name: str
    json_type: str
    required: bool = False
    enum: tuple[JsonValue, ...] | None = None
    numbers: tuple[Decimal, ...] | None = None
    minimum: JsonValue | None = None
    maximum: JsonValue | None = None
    at_least: Decimal | None = None
    at_most: Decimal | None = None
    max_length: int | None = None
    pattern: str | None = None
    is_date: bool = False
    min_items: int | None = None
    max_items: int | None = None
    kind: DecimalKind | None = None
    positive: bool = False
    element: "_Object | None" = None


@dataclass(frozen=True, slots=True)
class _Object:
    """The declared parameters of one object, in declaration order, and their names."""

    params: tuple[_Param, ...] = ()
    names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _Level:
    """The fields behind the parameters of one object and the name of its quantity.

    ``types`` maps a parameter name to the type of the field it writes or filters by, which
    is where the precision of a number comes from; a parameter that is no field of the level
    - ``keyword``, ``page``, the two ends of a date range - is absent from it.
    """

    types: Mapping[str, FieldType] = FrozenMap()
    quantity: str | None = None


# --- the problems of one call ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Problem:
    """One thing that is wrong with the arguments: a path, a reason and what was expected."""

    path: str
    reason: str
    expected: JsonValue | None = None

    @property
    def item(self) -> Mapping[str, JsonValue]:
        """The detail item of the problem, its keys in order (design.md data model 6)."""
        item: dict[str, JsonValue] = {_FIELD_KEY: self.path, _REASON_KEY: self.reason}
        if self.expected is not None:
            item[_EXPECTED_KEY] = self.expected
        return item

    @property
    def message(self) -> str:
        """The problem in simplified Chinese, naming its field path (requirement 13.7)."""
        template = _MESSAGES.get(self.reason, "参数 {field} 不符合要求")
        return template.format(field=self.path, expected=_expected_text(self.expected))


@dataclass(slots=True)
class _Found:
    """What the walk over the arguments collected: the problems and the coerced paths."""

    problems: list[_Problem] = field(default_factory=list)
    coerced: list[str] = field(default_factory=list)

    def add(self, path: str, reason: str, expected: JsonValue | None = None) -> None:
        """Record one problem; the walk goes on, so every path gets its own item."""
        self.problems.append(_Problem(path, reason, expected))


def _error(code: ErrorCode, problems: Sequence[_Problem], rest: str) -> ErpError:
    """The error of ``problems``: one detail item each, the first one in the message."""
    first = problems[0]
    message = first.message
    if len(problems) > 1:
        message = f"{message}；另有 {len(problems) - 1} {rest}"
    return ErpError(code, message, details=[problem.item for problem in problems])


def _expected_text(expected: JsonValue | None) -> str:
    """``expected`` as it reads in a message: a list of values separated by ``、``."""
    if expected is None:
        return ""
    if isinstance(expected, str):
        return expected
    if isinstance(expected, bool):
        return "true" if expected else "false"
    if isinstance(expected, Sequence):
        return "、".join(_expected_text(item) for item in expected)
    return str(expected)


# --- the upper bounds, which outrank every other error (requirement 12.3) ----------------


def _over_bounds(plan: _Object, raw: Mapping[str, object]) -> tuple[_Problem, ...]:
    """The top-level parameters over their upper bound, in declaration order.

    The ``maximum`` of ``page_size`` and the ``maxItems`` of ``lines``, the two upper bounds
    requirement 11.6 declares. A value that is no integer or no array at all is not over the
    bound: it is a type error of the traversal (requirement 12.2).
    """
    found: list[_Problem] = []
    for param in plan.params:
        if param.name not in raw:
            continue
        value = raw[param.name]
        if param.json_type == _INTEGER and param.at_most is not None:
            number = _as_integer(value)
            if number is not None and number > param.at_most:
                found.append(_Problem(param.name, MAXIMUM_REASON, param.maximum))
        elif param.json_type == _ARRAY and param.max_items is not None:
            if isinstance(value, list | tuple) and len(value) > param.max_items:
                found.append(_Problem(param.name, MAX_ITEMS_REASON, param.max_items))
    return tuple(found)


# --- the traversal (requirements 12.2, 12.12 and 12.13) ----------------------------------


def _object_values(
    plan: _Object, raw: Mapping[str, object], prefix: str, found: _Found
) -> dict[str, ArgValue]:
    """The normalized arguments of one object; problems go to ``found``.

    The declared parameters in declaration order, each missing required one recorded, then
    the keys the object does not declare, by the code point order of their names
    (requirement 12.12). ``prefix`` is what the path of a parameter of this object starts
    with: the empty string at the top level and ``lines[0].`` inside an element.
    """
    values: dict[str, ArgValue] = {}
    for param in plan.params:
        path = f"{prefix}{param.name}"
        if param.name not in raw:
            if param.required:
                found.add(path, REQUIRED_REASON)
            continue
        value = _param_value(param, raw[param.name], path, found)
        if value is not None:
            values[param.name] = value
    for name in sorted(key for key in raw if key not in plan.names):
        found.add(f"{prefix}{name}", UNKNOWN_FIELD_REASON)
    return values


def _param_value(param: _Param, value: object, path: str, found: _Found) -> ArgValue | None:
    """The normalized value of one parameter, None when it was rejected.

    No accepted value is None - an inputSchema declares no parameter that may be empty
    (requirement 11.3) - so None says that ``found`` carries the problem of this path.

    The patterns are written out rather than taken from the constants above: a bare name in
    a ``case`` binds the subject instead of comparing with it.
    """
    match param.json_type:
        case "array":
            return _array_value(param, value, path, found)
        case "number":
            return _number_value(param, value, path, found)
        case "integer":
            return _integer_value(param, value, path, found)
        case "boolean":
            if not isinstance(value, bool):
                found.add(path, TYPE_REASON, _BOOLEAN)
                return None
            return value
        case "string":
            return _string_value(param, value, path, found)
        case _:
            # No inputSchema declares a parameter of another type (requirement 11.11).
            found.add(path, TYPE_REASON, param.json_type)
            return None


def _array_value(param: _Param, value: object, path: str, found: _Found) -> Lines | None:
    """The elements of an array parameter: the lines of a bill (requirements 11.6, 12.12).

    ``minItems`` and ``maxItems`` first, then the elements by index; an element that is no
    object is a type error of its own path and contributes no line. The ``maxItems`` of a
    call that reaches here is within the bound, which the upper-bound stage settled.
    """
    if not isinstance(value, list | tuple):
        found.add(path, TYPE_REASON, _ARRAY)
        return None
    if param.min_items is not None and len(value) < param.min_items:
        found.add(path, MIN_ITEMS_REASON, param.min_items)
    elif param.max_items is not None and len(value) > param.max_items:
        found.add(path, MAX_ITEMS_REASON, param.max_items)
    element = param.element if param.element is not None else _Object()
    lines: list[Arguments] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, Mapping):
            found.add(item_path, TYPE_REASON, _OBJECT)
            continue
        values = _object_values(element, item, f"{item_path}.", found)
        lines.append(FrozenMap(_as_arguments(values)))
    return tuple(lines)


def _as_arguments(values: Mapping[str, ArgValue]) -> dict[str, Value]:
    """The values of one element as the arguments of a line: no element holds an array."""
    return {name: value for name, value in values.items() if not isinstance(value, tuple)}


def _number_value(param: _Param, value: object, path: str, found: _Found) -> Decimal | None:
    """One qty, price, money or rate argument (requirements 12.4 to 12.6, 12.13).

    A JSON number or a decimal string, the latter recorded in ``coerced``; a JSON boolean, a
    non-finite number, a string of another form (``"1e3"``, ``" 5"``, ``"1,000"``) and a value
    with more significant digits than the engine's arithmetic holds are type errors.
    ``enum``, ``minimum`` and ``maximum`` compare the value as it was written, so 0.130 is
    0.13; the argument is then rounded to the precision of its field, and a line quantity
    that is not greater than 0 after rounding is reported here as well.
    """
    number = _as_decimal(value)
    if number is None:
        found.add(path, TYPE_REASON, _NUMBER)
        return None
    rounded = _rounded(number, param.kind)
    if rounded is None:
        found.add(path, TYPE_REASON, _NUMBER)
        return None
    if isinstance(value, str):
        found.coerced.append(path)
    if _out_of_range(param, number, path, found):
        return None
    if param.positive and rounded <= 0:
        found.add(path, POSITIVE_REASON)
        return None
    return rounded


def _integer_value(param: _Param, value: object, path: str, found: _Found) -> int | None:
    """One int argument: a JSON integer, a float without a fraction, never a boolean.

    ``page`` and ``page_size`` below their ``minimum`` of 1 are rejected here, before the
    engine sees them (requirement 5.15).
    """
    number = _as_integer(value)
    if number is None:
        found.add(path, TYPE_REASON, _INTEGER)
        return None
    if _out_of_range(param, Decimal(number), path, found):
        return None
    return number


def _out_of_range(param: _Param, number: Decimal, path: str, found: _Found) -> bool:
    """Whether ``number`` breaks the ``enum``, ``minimum`` or ``maximum`` of ``param``.

    Every comparison is between Decimals, so a value equals an allowed one whenever the two
    are numerically equal (requirement 12.4).
    """
    if param.numbers is not None and all(number != allowed for allowed in param.numbers):
        found.add(path, ENUM_REASON, param.enum)
        return True
    if param.at_least is not None and number < param.at_least:
        found.add(path, MINIMUM_REASON, param.minimum)
        return True
    if param.at_most is not None and number > param.at_most:
        found.add(path, MAXIMUM_REASON, param.maximum)
        return True
    return False


def _string_value(param: _Param, value: object, path: str, found: _Found) -> str | date | None:
    """One text argument: a ref, an enum code, a string, a text, a period or a date.

    A lone surrogate code point is a type error, so nothing the call writes can fail to
    encode as UTF-8 later on (design.md 5.2). A date parameter comes back as a
    ``datetime.date``, which is what the storage layer writes (design.md data model 3); a
    text that is no date of the calendar, such as ``2026-02-30``, is a format error
    (requirement 12.2).
    """
    if not isinstance(value, str) or _SURROGATE.search(value) is not None:
        found.add(path, TYPE_REASON, _STRING)
        return None
    if param.enum is not None and all(value != allowed for allowed in param.enum):
        found.add(path, ENUM_REASON, param.enum)
        return None
    if param.max_length is not None and len(value) > param.max_length:
        found.add(path, MAX_LENGTH_REASON, param.max_length)
        return None
    if param.pattern is not None and not _matches(param.pattern, value):
        found.add(path, PATTERN_REASON, param.pattern)
        return None
    if not param.is_date:
        return value
    parsed = _calendar_date(value)
    if parsed is None:
        found.add(path, FORMAT_REASON, _DATE_FORMAT)
    return parsed


# --- reading one JSON value --------------------------------------------------------------


def _as_decimal(value: object) -> Decimal | None:
    """``value`` as a Decimal, None when it is no number (requirements 12.4, 12.5, 12.13)."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return to_decimal(value)
    except (TypeError, ValueError):
        return None


def _as_integer(value: object) -> int | None:
    """``value`` as an int, None when it is no integer; a JSON boolean is no number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _rounded(number: Decimal, kind: DecimalKind | None) -> Decimal | None:
    """``number`` rounded to the precision of ``kind`` (requirements 4.7 and 12.6).

    None for a value of more significant digits than the engine's arithmetic holds, which
    is reported as a type error: no argument the engine cannot compute with gets through.
    ``kind`` is None only for a number parameter whose field no valid instance has.
    """
    if kind is None:
        return number
    try:
        return round_half_up(number, PLACES[kind])
    except decimal.InvalidOperation:
        return None


def _matches(pattern: str, text: str) -> bool:
    """Whether ``text`` matches the ``pattern`` of its parameter (requirement 11.11).

    The one pattern the generator writes is the fully anchored period pattern
    (``PERIOD_PATTERN`` of :mod:`erp_mock.tools.input_schema`); its anchors are dropped and
    the rest must match the whole text, so a trailing newline does not slip through
    Python's ``$``, which JSON Schema's ECMAScript regexes do not accept either.
    """
    body = pattern.removeprefix("^").removesuffix("$")
    return re.fullmatch(body, text) is not None


def _calendar_date(text: str) -> date | None:
    """``text`` as a date, None unless it is a ``YYYY-MM-DD`` date of the calendar (12.2)."""
    if _DATE_TEXT.fullmatch(text) is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


# --- building the plan from the schema and the model (design.md 5.2) ---------------------


def _plan(domain: DomainModel, spec: ToolSpec) -> _Object:
    """The schema of ``spec`` with the fields of ``domain`` behind its parameters."""
    level, element = _levels(domain, spec)
    return _object_plan(spec.input_schema, level, element)


def _levels(domain: DomainModel, spec: ToolSpec) -> tuple[_Level, _Level]:
    """The fields behind the top-level parameters and behind those of the lines element.

    What a tool works on decides them (requirement 11.4): the record or the header of an
    entity operation and the lines of its bill, the lines of the bill a push generates, and
    the declared parameters of a query, each of which states its own type
    (:class:`~erp_mock.config.model.QueryParamModel`). A target that is no object of the
    model, which no valid instance has, leaves both levels empty: every parameter is then
    checked against its schema alone.
    """
    match spec.kind:
        case "query":
            query = domain.queries.get(spec.target)
            types: Mapping[str, FieldType] = (
                FrozenMap()
                if query is None
                else {name: param.type for name, param in query.params.items()}
            )
            return _Level(types), _Level()
        case "push":
            conversion = domain.conversions.get(spec.target)
            target = None if conversion is None else domain.bills.get(conversion.target)
            return _Level(), _line_level(target)
        case "entity_op":
            bill = domain.bills.get(spec.target)
            if bill is not None:
                return _Level(_field_types(bill.header)), _line_level(bill)
            entity = domain.entities.get(spec.target)
            fields = FrozenMap() if entity is None else entity.fields
            return _Level(_field_types(fields)), _Level()
        case _:
            return _Level(), _Level()


def _line_level(bill: BillModel | None) -> _Level:
    """The fields of ``bill``'s lines and the name of their quantity (requirement 12.2)."""
    if bill is None:
        return _Level()
    return _Level(_field_types(bill.lines), quantity_field(bill))


def _field_types(fields: Mapping[str, FieldModel]) -> Mapping[str, FieldType]:
    """The type of each field of one level, by field name."""
    return {name: declared.type for name, declared in fields.items()}


def _object_plan(
    schema: Mapping[str, JsonValue], level: _Level, element: _Level | None = None
) -> _Object:
    """The parameters of one schema object, in declaration order.

    ``level`` holds the fields behind the parameters of this object and ``element`` those
    behind the parameters of its array elements, None for an object that has no array - an
    element object, whose own elements no inputSchema declares.
    """
    required = frozenset(
        name for name in _sequence(schema.get("required")) if isinstance(name, str)
    )
    params = tuple(
        _param_plan(name, _mapping(sub), name in required, level, element)
        for name, sub in _mapping(schema.get("properties")).items()
    )
    return _Object(params, frozenset(param.name for param in params))


def _param_plan(
    name: str,
    schema: Mapping[str, JsonValue],
    required: bool,
    level: _Level,
    element: _Level | None,
) -> _Param:
    """One parameter of a schema object, with everything it is checked against."""
    json_type = _text(schema.get("type"))
    allowed = tuple(_sequence(schema.get("enum"))) or None
    numeric = json_type in {_NUMBER, _INTEGER}
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    inner: _Object | None = None
    if json_type == _ARRAY:
        inner = _object_plan(_mapping(schema.get("items")), element or _Level())
    return _Param(
        name=name,
        json_type=json_type,
        required=required,
        enum=allowed,
        numbers=None if allowed is None or not numeric else _decimals(allowed),
        minimum=minimum,
        maximum=maximum,
        at_least=_bound(minimum),
        at_most=_bound(maximum),
        max_length=_count(schema.get("maxLength")),
        pattern=_text(schema.get("pattern")) or None,
        is_date=_text(schema.get("format")) == _DATE_FORMAT,
        min_items=_count(schema.get("minItems")),
        max_items=_count(schema.get("maxItems")),
        kind=_kind(level.types.get(name)),
        positive=name == level.quantity,
        element=inner,
    )


def _kind(field_type: FieldType | None) -> DecimalKind | None:
    """The precision a parameter of ``field_type`` is rounded to, None for other types."""
    match field_type:
        case "qty" | "price" | "money" | "rate":
            return field_type
        case _:
            return None


def _decimals(allowed: Sequence[JsonValue]) -> tuple[Decimal, ...]:
    """The allowed values of a numeric ``enum`` as Decimals (requirement 12.4)."""
    found = [_as_decimal(value) for value in allowed]
    return tuple(value for value in found if value is not None)


def _bound(value: JsonValue | None) -> Decimal | None:
    """A ``minimum`` or ``maximum`` as the Decimal it is compared as (requirement 12.4)."""
    return None if value is None else _as_decimal(value)


def _mapping(value: JsonValue | None) -> Mapping[str, JsonValue]:
    """``value`` as a schema object, empty when the keyword is absent."""
    return value if isinstance(value, Mapping) else FrozenMap()


def _sequence(value: JsonValue | None) -> Sequence[JsonValue]:
    """``value`` as a schema array, empty when the keyword is absent."""
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return value


def _text(value: JsonValue | None) -> str:
    """``value`` as the text of a keyword, empty when the keyword is absent."""
    return value if isinstance(value, str) else ""


def _count(value: JsonValue | None) -> int | None:
    """``value`` as the count of a keyword, None when the keyword is absent."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None
