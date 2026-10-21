"""The outputSchema and the annotations of every generated tool (requirements 11.7, 11.8).

The third part of the Tool_Generator of design.md 5.1: :mod:`erp_mock.tools.generator`
settles which tools there are, :mod:`erp_mock.tools.input_schema` says what each one takes,
and this module says what each one returns and what a client may assume about calling it.

The schema is built from the same field view of the resolved domain model
(:mod:`erp_mock.config.model`) the input schema is built from, so a key exists exactly
because a business pack declared the field behind it. What a tool returns, per
requirement 13:

=====================================  ===================================================
tool                                   result
=====================================  ===================================================
``<entity>_get`` and the other          every non-internal field of the record, the
reads and writes of one record          conversion records of the material entity last
                                        (requirements 5.12, 5.16, 6.9)
``<bill>_get``, ``_create``,            the header fields, then ``lines`` with every field
``_update``, ``_delete``, a state       of every line (requirements 13.2, 13.9)
operation and a push
``<object>_search``                     ``items`` with the summary fields, then ``total``,
                                        ``page``, ``page_size``, ``has_more``
                                        (requirements 5.10, 5.11)
a declarative query                     ``latest`` and ``history`` (requirement 5.13)
the stock balance query                 a list of material, warehouse, base unit and the
                                        quantity on hand (requirements 9.7, 9.12)
``business_context_get``                ``business_date``, ``current_period``, ``org``,
                                        ``user`` (requirement 16.10)
=====================================  ===================================================

How a value is written (requirements 13.3 and 13.4): a ref field and the header's
``source_bill_id`` become a nested object under the name without ``_id`` - ``id``, ``code``
and ``name`` of the referenced record, as far as the referenced entity declares them, and
``id`` and ``number`` for a bill; ``qty``, ``price``, ``money`` and ``rate`` values are
decimal strings and not JSON numbers; ``int`` is an integer, ``bool`` a boolean and every
other type a string. A key whose value can be empty has ``null`` in its ``type``, which is
every key but the ones the engine assigns itself (:data:`ASSIGNED`).

The shape of the schema (requirement 11.7): every object says
``additionalProperties: false`` and requires every key it declares, so the properties of a
result are exactly the keys it carries and the order they are declared in is the order of
those keys, which is what the result assembly reads them in (design.md 5.4). The one
optional key is ``truncated``, the last property of every top-level result object, which a
result carries only when it was truncated (requirement 13.5). :func:`schema_object` is the
only way a schema object is built here and writes the keys in the fixed order of
:data:`KEYWORDS`, so two runs of the generator produce byte-identical text
(requirement 11.10).

The annotations are the hints a client reads before it calls a tool (requirement 11.8):
:data:`READ_ONLY` for a search, a get, a query and the built-in tool, and for a write tool
``destructiveHint`` false where it only adds - create, a push, submit and approve - and
true where it changes, withdraws or ends something. ``openWorldHint`` is false everywhere:
a sandbox is a closed world.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.fields import FieldType
from erp_mock.config.model import (
    BillModel,
    DomainModel,
    EntityModel,
    FieldModel,
    FieldRef,
    QueryModel,
    SearchModel,
    UnitConversionModel,
)
from erp_mock.tools.input_schema import (
    BALANCE_PARAMS,
    ID_PARAM,
    LINES_PARAM,
    PAGE_PARAM,
    PAGE_SIZE_PARAM,
)

__all__ = [
    "ADDITIVE_ACTIONS",
    "ASSIGNED",
    "BUILTIN_OUTPUT",
    "BUSINESS_DATE",
    "CURRENT_PERIOD",
    "HAS_MORE",
    "HISTORY",
    "ITEMS",
    "KEYWORDS",
    "LATEST",
    "ORG",
    "QTY_BASE",
    "READ_ONLY",
    "READ_ONLY_ACTIONS",
    "TOTAL",
    "TRUNCATED",
    "UNIT_CONVERSIONS",
    "USER",
    "ToolAnnotationsSpec",
    "ToolOutput",
    "annotations_of",
    "bill_ref_schema",
    "can_be_null",
    "entity_ref_schema",
    "field_schema",
    "object_output",
    "push_output",
    "query_output",
    "result_key",
    "schema_object",
    "type_schema",
]

# The JSON Schema keywords an outputSchema uses, in the order they are written. A result
# schema states structure and nothing else: the type of a value, the keys of an object,
# which of them must be there, the element of an array and that no other key may appear
# (requirement 11.7). The 14 keywords an inputSchema may use are the compatible subset a
# client validates arguments against (requirement 11.3); the constraints among them -
# enum, pattern, maxLength, the bounds - belong to what a tool accepts and say nothing
# about what it returns, so they are not written here.
KEYWORDS: Final[tuple[str, ...]] = (
    "type",
    "properties",
    "required",
    "items",
    "additionalProperties",
)

# The JSON types a result value has.
_OBJECT: Final = "object"
_ARRAY: Final = "array"
_STRING: Final = "string"
_INTEGER: Final = "integer"
_BOOLEAN: Final = "boolean"
_NULL: Final = "null"

# The keys the engine adds itself to a result. ``truncated`` is the only optional one and
# the last property of every top-level object (requirements 11.7 and 13.5); the four of a
# list and ``lines`` are what tool-design.md section 5 writes.
TRUNCATED: Final = "truncated"
ITEMS: Final = "items"
TOTAL: Final = "total"
HAS_MORE: Final = "has_more"
# The two keys of a declarative query's result (requirement 5.13).
LATEST: Final = "latest"
HISTORY: Final = "history"
# The four keys of the business context, in the order requirement 16.10 returns them.
BUSINESS_DATE: Final = "business_date"
CURRENT_PERIOD: Final = "current_period"
ORG: Final = "org"
USER: Final = "user"

# The conversion records a material carries in its own result (requirement 6.9). The key
# is the engine's, the name of the unit_conversion primitive for the array it returns; the
# keys inside each item are the pack's, the conversion entity's own unit and factor fields.
UNIT_CONVERSIONS: Final = "unit_conversions"

# The standard fields a result returns next to the declared ones, by the name they have in
# the field view (meta-model.md section 4).
_NUMBER: Final = "number"
_BILL_TYPE: Final = "bill_type"
_STATUS: Final = "status"
_BIZ_DATE: Final = "biz_date"
_LINE_NO: Final = "line_no"
_CREATED_SEQ: Final = "created_seq"
_UPDATED_SEQ: Final = "updated_seq"
_SOURCE_BILL_ID: Final = "source_bill_id"
_CODE: Final = "code"
_NAME: Final = "name"
_AMOUNT_WITH_TAX: Final = "amount_with_tax"
# The quantity on hand of the stock balance, in the base unit of the material
# (requirement 9.7); the engine's own column name (design.md data model 3).
QTY_BASE: Final = "qty_base"

# The keys the engine assigns itself on every record it writes, which therefore never hold
# null: the internal ID, the generated code of an entity that declares a code rule, a
# bill's number, type, status and operation sequence numbers, and a line's number
# (requirements 4.3, 4.4, 4.5 and 7.1). Every other key can be empty - a draft is written
# before its required fields are filled in (requirement 7.5), a computed amount is empty
# while the line it comes from is (requirement 6.10) - and holds null then
# (requirement 13.4). The name counts only where the field is a standard field: a pack that
# declares a ``code`` field of its own fills it itself.
ASSIGNED: Final[frozenset[str]] = frozenset(
    {
        ID_PARAM,
        _CODE,
        _NUMBER,
        _BILL_TYPE,
        _STATUS,
        _LINE_NO,
        _CREATED_SEQ,
        _UPDATED_SEQ,
    }
)

# The suffix a ref field's name ends with and a result drops (requirement 13.3).
_ID_SUFFIX: Final = "_id"

# The one primitive query of M0, the stock balance of the inventory primitive
# (requirement 9.7); the same pair :func:`~erp_mock.tools.input_schema.query_input` reads.
_INVENTORY: Final = "inventory"
_BALANCE: Final = "balance"

# The JSON type of each of the 12 field types (meta-model.md section 3, requirement 13.4).
# A ref field is no scalar: it returns a nested object (:func:`entity_ref_schema`).
_JSON_TYPE: Final[Mapping[FieldType, str]] = MappingProxyType(
    {
        "string": _STRING,
        "text": _STRING,
        "enum": _STRING,
        "date": _STRING,
        "period": _STRING,
        # A decimal value is a string, never a JSON number, so no client turns it into a
        # binary float (requirement 13.4).
        "qty": _STRING,
        "price": _STRING,
        "money": _STRING,
        "rate": _STRING,
        "int": _INTEGER,
        "bool": _BOOLEAN,
        "ref": _OBJECT,
    }
)


# --- the annotations of one tool (requirement 11.8) --------------------------------------

# The names the annotations have in tools/list, which are those of the MCP schema.
_READ_ONLY_HINT: Final = "readOnlyHint"
_DESTRUCTIVE_HINT: Final = "destructiveHint"
_OPEN_WORLD_HINT: Final = "openWorldHint"

# The one action that returns a list of records instead of one record (requirement 5.10).
_SEARCH_ACTION: Final = "search"
# The actions whose tool reads and writes nothing: a search, a get and a query, to which
# the built-in tool belongs (requirement 11.8).
READ_ONLY_ACTIONS: Final[frozenset[str]] = frozenset({_SEARCH_ACTION, "get", "query"})
# The action of a push tool (:data:`~erp_mock.tools.generator.PUSH_ACTION`), which creates
# a new bill and changes nothing that is there.
_PUSH_ACTION: Final = "create_from"
# The actions of a write tool that only add or advance, and whose destructiveHint is
# therefore false (requirement 11.8): a new record, a pushed bill, a submit and an approve.
# Every other write tool - update, delete, the withdrawals, close and unclose - changes or
# ends what is there.
ADDITIVE_ACTIONS: Final[frozenset[str]] = frozenset({"create", _PUSH_ACTION, "submit", "approve"})


@dataclass(frozen=True, slots=True)
class ToolAnnotationsSpec:
    """The annotations of one tool: the hints of requirement 11.8.

    ``destructive_hint`` is None for a read-only tool and left out of tools/list there: the
    hint says what a call changes, which is meaningful only where a tool changes anything
    at all. ``open_world_hint`` is false for every tool of the engine: a call reaches the
    sandbox of the session and nothing outside it.
    """

    read_only_hint: bool = False
    destructive_hint: bool | None = None
    open_world_hint: bool = False

    def as_json(self) -> FrozenMap[str, JsonValue]:
        """The annotations under the names and in the order tools/list carries them."""
        items: list[tuple[str, JsonValue]] = [(_READ_ONLY_HINT, self.read_only_hint)]
        if self.destructive_hint is not None:
            items.append((_DESTRUCTIVE_HINT, self.destructive_hint))
        items.append((_OPEN_WORLD_HINT, self.open_world_hint))
        return FrozenMap(items)


# A tool that changes nothing: a search, a get, a query and business_context_get.
READ_ONLY: Final = ToolAnnotationsSpec(read_only_hint=True)
# A write tool that only adds or advances, and one that changes or ends something.
_ADDITIVE: Final = ToolAnnotationsSpec(destructive_hint=False)
_DESTRUCTIVE: Final = ToolAnnotationsSpec(destructive_hint=True)


def annotations_of(action: str) -> ToolAnnotationsSpec:
    """The annotations of the tool that performs ``action`` (requirement 11.8).

    ``action`` is an action of the word list of tool-design.md section 2 or the
    ``create_from`` of a push tool; an action outside the list, which loading step 6
    reports (requirement 11.2), counts as a write tool that changes something.
    """
    if action in READ_ONLY_ACTIONS:
        return READ_ONLY
    return _ADDITIVE if action in ADDITIVE_ACTIONS else _DESTRUCTIVE


# --- one schema object -------------------------------------------------------------------


def schema_object(**items: JsonValue) -> FrozenMap[str, JsonValue]:
    """One schema object, its keys in the fixed order of :data:`KEYWORDS`.

    The only way this module writes a schema: a keyword outside the five raises ValueError,
    so no caller adds a constraint a result would then have to satisfy, and the key order
    does not depend on the order the caller passes them in.
    """
    unknown = sorted(set(items) - set(KEYWORDS))
    if unknown:
        allowed = "、".join(KEYWORDS)
        raise ValueError(f"outputSchema 不能使用关键字 {'、'.join(unknown)}，只能使用 {allowed}")
    return FrozenMap((keyword, items[keyword]) for keyword in KEYWORDS if keyword in items)


@dataclass(frozen=True, slots=True)
class _Key:
    """One key of a result object: its name and the schema of its value."""

    name: str
    schema: FrozenMap[str, JsonValue]


def _object_schema(
    keys: Sequence[_Key], *, nullable: bool = False, required: Sequence[str] | None = None
) -> FrozenMap[str, JsonValue]:
    """The object of ``keys``: their properties in order, every one of them required.

    ``nullable`` adds ``null`` to the type, which a nested object needs: a ref field whose
    value is empty returns null (requirement 13.3). ``required`` overrides the keys that
    must be there and is written only for the built-in tool, whose schema is the same for
    every instance (:data:`BUILTIN_OUTPUT`).
    """
    items: dict[str, JsonValue] = {
        # A tuple, not a list: nothing a loaded instance holds may be modified
        # (requirement 2.11), and a tuple serializes like a JSON array (common.jsonio).
        "type": (_OBJECT, _NULL) if nullable else _OBJECT,
        "properties": FrozenMap((key.name, key.schema) for key in keys),
    }
    names = tuple(key.name for key in keys) if required is None else tuple(required)
    if names:
        items["required"] = names
    items["additionalProperties"] = False
    return schema_object(**items)


def _array_schema(keys: Sequence[_Key]) -> FrozenMap[str, JsonValue]:
    """An array of result objects: a bill's lines, a list's items, a query's history."""
    return schema_object(type=_ARRAY, items=_object_schema(keys))


