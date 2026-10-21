"""The structuredContent, the text and the error object of one tool result (req 13).

The last step of a tools/call (design.md 5.3 step 14): the engine has produced a view -
typed values with resolved references (:mod:`erp_mock.domain.views`) - and this module
turns it into the JSON a client reads. Two kinds of payload come out of it
(:class:`ResultPayload`):

- a successful call returns :func:`result_payload`: ``structured`` is the
  structuredContent, ``text`` the one text item of ``content``, which is the compact JSON
  of exactly that object (requirement 13.1);
- a failed call returns :func:`error_payload`: ``error`` is the value of the one ``error``
  key, and ``text`` the compact JSON of ``{"error": ...}``; there is no structuredContent
  (requirement 13.6).

How the keys of a successful result are ordered (requirement 13.1): every object is built
by walking the ``properties`` of the outputSchema of the tool
(:mod:`erp_mock.tools.output_schema`) and taking the value of each property from the view,
so the key order of the result is the declaration order of the schema and the two cannot
drift apart - a key the view carries that the schema does not declare, or a required key
the view does not carry, raises RuntimeError rather than producing a result that fails its
own schema. That is also why :mod:`erp_mock.domain.views` may spell out the result keys of
requirement 13.3 a second time without importing this layer (requirement 24.8): the
assembly here is what makes the two agree.

How a value is written (requirement 13.4). The view hands over Python values and the
schema says nothing about a field's type beyond its JSON type, so the decimal kind of each
key - which decides that a money value keeps exactly 2 decimals - is read from the field
view of the configuration into :class:`ResultKinds` (:func:`result_kinds`) and the value
goes through the Decimal_Formatter of :func:`~erp_mock.common.decimals.format_decimal`. A
date becomes its ``YYYY-MM-DD`` text, an int and a bool stay what they are, a reference
becomes the nested object of its summary, and an empty value becomes null, with its key
kept.

Truncation (requirement 13.5, :func:`truncate_result`). The text of a successful result
stays within :data:`TEXT_LIMIT` bytes: where it does not, the one detail array of the
result - ``items`` of a list, ``lines`` of a bill, ``history`` of a query - is cut to its
longest prefix that fits once ``truncated`` is added, which is found by binary search. The
size of a prefix grows strictly with the number of elements kept, so the prefix found is
the one removing elements from the end one by one arrives at. Nothing else of the result
changes, and ``truncated`` appears only in a result that was actually shortened. An error
result is never truncated.

Opt-in paginated queries instead fit their history and continuation metadata together
within the same byte limit. They advance by the complete records actually returned and
never silently discard the rest of the matching sequence. Other operations retain the
legacy truncation contract.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, assert_never

from erp_mock.common.decimals import DecimalKind, format_decimal
from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact, encode_utf8
from erp_mock.config.model import (
    BillModel,
    DomainModel,
    EntityModel,
    FieldModel,
    FieldType,
    QueryModel,
    UnitConversionModel,
)
from erp_mock.domain.engine import View
from erp_mock.domain.views import (
    PageView,
    QueryView,
    RecordView,
    RefSummary,
    ViewValue,
    referenced,
    result_key,
    summary_keys,
)
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import LINES_PARAM, PAGE_PARAM, PAGE_SIZE_PARAM
from erp_mock.tools.output_schema import (
    HAS_MORE,
    HISTORY,
    ITEMS,
    LATEST,
    QTY_BASE,
    TOTAL,
    TRUNCATED,
    UNIT_CONVERSIONS,
)

__all__ = [
    "CODE",
    "DETAILS",
    "ERROR_KEY",
    "INTERNAL_MESSAGE",
    "MESSAGE",
    "NO_KINDS",
    "RULE_ID",
    "TEXT_LIMIT",
    "ResultKinds",
    "ResultPayload",
    "ResultValue",
    "error_payload",
    "internal_error",
    "result_kinds",
    "result_object",
    "result_payload",
    "truncate_result",
]

# The size a successful result's text stays within, in UTF-8 bytes (requirement 13.5).
TEXT_LIMIT: Final = 16384

# The one key an error result's text carries, and the four keys of the error object, in
# the order it carries them (requirement 13.6).
ERROR_KEY: Final = "error"
CODE: Final = "code"
MESSAGE: Final = "message"
RULE_ID: Final = "rule_id"
DETAILS: Final = "details"

# The message of every INTERNAL_ERROR, written by the framework: it names no exception,
# no stack and no statement, and the error carries no detail item (requirement 13.10,
# design.md data model 6).
INTERNAL_MESSAGE: Final = "服务内部错误，本次调用未产生任何写入"

# The schema keywords this module reads, three of the five an outputSchema uses
# (:data:`~erp_mock.tools.output_schema.KEYWORDS`, requirement 11.7): the properties of an
# object, which of them must be there, and the element schema of an array. The last one is
# spelled like the ``items`` key of a list result (:data:`ITEMS`) and is not it: this is a
# keyword of the schema, that is a key of the result.
_PROPERTIES: Final = "properties"
_REQUIRED: Final = "required"
_ITEMS: Final = "items"


# One value of a result object as the engine hands it over: a view value, a whole record -
# the ``latest`` of a declarative query - or one of the page values of a search.
type ResultValue = ViewValue | RecordView


@dataclass(frozen=True, slots=True)
class ResultPayload:
    """The structuredContent or the error object of one tool result, and its text.

    ``text`` is the one text item of ``content`` and is always there: the compact JSON of
    ``structured`` for a successful call and of ``{"error": <error>}`` for a failed one
    (requirements 13.1 and 13.6). Exactly one of ``structured`` and ``error`` is set, so
    :attr:`is_error` says which kind of result it is; a failed call carries no
    structuredContent at all.
    """

    text: str
    structured: dict[str, JsonValue] | None = None
    error: dict[str, JsonValue] | None = None

    @property
    def is_error(self) -> bool:
        """Whether the call ended with an error code (requirement 13.6)."""
        return self.error is not None


@dataclass(frozen=True, slots=True)
class ResultKinds:
    """The decimal kind of every value of one result object, and of its nested objects.

    ``kinds`` maps the result key of each qty, price, money or rate field of the object to
    its kind, which the Decimal_Formatter needs to write the value (requirement 13.4);
    ``children`` holds the same for each nested object and array of the object, by the key
    it sits under - the ``lines`` of a bill, the ``items`` of a list, the ``latest`` and
    ``history`` of a query, the conversion records of a material and a reference whose
    summary carries a decimal value.
    """

    kinds: FrozenMap[str, DecimalKind] = FrozenMap()
    children: FrozenMap[str, "ResultKinds"] = FrozenMap()


# A result object no value of which is a decimal: the business context, and the fallback
# for a tool whose target the instance does not have.
NO_KINDS: Final = ResultKinds()


# --- the payload of one call -------------------------------------------------------------


def result_payload(domain: DomainModel, spec: ToolSpec, view: View) -> ResultPayload:
    """The payload of a successful call of ``spec`` (requirements 13.1 to 13.5).

    The view of the operation as the structuredContent of the tool, the keys of every
    object in the declaration order of its outputSchema, and the compact JSON of exactly
    that object as the text, shortened to :data:`TEXT_LIMIT` bytes where it has to be.

    Raises RuntimeError when the view and the outputSchema do not carry the same keys,
    which is a mismatch between :mod:`erp_mock.domain.views` and
    :mod:`erp_mock.tools.output_schema` and no error of the call.
    """
    built = result_object(spec.output_schema, view, result_kinds(domain, spec))
    if isinstance(view, QueryView) and view.offset is not None:
        structured, text = _paginate_result(built)
    else:
        structured, text = truncate_result(built)
    return ResultPayload(text=text, structured=structured)


def error_payload(error: ErpError) -> ResultPayload:
    """The payload of a call that ended with an error code (requirements 13.6 to 13.8).

    The text is the compact JSON of an object whose one key is ``error``, whose value
    carries ``code``, ``message``, ``rule_id`` and ``details`` in that order and always
    carries all four: ``rule_id`` is null for an error no rule decided and ``details`` an
    empty array for an error with no detail item. The detail items are written as the
    raiser built them - their values are already in output form (requirement 13.4) and
    their order is the report order of the check that failed. An error result is not
    truncated.
    """
    found: dict[str, JsonValue] = {
        CODE: error.code.value,
        MESSAGE: error.message,
        RULE_ID: error.rule_id,
        DETAILS: [dict(item) for item in error.details],
    }
    return ResultPayload(text=dumps_compact({ERROR_KEY: found}), error=found)


def internal_error() -> ErpError:
    """The error a call that hit an unexpected exception ends with (req 12.8, 13.10).

    INTERNAL_ERROR with the one preset message of :data:`INTERNAL_MESSAGE`, no rule and no
    detail item, so nothing of the exception reaches the client; the tools layer logs the
    stack to stderr and rolls the call back before it returns this.
    """
    return ErpError(ErrorCode.INTERNAL_ERROR, INTERNAL_MESSAGE)


# --- the result object (requirements 13.1, 13.3 and 13.4) --------------------------------


def result_object(
    schema: Mapping[str, JsonValue], view: View, kinds: ResultKinds = NO_KINDS
) -> dict[str, JsonValue]:
    """``view`` as the result object ``schema`` describes, keys in its order (req 13.1).

    ``schema`` is the outputSchema of the tool and ``kinds`` the decimal kinds of its
    result (:func:`result_kinds`). The object is not truncated yet, which
    :func:`truncate_result` does; ``truncated`` is therefore not among its keys.

    Raises RuntimeError when the view carries a key the schema does not declare or leaves
    out a key the schema requires.
    """
    return _object(schema, _top_values(view), kinds, "")


def _top_values(view: View) -> Mapping[str, ResultValue]:
    """The values of the top-level object of a result, by the key each one takes.

    A record view is the result itself - a get, a write, a state operation and a push
    (requirements 5.16 and 13.2) - a page view is the records of the page and the page they
    come from (requirement 5.11), and a query view the first matching record and the first
    few of them (requirement 5.13).
    """
    if isinstance(view, RecordView):
        return view.fields
    if isinstance(view, PageView):
        page: dict[str, ResultValue] = {
            ITEMS: view.items,
            TOTAL: view.total,
            PAGE_PARAM: view.page,
            PAGE_SIZE_PARAM: view.page_size,
            HAS_MORE: view.has_more,
        }
        return page
    query: dict[str, ResultValue] = {LATEST: view.latest, HISTORY: view.history}
    if view.offset is not None:
        query.update(_page_metadata(view.offset, len(view.history), view.has_more))
    return query


def _page_metadata(offset: int, count: int, has_more: bool) -> dict[str, str | int | bool | None]:
    next_offset = offset + count if has_more else None
    return {
        "offset": offset,
        "returned_count": count,
        "next_offset": next_offset,
        HAS_MORE: has_more,
        "continuation": (
            f"还有记录；保持其他查询参数不变，使用 offset={next_offset} 继续读取。"
            if has_more
            else "已到结果末尾，无需继续翻页。"
        ),
    }


def _object(
    schema: Mapping[str, JsonValue],
    values: Mapping[str, ResultValue],
    kinds: ResultKinds,
    path: str,
) -> dict[str, JsonValue]:
    """One result object: the properties of ``schema`` in order, their values from ``values``.

    A property the object has no value for is left out where the schema does not require it
    - the ``code`` and the ``name`` of a context entity that declares neither
    (requirement 13.3) - and raises RuntimeError where it does. ``truncated`` is never
    taken from a view: :func:`truncate_result` is the only place it comes from
    (requirement 13.5).
    """
    properties = _properties(schema)
    required = _required(schema)
    unknown = [name for name in values if name not in properties]
    if unknown:
        raise RuntimeError(
            f"结果中的键 {'、'.join(unknown)} 不在 {path or '结果'} 的 outputSchema 中"
        )
    found: dict[str, JsonValue] = {}
    for name, declared in properties.items():
        if name == TRUNCATED:
            continue
        if name not in values:
            if name in required:
                raise RuntimeError(f"结果缺少 outputSchema 要求的键 {path}{name}")
            continue
        found[name] = _value(values[name], _as_object(declared), kinds, name, f"{path}{name}")
    return found


def _value(
    value: ResultValue,
    schema: Mapping[str, JsonValue],
    kinds: ResultKinds,
    name: str,
    path: str,
) -> JsonValue:
    """The JSON of one value of a result object (requirements 13.3 and 13.4).

    An empty value is null, a reference and a nested record the object of their keys, an
    array of records the list of those objects, a decimal the text of the Decimal_Formatter
    and a date its ``YYYY-MM-DD`` text; an int, a bool and a string are themselves.

    Raises RuntimeError for a value of another type, which no view produces.
    """
    if value is None:
        return None
    child = kinds.children.get(name, NO_KINDS)
    # The summary of a reference and the record of a query's ``latest`` are both one object.
    if isinstance(value, RefSummary | RecordView):
        return _object(schema, value.fields, child, f"{path}.")
    if isinstance(value, tuple):
        element = _as_object(schema.get(_ITEMS))
        return [
            _object(element, item.fields, child, f"{path}[{index}].")
            for index, item in enumerate(value)
        ]
    # A bool is an int to Python, so it is recognized first.
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return _decimal_text(value, kinds.kinds.get(name))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int | str):
        return value
    raise RuntimeError(f"结果的 {path} 是 {type(value).__name__}，不是结果可以返回的值")


def _decimal_text(value: Decimal, kind: DecimalKind | None) -> str:
    """The text of a decimal value: the Decimal_Formatter of its kind (req 4.8, 13.4).

    ``kind`` is None only for a key the field view of the configuration does not name as a
    decimal field, which leaves the canonical text of the value itself: every decimal a
    view carries was read from storage, which keeps exactly that text (design.md 3.1).
    """
    return format(value, "f") if kind is None else format_decimal(kind, value)


def _properties(schema: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """The ``properties`` of a schema object, empty for a schema that declares none."""
    found = schema.get(_PROPERTIES)
    return found if isinstance(found, Mapping) else FrozenMap()


def _required(schema: Mapping[str, JsonValue]) -> frozenset[str]:
    """The keys a schema object requires, empty for a schema that requires none."""
    found = schema.get(_REQUIRED)
    if not isinstance(found, Sequence) or isinstance(found, str):
        return frozenset()
    return frozenset(name for name in found if isinstance(name, str))


def _as_object(schema: JsonValue) -> Mapping[str, JsonValue]:
    """``schema`` as the schema object it is, empty where it is no object at all."""
    return schema if isinstance(schema, Mapping) else FrozenMap()


# --- truncation (requirement 13.5) -------------------------------------------------------


def truncate_result(
    structured: Mapping[str, JsonValue],
) -> tuple[dict[str, JsonValue], str]:
    """``structured`` and its text, cut to :data:`TEXT_LIMIT` bytes (requirement 13.5).

    A result whose text fits comes back as it is, without ``truncated``. One that does not
    keeps the longest prefix of its detail array (:func:`_detail_key`) whose text fits once
    ``truncated`` is added, and carries ``truncated`` true as its last key; every other
    field keeps the value it had, the total, the page and the header amounts included.

    Two results come back untruncated although their text is too long, because there is
    nothing to remove: one with no detail array, and one whose detail array is already
    empty. A result whose other fields alone exceed the limit keeps the empty array and
    says it was truncated.
    """
    text = dumps_compact(structured)
    if _size(text) <= TEXT_LIMIT:
        return (dict(structured), text)
    key = _detail_key(structured)
    if key is None:
        return (dict(structured), text)
    items = _as_array(structured[key])
    if not items:
        return (dict(structured), text)
    kept = _with_prefix(structured, key, items, _longest_prefix(structured, key, items))
    return (kept, dumps_compact(kept))


def _paginate_result(structured: Mapping[str, JsonValue]) -> tuple[dict[str, JsonValue], str]:
    """Fit complete records and continuation metadata together into one 16 KiB page.

    Advance by the number actually emitted, never by the requested limit. The domain
    selects candidates before the byte budget is applied; records removed here remain
    reachable through the returned offset. Legacy non-paginated operations are unchanged.
    """
    rows = _as_array(structured[HISTORY])
    offset = structured["offset"]
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise RuntimeError("分页查询缺少有效 offset")

    def page(count: int) -> dict[str, JsonValue]:
        result = dict(structured)
        result[HISTORY] = list(rows[:count])
        result.update(
            _page_metadata(offset, count, bool(structured[HAS_MORE]) or count < len(rows))
        )
        return result

    # The final-page notice is shorter than a continuation notice. Check the complete
    # page first; only proper prefixes have a monotone byte size for binary search.
    full = page(len(rows))
    full_text = dumps_compact(full)
    if _size(full_text) <= TEXT_LIMIT:
        return full, full_text
    minimum = page(1 if rows else 0)
    if _size(dumps_compact(minimum)) > TEXT_LIMIT:
        raise ErpError(
            ErrorCode.LIMIT_EXCEEDED,
            "单条完整记录及分页信息超过 16 KiB；请修复数据集或返回字段配置后再评测",
            details=({"limit": "query_page_bytes", "max": TEXT_LIMIT},),
        )
    low, high = 1, len(rows) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if _size(dumps_compact(page(middle))) <= TEXT_LIMIT:
            low = middle
        else:
            high = middle - 1
    result = page(low)
    return result, dumps_compact(result)


def _detail_key(structured: Mapping[str, JsonValue]) -> str | None:
    """The key of the detail array of a result, None for a result that has none.

    The one array of the top-level object: ``items`` of a list result and of the stock
    balance, ``lines`` of a bill, ``history`` of a declarative query and the conversion
    records of a material. Every other value of a result is a scalar or a nested object, so
    the first array is the only one.
    """
    return next((name for name, value in structured.items() if isinstance(value, list)), None)


def _longest_prefix(
    structured: Mapping[str, JsonValue], key: str, items: Sequence[JsonValue]
) -> int:
    """How many elements of ``items`` a truncated result keeps (requirement 13.5).

    The largest number whose result fits, found by binary search: the size of the text
    grows strictly with the number of elements kept, so this is the number that removing
    elements from the end one by one arrives at. 0 where even the empty array does not fit.
    """
    low, high, best = 0, len(items) - 1, 0
    while low <= high:
        middle = (low + high) // 2
        if _size(dumps_compact(_with_prefix(structured, key, items, middle))) <= TEXT_LIMIT:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return best


def _with_prefix(
    structured: Mapping[str, JsonValue], key: str, items: Sequence[JsonValue], count: int
) -> dict[str, JsonValue]:
    """``structured`` with the first ``count`` elements of its detail array, and truncated.

    The keys keep their order and ``truncated`` comes last, the place the outputSchema
    declares it in (requirement 11.7).
    """
    kept: dict[str, JsonValue] = {
        name: list(items[:count]) if name == key else value for name, value in structured.items()
    }
    kept[TRUNCATED] = True
    return kept


def _as_array(value: JsonValue) -> Sequence[JsonValue]:
    """``value`` as the array it is, empty where it is no array."""
    return value if isinstance(value, list) else ()


def _size(text: str) -> int:
    """The size of ``text`` in UTF-8 bytes, as it is written out (requirement 13.5)."""
    return len(encode_utf8(text))


# --- the decimal kinds of one result (requirement 13.4) ----------------------------------


def result_kinds(domain: DomainModel, spec: ToolSpec) -> ResultKinds:
    """The decimal kinds of the result of ``spec`` (:class:`ResultKinds`).

    Read from the field view of what the tool works on: the entity or bill of an operation,
    the target bill of a push, the fields a query returns, and nothing for the built-in
    tool, whose result carries no decimal value (requirement 16.10). A target the instance
    does not have - which step 5 reports (requirement 2.6) - contributes no kind.
    """
    match spec.kind:
        case "entity_op":
            return _object_kinds(domain, referenced(domain, spec.target))
        case "push":
            conversion = domain.conversions.get(spec.target)
            target = None if conversion is None else domain.bills.get(conversion.target)
            return _object_kinds(domain, target)
        case "query":
            return _query_kinds(domain, domain.queries.get(spec.target))
        case "builtin":
            return NO_KINDS
        case _ as unknown:
            assert_never(unknown)


def _object_kinds(domain: DomainModel, item: EntityModel | BillModel | None) -> ResultKinds:
    """The kinds of the result of an operation on ``item`` (requirements 5.10 and 5.12).

    The record itself, which a get, a write and a state operation return, plus the same
    record under ``items``, where a search returns the summary of each record of the page.
    """
    record = _record_kinds(domain, item)
    return ResultKinds(record.kinds, FrozenMap({**record.children, ITEMS: record}))


def _record_kinds(domain: DomainModel, item: EntityModel | BillModel | None) -> ResultKinds:
    """The kinds of one record of ``item``: its own level, its lines or its conversions."""
    if item is None:
        return NO_KINDS
    if isinstance(item, BillModel):
        header = _level_kinds(domain, item.header.values())
        lines = _level_kinds(domain, item.lines.values())
        return ResultKinds(header.kinds, FrozenMap({**header.children, LINES_PARAM: lines}))
    level = _level_kinds(domain, item.fields.values())
    conversion = item.unit_conversion
    if conversion is None:
        return level
    records = _conversion_kinds(domain, conversion)
    return ResultKinds(level.kinds, FrozenMap({**level.children, UNIT_CONVERSIONS: records}))


def _level_kinds(domain: DomainModel, fields: Iterable[FieldModel]) -> ResultKinds:
    """The kinds of the decimal fields of one level, by the result key of each (13.3, 13.4).

    A ref field contributes the kinds of its summary instead, which an entity that declares
    its ``code`` or its ``name`` as a decimal field has.
    """
    kinds: dict[str, DecimalKind] = {}
    children: dict[str, ResultKinds] = {}
    for field in fields:
        key = result_key(field)
        if field.nested:
            summary = _ref_kinds(domain, field.ref)
            if summary.kinds:
                children[key] = summary
        elif (kind := _decimal_kind(field.type)) is not None:
            kinds[key] = kind
    return ResultKinds(FrozenMap(kinds), FrozenMap(children))


def _ref_kinds(domain: DomainModel, target: str | None) -> ResultKinds:
    """The kinds of the summary of a reference to ``target`` (requirement 13.3).

    The summary of a bill holds its id and its number, so only a reference to an entity can
    carry a decimal value at all, and only where the entity declares its ``code`` or its
    ``name`` as a decimal field.
    """
    item = referenced(domain, target)
    if not isinstance(item, EntityModel):
        return NO_KINDS
    kinds: dict[str, DecimalKind] = {}
    for key in summary_keys(item):
        field = item.fields.get(key)
        if field is not None and (kind := _decimal_kind(field.type)) is not None:
            kinds[key] = kind
    return ResultKinds(FrozenMap(kinds))


def _conversion_kinds(domain: DomainModel, conversion: UnitConversionModel) -> ResultKinds:
    """The kinds of the conversion records a material carries (requirement 6.9).

    Each record holds the conversion unit and the factor, under the names the pack gave the
    two fields of its conversion entity.
    """
    entity = domain.entities.get(conversion.entity)
    declared: Mapping[str, FieldModel] = FrozenMap() if entity is None else entity.fields
    names = (conversion.unit_field, conversion.factor_field)
    found = [field for name in names if (field := declared.get(name)) is not None]
    return _level_kinds(domain, found)


def _query_kinds(domain: DomainModel, query: QueryModel | None) -> ResultKinds:
    """The kinds of the result of a query tool (requirements 5.13, 9.7).

    A declarative query returns the same fields under ``latest`` and under ``history``; the
    stock balance query of the inventory primitive returns the quantity on hand of each
    pair, which is a qty in the base unit of the material.
    """
    if query is None:
        return NO_KINDS
    if query.primitive is not None:
        balance = ResultKinds(FrozenMap({QTY_BASE: _BALANCE_KIND}))
        return ResultKinds(children=FrozenMap({ITEMS: balance}))
    level = _level_kinds(domain, _query_fields(domain, query))
    return ResultKinds(children=FrozenMap({LATEST: level, HISTORY: level}))


def _query_fields(domain: DomainModel, query: QueryModel) -> list[FieldModel]:
    """The fields a declarative query returns, each one once (requirement 5.13).

    The fields of ``returns.latest`` and of ``returns.history``, each read from the level of
    the source it names, so a query over the lines of a bill reads fields of both levels; a
    field of both levels with one name is returned once, as the outputSchema declares it.
    """
    source = query.source
    if source is None:
        return []
    found: dict[str, FieldModel] = {}
    for ref in (*query.latest, *query.history):
        field = domain.fields_of(source, ref.level).get(ref.name)
        if field is not None:
            found.setdefault(result_key(field), field)
    return list(found.values())


def _decimal_kind(field_type: FieldType) -> DecimalKind | None:
    """The decimal kind of ``field_type``, None for a type that is no decimal (req 13.4)."""
    match field_type:
        case "qty" | "price" | "money" | "rate":
            return field_type
        case _:
            return None


# The kind of the quantity on hand the stock balance query returns: a qty in the base unit
# of the material, the type of the engine's own column (design.md data model 3).
_BALANCE_KIND: Final[DecimalKind] = "qty"
