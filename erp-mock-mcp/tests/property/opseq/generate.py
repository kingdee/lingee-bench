"""The state-aware generator of the random operation sequences (requirement 25.2).

:func:`walk` draws a sequence of 1 to 50 operations and runs it against the sandbox of one
example, one operation at a time: every step first reads what the sandbox holds
(:mod:`tests.property.opseq.state`) and then draws an operation against it, so the
sequences reach the states the later operations need - a bill must be created before it can
be submitted, approved and closed, and approved before it can be pushed::

    with open_sandbox(start, parent) as session:
        performed = walk(data, session)          # data is the st.data() of the test

Seven draws in ten are a sensible operation and the other three are noise
(:data:`SENSIBLE`, design.md property 17). A sensible operation names records that are
there, states the operation starts from and quantities inside the quota, so it is expected
to go through; a noisy one carries exactly one deliberate flaw (:data:`Flaw`) and is
expected to fail:

====================  ==================================================================
flaw                  what the call carries, and what it ends with
====================  ==================================================================
``bad_value``         a value of the wrong type, the wrong format or over its bound, for
                      one drawn parameter (VALIDATION_FAILED, requirement 12.3)
``bad_ref``           a reference to a record that is disabled or is not there
                      (R-GEN-006, NOT_FOUND, requirements 5.6 and 5.7)
``missing_required``  one required parameter left out (VALIDATION_FAILED, req 12.2)
``no_lines``          an empty ``lines`` array (VALIDATION_FAILED, requirement 11.4)
``missing_id``        an id of the right shape that no record answers (NOT_FOUND, 5.6)
``wrong_state``       a bill in a state the operation does not allow, or a source bill in
                      a state the conversion does not accept (INVALID_STATE, R-GEN-001,
                      R-GEN-004, requirements 7.3, 7.4 and 8.3)
``over_quota``        a pushed line that takes more than its source line has left
                      (R-GEN-005, requirements 8.5 and 8.6)
``missing_line``      a source line the source bill of the push does not have
                      (NOT_FOUND, requirements 5.6 and 8.4)
====================  ==================================================================

One flaw per call and everything else of it sensible, so a noisy call reaches the step its
flaw is about rather than stopping at input validation every time: that is what spreads the
failures over the error codes the properties of task 16 read
(:attr:`~tests.property.opseq.session.Performed.error_code`). A sensible call is not
promised to go through, and some do not - a quantity that breaks a rule of the business
pack, an approve that would take the stock of a warehouse below zero (R-GEN-007), a push of
a source bill whose quota is used up - which is the point of the 70 against 30: both paths
of every operation come up on their own.

Everything is read off the configuration: which operations a bill has comes from the tools
the profile exposes (:class:`~tests.property.opseq.start.BillTools`), which values a
parameter takes from its field model and from the inputSchema of the tool
(:class:`Params`), and the states an operation runs between from the transition table.
Nothing here names an entity, a bill or a field of the fixture, so the same generator walks
the ``purchase-basic`` starting point of task 19.8.

The ten operations of requirement 25.2 are :data:`OPERATIONS`, by the action of the tool
that performs them; :attr:`~tests.property.opseq.session.Performed.action` is what a test
counts them with.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Final, Literal, Protocol, assert_never

from hypothesis import strategies as st

from erp_mock.common.decimals import PLACES, format_decimal
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.fields import FieldType
from erp_mock.config.model import DomainModel, FieldModel
from erp_mock.domain.conversion import SOURCE_LINE_ID
from erp_mock.domain.state_machine import (
    DRAFT,
    StateAction,
    required_header_fields,
    required_line_fields,
)
from erp_mock.tools.generator import PUSH_ACTION, ToolSpec
from erp_mock.tools.input_schema import ID_PARAM, LINES_PARAM
from tests.property.opseq.session import Call, Performed, Session
from tests.property.opseq.start import BillTools, PushTools, Start
from tests.property.opseq.state import (
    QTY_UNIT,
    Held,
    Refs,
    Source,
    SourceLine,
    missing_id,
    read_bills,
    read_source,
)

__all__ = [
    "MAX_DRAWN_LINES",
    "MAX_OPS",
    "MIN_OPS",
    "MISSING_LINE_NO",
    "OPERATIONS",
    "SENSIBLE",
    "Draw",
    "Flaw",
    "Kind",
    "Params",
    "Values",
    "next_call",
    "param_value",
    "params_of",
    "push_args",
    "walk",
    "write_args",
]


class Draw(Protocol):
    """How the generator draws one value: the ``draw`` of the ``st.data()`` of the test.

    :class:`hypothesis.strategies.DrawFn`, which is the same callable for a composite
    strategy, declares a ``__signature__`` that a bound method does not have, so the
    generator takes the call form it actually uses: one strategy, one value.
    """

    def __call__[T](self, strategy: st.SearchStrategy[T], /) -> T: ...


# How many operations one sequence holds (requirement 25.2).
MIN_OPS: Final = 1
MAX_OPS: Final = 50

# How many lines a drawn create, update or push carries at most. Few lines keep an example
# small and make two lines of one push name the same source line often enough, which is
# the shape requirement 8.7 is about (:func:`_chosen_lines`).
MAX_DRAWN_LINES: Final = 3

# Whether one step is a sensible operation or noise: seven draws in ten are sensible
# (design.md property 17). sampled_from shrinks towards the first element, so a shrinking
# example loses its noise before it loses the operations that built the state up.
SENSIBLE: Final = st.sampled_from((True,) * 7 + (False,) * 3)

# The ten operations of requirement 25.2, by the action of the tool that performs them: the
# create of a record, a push, and the update, the delete and the six state operations of a
# bill.
OPERATIONS: Final[tuple[str, ...]] = (
    "create",
    PUSH_ACTION,
    "update",
    "delete",
    "submit",
    "unsubmit",
    "approve",
    "unapprove",
    "close",
    "unclose",
)

# The line number of a source line no bill has: a bill's lines are numbered from 1 without
# a gap (requirement 4.2), and a drawn bill never reaches 99 lines.
MISSING_LINE_NO: Final = 99

# What one step draws (module docstring).
type Kind = Literal["create", "push", "update", "delete", "state"]

# How often each kind is drawn, among the kinds the current state has an operation for
# (:func:`_kinds`). The state operations carry the most weight and the delete the least,
# because a sequence only reaches the later operations by walking a bill up its state
# machine - a push needs an approved source bill and an unclose a closed one - and every
# delete takes a draft back off that walk. The weights change how often each operation
# comes up, not which ones can: a sequence of 50 operations reaches all ten either way.
KIND_WEIGHTS: Final[Mapping[Kind, int]] = {
    "create": 3,
    "state": 4,
    "push": 2,
    "update": 2,
    "delete": 1,
}

# The one deliberate flaw of a noisy call (module docstring).
type Flaw = Literal[
    "bad_value",
    "bad_ref",
    "missing_required",
    "no_lines",
    "missing_id",
    "wrong_state",
    "over_quota",
    "missing_line",
]

# The flaws that replace the value of one drawn parameter, rather than one of a call's ids.
_VALUE_FLAWS: Final[frozenset[str]] = frozenset({"bad_value", "bad_ref"})

# Which record a write names when the step is noise: one that is not there at all, one in a
# state the operation does not allow, or the right one with a broken value
# (:func:`_write_targets`).
type Aim = Literal["missing_id", "wrong_state", "values"]
_WRITE_AIMS: Final[tuple[Aim, ...]] = ("missing_id", "wrong_state", "values")
_STATE_AIMS: Final[tuple[Aim, ...]] = ("missing_id", "wrong_state")

# The flaws a noisy push carries (module docstring).
_PUSH_FLAWS: Final[tuple[Flaw, ...]] = (
    "missing_id",
    "wrong_state",
    "over_quota",
    "missing_line",
    "bad_value",
)

# Which source bill each of them names: one that is not there, one in a state the
# conversion does not accept (R-GEN-004), or the right one (:func:`_push_targets`).
_PUSH_AIMS: Final[Mapping[str, Aim]] = {"missing_id": "missing_id", "wrong_state": "wrong_state"}

# The two standard line parameters of a pricing bill the amount quota is counted in
# (requirements 6.1 and 8.6).
_QTY_PARAM: Final = "qty"
_PRICE_PARAM: Final = "price"

# The smallest price a price field holds, which an amount quota is gone over by (req 4.7).
_PRICE_UNIT: Final = Decimal(1).scaleb(-PLACES["price"])

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)

# The keywords of an inputSchema the parameters and their values are read from (req 11.3).
_PROPERTIES: Final = "properties"
_REQUIRED: Final = "required"
_ITEMS: Final = "items"
_ENUM: Final = "enum"
_MAX_LENGTH: Final = "maxLength"


# --- the values of one parameter ---------------------------------------------------------

# The sensible values of each decimal field type, as the text or the JSON number a client
# sends. A quantity is positive, since a line of 0 is refused (requirement 12.6); the texts
# cover the decimal strings a number parameter accepts, which the normalization records as
# coerced (requirement 12.5).
_QUANTITIES: Final[tuple[JsonValue, ...]] = ("1", 2, "0.5", "3.25", 10)
_PRICES: Final[tuple[JsonValue, ...]] = ("0.85", 1, "12.5", 0)
_RATES: Final[tuple[JsonValue, ...]] = ("0.13", 0, "0.06")
_MONEY: Final[tuple[JsonValue, ...]] = ("100", "0.01", 25)

# The values that are no number at all, or outside the bound of one: a decimal string with
# an exponent and one with letters are refused as types, a negative value by the minimum of
# the parameter, and a boolean does not stand in for a number (requirements 12.3 to 12.5).
_BAD_NUMBERS: Final[tuple[JsonValue, ...]] = ("1e3", "甲", "-1", True, "")

# The sensible and the broken values of the other field types; a broken one is of the wrong
# JSON type, of the wrong format or outside a declared bound (requirement 12.3).
_INTEGERS: Final[tuple[JsonValue, ...]] = (0, 1, 30)
_BAD_INTEGERS: Final[tuple[JsonValue, ...]] = ("7", True, 1.5)
_BOOLEANS: Final[tuple[JsonValue, ...]] = (True, False)
_BAD_BOOLEANS: Final[tuple[JsonValue, ...]] = ("true", 1)
_BAD_DATES: Final[tuple[JsonValue, ...]] = ("2026-02-30", "2026/10/09", "", "20261009")
_BAD_PERIODS: Final[tuple[JsonValue, ...]] = ("2026-13", "2026-1", "")
_BAD_TEXTS: Final[tuple[JsonValue, ...]] = (7, True, None)
_BAD_ENUMS: Final[tuple[JsonValue, ...]] = ("无此取值", "", 1)

# How many days a drawn date is away from the anchor date of the sandbox (req 16.2).
_DAY_OFFSETS: Final[tuple[int, ...]] = (0, 1, -1, 7, -30)


@dataclass(frozen=True, slots=True)
class Values:
    """What the values of one call are drawn from (:func:`param_value`).

    - ``refs``: the records a reference may name, enabled apart from disabled
      (:class:`~tests.property.opseq.state.Refs`);
    - ``anchor_date``: the business date of the sandbox, which a drawn date is an offset
      from (requirement 16.2);
    - ``mark``: the number of the call being drawn, which goes into every drawn text, so a
      value of a ``unique`` field does not collide with one an earlier call wrote
      (requirement 5.4).
    """

    refs: Refs
    anchor_date: date
    mark: int

    @property
    def texts(self) -> tuple[JsonValue, ...]:
        """The sensible values of a string or text parameter, each carrying ``mark``."""
        return (f"夹具文本{self.mark}", f"夹具第 {self.mark} 项", str(self.mark))


def param_value(
    draw: Draw,
    values: Values,
    field: FieldModel | None,
    schema: Mapping[str, JsonValue],
    *,
    broken: bool = False,
) -> JsonValue:
    """Draw the value of one parameter, sensible or broken (module docstring).

    ``field`` is the field model of the parameter, None for a parameter that is no field of
    its level, and ``schema`` its piece of the tool's inputSchema, which the enum values and
    ``maxLength`` come from (requirement 11.3). ``broken`` draws from the values the
    parameter does not accept instead, which is the ``bad_value`` flaw.
    """
    options = _enum_options(schema)
    if options:
        return draw(st.sampled_from(_BAD_ENUMS if broken else options))
    field_type: FieldType | None = None if field is None else field.type
    match field_type:
        case "ref":
            return _ref_value(draw, values, field, broken=broken)
        case "qty":
            return draw(st.sampled_from(_BAD_NUMBERS if broken else _QUANTITIES))
        case "price":
            return draw(st.sampled_from(_BAD_NUMBERS if broken else _PRICES))
        case "rate":
            return draw(st.sampled_from(_BAD_NUMBERS if broken else _RATES))
        case "money":
            return draw(st.sampled_from(_BAD_NUMBERS if broken else _MONEY))
        case "int":
            return draw(st.sampled_from(_BAD_INTEGERS if broken else _INTEGERS))
        case "bool":
            return draw(st.sampled_from(_BAD_BOOLEANS if broken else _BOOLEANS))
        case "date":
            if broken:
                return draw(st.sampled_from(_BAD_DATES))
            return (values.anchor_date + timedelta(days=draw(_OFFSETS))).isoformat()
        case "period":
            if broken:
                return draw(st.sampled_from(_BAD_PERIODS))
            return values.anchor_date.strftime("%Y-%m")
        case _:
            return _text_value(draw, values, schema, broken=broken)


# The day offsets as a strategy, so the date branch of :func:`param_value` draws one value.
_OFFSETS: Final = st.sampled_from(_DAY_OFFSETS)


def _ref_value(draw: Draw, values: Values, field: FieldModel | None, *, broken: bool) -> JsonValue:
    """Draw the value of a reference parameter (requirements 5.6 and 5.7).

    A sensible value names an enabled record of the entity the field refers to; a broken one
    names a disabled record, which breaks R-GEN-006, or an id no record answers. An entity
    the sandbox holds no enabled record of leaves a sensible value with the missing id as
    well, which the call then reports as NOT_FOUND.
    """
    entity = None if field is None else field.ref
    if entity is None:
        return values.refs.absent_id("")
    absent = values.refs.absent_id(entity)
    if broken:
        return draw(st.sampled_from((*values.refs.unusable(entity), absent)))
    usable = values.refs.of(entity)
    return draw(st.sampled_from(usable)) if usable else absent


def _text_value(
    draw: Draw, values: Values, schema: Mapping[str, JsonValue], *, broken: bool
) -> JsonValue:
    """Draw the value of a string, text or unknown parameter (requirement 12.3).

    A broken value is one character over ``maxLength`` where the parameter declares one,
    which is the bound of the parameter, and of the wrong JSON type otherwise.
    """
    if not broken:
        return draw(st.sampled_from(values.texts))
    limit = schema.get(_MAX_LENGTH)
    if isinstance(limit, int):
        return "甲" * (limit + 1)
    return draw(st.sampled_from(_BAD_TEXTS))


def _enum_options(schema: Mapping[str, JsonValue]) -> tuple[JsonValue, ...]:
    """The values an enum parameter accepts, empty for a parameter that is none (11.3)."""
    options = schema.get(_ENUM)
    if isinstance(options, Sequence) and not isinstance(options, str):
        return tuple(options)
    return ()


# --- the parameters of one write tool (requirement 11.3) ---------------------------------


@dataclass(frozen=True, slots=True)
class Params:
    """The parameters of one write tool: what each takes and which are required.

    ``header`` are the parameters of the header of a bill or of the record of an entity and
    ``lines`` the parameters of one element of the ``lines`` array, both without the ``id``
    of the record and without ``lines`` itself; ``required`` and ``line_required`` are the
    ones the inputSchema declares required (requirement 11.4) and ``draft`` and
    ``line_draft`` the fields the bill needs filled to leave draft, which R-GEN-002 asks
    for (requirement 7.5, :meth:`carried`). ``fields`` and ``line_fields`` are the field
    models of each level, which decide what a value looks like, ``schemas`` and
    ``line_schemas`` the pieces of the inputSchema, and ``takes_lines`` whether the tool
    carries a ``lines`` array at all.
    """

    spec: ToolSpec
    header: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    required: frozenset[str] = frozenset()
    line_required: frozenset[str] = frozenset()
    fields: FrozenMap[str, FieldModel] = FrozenMap()
    line_fields: FrozenMap[str, FieldModel] = FrozenMap()
    schemas: FrozenMap[str, JsonValue] = FrozenMap()
    line_schemas: FrozenMap[str, JsonValue] = FrozenMap()
    draft: frozenset[str] = frozenset()
    line_draft: frozenset[str] = frozenset()
    takes_lines: bool = False

    def field(self, name: str, *, line: bool) -> FieldModel | None:
        """The field model of the parameter ``name``, None when it is no field."""
        return (self.line_fields if line else self.fields).get(name)

    def carried(self, name: str, *, line: bool) -> bool:
        """Whether a call always carries the parameter ``name`` (requirements 7.5, 11.4).

        The parameters the inputSchema declares required, and the ones the bill needs
        filled to leave draft that the configuration fills with nothing of its own: a
        drawn call would leave such a parameter out half the time and the submit of the
        bill it created would then end with R-GEN-002, which says more about the generator
        than about the engine. A parameter with a default of any kind stays drawn for, so
        the defaults of the configuration are exercised (requirement 5.1).
        """
        if name in (self.line_required if line else self.required):
            return True
        if name not in (self.line_draft if line else self.draft):
            return False
        field = self.field(name, line=line)
        return field is not None and not _has_default(field)

    def schema(self, name: str, *, line: bool) -> Mapping[str, JsonValue]:
        """The inputSchema piece of the parameter ``name``, empty when it has none."""
        found = (self.line_schemas if line else self.schemas).get(name)
        return found if isinstance(found, Mapping) else FrozenMap()

    def names(self, *, line: bool) -> tuple[str, ...]:
        """The parameters of one level, in declaration order (requirement 11.3)."""
        return self.lines if line else self.header

    def refs(self, *, line: bool) -> tuple[str, ...]:
        """The reference parameters of one level, which ``bad_ref`` lands on (req 5.6)."""
        return tuple(name for name in self.names(line=line) if _is_ref(self.field(name, line=line)))


def params_of(domain: DomainModel, spec: ToolSpec, target: str | None = None) -> Params:
    """The parameters of the write tool ``spec`` (:class:`Params`).

    ``target`` is the entity or bill the values belong to, ``spec.target`` for an entity
    operation and the target bill of the conversion for a push (requirement 8.2).
    """
    of = target if target is not None else spec.target
    bill = domain.bills.get(of)
    schema = spec.input_schema
    properties = _object(schema.get(_PROPERTIES))
    element = _object(_object(properties.get(LINES_PARAM)).get(_ITEMS))
    line_properties = _object(element.get(_PROPERTIES))
    return Params(
        spec=spec,
        header=tuple(name for name in properties if name not in (ID_PARAM, LINES_PARAM)),
        lines=tuple(line_properties),
        required=_names(schema.get(_REQUIRED)),
        line_required=_names(element.get(_REQUIRED)),
        fields=domain.fields_of(of, "header" if bill is not None else "record"),
        line_fields=domain.fields_of(of, "line"),
        schemas=FrozenMap(properties),
        line_schemas=FrozenMap(line_properties),
        draft=frozenset(() if bill is None else required_header_fields(bill)),
        line_draft=frozenset(() if bill is None else required_line_fields(bill)),
        takes_lines=LINES_PARAM in properties,
    )


def _has_default(field: FieldModel) -> bool:
    """Whether the configuration fills ``field`` when a call leaves it out (req 5.1)."""
    return (
        field.default is not None
        or field.default_from is not None
        or field.default_context is not None
    )


def _object(value: JsonValue | None) -> Mapping[str, JsonValue]:
    """``value`` as the JSON object it is, empty when it is none."""
    return value if isinstance(value, Mapping) else FrozenMap()


def _names(value: JsonValue | None) -> frozenset[str]:
    """The ``required`` list of a schema as a set of parameter names."""
    if isinstance(value, Sequence) and not isinstance(value, str):
        return frozenset(name for name in value if isinstance(name, str))
    return frozenset()


def _is_ref(field: FieldModel | None) -> bool:
    """Whether the parameter refers to a record of an entity (requirement 5.6)."""
    return field is not None and field.type == "ref"


# --- the arguments of one create or update (requirements 5.1 to 5.3) ---------------------

# Which parameter of a call a value flaw lands on: the name of the parameter and the index
# of the line it belongs to, None for a parameter of the header.
type Slot = tuple[str, int | None]


def write_args(
    draw: Draw,
    params: Params,
    values: Values,
    *,
    lines: int | None,
    flaw: Flaw | None = None,
) -> dict[str, JsonValue]:
    """Draw the arguments of one create or update: the header values and the lines.

    Every parameter the call must carry is carried (:meth:`Params.carried`) and every other
    one is drawn for, so the defaults of the configuration are exercised as well
    (requirement 5.1). ``lines`` is how
    many elements the ``lines`` array holds, None for a call that carries no array at all -
    an update that leaves the lines of the bill as they are (requirement 5.3). ``flaw`` is
    the one deliberate flaw of a noisy call: the parameter it lands on is drawn among the
    ones it applies to, and a call with no such parameter carries no flaw at all.
    """
    count = 0 if flaw == "no_lines" else lines
    slot = _draw_slot(draw, params, count, flaw) if flaw is not None else None
    args = dict(_level_args(draw, params, values, line=None, slot=slot, flaw=flaw))
    if count is not None:
        args[LINES_PARAM] = [
            dict(_level_args(draw, params, values, line=index, slot=slot, flaw=flaw))
            for index in range(count)
        ]
    return args


def _level_args(
    draw: Draw,
    params: Params,
    values: Values,
    *,
    line: int | None,
    slot: Slot | None,
    flaw: Flaw | None,
) -> Mapping[str, JsonValue]:
    """The arguments of one level: the header of the call, or one of its lines."""
    at_line = line is not None
    drawn: dict[str, JsonValue] = {}
    for name in params.names(line=at_line):
        here = slot == (name, line)
        if here and flaw == "missing_required":
            continue
        if not here and not params.carried(name, line=at_line) and not draw(st.booleans()):
            continue
        drawn[name] = param_value(
            draw,
            values,
            params.field(name, line=at_line),
            params.schema(name, line=at_line),
            broken=here and flaw in _VALUE_FLAWS,
        )
    return drawn


def _draw_slot(draw: Draw, params: Params, lines: int | None, flaw: Flaw) -> Slot | None:
    """Draw the parameter a value flaw lands on, None when the call has no such one."""
    if flaw not in _VALUE_FLAWS and flaw != "missing_required":
        return None
    indexes: list[int | None] = [None, *range(0 if lines is None else lines)]
    slots = [
        (name, index)
        for index in indexes
        for name in _flawed_names(params, flaw, line=index is not None)
    ]
    return draw(st.sampled_from(slots)) if slots else None


def _flawed_names(params: Params, flaw: Flaw, *, line: bool) -> tuple[str, ...]:
    """The parameters of one level that ``flaw`` may land on (module docstring)."""
    match flaw:
        case "bad_ref":
            return params.refs(line=line)
        case "missing_required":
            required = params.line_required if line else params.required
            return tuple(name for name in params.names(line=line) if name in required)
        case _:
            return params.names(line=line)


def _write_flaws(params: Params) -> tuple[Flaw, ...]:
    """The flaws a create or an update of ``params`` may carry (module docstring)."""
    flaws: list[Flaw] = ["bad_value"]
    if params.refs(line=False) or params.refs(line=True):
        flaws.append("bad_ref")
    if params.required or params.line_required:
        flaws.append("missing_required")
    if params.takes_lines:
        flaws.append("no_lines")
    return tuple(flaws)


def _line_count(draw: Draw, params: Params, *, optional: bool = False) -> int | None:
    """How many lines a call carries: 1 to 3, or None for a call that carries no array.

    ``optional`` is an update, which may leave the lines of the bill alone
    (requirement 5.3); a create of a bill always sends its lines (requirement 11.4).
    """
    if not params.takes_lines or (optional and not draw(st.booleans())):
        return None
    return draw(st.integers(min_value=1, max_value=MAX_DRAWN_LINES))


# --- the arguments of one push (requirements 8.1 and 8.5) --------------------------------


def push_args(
    draw: Draw,
    push: PushTools,
    params: Params,
    values: Values,
    source: Source,
    *,
    flaw: Flaw | None = None,
) -> dict[str, JsonValue]:
    """Draw the arguments of one push: the source bill and the lines that take from it.

    A sensible push names 1 to 3 of the source lines that still have a quota and takes what
    each of them has left, so the sum per source line stays inside its Pushable_Qty
    (requirement 8.5); a source bill with nothing left is pushed over the quota instead,
    which is what the ``over_quota`` flaw sends on purpose. ``missing_line`` names a line
    the source bill does not have, and ``bad_value`` breaks the other parameters of its
    lines.
    """
    over = flaw == "over_quota" or source.exhausted
    chosen = _chosen_lines(draw, source, over=over)
    # Every line of a push limited by amount takes from the one quota of the source bill,
    # so they share it; the lines of a push limited by quantity name a source line each
    # (:func:`_chosen_lines`) and take the whole quota of it (requirements 8.5 and 8.6).
    share = len(chosen) if push.conversion.limit_by == "amount" else 1
    return {
        push.source_param: source.id,
        LINES_PARAM: [
            _push_line(draw, push, params, values, source, line, over=over, share=share, flaw=flaw)
            for line in chosen
        ],
    }


def _chosen_lines(draw: Draw, source: Source, *, over: bool) -> list[SourceLine | None]:
    """The source lines the drawn push names (requirements 8.5 and 8.7).

    A push that stays inside the quota names 1 to 3 *different* source lines, so what it
    takes per source line is the quota of that line (the lines of a conversion limited by
    amount share the one quota of the bill instead, :func:`_push_amounts`); a push that
    goes over the quota draws with repetition, so two of its lines may name the same source
    line, which is the shape requirement 8.7 is about. A source bill with no line at all is
    named with one line that has none (:func:`_source_line_id`).
    """
    pool = list(source.lines if over else source.usable_lines)
    if not pool:
        return [None]
    count = draw(st.integers(min_value=1, max_value=MAX_DRAWN_LINES))
    if over:
        return [draw(st.sampled_from(pool)) for _ in range(count)]
    chosen: list[SourceLine | None] = []
    for _ in range(min(count, len(pool))):
        line = draw(st.sampled_from(pool))
        pool.remove(line)
        chosen.append(line)
    return chosen


def _push_line(
    draw: Draw,
    push: PushTools,
    params: Params,
    values: Values,
    source: Source,
    line: SourceLine | None,
    *,
    over: bool,
    share: int,
    flaw: Flaw | None,
) -> dict[str, JsonValue]:
    """One line of a push: the source line it names, what it takes and its own values.

    ``line`` is None for a source bill with no line at all, whose push names a line id of
    the right shape that is not there. The quantity and, for a conversion limited by
    amount, the price are what the quota is counted in (requirements 8.5 and 8.6); every
    other parameter of the line is drawn like any other value.
    """
    quantity, price = _push_amounts(push, source, line, over=over, share=share)
    quantity_param = _quantity_param(push)
    drawn: dict[str, JsonValue] = {}
    for name in params.lines:
        if name == SOURCE_LINE_ID:
            drawn[name] = _source_line_id(source, line, flaw=flaw)
        elif name == quantity_param:
            drawn[name] = quantity
        elif price is not None and name == _PRICE_PARAM:
            drawn[name] = price
        elif params.carried(name, line=True) or draw(st.booleans()):
            drawn[name] = param_value(
                draw,
                values,
                params.field(name, line=True),
                params.schema(name, line=True),
                broken=flaw == "bad_value",
            )
    return drawn


def _push_amounts(
    push: PushTools, source: Source, line: SourceLine | None, *, over: bool, share: int
) -> tuple[JsonValue, JsonValue | None]:
    """The quantity and the price one pushed line sends (requirements 8.5 and 8.6).

    Limited by quantity: what the source line has left, in the unit the pushed line is
    measured in, or one quantity unit more than that when the line goes over the quota.
    Limited by amount: one unit of quantity and a price of half of the share of the quota
    this line has, so the amount of the pushed bill stays inside the quota whatever the tax
    rate of its lines adds to it, and the whole quota plus one when it goes over.
    """
    if push.conversion.limit_by != "amount":
        quota = _ZERO if line is None else line.takeable
        return format_decimal("qty", quota + QTY_UNIT if over else quota), None
    left = _ZERO if source.amount is None else source.amount
    inside = (left / (2 * share)).quantize(_PRICE_UNIT, rounding=ROUND_DOWN)
    price = left + _ONE if over else inside
    return format_decimal("qty", _ONE), format_decimal("price", price)


def _source_line_id(source: Source, line: SourceLine | None, *, flaw: Flaw | None) -> str:
    """The source line a pushed line names (requirements 8.4 and 8.5).

    The line itself, or a line number the source bill does not have, which is the
    ``missing_line`` flaw and what a push of a source bill without lines sends.
    """
    if line is not None and flaw != "missing_line":
        return line.id
    return f"{source.id}-{MISSING_LINE_NO}"


def _quantity_param(push: PushTools) -> str | None:
    """The line parameter the quota of the push is counted in (requirements 8.5, 8.6).

    The quantity field of the target bill's lines, or the standard ``qty`` of a pricing
    bill that declares no quantity semantics; None for a target whose lines carry no
    quantity at all, whose quota the price alone decides.
    """
    if push.target.qty_field is not None:
        return push.target.qty_field
    return _QTY_PARAM if push.target.pricing else None


# --- which operation one step draws ------------------------------------------------------


def next_call(draw: Draw, session: Session) -> Call | None:
    """Draw the next operation of a sequence against the state the sandbox is in now.

    Seven draws in ten are sensible and the rest carry one flaw (module docstring). Returns
    None for a surface that has nothing to draw, which no instance with a write tool has:
    the create of a bill is always available.
    """
    start = session.start
    sensible: bool = draw(SENSIBLE)
    held = read_bills(session.store, start)
    values = Values(refs=session.refs(), anchor_date=start.anchor_date, mark=session.seq + 1)
    kinds = _kinds(start, held, sensible=sensible)
    if not kinds:
        return None
    kind: Kind = draw(st.sampled_from(_weighted(kinds)))
    match kind:
        case "create":
            return _create_call(draw, start, values, sensible=sensible)
        case "update":
            return _update_call(draw, start, held, values, sensible=sensible)
        case "delete":
            return _delete_call(draw, held, sensible=sensible)
        case "state":
            return _state_call(draw, held, sensible=sensible)
        case "push":
            return _push_call(draw, session, held, values, sensible=sensible)
        case _ as unknown:
            assert_never(unknown)


def walk(
    data: st.DataObject, session: Session, *, steps: int | None = None
) -> tuple[Performed, ...]:
    """Draw a sequence of operations, run it in ``session`` and return what each call did.

    ``steps`` is how many operations the sequence holds; None draws 1 to 50 of them, which
    is the length requirement 25.2 asks for. The operations are drawn one at a time against
    the state the earlier ones left behind (module docstring), and a call that fails does
    not end the sequence: the next operation is drawn against the state the failed call
    left, which is the state before it (requirement 12.7).
    """
    count = (
        steps
        if steps is not None
        else data.draw(st.integers(min_value=MIN_OPS, max_value=MAX_OPS), label="操作个数")
    )
    performed: list[Performed] = []
    for _ in range(count):
        call = next_call(data.draw, session)
        if call is None:
            break
        performed.append(session.perform(call))
    return tuple(performed)


def _weighted(kinds: Sequence[Kind]) -> tuple[Kind, ...]:
    """``kinds`` with each kind as often as :data:`KIND_WEIGHTS` gives it (module docstring)."""
    return tuple(kind for kind in kinds for _ in range(KIND_WEIGHTS[kind]))


def _kinds(start: Start, held: Sequence[Held], *, sensible: bool) -> tuple[Kind, ...]:
    """The operations that can be drawn against the current state (module docstring).

    A sensible operation needs a record to work on - a draft to change, a bill in the state
    an action starts from, a source bill in the state a push requires - so the kinds it may
    draw grow as the sequence builds the state up. A noisy one always has the id of a record
    that is not there to name, so every kind the surface has is available.
    """
    aim: Aim | None = None if sensible else "missing_id"
    kinds: list[Kind] = []
    if any(tools.create is not None for tools in start.bills):
        kinds.append("create")
    if _write_targets(held, _update_of, aim=aim):
        kinds.append("update")
    if _write_targets(held, _delete_of, aim=aim):
        kinds.append("delete")
    if _state_targets(held, aim=aim):
        kinds.append("state")
    if _push_targets(start, held, aim=aim):
        kinds.append("push")
    return tuple(kinds)


def _create_call(draw: Draw, start: Start, values: Values, *, sensible: bool) -> Call:
    """One create of a drawn bill type (requirement 5.1)."""
    creatable = [tools for tools in start.bills if tools.create is not None]
    spec = _spec(draw(st.sampled_from(creatable)).create)
    params = params_of(start.domain, spec)
    flaw = None if sensible else draw(st.sampled_from(_write_flaws(params)))
    args = write_args(draw, params, values, lines=_line_count(draw, params), flaw=flaw)
    return Call(spec.name, args)


def _update_call(
    draw: Draw, start: Start, held: Sequence[Held], values: Values, *, sensible: bool
) -> Call:
    """One update of a drawn bill: a draft, or a bill an update may not touch (req 7.3)."""
    aim = None if sensible else draw(st.sampled_from(_WRITE_AIMS))
    tools, record_id = _draw_target(draw, held, _update_of, aim=aim)
    spec = _spec(tools.update)
    params = params_of(start.domain, spec)
    flaw = draw(st.sampled_from(_write_flaws(params))) if aim == "values" else None
    args = write_args(
        draw, params, values, lines=_line_count(draw, params, optional=True), flaw=flaw
    )
    return Call(spec.name, {ID_PARAM: record_id, **args})


def _delete_call(draw: Draw, held: Sequence[Held], *, sensible: bool) -> Call:
    """One delete of a drawn bill: a draft, or one a delete may not touch (req 7.3)."""
    aim = None if sensible else draw(st.sampled_from(_STATE_AIMS))
    tools, record_id = _draw_target(draw, held, _delete_of, aim=aim)
    return Call(_spec(tools.delete).name, {ID_PARAM: record_id})


def _state_call(draw: Draw, held: Sequence[Held], *, sensible: bool) -> Call:
    """One state operation of a drawn bill (requirements 7.1, 7.2 and 7.4).

    The action is drawn first, among the ones the current state has a bill for, and the
    bill after it: the surface has many more bills with a submit than with an unclose, and
    drawing a (bill, action) pair straight away would make the rarer operations rare in the
    sequences as well (requirement 25.2 asks for all ten).
    """
    aim = None if sensible else draw(st.sampled_from(_STATE_AIMS))
    targets = _state_targets(held, aim=aim) or _state_targets(held, aim="missing_id")
    actions = tuple(dict.fromkeys(action for _, action, _ in targets))
    action = draw(st.sampled_from(actions))
    tools, _, record_id = draw(
        st.sampled_from([target for target in targets if target[1] == action])
    )
    return Call(tools.states[action].name, {ID_PARAM: record_id})


def _push_call(
    draw: Draw, session: Session, held: Sequence[Held], values: Values, *, sensible: bool
) -> Call:
    """One push of a drawn source bill (requirement 8.1)."""
    start = session.start
    flaw = None if sensible else draw(st.sampled_from(_PUSH_FLAWS))
    aim = None if flaw is None else _PUSH_AIMS.get(flaw)
    # A flaw of the lines needs a source bill the push gets past step 3 of
    # (requirement 8.3); where there is none, the push names one in another state and
    # ends there, and where there is no source bill at all, one that is not there.
    targets = (
        _push_targets(start, held, aim=aim)
        or _push_targets(start, held, aim="wrong_state")
        or _push_targets(start, held, aim="missing_id")
    )
    push, source_id = draw(st.sampled_from(targets))
    source = read_source(session.store, push, source_id)
    params = params_of(start.domain, push.spec, push.target.name)
    return Call(push.spec.name, push_args(draw, push, params, values, source, flaw=flaw))


# --- the records one operation may name (requirements 7.3 and 7.4) -----------------------

# Which write tool of a bill a target is drawn for.
type Pick = Callable[[BillTools], ToolSpec | None]


def _update_of(tools: BillTools) -> ToolSpec | None:
    return tools.update


def _delete_of(tools: BillTools) -> ToolSpec | None:
    return tools.delete


def _write_targets(
    held: Sequence[Held], pick: Pick, *, aim: Aim | None
) -> tuple[tuple[BillTools, str], ...]:
    """The bills an update or a delete may name, by the aim of the step.

    ``aim`` None is a sensible call, which names a draft, the one state a write of a bill
    is allowed in (requirement 7.3); ``wrong_state`` names a bill in any other state, which
    breaks R-GEN-001, and ``missing_id`` an id no bill answers.
    """
    targets: list[tuple[BillTools, str]] = []
    for group in held:
        if pick(group.tools) is None:
            continue
        if aim == "missing_id":
            targets.append((group.tools, missing_id(group.tools.bill.id_prefix)))
            continue
        ids = group.outside(DRAFT) if aim == "wrong_state" else group.in_state(DRAFT)
        targets += [(group.tools, record_id) for record_id in ids]
    return tuple(targets)


def _state_targets(
    held: Sequence[Held], *, aim: Aim | None
) -> tuple[tuple[BillTools, StateAction, str], ...]:
    """The (bill, action, record) a state operation may name, by the aim of the step.

    ``aim`` None names a bill in the state the action starts from, so the operation goes
    through (requirement 7.2); ``wrong_state`` names a bill in any other state and
    ``missing_id`` an id no bill answers, both of which end in INVALID_STATE or NOT_FOUND
    (requirements 7.4 and 5.6).
    """
    targets: list[tuple[BillTools, StateAction, str]] = []
    for group in held:
        for action in group.tools.states:
            pre = group.tools.pre_state(action)
            if pre is None:
                continue
            if aim == "missing_id":
                targets.append((group.tools, action, missing_id(group.tools.bill.id_prefix)))
                continue
            ids = group.outside(pre) if aim == "wrong_state" else group.in_state(pre)
            targets += [(group.tools, action, record_id) for record_id in ids]
    return tuple(targets)


def _push_targets(
    start: Start, held: Sequence[Held], *, aim: Aim | None
) -> tuple[tuple[PushTools, str], ...]:
    """The source bills a push may name, by the aim of the step (requirements 8.1, 8.3).

    ``aim`` None names a source bill in the state the conversion requires, so the push
    reaches the quota check; ``wrong_state`` names one in any other state, which breaks
    R-GEN-004, and ``missing_id`` one that is not there. A noisy push whose conversion has
    no source bill to name falls back to the missing id, so the kind stays available.
    """
    by_name = {group.name: group for group in held}
    targets: list[tuple[PushTools, str]] = []
    for push in start.pushes:
        if aim == "missing_id":
            targets.append((push, missing_id(push.source.id_prefix)))
            continue
        group = by_name.get(push.conversion.source)
        required = push.conversion.require_source_status
        if group is None:
            continue
        ids = group.outside(required) if aim == "wrong_state" else group.in_state(required)
        targets += [(push, source_id) for source_id in ids]
    return tuple(targets)


def _draw_target(
    draw: Draw, held: Sequence[Held], pick: Pick, *, aim: Aim | None
) -> tuple[BillTools, str]:
    """Draw one bill an update or a delete may name, falling back to a missing id.

    A noisy call whose aim finds no bill - ``wrong_state`` before any bill left draft -
    names an id that is not there instead, so the kind stays available whatever the state
    (requirement 5.6).
    """
    targets = _write_targets(held, pick, aim=aim)
    if not targets:
        targets = _write_targets(held, pick, aim="missing_id")
    return draw(st.sampled_from(targets))


def _spec(spec: ToolSpec | None) -> ToolSpec:
    """``spec`` as the tool it is; the candidate lists only hold tools that are there."""
    assert spec is not None, "the target was drawn for a tool the profile does not expose"
    return spec