def _result(keys: Sequence[_Key]) -> FrozenMap[str, JsonValue]:
    """A top-level result object: ``keys``, then the optional ``truncated`` (req 11.7).

    ``truncated`` is the last property and the only one that is not required: a result
    carries it only when the detail array was shortened to fit the size limit
    (requirement 13.5).
    """
    properties: list[tuple[str, JsonValue]] = [(key.name, key.schema) for key in keys]
    properties.append((TRUNCATED, schema_object(type=_BOOLEAN)))
    items: dict[str, JsonValue] = {"type": _OBJECT, "properties": FrozenMap(properties)}
    names = tuple(key.name for key in keys)
    if names:
        items["required"] = names
    items["additionalProperties"] = False
    return schema_object(**items)


# --- the value of one field (requirements 13.3 and 13.4) ---------------------------------


def type_schema(field_type: FieldType, *, nullable: bool) -> FrozenMap[str, JsonValue]:
    """The schema of one scalar field type (meta-model.md section 3, requirement 13.4).

    ``nullable`` puts ``null`` next to the type, for a value that can be empty. A ref type
    reaches this only where its target is unknown, and gets the object of an unresolved
    reference (:func:`entity_ref_schema`).
    """
    found = _JSON_TYPE[field_type]
    return schema_object(type=(found, _NULL) if nullable else found)


