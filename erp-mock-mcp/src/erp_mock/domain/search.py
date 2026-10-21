"""The search primitive: which records match, in which order, and which page of them.

What a search of an entity or a bill does, in the slots of the pipeline design.md 4.1
assigns it. The slot functions of :mod:`erp_mock.domain.engine` call the functions of this
module, so the order the errors of one call are reported in stays with the pipeline:

=====================  ==================================================================
slot                   what this module contributes
=====================  ==================================================================
``references``         :func:`check_search_references`: every reference parameter of the
                       search names a record that is there (requirement 5.6)
``view``               :func:`run_search`: the records that match, sorted, the page of
                       them and their summary fields (requirements 5.9 to 5.11, 5.14)
=====================  ==================================================================

A search writes nothing, so every other slot passes it by.

What matches (requirement 5.9). A record matches when it satisfies every condition the
caller gave, and a condition that was not given does not filter:

- ``keyword`` matches when at least one keyword field of the object contains it, ASCII
  letters compared without case and every other character as written. An absent or empty
  keyword does not filter.
- a filter that is no date compares the field with the value for equality.
- a date filter is the two parameters requirement 11.5 generates, ``date_from`` and
  ``date_to`` for a bill's ``biz_date`` and ``F_from`` and ``F_to`` for every other date
  field F (:func:`date_range_params`); both ends are included and one end may be given
  alone.

The three run in SQL (design.md 4.2): equality, the two ends of a date range, and
``LIKE ... ESCAPE '\\'`` over the keyword fields (:func:`keyword_pattern`), which is where
the case rule of requirement 5.9 comes from - SQLite's LIKE ignores case for ASCII letters
and for nothing else. The sort and the page are decided afterwards in Python, on typed
values.

The order (requirement 5.14). The keys of ``default_sort``, each ascending unless the pack
wrote ``desc``, and the internal id ascending as the last key, so two records with equal
keys always come back in the same order. A value is compared within its own type: numbers
by magnitude, dates chronologically, strings by code point, which is what the typed values
of the storage layer compare as (:func:`order_value`). An empty value comes after every
value of an ascending key, and a descending key reverses that with everything else.

The page (requirement 5.11). ``page`` is 1 and ``page_size`` is 20 where the caller gave
neither; ``total`` counts every record that matched, ``items`` is the slice that belongs to
the page, and ``has_more`` says whether records follow it
(:attr:`~erp_mock.domain.views.PageView.has_more`). A page past the last one is an empty
``items`` and no error, as is a search that matched nothing. The tools layer rejects a
``page`` or ``page_size`` below 1 before the engine sees it (requirement 5.15), so a
non-positive one here is a programming error and raises.

The summary fields (requirement 5.10). A search returns no lines and only these fields, in
this order:

- an entity: ``id``, ``code`` and ``name`` as far as it declares them, ``status`` when it
  keeps one, then the fields of its search - the keyword fields, then the filters;
- a bill: ``id``, ``number``, ``status``, ``biz_date``, the partner reference it declares,
  the fields of its filters and, for a bill with pricing, ``amount_with_tax``.

No field appears twice and a name the object has no field for is left out
(:func:`summary_fields`). This is the same rule, in the same order, as the ``items`` of the
outputSchema of the search tool (``_record_summary`` and ``_bill_summary`` of
:mod:`erp_mock.tools.output_schema`); the two are separate because domain must not import
tools (requirement 24.8), and the result assembly reads the view in the order of that
schema (design.md 5.4). The parameter names this module reads are those of the same tool's
inputSchema (``KEYWORD_PARAM``, ``PAGE_PARAM``, ``PAGE_SIZE_PARAM`` and ``DATE_RANGE`` of
:mod:`erp_mock.tools.input_schema`), repeated here for the same reason.

:class:`SortKey` and :func:`sorted_records` are the ordering of requirement 5.14 itself,
which :mod:`erp_mock.domain.query` sorts a declarative query's records with as well
(requirement 5.13).
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import (
    BillModel,
    DomainModel,
    EntityModel,
    FieldModel,
    SearchModel,
    SortRef,
)
from erp_mock.domain.crud import Reference, field_path, missing_references, not_found, own_fields
from erp_mock.domain.views import PageView, field_view
from erp_mock.state.schema import ID_COLUMN
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "DATE_RANGE",
    "DEFAULT_PAGE",
    "DEFAULT_PAGE_SIZE",
    "KEYWORD_PARAM",
    "LIKE_ESCAPE",
    "LIKE_WILDCARDS",
    "PAGE_PARAM",
    "PAGE_SIZE_PARAM",
    "SearchQuery",
    "SortKey",
    "check_search_references",
    "date_range_params",
    "keyword_pattern",
    "level_sort_keys",
    "matching_records",
    "order_value",
    "page_number",
    "run_search",
    "search_parameters",
    "search_query",
    "search_references",
    "sorted_records",
    "summary_fields",
]

# The parameters of a search tool the engine names itself (requirement 11.4); the names of
# the inputSchema of that tool (see the module docstring).
KEYWORD_PARAM: Final = "keyword"
PAGE_PARAM: Final = "page"
PAGE_SIZE_PARAM: Final = "page_size"

# The two ends a date filter becomes, in the order they are declared (requirement 11.5).
DATE_RANGE: Final[tuple[str, str]] = ("_from", "_to")

# The page a search returns when the caller names none (requirement 5.11).
DEFAULT_PAGE: Final = 1
DEFAULT_PAGE_SIZE: Final = 20

# The escape character of the keyword pattern and the characters it escapes: the two
# wildcards of SQL LIKE and the escape character itself, so a keyword matches as written
# (requirement 5.9). The escape is the one every LIKE of the storage layer uses
# (``_LIKE_ESCAPE`` of :mod:`erp_mock.state.store`).
LIKE_ESCAPE: Final = "\\"
LIKE_WILDCARDS: Final[tuple[str, ...]] = ("%", "_")

# The standard fields a summary returns next to the declared ones (meta-model.md 4): the
# code and the name of a piece of basic data, the number, the state, the business date and
# the total of a bill. ``status`` belongs to both levels.
_CODE: Final = "code"
_NAME: Final = "name"
_NUMBER: Final = "number"
_STATUS: Final = "status"
_BIZ_DATE: Final = "biz_date"
_AMOUNT_WITH_TAX: Final = "amount_with_tax"

# The name whose date filter becomes ``date_from`` and ``date_to`` rather than
# ``biz_date_from`` and ``biz_date_to`` (requirement 11.5).
_DATE: Final = "date"


@dataclass(frozen=True, slots=True)
class SortKey:
    """One sort key of a read: how to read its value from a record and in which direction.

    ``value`` reads the value the records are compared by, which for a search is a field of
    the record itself and for a declarative query may be a field of the bill a line belongs
    to (requirement 5.13); ``descending`` is what ``desc`` declared (requirement 5.14).
    """

    value: Callable[[Record], Value]
    descending: bool = False


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """What the arguments of one search amount to (requirements 5.9 and 5.11).

    ``where`` are the equality filters, ``at_least`` and ``at_most`` the two ends of each
    date filter and ``like`` the keyword pattern per keyword field, which the storage layer
    takes as they are (:meth:`~erp_mock.state.store.UnitOfWork.select`); ``page`` and
    ``page_size`` are the page to return, the defaults of requirement 5.11 where the caller
    named none.
    """

    where: FrozenMap[str, Value] = FrozenMap()
    like: FrozenMap[str, str] = FrozenMap()
    at_least: FrozenMap[str, date] = FrozenMap()
    at_most: FrozenMap[str, date] = FrozenMap()
    page: int = DEFAULT_PAGE
    page_size: int = DEFAULT_PAGE_SIZE


# --- the parameters of one search tool (requirements 11.4 and 11.5) ----------------------


def date_range_params(name: str) -> tuple[str, str]:
    """The two parameters the date filter ``name`` becomes (requirement 11.5).

    ``date_from`` and ``date_to`` for the ``biz_date`` of a bill, ``F_from`` and ``F_to``
    for every other date field F.
    """
    stem = _DATE if name == _BIZ_DATE else name
    start, end = DATE_RANGE
    return (f"{stem}{start}", f"{stem}{end}")


def search_parameters(item: EntityModel | BillModel) -> tuple[str, ...]:
    """The parameters the search tool of ``item`` takes, in order (requirement 11.4).

    ``keyword`` where the object declares keyword fields, one parameter per filter - two
    for a date filter - and ``page`` and ``page_size``, which every search has. A filter
    that names no field of the level contributes none, as it contributes no condition.
    """
    fields = own_fields(item)
    search = item.search
    names: list[str] = []
    if search is not None and search.keyword_fields:
        names.append(KEYWORD_PARAM)
    for field in _filter_fields(search, fields):
        names.extend(date_range_params(field.name) if field.type == "date" else (field.name,))
    names += [PAGE_PARAM, PAGE_SIZE_PARAM]
    return tuple(names)


# --- the references of a search (requirement 5.6) ----------------------------------------


def search_references(
    item: EntityModel | BillModel, args: Mapping[str, Value]
) -> tuple[Reference, ...]:
    """The references the caller filters by, in parameter order (requirement 5.6).

    One per ref filter the caller gave a value for; the path is the parameter's own name,
    which is the name of the field it filters by (requirement 11.4). A filter the caller
    left out or sent empty names no record and brings no reference.
    """
    fields = own_fields(item)
    found: list[Reference] = []
    for field in _filter_fields(item.search, fields):
        value = args.get(field.name)
        if field.type == "ref" and isinstance(value, str):
            found.append(Reference(path=field_path(field.name), target=field.ref, id=value))
    return tuple(found)


def check_search_references(
    uow: UnitOfWork, domain: DomainModel, item: EntityModel | BillModel, args: Mapping[str, Value]
) -> None:
    """Step 1 of a search: every reference parameter names a record (requirement 5.6).

    Raises one NOT_FOUND for all of them at once, its details carrying the field path of
    each (:func:`~erp_mock.domain.crud.not_found`).
    """
    missing = missing_references(uow, domain, search_references(item, args))
    if missing:
        raise not_found(domain, missing)


# --- the conditions of one search (requirement 5.9) --------------------------------------


def keyword_pattern(keyword: str) -> str:
    """The SQL pattern that matches a value containing ``keyword`` (requirement 5.9).

    The keyword is taken as written: the two wildcards of LIKE and the escape character
    itself are escaped with a backslash, which is the ``ESCAPE`` of every LIKE the storage
    layer emits, and the pattern is wrapped in ``%`` on both sides.
    """
    escaped = keyword
    for character in (LIKE_ESCAPE, *LIKE_WILDCARDS):
        escaped = escaped.replace(character, f"{LIKE_ESCAPE}{character}")
    return f"%{escaped}%"


def search_query(item: EntityModel | BillModel, args: Mapping[str, Value]) -> SearchQuery:
    """The conditions and the page of the search ``args`` ask for (requirements 5.9, 5.11).

    Every condition the caller gave takes part and every one it left out does not: a filter
    that is absent or null, an end of a date range that is absent, and a keyword that is
    absent or empty do not filter. ``page`` and ``page_size`` fall back to the defaults of
    requirement 5.11.

    Raises KeyError for an argument that names no parameter of this search tool and
    ValueError for a page or a page size below 1, neither of which a generated tool can
    send (requirements 11.4 and 5.15).
    """
    unknown = sorted(set(args) - set(search_parameters(item)))
    if unknown:
        raise KeyError(f"the search of {item.name} has no parameter {', '.join(unknown)}")
    fields = own_fields(item)
    where: dict[str, Value] = {}
    at_least: dict[str, date] = {}
    at_most: dict[str, date] = {}
    for field in _filter_fields(item.search, fields):
        if field.type != "date":
            value = args.get(field.name)
            if value is not None:
                where[field.name] = value
            continue
        start, end = date_range_params(field.name)
        for param, bounds in ((start, at_least), (end, at_most)):
            bound = args.get(param)
            if isinstance(bound, date):
                bounds[field.name] = bound
    return SearchQuery(
        where=FrozenMap(where),
        like=FrozenMap(_keyword_patterns(item.search, fields, args.get(KEYWORD_PARAM))),
        at_least=FrozenMap(at_least),
        at_most=FrozenMap(at_most),
        page=page_number(args, PAGE_PARAM, DEFAULT_PAGE),
        page_size=page_number(args, PAGE_SIZE_PARAM, DEFAULT_PAGE_SIZE),
    )


def matching_records(
    uow: UnitOfWork, item: EntityModel | BillModel, query: SearchQuery
) -> list[Record]:
    """The records of ``item`` that satisfy every condition of ``query`` (req 5.9).

    Read in SQL, where equality, the ends of a date range and the keyword pattern are all
    applied (design.md 4.2); a bill is read from its header table, so no line takes part in
    a search.
    """
    return uow.select(
        item.name,
        where=query.where,
        like=query.like,
        at_least=query.at_least,
        at_most=query.at_most,
    )


# --- the order of the records (requirement 5.14) -----------------------------------------


def order_value(value: Value) -> tuple[int, Value]:
    """The sort key of one value: its own type decides, an empty value last (req 5.14).

    The typed values of one column are of one type, so comparing them is comparing numbers
    by magnitude, dates chronologically and strings by code point (design.md data model 3).
    An empty value is ranked after every value of an ascending key, which a descending key
    reverses with everything else; two keys of different rank are decided by the rank and
    two empty ones are equal, so a value is only ever compared with one of its own type.
    """
    return (1, None) if value is None else (0, value)


def level_sort_keys(
    fields: Mapping[str, FieldModel], sort: Sequence[SortRef]
) -> tuple[SortKey, ...]:
    """The sort keys of ``sort`` read from the record itself (requirement 5.14).

    The keys of the ``default_sort`` of a search, and of the sort of a declarative query
    whose fields are those of the records it reads. A key that names no field of the level
    is left out, as a field loading step 5 could not resolve.
    """
    return tuple(
        SortKey(value=_field_reader(ref.field.name), descending=ref.descending)
        for ref in sort
        if ref.field.name in fields
    )


def sorted_records(records: Sequence[Record], keys: Sequence[SortKey] = ()) -> list[Record]:
    """``records`` by ``keys``, the internal id ascending as the last key (req 5.14, 5.13).

    The keys are applied from the least significant to the most significant one and each
    sort is stable, so a record whose keys are all equal to another's keeps the order the
    id gave it.
    """
    ordered = sorted(records, key=_id_of)
    for key in reversed(keys):
        ordered = sorted(ordered, key=_order_reader(key), reverse=key.descending)
    return ordered


# --- the page of a read (requirement 5.11) -----------------------------------------------


def page_number(args: Mapping[str, Value], param: str, default: int) -> int:
    """The value of ``param``, the default where the caller named none (requirement 5.11).

    The page and the page size of a read: of a search, and of the stock balance query of
    the inventory primitive, which is paged the same way (requirement 9.12).

    Raises TypeError for a value that is no integer and ValueError for one below 1, which
    the tools layer rejects as VALIDATION_FAILED before the engine sees it
    (requirement 5.15).
    """
    value = args.get(param)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{param} is an integer parameter, got {type(value).__name__}: {value!r}")
    if value < 1:
        raise ValueError(f"{param} starts at 1, got {value}")
    return value


# --- the summary fields of one record (requirement 5.10) ---------------------------------


def summary_fields(item: EntityModel | BillModel) -> tuple[FieldModel, ...]:
    """The fields a search returns of one record of ``item``, in order (requirement 5.10).

    The names of the module docstring, each of them once and each of them a field of the
    level: a name the object has no field for - a ``code`` an entity does not declare, a
    search field loading step 5 could not resolve - is left out.
    """
    fields = own_fields(item)
    if isinstance(item, BillModel):
        names = [ID_COLUMN, _NUMBER, _STATUS, _BIZ_DATE]
        if item.partner_field is not None:
            names.append(item.partner_field)
        names += [] if item.search is None else [ref.name for ref in item.search.filters]
        if item.pricing:
            names.append(_AMOUNT_WITH_TAX)
    else:
        names = [ID_COLUMN, _CODE, _NAME]
        if item.has_status:
            names.append(_STATUS)
        names += _search_names(item.search)
    return tuple(fields[name] for name in dict.fromkeys(names) if name in fields)


# --- the search itself -------------------------------------------------------------------


def run_search(
    uow: UnitOfWork, domain: DomainModel, item: EntityModel | BillModel, args: Mapping[str, Value]
) -> PageView:
    """The page of records the search ``args`` ask for (requirements 5.9 to 5.11, 5.14).

    The records that match are read, sorted and counted, and the page is cut out of them;
    every record of the page comes back with its summary fields alone
    (:func:`summary_fields`). A page past the last one and a search that matched nothing
    both return an empty ``items`` (requirement 5.11).
    """
    query = search_query(item, args)
    matching = matching_records(uow, item, query)
    ordered = sorted_records(matching, level_sort_keys(own_fields(item), _default_sort(item)))
    start = (query.page - 1) * query.page_size
    fields = summary_fields(item)
    items = tuple(
        field_view(uow, domain, fields, record)
        for record in ordered[start : start + query.page_size]
    )
    return PageView(items=items, total=len(ordered), page=query.page, page_size=query.page_size)


# --- internals ---------------------------------------------------------------------------


def _default_sort(item: EntityModel | BillModel) -> tuple[SortRef, ...]:
    """The ``default_sort`` of the search of ``item``, empty where it declares none (5.14)."""
    return () if item.search is None else item.search.default_sort


def _filter_fields(
    search: SearchModel | None, fields: Mapping[str, FieldModel]
) -> Iterator[FieldModel]:
    """The fields the filters of ``search`` compare, in declaration order (req 5.9).

    A filter that names no field of the level is left out, which is a field loading step 5
    could not resolve.
    """
    for ref in () if search is None else search.filters:
        field = fields.get(ref.name)
        if field is not None:
            yield field


def _keyword_patterns(
    search: SearchModel | None, fields: Mapping[str, FieldModel], keyword: Value
) -> Iterator[tuple[str, str]]:
    """One LIKE pattern per keyword field, none where the keyword does not filter (5.9).

    An absent keyword and the empty string leave the condition out, so a search without a
    keyword matches every record.
    """
    if search is None or not isinstance(keyword, str) or not keyword:
        return
    pattern = keyword_pattern(keyword)
    for ref in search.keyword_fields:
        if ref.name in fields:
            yield (ref.name, pattern)


def _search_names(search: SearchModel | None) -> list[str]:
    """The fields a search reads: its keyword fields, then its filters (requirement 5.10)."""
    if search is None:
        return []
    return [ref.name for ref in (*search.keyword_fields, *search.filters)]


def _field_reader(name: str) -> Callable[[Record], Value]:
    """A reader of the field ``name`` of the record itself (requirement 5.14)."""
    return lambda record: record.get(name)


def _order_reader(key: SortKey) -> Callable[[Record], tuple[int, Value]]:
    """The comparison key of one sort key, an empty value ranked last (requirement 5.14)."""
    return lambda record: order_value(key.value(record))


def _id_of(record: Record) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""
