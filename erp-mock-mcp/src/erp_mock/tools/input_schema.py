"""The inputSchema of every generated tool (requirement 11.3 to 11.6, 11.11 and 11.12).

Part of the Tool_Generator of design.md 5.1: :mod:`erp_mock.tools.generator` settles which
tools there are and what each one is called, this module says what each of them takes. The
schema is built from the field view of the resolved domain model
(:mod:`erp_mock.config.model`) and from nothing else, so a parameter exists exactly because
a business pack declared the field behind it.

What a tool takes, per requirement 11.4:

=====================================  ===================================================
tool                                   parameters
=====================================  ===================================================
``<entity>_create``                    the writable fields of the record
``<bill>_create``                      the writable header fields, then ``lines``
``<object>_update``                    ``id``, then the same as create
``<object>_get``, ``_delete``,         ``id`` alone
``_submit`` and the other state
operations
``<object>_search``                    ``keyword``, the filters, ``page``, ``page_size``
``<bill>_create_from_<short>``         ``<short>_id`` and ``lines``
a declarative query                    the parameters the exposure declares
the stock balance query                ``material_id``, ``warehouse_id``, the two page ones
``business_context_get``               none
=====================================  ===================================================

The order of the parameters (requirement 11.4, design.md 5.1): the fields the pack declares
at that level, in declaration order (``declared_fields`` of the domain model, which holds
the same-name declarations of standard fields at the place the pack wrote them), then the
writable standard fields it did not declare, in the order of :data:`STANDARD_ORDER`, and
``lines`` last. The lines element object is built the same way from the lines of the bill.
A pack that wants ``price_includes_tax`` or a line's ``tax_rate`` somewhere else declares it
by name; it moves the parameter and adds no field (requirement 1.5).

The type of a parameter is the type of its field, by the table of meta-model.md section 3
(requirement 11.11, :func:`field_schema`); ``required`` holds the fields the pack declared
required that have no default of any kind, plus ``qty`` and ``price`` of a pricing bill's
lines, plus ``lines`` itself where it must be given - an update may leave the lines alone
(requirements 5.2 and 11.12). A date is ``format: date``; whether it is a date of the
calendar is checked by the input validator, which also accepts a decimal string for a
number and rounds it to the field precision (requirements 12.2, 12.5 and 12.6).

Only the 14 keywords of tool-design.md section 4 are used, and every object says
``additionalProperties: false`` (requirement 11.3). :func:`schema_object` is the only way a
schema object is built here: it rejects any other keyword and puts the keys in the fixed
order of :data:`KEYWORDS`, so two runs of the generator produce byte-identical text
(requirement 11.10) and task 9.4, which fills in the ``description`` of each parameter, has
one place that decides where that key goes. An empty ``required`` is left out; ``properties``
is always written, empty for ``business_context_get``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.fields import DEFAULT_MAX_LENGTH, FieldType
from erp_mock.config.model import (
    BillModel,
    ConversionModel,
    EntityModel,
    FieldLevel,
    FieldModel,
    QueryModel,
    SearchModel,
)

__all__ = [
    "BALANCE_PARAMS",
    "BUILTIN_INPUT",
    "DATE_RANGE",
    "ID_PARAM",
    "KEYWORDS",
    "KEYWORD_PARAM",
    "LINES_PARAM",
    "MAX_LINES",
    "MAX_PAGE_SIZE",
    "MIN_LINES",
    "MIN_PAGE",
    "PAGE_PARAM",
    "PAGE_SIZE_PARAM",
    "PERIOD_PATTERN",
    "PRICING_REQUIRED",
    "STANDARD_ORDER",
    "ToolInput",
    "bill_input",
    "entity_input",
    "field_schema",
    "push_input",
    "query_input",
    "schema_object",
    "type_schema",
]

# The 14 JSON Schema keywords an inputSchema may use (tool-design.md section 4,
# requirement 11.3), in the order they are written. The order groups what each key says
# about a parameter: its type, then the values it may take, then its size, then the text
# task 9.4 fills in, then the structure of an object or an array. ``description`` is in the
# list because the keyword is allowed; this module writes none (requirement 3.7 takes every
# description verbatim from the variant of the profile).
KEYWORDS: Final[tuple[str, ...]] = (
    "type",
    "format",
    "pattern",
    "enum",
    "minimum",
    "maximum",
    "maxLength",
    "minItems",
    "maxItems",
    "description",
    "properties",
    "required",
    "items",
    "additionalProperties",
)

# The parameters the engine adds itself: the internal ID a tool works on, the lines of a
# bill, and the three of a search (requirement 11.4).
ID_PARAM: Final = "id"
LINES_PARAM: Final = "lines"
KEYWORD_PARAM: Final = "keyword"
PAGE_PARAM: Final = "page"
PAGE_SIZE_PARAM: Final = "page_size"

# The limits of requirement 11.6: a page holds at most 100 records and a bill at most 200
# lines, and a bill is written with at least one line. Exceeding either upper bound is
# LIMIT_EXCEEDED rather than a schema error (requirement 12.3).
MIN_PAGE: Final = 1
MAX_PAGE_SIZE: Final = 100
MIN_LINES: Final = 1
MAX_LINES: Final = 200

# A period parameter, ``YYYY-MM`` (requirement 11.11); the pattern of
# :data:`~erp_mock.config.fields.PeriodText`, which validates the same text in a
# configuration.
PERIOD_PATTERN: Final = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

# The two parameters a date filter becomes, both ends included (requirement 11.5). The
# biz_date of a bill becomes ``date_from`` and ``date_to``, every other date field ``F``
# becomes ``F_from`` and ``F_to``.
DATE_RANGE: Final[tuple[str, str]] = ("_from", "_to")
_BIZ_DATE: Final = "biz_date"
# The word "date": the ``format`` of a date parameter (requirement 11.11) and the stem of
# the two parameters a biz_date filter becomes.
_DATE: Final = "date"

# The suffix of a ref parameter, which is how a tool refers to a record: never a name or a
# code (tool-design.md section 4). A push tool's source is ``<short name>_id``
# (requirement 11.13).
_ID_SUFFIX: Final = "_id"

# The writable standard fields of each level, in the order requirement 11.4 puts them in
# after the declared fields. Which of them a level has depends on the object: the amounts
# of pricing bring ``price_includes_tax``, a pricing bill's lines bring ``qty``, ``price``
# and ``tax_rate``, and an entity has ``status`` only when ``has_status`` is true
# (requirement 1.3).
STANDARD_ORDER: Final[Mapping[FieldLevel, tuple[str, ...]]] = MappingProxyType(
    {
        "record": ("status",),
        "header": (_BIZ_DATE, "price_includes_tax", "remark"),
        "line": ("qty", "price", "tax_rate"),
    }
)

# The standard line fields the engine requires itself: the quantity and the price a pricing
# bill computes its amounts from (requirement 11.12). A pack that gives either a default
# makes it optional again, as it does for a field of its own.
PRICING_REQUIRED: Final[tuple[str, ...]] = ("qty", "price")

# The parameters of the stock balance query the inventory primitive provides
# (requirement 9.7): the material and the warehouse, both optional, and the two page
# parameters (requirement 9.12). The names are the engine's own, the columns it keeps the
# balance under (design.md data model 3), and not those of the entities a pack happens to
# use for materials and warehouses.
BALANCE_PARAMS: Final[tuple[str, str]] = ("material_id", "warehouse_id")
_INVENTORY: Final = "inventory"
_BALANCE: Final = "balance"


@dataclass(frozen=True, slots=True)
class ToolInput:
    """The inputSchema of one tool and the paths of its parameters.

    ``schema`` is the top-level object, which goes into :attr:`ToolSpec.input_schema
    <erp_mock.tools.generator.ToolSpec.input_schema>`; ``param_paths`` names every
    parameter that needs a description, in the order they are declared: a top-level
    parameter by its name, ``lines`` included, and a parameter of the lines element as
    ``lines[].<name>`` (requirement 3.2).
    """

    schema: FrozenMap[str, JsonValue]
    param_paths: tuple[str, ...]


def schema_object(**items: JsonValue) -> FrozenMap[str, JsonValue]:
    """One schema object, its keys in the fixed order of :data:`KEYWORDS`.

    The only way this module writes a schema: a keyword outside the 14 of
    requirement 11.3 raises ValueError, so no caller can smuggle in ``$ref``, ``oneOf`` or
    a keyword of a later draft, and the key order does not depend on the order the caller
    passes them in.
    """
    unknown = sorted(set(items) - set(KEYWORDS))
    if unknown:
        allowed = "、".join(KEYWORDS)
        raise ValueError(f"inputSchema 不能使用关键字 {'、'.join(unknown)}，只能使用 {allowed}")
    return FrozenMap((keyword, items[keyword]) for keyword in KEYWORDS if keyword in items)


# --- the type of one parameter (requirements 11.6 and 11.11) -----------------------------


def field_schema(field: FieldModel) -> FrozenMap[str, JsonValue]:
    """The schema of the parameter that writes or filters by ``field`` (requirement 11.11).

    The type table of meta-model.md section 3, with the constraints the field carries: the
    ``maxLength`` of a string or text field, the codes of an enum field, the allowed values
    of a qty, price, money or rate field as JSON numbers, and ``minimum`` 0 for those four
    types.
    """
    return type_schema(
        field.type,
        max_length=field.max_length,
        codes=tuple(field.enum_values),
        values=field.values,
    )


def type_schema(
    field_type: FieldType,
    *,
    max_length: int | None = None,
    codes: Sequence[str] = (),
    values: Sequence[str] = (),
) -> FrozenMap[str, JsonValue]:
    """The schema of one field type (meta-model.md section 3, requirement 11.11).

    ``max_length`` is the limit of a string or text field, the default of the type when it
    is None; ``codes`` are the values of an enum field and ``values`` the allowed decimal
    values of a qty, price, money or rate field, which become a numeric ``enum``
    (requirement 12.4 compares it by value, so ``0.130`` equals ``0.13``). A parameter of a
    declarative query carries none of the three (requirement 5.13), so it gets the type
    alone.
    """
    match field_type:
        case "string" | "text":
            limit = DEFAULT_MAX_LENGTH[field_type] if max_length is None else max_length
            return schema_object(type="string", maxLength=limit)
        case "int":
            return schema_object(type="integer")
        case "bool":
            return schema_object(type="boolean")
        case "enum":
            if not codes:
                return schema_object(type="string")
            return schema_object(type="string", enum=tuple(codes))
        case "date":
            return schema_object(type="string", format=_DATE)
        case "period":
            return schema_object(type="string", pattern=PERIOD_PATTERN)
        case "qty" | "price" | "money" | "rate":
            if not values:
                return schema_object(type="number", minimum=0)
            allowed = tuple(_json_number(value) for value in values)
            return schema_object(type="number", enum=allowed, minimum=0)
        case "ref":
            return schema_object(type="string")


def _json_number(text: str) -> int | float:
    """A declared decimal value as the JSON number of an ``enum`` (requirement 11.11).

    An integral value is written as an integer (``"0"`` as ``0``), every other one as the
    shortest float that reads back as the same value, which is what a JSON number of at
    most 15 significant digits gives the input validator (requirement 12.4).
    """
    value = Decimal(text)
    return int(value) if value == value.to_integral_value() else float(value)


# --- parameters --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Param:
    """One parameter: its name, its schema and whether it must be given."""

    name: str
    schema: FrozenMap[str, JsonValue]
    required: bool = False


_STRING: Final = schema_object(type="string")
# The id of the record a tool works on, always required (requirement 11.4).
_ID: Final = _Param(ID_PARAM, _STRING, required=True)
_DATE_SCHEMA: Final = schema_object(type="string", format=_DATE)


def _object_schema(params: Sequence[_Param]) -> FrozenMap[str, JsonValue]:
    """The object of ``params``: their properties in order, then required, then closed."""
    items: dict[str, JsonValue] = {
        "type": "object",
        "properties": FrozenMap((param.name, param.schema) for param in params),
    }
    required = tuple(param.name for param in params if param.required)
    if required:
        items["required"] = required
    items["additionalProperties"] = False
    return schema_object(**items)


def _input(top: Sequence[_Param], lines: Sequence[_Param] = ()) -> ToolInput:
    """The schema of ``top`` and the paths of every parameter in it (requirement 3.2)."""
    paths: list[str] = []
    for param in top:
        paths.append(param.name)
        if param.name == LINES_PARAM:
            paths.extend(f"{LINES_PARAM}[].{line.name}" for line in lines)
    return ToolInput(schema=_object_schema(top), param_paths=tuple(paths))


def _lines_param(lines: Sequence[_Param], *, required: bool) -> _Param:
    """The ``lines`` array: 1 to 200 elements of the lines object (requirement 11.6)."""
    schema = schema_object(
        type="array",
        minItems=MIN_LINES,
        maxItems=MAX_LINES,
        items=_object_schema(lines),
    )
    return _Param(LINES_PARAM, schema, required)


def _level_params(
    fields: Mapping[str, FieldModel],
    declared: Sequence[str],
    level: FieldLevel,
    *,
    required: bool,
) -> tuple[_Param, ...]:
    """The writable fields of one level as parameters, in the order of requirement 11.4.

    ``declared`` is the declaration order of the level and ``fields`` its field view; a
    declared name that is read-only, computed or no field of the view at all - which step 5
    reports (requirement 1.6) - contributes no parameter. ``required`` says whether the
    fields may be required at all: the top level of an update requires ``id`` and nothing
    else (requirement 11.12).
    """
    names = [name for name in dict.fromkeys(declared) if _writable(fields, name)]
    placed = frozenset(names)
    names += [
        name for name in STANDARD_ORDER[level] if name not in placed and _writable(fields, name)
    ]
    return tuple(
        _Param(name, field_schema(fields[name]), required and _required(fields[name], level))
        for name in names
    )


def _writable(fields: Mapping[str, FieldModel], name: str) -> bool:
    """Whether ``name`` is a field of the level that a write tool may write (req 1.3)."""
    field = fields.get(name)
    return field is not None and field.writable


def _required(field: FieldModel, level: FieldLevel) -> bool:
    """Whether ``field`` must be given when it is written (requirement 11.12).

    The fields the pack declared required, and the quantity and price of a pricing bill's
    lines; a field with a default of any kind - a constant, another record's field or the
    business context - is optional, the engine filling it in (requirement 5.1).
    """
    if field.default is not None or field.default_from is not None:
        return False
    if field.default_context is not None:
        return False
    pricing = level == "line" and field.origin == "standard" and field.name in PRICING_REQUIRED
    return field.required or pricing


def _search_params(
    search: SearchModel | None, fields: Mapping[str, FieldModel]
) -> tuple[_Param, ...]:
    """The parameters of a search tool (requirements 11.4 and 11.5).

    ``keyword`` when the object declares ``keyword_fields``, one parameter per filter -
    two for a date filter - and the two page parameters, which every search has. Every one
    of them is optional: a condition that is not given does not filter (requirement 5.9).
    """
    params: list[_Param] = []
    if search is not None and search.keyword_fields:
        params.append(_Param(KEYWORD_PARAM, _STRING))
    for filter_field in () if search is None else search.filters:
        field = fields.get(filter_field.name)
        if field is None:
            continue
        if field.type == "date":
            params.extend(_date_range(field.name))
        else:
            params.append(_Param(field.name, field_schema(field)))
    params.extend(_page_params())
    return tuple(params)


def _date_range(name: str) -> tuple[_Param, _Param]:
    """The two ends of a date filter, ``date_from`` and ``date_to`` for biz_date (11.5)."""
    stem = _DATE if name == _BIZ_DATE else name
    start, end = DATE_RANGE
    return (_Param(f"{stem}{start}", _DATE_SCHEMA), _Param(f"{stem}{end}", _DATE_SCHEMA))


def _page_params() -> tuple[_Param, _Param]:
    """``page`` and ``page_size``, each at least 1 and the page size at most 100 (11.6)."""
    return (
        _Param(PAGE_PARAM, schema_object(type="integer", minimum=MIN_PAGE)),
        _Param(
            PAGE_SIZE_PARAM,
            schema_object(type="integer", minimum=MIN_PAGE, maximum=MAX_PAGE_SIZE),
        ),
    )


# --- the inputSchema of one tool ---------------------------------------------------------

# business_context_get takes no parameters: it reports the business context of the sandbox
# (requirement 16.10).
BUILTIN_INPUT: Final = ToolInput(schema=_object_schema(()), param_paths=())


def entity_input(entity: EntityModel, action: str) -> ToolInput:
    """The inputSchema of the ``action`` tool of ``entity`` (requirement 11.4).

    ``action`` is one of the entity operations (``search``, ``get``, ``create``, ``update``,
    ``delete``); any other action gets the ``id`` of a read of one record, which is what the
    operations of M0 that are not create, update or search take.
    """
    match action:
        case "create":
            return _input(_record_params(entity, required=True))
        case "update":
            return _input((_ID, *_record_params(entity, required=False)))
        case "search":
            return _input(_search_params(entity.search, entity.fields))
        case _:
            return _input((_ID,))


def bill_input(bill: BillModel, action: str) -> ToolInput:
    """The inputSchema of the ``action`` tool of ``bill`` (requirements 11.4 and 11.12).

    create takes the header and the lines, update takes the id as well and requires nothing
    else - the fields it is not given keep their value and absent lines stay as they are
    (requirement 5.2) - search takes the search parameters, and every state operation takes
    the id alone.
    """
    match action:
        case "create":
            lines = _line_params(bill)
            header = _header_params(bill, required=True)
            return _input((*header, _lines_param(lines, required=True)), lines)
        case "update":
            lines = _line_params(bill)
            header = _header_params(bill, required=False)
            return _input((_ID, *header, _lines_param(lines, required=False)), lines)
        case "search":
            return _input(_search_params(bill.search, bill.header))
        case _:
            return _input((_ID,))


def push_input(conversion: ConversionModel, target: BillModel | None, short_name: str) -> ToolInput:
    """The inputSchema of the push tool of ``conversion`` (requirements 11.4 and 11.12).

    ``<short name>_id`` names the source bill, the short name being the one that also names
    the tool (requirement 11.13), and the lines element holds the
    ``required_line_inputs`` of the conversion, in that order and all of them required:
    every other field of a pushed line comes from the mapping or from a default
    (requirement 8.1). ``target`` is the bill the push produces, whose lines those inputs
    are fields of, and None when step 5 could not resolve it.
    """
    source = _Param(f"{short_name}{_ID_SUFFIX}", _STRING, required=True)
    fields: Mapping[str, FieldModel] = FrozenMap() if target is None else target.lines
    lines = tuple(
        _Param(name, field_schema(fields[name]), required=True)
        for name in conversion.required_line_inputs
        if name in fields
    )
    return _input((source, _lines_param(lines, required=True)), lines)


def query_input(query: QueryModel) -> ToolInput:
    """The inputSchema of a query tool: declared parameters, or those of a primitive.

    A declarative query takes the parameters the exposure declares, in declaration order,
    each one matched against the field it maps to (requirement 5.13); a primitive query
    takes what the primitive defines, which in M0 is the stock balance of the inventory
    primitive (requirements 9.7 and 9.12).
    """
    if query.primitive is not None:
        return _primitive_input(query)
    params = tuple(
        _Param(param.name, type_schema(param.type), required=param.required)
        for param in query.params.values()
    )
    if query.history_paginated:
        params += (
            _Param("offset", schema_object(type="integer", minimum=0)),
            _Param("limit", schema_object(type="integer", minimum=1, maximum=query.history_limit)),
        )
    return _input(params)


def _primitive_input(query: QueryModel) -> ToolInput:
    """The parameters of the one primitive query of M0 (requirements 9.7 and 9.12).

    A query no primitive provides takes no parameters; step 5 reports it, so no valid
    instance has one (requirement 2.6).
    """
    if (query.primitive, query.query) != (_INVENTORY, _BALANCE):
        return _input(())
    filters = tuple(_Param(name, _STRING) for name in BALANCE_PARAMS)
    return _input((*filters, *_page_params()))


def _record_params(entity: EntityModel, *, required: bool) -> tuple[_Param, ...]:
    """The writable fields of an entity record, in the order of requirement 11.4."""
    return _level_params(entity.fields, entity.declared_fields, "record", required=required)


def _header_params(bill: BillModel, *, required: bool) -> tuple[_Param, ...]:
    """The writable fields of a bill header, in the order of requirement 11.4."""
    return _level_params(bill.header, bill.header_declared_fields, "header", required=required)


def _line_params(bill: BillModel) -> tuple[_Param, ...]:
    """The writable fields of a bill's lines, in the order of requirement 11.4.

    The lines element of an update requires what the element of a create requires: lines
    are replaced as a whole and a new line is filled in like a new bill's (requirement 5.3),
    so it is the array itself, and not its elements, that an update leaves optional.
    """
    return _level_params(bill.lines, bill.line_declared_fields, "line", required=True)