def field_schema(domain: DomainModel, field: FieldModel) -> FrozenMap[str, JsonValue]:
    """The schema of the value ``field`` returns (requirements 13.3 and 13.4).

    A ref field, the header's ``source_bill_id`` included, returns the nested object of the
    record it refers to; every other field returns the scalar of its type. ``domain`` is
    read for the field view of the referenced entity, which decides whether the nested
    object carries a ``code`` and a ``name``.
    """
    if not field.nested:
        return type_schema(field.type, nullable=can_be_null(field))
    if field.ref is None:
        # The target of source_bill_id is the bill its record's source_bill_type names, so
        # it is known per record and not per field (requirement 13.3); any other ref field
        # without a target is one step 5 could not resolve.
        if field.origin == "standard" and field.name == _SOURCE_BILL_ID:
            return bill_ref_schema()
        return entity_ref_schema(domain, None)
    if field.ref in domain.bills:
        return bill_ref_schema()
    return entity_ref_schema(domain, field.ref)


def can_be_null(field: FieldModel) -> bool:
    """Whether the value of ``field`` can be null in a result (requirement 13.4).

    Every field but the ones the engine assigns itself (:data:`ASSIGNED`); a key is never
    left out, so an empty value is null.
    """
    return not (field.origin == "standard" and field.name in ASSIGNED)


def result_key(field: FieldModel) -> str:
    """The key ``field`` takes in a result: a ref field without its ``_id`` (req 13.3)."""
    return field.name.removesuffix(_ID_SUFFIX) if field.nested else field.name


def entity_ref_schema(domain: DomainModel, target: str | None) -> FrozenMap[str, JsonValue]:
    """The nested object a reference to a record of ``target`` returns (req 13.3).

    ``id``, then ``code`` and ``name`` as far as the entity declares them - an entity
    without one of the two returns no such key at all. ``target`` is None for a reference
    step 5 could not resolve, which leaves the id alone.
    """
    entity = None if target is None else domain.entities.get(target)
    fields: Mapping[str, FieldModel] = FrozenMap() if entity is None else entity.fields
    keys = [_Key(ID_PARAM, schema_object(type=_STRING))]
    keys += [
        _Key(name, type_schema(found.type, nullable=can_be_null(found)))
        for name in (_CODE, _NAME)
        if (found := fields.get(name)) is not None and not found.nested
    ]
    return _object_schema(keys, nullable=True)


def bill_ref_schema() -> FrozenMap[str, JsonValue]:
    """The nested object a reference to a bill returns: ``id`` and ``number`` (13.3)."""
    keys = (
        _Key(ID_PARAM, schema_object(type=_STRING)),
        _Key(_NUMBER, schema_object(type=_STRING)),
    )
    return _object_schema(keys, nullable=True)


# --- the result of one tool --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """What one tool returns and what a client may assume about calling it.

    ``schema`` is the top-level result object, which goes into
    :attr:`ToolSpec.output_schema <erp_mock.tools.generator.ToolSpec.output_schema>`, and
    ``annotations`` the hints of requirement 11.8.
    """

    schema: FrozenMap[str, JsonValue]
    annotations: ToolAnnotationsSpec


# business_context_get returns the business context of the sandbox: the anchor date, the
# month it falls in, the default organization and the current user (requirement 16.10). The
# framework writes this schema, so it is the same for every instance and cannot read the
# field view of the two context entities; a context entity that declares no code or name
# returns no such key (requirement 13.3), which is why the two nested objects require the
# id alone - the one place besides ``truncated`` where a declared key is optional.
_CONTEXT_REF: Final = _object_schema(
    (
        _Key(ID_PARAM, schema_object(type=_STRING)),
        _Key(_CODE, schema_object(type=(_STRING, _NULL))),
        _Key(_NAME, schema_object(type=(_STRING, _NULL))),
    ),
    nullable=True,
    required=(ID_PARAM,),
)
BUILTIN_OUTPUT: Final = ToolOutput(
    schema=_result(
        (
            _Key(BUSINESS_DATE, schema_object(type=_STRING)),
            _Key(CURRENT_PERIOD, schema_object(type=_STRING)),
            _Key(ORG, _CONTEXT_REF),
            _Key(USER, _CONTEXT_REF),
        )
    ),
    annotations=READ_ONLY,
)


def object_output(domain: DomainModel, item: EntityModel | BillModel, action: str) -> ToolOutput:
    """What the ``action`` tool of an entity or a bill returns (requirement 13.1).

    A search returns the list of summary records (requirement 5.10); every other action -
    get, create, update, delete and each state operation - returns the whole record, with
    every line of a bill (requirements 5.12, 5.16, 13.2 and 13.9).
    """
    search = action == _SEARCH_ACTION
    if isinstance(item, BillModel):
        keys = _bill_summary(domain, item) if search else _bill_keys(domain, item)
    else:
        keys = _record_summary(domain, item) if search else _record_keys(domain, item)
    schema = _list_result(keys) if search else _result(keys)
    return ToolOutput(schema=schema, annotations=annotations_of(action))


def push_output(domain: DomainModel, target: BillModel | None) -> ToolOutput:
    """What a push tool returns: the target bill it generated (requirement 13.2).

    The result is the one a get of that bill returns, lines included. ``target`` is None
    when step 5 could not resolve the target of the conversion.
    """
    keys = () if target is None else _bill_keys(domain, target)
    return ToolOutput(schema=_result(keys), annotations=annotations_of(_PUSH_ACTION))


def query_output(domain: DomainModel, query: QueryModel) -> ToolOutput:
    """What a query tool returns: ``latest`` and ``history``, or what a primitive defines.

    A declarative query returns the fields of ``returns.latest`` of the first matching
    record, null when none matches, and ``history`` with the fields of
    ``returns.history.fields`` of the first few (requirement 5.13); the stock balance query
    of the inventory primitive returns a list (requirements 9.7 and 9.12).
    """
    if query.primitive is not None:
        return ToolOutput(schema=_primitive_result(domain, query), annotations=READ_ONLY)
    keys: tuple[_Key, ...] = (
        _Key(LATEST, _object_schema(_query_keys(domain, query, query.latest), nullable=True)),
        _Key(HISTORY, _array_schema(_query_keys(domain, query, query.history))),
    )
    if query.history_paginated:
        keys += (
            _Key("offset", schema_object(type=_INTEGER)),
            _Key("returned_count", schema_object(type=_INTEGER)),
            _Key("next_offset", schema_object(type=(_INTEGER, _NULL))),
            _Key(HAS_MORE, schema_object(type=_BOOLEAN)),
            _Key("continuation", schema_object(type=_STRING)),
        )
    return ToolOutput(schema=_result(keys), annotations=READ_ONLY)


# --- the keys of each result shape -------------------------------------------------------


def _record_keys(domain: DomainModel, entity: EntityModel) -> tuple[_Key, ...]:
    """Every field of an entity record, the conversion records last (req 5.12 and 6.9)."""
    keys = _field_keys(domain, entity.fields.values())
    conversion = entity.unit_conversion
    if conversion is None:
        return keys
    return (*keys, _Key(UNIT_CONVERSIONS, _conversion_array(domain, conversion)))


def _bill_keys(domain: DomainModel, bill: BillModel) -> tuple[_Key, ...]:
    """Every field of a bill header, then its lines, by line number (requirement 5.12)."""
    lines = _field_keys(domain, bill.lines.values())
    return (*_field_keys(domain, bill.header.values()), _Key(LINES_PARAM, _array_schema(lines)))


def _field_keys(domain: DomainModel, fields: Iterable[FieldModel]) -> tuple[_Key, ...]:
    """The keys of ``fields``, in the order of the field view."""
    return tuple(_Key(result_key(field), field_schema(domain, field)) for field in fields)


def _conversion_array(
    domain: DomainModel, conversion: UnitConversionModel
) -> FrozenMap[str, JsonValue]:
    """The conversion records of a material: the unit and the factor of each (req 6.9).

    Each item holds the conversion unit as a nested object and the factor as a decimal
    string, under the names the pack gave the two fields of its conversion entity; the
    array is empty when the material has no conversion record.
    """
    entity = domain.entities.get(conversion.entity)
    fields: Mapping[str, FieldModel] = FrozenMap() if entity is None else entity.fields
    names = (conversion.unit_field, conversion.factor_field)
    found = [field for name in names if (field := fields.get(name)) is not None]
    return _array_schema(_field_keys(domain, found))


def _list_result(items: Sequence[_Key]) -> FrozenMap[str, JsonValue]:
    """A list result: ``items``, then the page the records come from (requirement 5.11)."""
    keys = (
        _Key(ITEMS, _array_schema(items)),
        _Key(TOTAL, schema_object(type=_INTEGER)),
        _Key(PAGE_PARAM, schema_object(type=_INTEGER)),
        _Key(PAGE_SIZE_PARAM, schema_object(type=_INTEGER)),
        _Key(HAS_MORE, schema_object(type=_BOOLEAN)),
    )
    return _result(keys)


def _record_summary(domain: DomainModel, entity: EntityModel) -> tuple[_Key, ...]:
    """The summary fields of an entity record (requirement 5.10).

    ``id``, the ``code`` and the ``name`` the entity declares, its ``status`` when it keeps
    one, and the fields its search reads: the keyword fields and the filters, in
    declaration order. No field appears twice and no line is returned.
    """
    names = [ID_PARAM, _CODE, _NAME]
    if entity.has_status:
        names.append(_STATUS)
    names += _search_fields(entity.search)
    return _summary_keys(domain, entity.fields, names)


def _bill_summary(domain: DomainModel, bill: BillModel) -> tuple[_Key, ...]:
    """The summary fields of a bill (requirement 5.10).

    ``id``, ``number``, ``status``, ``biz_date``, the partner reference the bill declares,
    the fields of its filters and, for a bill with pricing, ``amount_with_tax``.
    """
    names = [ID_PARAM, _NUMBER, _STATUS, _BIZ_DATE]
    if bill.partner_field is not None:
        names.append(bill.partner_field)
    names += [] if bill.search is None else [ref.name for ref in bill.search.filters]
    if bill.pricing:
        names.append(_AMOUNT_WITH_TAX)
    return _summary_keys(domain, bill.header, names)


def _search_fields(search: SearchModel | None) -> list[str]:
    """The fields a search reads: its keyword fields, then its filters (requirement 5.9)."""
    if search is None:
        return []
    return [ref.name for ref in (*search.keyword_fields, *search.filters)]


def _summary_keys(
    domain: DomainModel, fields: Mapping[str, FieldModel], names: Sequence[str]
) -> tuple[_Key, ...]:
    """The keys of ``names``, in that order, each one once and each one a field of the level.

    A name that is no field of the level contributes no key: a search field step 5 could
    not resolve, or a standard field the object does not have.
    """
    found = [fields[name] for name in dict.fromkeys(names) if name in fields]
    return _field_keys(domain, found)


def _query_keys(
    domain: DomainModel, query: QueryModel, refs: Sequence[FieldRef]
) -> tuple[_Key, ...]:
    """The fields a declarative query returns, in the order ``returns`` lists them (5.13).

    Each field is read from the level of the source it names: the lines of the source bill
    for a ``line.`` path and the header for a ``header.`` one, so a query over the lines of
    a bill returns fields of both. A field of both levels with one name is returned once,
    under that name.
    """
    source = query.source
    if source is None:
        return ()
    found: dict[str, FieldModel] = {}
    for ref in refs:
        field = domain.fields_of(source, ref.level).get(ref.name)
        if field is not None:
            found.setdefault(result_key(field), field)
    return _field_keys(domain, found.values())


# --- the result of the one primitive query of M0 (requirements 9.7 and 9.12) -------------


def _primitive_result(domain: DomainModel, query: QueryModel) -> FrozenMap[str, JsonValue]:
    """The result of the stock balance query, a list of quantities on hand (req 9.7).

    Each item holds the material, the warehouse, the base unit of the material and the
    quantity on hand in that base unit. The material and the warehouse are the engine's own
    columns of the stock balance (design.md data model 3), named like the two parameters of
    the query; the base unit carries the name the material entity gave its base unit field.
    A query no primitive provides returns nothing: step 5 reports it (requirement 2.6).
    """
    if (query.primitive, query.query) != (_INVENTORY, _BALANCE):
        return _result(())
    material = _material_entity(domain)
    conversion = None if material is None else material.unit_conversion
    keys = [
        _Key(
            _ref_key(BALANCE_PARAMS[0]),
            entity_ref_schema(domain, None if material is None else material.name),
        ),
        _Key(_ref_key(BALANCE_PARAMS[1]), entity_ref_schema(domain, _warehouse_entity(domain))),
    ]
    if conversion is not None:
        base = entity_ref_schema(domain, conversion.unit_entity)
        keys.append(_Key(_ref_key(conversion.base_unit_field), base))
    keys.append(_Key(QTY_BASE, schema_object(type=_STRING)))
    return _list_result(keys)


def _ref_key(name: str) -> str:
    """The key a reference takes in a result: its field name without ``_id`` (req 13.3)."""
    return name.removesuffix(_ID_SUFFIX)


def _material_entity(domain: DomainModel) -> EntityModel | None:
    """The material entity of the instance: the one that declares unit_conversion (6.8).

    It is the entity the quantity semantics of a bill's lines refer to, and the one that
    names the base unit field and the conversion records; the first in model order where an
    instance declares more than one.
    """
    return next((item for item in domain.entities.values() if item.unit_conversion), None)


def _warehouse_entity(domain: DomainModel) -> str | None:
    """The warehouse entity of the instance: what an inventory linkage's field refers to.

    The warehouse of an in, out or adjust linkage and the two warehouses of a transfer are
    required ref fields of the bill (requirement 9.11), so the first one that resolves names
    the entity the stock balance counts per warehouse; None when no linkage resolves.
    """
    for linkage in domain.linkages.values():
        refs = (linkage.warehouse, linkage.from_warehouse, linkage.to_warehouse)
        for ref in refs:
            if ref is None:
                continue
            field = domain.fields_of(linkage.bill, ref.level).get(ref.name)
            if field is not None and field.ref is not None:
                return field.ref
    return None
