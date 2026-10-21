"""Property 15: the reference model of the three reads.

**Validates: Requirements 5.9, 5.11, 5.13, 5.14, 9.7, 9.12, 10.10**

For any table content and any arguments of a search, of a declarative query and of the stock
balance query, the result is the one an in-memory reference model gives: a keyword matches a
value that contains it with ASCII letters compared without case, a filter compares for
equality, a date range includes both of its ends, and a declarative filter selects a record
only where it is true; the records are ordered by the declared keys with the id as the last
one; ``total``, ``has_more`` and the slice of the page agree, and a page past the last one is
empty.

The reference model is the three steps of a read written as pure Python over lists of
records - a predicate per record, a comparison per pair of records, and a slice - and it
shares nothing with :mod:`erp_mock.domain.search`, :mod:`erp_mock.domain.query` or the
balance query of :mod:`erp_mock.domain.inventory`:

- what matches (requirement 5.9) is :func:`search_matches`: the keyword is folded to lower
  case over the ASCII letters alone and compared with ``in``, where the engine compares in
  SQL with ``LIKE ... ESCAPE '\\'``; a filter is ``==`` on the typed value; a date range is
  ``low <= value <= high``, either end on its own, and a record whose date is empty is in no
  range, as a comparison with NULL satisfies no SQL condition;
- the order (requirement 5.14) is :func:`row_order`, an explicit three-way comparison of one
  pair of records: the keys in turn, each value against the one of its own type, an empty
  value after every value of an ascending key, and the id of the record as the last key. The
  engine sorts instead - by the id and then by each key from the least significant one,
  every sort stable - so the two reach the same order by different means. Requirement 5.14
  does not say where an empty value goes; the engine's documented choice is what the model
  follows (:func:`~erp_mock.domain.search.order_value`);
- the page (requirement 5.11) is :func:`page_items` and :func:`more_follows`: the slice from
  ``(page - 1) × page_size``, which is empty for a page past the last one, and
  ``page × page_size < total``.

What the three reads add to that, each covered by one property below:

- a search (requirements 5.9 to 5.11 and 5.14) carries the conditions, the order and the
  page of the three bullets, and returns the summary fields of requirement 5.10, which
  :func:`summary_fields` writes out as its own rule: of an entity its id, code and name, its
  status where it keeps one, then its keyword fields and its filters; of a bill its id,
  number, status, business date, its partner, its filters and, with pricing, the total - each
  name once, each a field of the level, and a ref field under its name without ``_id``. Note
  what the two do not share: a bill's keyword fields are no summary fields of it, an entity's
  are;
- the stock balance query (requirements 9.7 and 9.12) filters by the material and by the
  warehouse, each optional and compared for equality, and orders by material id and then
  warehouse id with the id of the balance record as the last key;
- a declarative query (requirements 5.13 and 10.10) selects the records every parameter's
  ``maps_to`` field equals and the filter is true for, orders them by ``sort`` with the id
  of the source record as the last key, and returns the fields of ``returns.latest`` of the
  first record and those of ``returns.history.fields`` of the first ``limit`` of them.

The filter is the one part of a read the model cannot compute from the record alone: a
reference model of the expression language is property 9's. Each filter of :data:`FILTERS`
is therefore a text and a Python predicate written side by side, the predicate reading the
same record, bill, table and anchor date the text names (requirement 10.10), and only true
selects - an empty value makes a comparison unknown, which the predicate answers False for,
as the three-valued logic of requirement 10.6 does.

What is drawn, per example. The search declaration itself: which fields of the object are
keyword fields, which are filters and which are sort keys, and in which direction - so the
order is compared over strings, dates, integers, rates and amounts, ascending and
descending, where the declarations of the fixture only sort by a code and by a date. Then
the content of the tables, up to 6 records per table - an empty one now and then - whose
every field comes from a small pool, and the arguments of the call. The pools are small on
purpose: two records with equal sort keys, where the id decides, and a filter value no record
holds are what they are for. They hold an empty value for every field that may be empty,
ASCII case variants of one another, the two wildcards of SQL LIKE and the escape character,
and CJK text, which no case folding touches.

The content is written straight into a real sandbox over the tables of the engine test
fixture and read back through a plain select, so the model works on the values the storage
layer holds rather than on the drawn ones. Nothing is created through the engine: a create
would default and reject much of what is drawn, and what matters here is the read and not
how a record got there. The database is built once for the module and every example undoes
its own inserts before the transaction ends, so the examples do not see each other.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from functools import cmp_to_key
from typing import Any, Final, cast

import pytest
from hypothesis import example, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import (
    BillModel,
    DomainModel,
    EntityModel,
    FieldLevel,
    FieldModel,
    FieldRef,
    QueryModel,
    QueryParamModel,
    SearchModel,
    SortRef,
)
from erp_mock.domain.inventory import run_balance_query
from erp_mock.domain.query import query_filter, resolve_source, run_query, selected_records
from erp_mock.domain.search import run_search
from erp_mock.domain.views import RecordView, RefSummary, ViewValue
from erp_mock.state.schema import (
    BILL_ID_COLUMN,
    ID_COLUMN,
    MATERIAL_COLUMN,
    QTY_BASE_COLUMN,
    STOCK_BALANCE_TABLE,
    WAREHOUSE_COLUMN,
)
from erp_mock.state.store import Record, SandboxStore, UnitOfWork, Value
from tests.helpers.engine_env import empty_store, fixture_model
from tests.helpers.fixtures import ANCHOR_DATE

# One record of a table as the reference model reads it: the typed values of its columns, a
# column it holds no value for absent or None.
type Row = Mapping[str, Value]

# The record a path with nothing to read reads, which keeps every one of its values empty: a
# ``line.`` path of a query over bill headers, and the header of a line whose bill is not
# there (requirement 10.10).
NO_ROW: Final[Row] = FrozenMap()

# The objects of the engine test fixture the reads work on: the partner entity, which keeps a
# status and declares a code, and the pricing bill, which declares a partner and a date field
# of its own next to the standard business date (tests/fixtures/packs).
VENDOR: Final = "vendor"
ORDER: Final = "supply_order"
ORDER_LINE: Final = "supply_order_line"

# The query tool the inventory primitive provides (requirement 9.7).
BALANCE_QUERY: Final = "stock_level_query"

# The parameters a read names itself (requirements 11.4 and 11.5), repeated here so the
# arguments of an example are built without the engine's own names.
KEYWORD_PARAM: Final = "keyword"
PAGE_PARAM: Final = "page"
PAGE_SIZE_PARAM: Final = "page_size"
FROM_SUFFIX: Final = "_from"
TO_SUFFIX: Final = "_to"

# The page a read returns where the caller names none (requirements 5.11 and 9.12).
FIRST_PAGE: Final = 1
DEFAULT_SIZE: Final = 20

# The standard fields the reads name by themselves (meta-model.md section 4).
CODE: Final = "code"
NAME: Final = "name"
NUMBER: Final = "number"
STATUS: Final = "status"
BIZ_DATE: Final = "biz_date"
AMOUNT: Final = "amount"
AMOUNT_WITH_TAX: Final = "amount_with_tax"

# The stem the date filter of a bill's business date becomes (requirement 11.5).
DATE_STEM: Final = "date"

# The suffix a ref field's result key drops (requirement 13.3).
ID_SUFFIX: Final = "_id"

# A to Z folded to a to z and nothing else: the case rule of requirement 5.9, which is
# SQLite's LIKE over an ASCII letter and over no other character.
ASCII_FOLD: Final = str.maketrans({chr(point): chr(point + 32) for point in range(0x41, 0x5B)})

# How many records of one table an example holds, and the page arguments it may carry: a page
# size below the number of records, so a page past the last one comes up, and None for the
# defaults of requirement 5.11.
MAX_ROWS: Final = 6
PAGES: Final[tuple[int | None, ...]] = (None, 1, 2, 4)
PAGE_SIZES: Final[tuple[int | None, ...]] = (None, 1, 2, 5)

# How often a condition of a read is left out altogether, which keeps it from filtering
# (requirement 5.9): one draw in two, so a read over several conditions at once and a read
# over none both come up.
ABSENT: Final[tuple[bool, ...]] = (True, False)


# --- the reference model: what matches, in which order, and which page of it --------------


def folded(text: str) -> str:
    """``text`` with its ASCII letters lowered and every other character as written (5.9)."""
    return text.translate(ASCII_FOLD)


def contains(value: Value, keyword: str) -> bool:
    """Whether ``value`` contains ``keyword``, ASCII letters without case (req 5.9).

    An empty value contains nothing, as a LIKE over NULL satisfies no SQL condition.
    """
    return isinstance(value, str) and folded(keyword) in folded(value)


def on_or_after(value: Value, day: date) -> bool:
    """Whether ``value`` is a date on or after ``day``, the lower end of a range (11.5)."""
    return isinstance(value, date) and value >= day


def on_or_before(value: Value, day: date) -> bool:
    """Whether ``value`` is a date on or before ``day``, the upper end of a range (11.5)."""
    return isinstance(value, date) and value <= day


def greater(value: Value, limit: Decimal) -> bool:
    """Whether ``value`` is a number above ``limit``; an empty value is not (req 10.6)."""
    return isinstance(value, Decimal) and value > limit


def row_id(row: Row) -> str:
    """The id of ``row``; every record table keeps a text id that is NOT NULL."""
    value = row.get(ID_COLUMN)
    assert isinstance(value, str), f"a record without an id: {dict(row)}"
    return value


@dataclass(frozen=True, slots=True)
class Key:
    """One sort key of the reference model: how to read its value and in which direction."""

    read: Callable[[Row], Value]
    descending: bool = False


def value_order(left: Value, right: Value) -> int:
    """-1, 0 or 1 for two values of one column, an empty value last (requirement 5.14).

    The values of one column are of one type, so numbers compare by magnitude, dates
    chronologically and strings by code point. Requirement 5.14 does not say where an empty
    value goes; the engine ranks it after every value of an ascending key, which a descending
    key reverses with everything else.
    """
    if left is None or right is None:
        return 0 if left is None and right is None else (1 if left is None else -1)
    if left == right:
        return 0
    return -1 if cast(Any, left) < cast(Any, right) else 1


def row_order(left: Row, right: Row, keys: Sequence[Key]) -> int:
    """-1, 0 or 1 for two records by ``keys``, the id as the last key (requirement 5.14)."""
    for key in keys:
        order = value_order(key.read(left), key.read(right))
        if order:
            return -order if key.descending else order
    return value_order(row_id(left), row_id(right))


def ordered(rows: Sequence[Row], keys: Sequence[Key] = ()) -> list[Row]:
    """``rows`` in the order ``keys`` and the id put them in (requirements 5.14 and 5.13)."""

    def compare(left: Row, right: Row) -> int:
        return row_order(left, right, keys)

    return sorted(rows, key=cmp_to_key(compare))


def page_items(rows: Sequence[Row], page: int, page_size: int) -> list[Row]:
    """The records of the page ``page``, empty for a page past the last one (req 5.11)."""
    start = (page - 1) * page_size
    return list(rows[start : start + page_size])


def more_follows(total: int, page: int, page_size: int) -> bool:
    """Whether records follow the page ``page`` (requirement 5.11)."""
    return page * page_size < total


def asked_page(args: Mapping[str, Value]) -> tuple[int, int]:
    """The page and the page size ``args`` ask for, the defaults of requirement 5.11."""
    number, size = args.get(PAGE_PARAM), args.get(PAGE_SIZE_PARAM)
    return (
        number if isinstance(number, int) else FIRST_PAGE,
        size if isinstance(size, int) else DEFAULT_SIZE,
    )


def result_key(declared: FieldModel) -> str:
    """The key ``declared`` takes in a result: a ref field without its ``_id`` (req 13.3)."""
    name = declared.name
    return name.removesuffix(ID_SUFFIX) if declared.type == "ref" else name


def scalar(value: ViewValue) -> Value:
    """One value of a result object, a reference as the id it holds (requirement 13.3).

    A read returns a ref field as the nested summary of the record it points at; what the
    reference model holds is the id itself, which the summary carries whether the record it
    names is there or not.
    """
    if isinstance(value, RefSummary):
        return value.fields[ID_COLUMN]
    assert not isinstance(value, tuple), f"a scalar field returned an array: {value!r}"
    return value


def view_values(view: RecordView) -> dict[str, Value]:
    """One result object as its result keys and their scalar values (requirement 13.1)."""
    return {key: scalar(value) for key, value in view.fields.items()}


# --- the sandbox the reads work on -------------------------------------------------------


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SandboxStore]:
    """An open, empty sandbox over the tables of the fixture, one for the whole module."""
    with closing(empty_store(tmp_path_factory.mktemp("p15") / "sandbox")) as sandbox:
        yield sandbox


@contextmanager
def content_in(
    store: SandboxStore, content: Mapping[str, Sequence[Row]]
) -> Iterator[tuple[UnitOfWork, dict[str, tuple[Record, ...]]]]:
    """One transaction holding ``content``, which leaves the sandbox empty again.

    The records are inserted, the block reads them through the unit of work together with the
    content of each table as storage holds it, and the inserts are undone before the
    transaction ends: by the deletes when the block returns and by the rollback of the
    transaction when it raises. The next example therefore starts from the same empty
    database and reuses the same ids.
    """
    with store.transaction() as uow:
        written = [(table, row_id(row)) for table, rows in content.items() for row in rows]
        for table, rows in content.items():
            for row in rows:
                uow.insert(table, row)
        try:
            yield uow, {table: tuple(uow.select(table)) for table in content}
        finally:
            for table, record_id in written:
                uow.delete(table, record_id)


def item_of(domain: DomainModel, target: str) -> EntityModel | BillModel:
    """The entity or the bill ``target`` of the fixture's configuration."""
    bill = domain.bills.get(target)
    return bill if bill is not None else domain.entities[target]


def own_fields(item: EntityModel | BillModel) -> Mapping[str, FieldModel]:
    """The field view of the records a search reads: a bill's header or an entity record."""
    return item.header if isinstance(item, BillModel) else item.fields


# --- what is drawn: the content of one table ---------------------------------------------


@dataclass(frozen=True, slots=True)
class Pool:
    """The values one field of the generated content may hold, an empty value included."""

    name: str
    values: tuple[Value, ...]


def pool(name: str, *values: Value) -> Pool:
    """One field's pool, written out in the order its values are sampled from."""
    return Pool(name=name, values=values)


@st.composite
def row_lists(
    draw: st.DrawFn, prefix: str, pools: Sequence[Pool], count: int, extra: Sequence[Pool] = ()
) -> tuple[Row, ...]:
    """Draw ``count`` records whose ids are ``prefix`` and a number, each field from a pool.

    The ids are handed out in drawing order and compared by code point, so ``p10`` comes
    before ``p2``: a tie on every sort key is decided by the id and not by the order the
    records were written in (requirement 5.14).
    """
    drawn: list[Row] = []
    for index in range(1, count + 1):
        values: dict[str, Value] = {ID_COLUMN: f"{prefix}{index}"}
        for item in (*pools, *extra):
            values[item.name] = draw(st.sampled_from(item.values))
        drawn.append(FrozenMap(values))
    return tuple(drawn)


def counts(limit: int = MAX_ROWS) -> st.SearchStrategy[int]:
    """How many records a table of one example holds, an empty table included.

    Sampled rather than drawn as an integer, with the empty table last and the full one
    drawn twice, so a read over several records is the common case and an empty table the
    exception: what the conditions, the order and the page of a read do only shows on
    records.
    """
    return st.sampled_from((*range(1, limit + 1), limit, limit, 0))


def row_of(prefix: str, index: int, **values: Value) -> Row:
    """One record of a written-out example: its id and the fields it holds."""
    return FrozenMap({ID_COLUMN: f"{prefix}{index}", **values})


# --- property 15a: a search (requirements 5.9 to 5.11 and 5.14) --------------------------


@dataclass(frozen=True, slots=True)
class Searched:
    """One object a search may read: its content pools and what its search may declare.

    ``keywords``, ``filters`` and ``sorts`` are the fields the drawn declaration picks from,
    all of them fields of the level; every one of them is a pool as well, so the value a
    filter compares and the value a key orders by both vary. ``probes`` are the keywords a
    search of this object may carry: text the records of its pools hold, one case variant of
    it, a wildcard of SQL LIKE, text no record holds and the empty string, which does not
    filter (requirement 5.9).
    """

    target: str
    prefix: str
    level: FieldLevel
    pools: tuple[Pool, ...]
    keywords: tuple[str, ...]
    probes: tuple[str, ...]
    filters: tuple[str, ...]
    sorts: tuple[str, ...]

    def pool_of(self, name: str) -> tuple[Value, ...]:
        """The values the field ``name`` of the content may hold."""
        return next(item.values for item in self.pools if item.name == name)


# The partner entity: three string fields a keyword may read, a status, an integer and a
# rate, and a code that is empty for some records. The values repeat across records and
# differ by ASCII case alone, and two of them carry a wildcard of SQL LIKE.
VENDOR_SEARCH: Final = Searched(
    target=VENDOR,
    prefix="v",
    level="record",
    pools=(
        pool(CODE, "V0001", "v0001", "V_0002", "V%0003", None),
        pool(NAME, "Main Vendor", "main vendor", "夹具主力供货单位", None),
        pool("short_name", "MV", "主力", None),
        pool("contact", "A\\B", "甲", None),
        pool(STATUS, "enabled", "disabled", None),
        pool("pay_days", 0, 30, 45, None),
        pool("default_tax_rate", Decimal("0"), Decimal("0.13"), Decimal("0.09"), None),
    ),
    keywords=(CODE, NAME, "short_name", "contact"),
    probes=("", "v000", "V0001", "vendor", "主力", "a\\b", "_", "%", "ZZZ"),
    filters=(STATUS, "pay_days", "default_tax_rate"),
    sorts=(CODE, NAME, "short_name", STATUS, "pay_days", "default_tax_rate"),
)

# The pricing bill: a number a keyword may read, a remark, the standard business date and a
# date field of its own - so both namings of requirement 11.5 come up - a partner reference,
# a boolean and the total of a pricing bill.
ORDER_SEARCH: Final = Searched(
    target=ORDER,
    prefix="o",
    level="header",
    pools=(
        pool(NUMBER, "SPO202610090001", "spo202610090001", "SPO_0002", None),
        pool("remark", "备注", "note", "Note", None),
        pool(STATUS, "draft", "submitted", "approved", "closed", None),
        pool(BIZ_DATE, date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10), None),
        pool("need_date", date(2026, 10, 9), date(2026, 11, 1), None),
        pool("vendor_id", "v1", "v2", "v9", None),
        pool("price_includes_tax", True, False, None),
        pool(AMOUNT_WITH_TAX, Decimal("0"), Decimal("22.60"), Decimal("-5.00"), None),
    ),
    keywords=(NUMBER, "remark"),
    probes=("", "spo2026", "SPO202610090001", "0002", "note", "备注", "_", "%", "ZZZ"),
    filters=(STATUS, "vendor_id", BIZ_DATE, "need_date", "price_includes_tax"),
    sorts=(NUMBER, STATUS, BIZ_DATE, "need_date", AMOUNT_WITH_TAX),
)

SEARCHED: Final[tuple[Searched, ...]] = (VENDOR_SEARCH, ORDER_SEARCH)

# The filters of a search that are date fields, which become the two parameters of
# requirement 11.5 rather than one.
DATE_FILTERS: Final[tuple[str, ...]] = (BIZ_DATE, "need_date")


@dataclass(frozen=True, slots=True)
class SearchCase:
    """One example of a search: what the object declares, what it holds and what is asked.

    ``keyword_fields``, ``filters`` and ``sort`` are the drawn search declaration, which
    replaces the one the fixture's pack wrote; ``rows`` is the content of the table and
    ``args`` the arguments of the call, the parameters of requirements 11.4 and 11.5.
    """

    searched: Searched
    keyword_fields: tuple[str, ...]
    filters: tuple[str, ...]
    sort: tuple[tuple[str, bool], ...]
    rows: tuple[Row, ...]
    args: Mapping[str, Value]


def date_params(name: str) -> tuple[str, str]:
    """The two parameters the date filter ``name`` becomes (requirement 11.5).

    ``date_from`` and ``date_to`` for the business date of a bill, ``F_from`` and ``F_to``
    for every other date field F.
    """
    stem = DATE_STEM if name == BIZ_DATE else name
    return (f"{stem}{FROM_SUFFIX}", f"{stem}{TO_SUFFIX}")


def search_matches(case: SearchCase, row: Row) -> bool:
    """Whether ``row`` satisfies every condition the arguments carry (requirement 5.9).

    The keyword over the keyword fields, each filter that was given for equality, and both
    ends of each date range that was given; a keyword that is absent or empty and a filter
    that was not given do not filter.
    """
    keyword = case.args.get(KEYWORD_PARAM)
    if isinstance(keyword, str) and keyword:
        if not any(contains(row.get(name), keyword) for name in case.keyword_fields):
            return False
    for name in case.filters:
        if name in DATE_FILTERS:
            start, end = date_params(name)
            low, high = case.args.get(start), case.args.get(end)
            if isinstance(low, date) and not on_or_after(row.get(name), low):
                return False
            if isinstance(high, date) and not on_or_before(row.get(name), high):
                return False
            continue
        wanted = case.args.get(name)
        if wanted is not None and row.get(name) != wanted:
            return False
    return True


def summary_fields(
    item: EntityModel | BillModel, case: SearchCase, fields: Mapping[str, FieldModel]
) -> tuple[FieldModel, ...]:
    """The fields one record of a search result carries, in order (requirement 5.10).

    Of an entity: its id, its code and its name as far as it declares them, its status where
    it keeps one, then its keyword fields and its filters. Of a bill: its id, number, status
    and business date, its partner where it declares one, its filters and, with pricing, its
    total - its keyword fields are no summary fields of it. Each name once, and each a field
    of the level.
    """
    if isinstance(item, BillModel):
        names = [ID_COLUMN, NUMBER, STATUS, BIZ_DATE]
        if item.partner_field is not None:
            names.append(item.partner_field)
        names += case.filters
        if item.pricing:
            names.append(AMOUNT_WITH_TAX)
    else:
        names = [ID_COLUMN, CODE, NAME]
        if item.has_status:
            names.append(STATUS)
        names += [*case.keyword_fields, *case.filters]
    return tuple(fields[name] for name in dict.fromkeys(names) if name in fields)


def summary_object(summary: Sequence[FieldModel], row: Row) -> dict[str, Value]:
    """One record of a search result: its summary fields under their result keys (5.10)."""
    return {result_key(declared): row.get(declared.name) for declared in summary}


def reader(name: str) -> Callable[[Row], Value]:
    """A reader of the field ``name`` of the record itself (requirement 5.14)."""
    return lambda row: row.get(name)


def search_keys(case: SearchCase) -> tuple[Key, ...]:
    """The sort keys of the drawn declaration, read from the record itself (req 5.14)."""
    return tuple(Key(read=reader(name), descending=descending) for name, descending in case.sort)


def searched_item(domain: DomainModel, case: SearchCase) -> EntityModel | BillModel:
    """The object of ``case`` with the drawn search declaration in place of its own.

    The tables of the sandbox come from the field views, which this leaves alone, so one
    sandbox serves every declaration an example draws (requirements 5.9 to 5.11 and 5.14).
    """
    level = case.searched.level
    search = SearchModel(
        keyword_fields=tuple(FieldRef(level, name) for name in case.keyword_fields),
        filters=tuple(FieldRef(level, name) for name in case.filters),
        default_sort=tuple(
            SortRef(FieldRef(level, name), descending=descending) for name, descending in case.sort
        ),
    )
    return replace(item_of(domain, case.searched.target), search=search)


@st.composite
def search_args(draw: st.DrawFn, case: SearchCase) -> dict[str, Value]:
    """Draw the arguments of one search: a keyword, the filters and the page (11.4, 11.5).

    Only the parameters the drawn declaration gives the tool are drawn, since no generated
    search tool takes another (requirement 11.4): a keyword only where the object declares
    keyword fields, and one parameter per filter - two for a date filter. Every one of them
    may be left out, which leaves its condition out as well, and the page and the page size
    may be left out for the defaults of requirement 5.11.
    """
    args: dict[str, Value] = {}
    if case.keyword_fields and not draw(st.sampled_from(ABSENT)):
        args[KEYWORD_PARAM] = draw(st.sampled_from(case.searched.probes))
    for name in case.filters:
        values = case.searched.pool_of(name)
        if name in DATE_FILTERS:
            for param in date_params(name):
                bound = None if draw(st.sampled_from(ABSENT)) else draw(st.sampled_from(values))
                if bound is not None:
                    args[param] = bound
            continue
        wanted = None if draw(st.sampled_from(ABSENT)) else draw(st.sampled_from(values))
        if wanted is not None:
            args[name] = wanted
    for param, numbers in ((PAGE_PARAM, PAGES), (PAGE_SIZE_PARAM, PAGE_SIZES)):
        number = draw(st.sampled_from(numbers))
        if number is not None:
            args[param] = number
    return args


@st.composite
def search_cases(draw: st.DrawFn) -> SearchCase:
    """Draw one example: an object, its search declaration, its content and the arguments."""
    searched = draw(st.sampled_from(SEARCHED))
    case = SearchCase(
        searched=searched,
        keyword_fields=tuple(
            draw(st.lists(st.sampled_from(searched.keywords), unique=True, max_size=3))
        ),
        filters=tuple(draw(st.lists(st.sampled_from(searched.filters), unique=True, max_size=2))),
        sort=tuple(
            draw(
                st.lists(
                    st.tuples(st.sampled_from(searched.sorts), st.booleans()),
                    unique_by=lambda key: key[0],
                    max_size=2,
                )
            )
        ),
        rows=draw(row_lists(searched.prefix, searched.pools, draw(counts()))),
        args=FrozenMap(),
    )
    return replace(case, args=FrozenMap(draw(search_args(case))))


def search_case(
    searched: Searched,
    *,
    rows: Sequence[Row] = (),
    keyword_fields: Sequence[str] = (),
    filters: Sequence[str] = (),
    sort: Sequence[tuple[str, bool]] = (),
    args: Mapping[str, Value] = FrozenMap(),
) -> SearchCase:
    """One search example written out, its records given as mappings of their fields."""
    return SearchCase(
        searched=searched,
        keyword_fields=tuple(keyword_fields),
        filters=tuple(filters),
        sort=tuple(sort),
        rows=tuple(rows),
        args=FrozenMap(args),
    )


# Feature: erp-mock-mcp-m0, Property 15: 检索与查询的参考模型
@pytest.mark.req("5.9", "5.10", "5.11", "5.14")
@settings(max_examples=100)
@given(case=search_cases())
# The two wildcards of SQL LIKE are taken as written: '%V_0002%' would match V0001 as a
# pattern, and escaped it matches the one code that holds an underscore (requirement 5.9).
@example(
    case=search_case(
        VENDOR_SEARCH,
        rows=[row_of("v", 1, code="V0001"), row_of("v", 2, code="V_0002")],
        keyword_fields=[CODE],
        args={KEYWORD_PARAM: "_"},
    )
)
# A keyword matches any keyword field, ASCII letters without case, and leaves a record whose
# keyword fields are all empty out (requirement 5.9).
@example(
    case=search_case(
        VENDOR_SEARCH,
        rows=[
            row_of("v", 1, code="v0001", name="Main Vendor"),
            row_of("v", 2, code=None, name="main vendor"),
            row_of("v", 3),
        ],
        keyword_fields=[CODE, NAME],
        args={KEYWORD_PARAM: "V0001"},
    )
)
# A date range includes both of its ends, both ends may name one day, and a record without a
# business date is in no range (requirements 5.9 and 11.5).
@example(
    case=search_case(
        ORDER_SEARCH,
        rows=[
            row_of("o", 1, biz_date=date(2026, 10, 8)),
            row_of("o", 2, biz_date=date(2026, 10, 9)),
            row_of("o", 3, biz_date=None),
        ],
        filters=[BIZ_DATE],
        args={"date_from": date(2026, 10, 9), "date_to": date(2026, 10, 9)},
    )
)
# Both namings of requirement 11.5 at once: the business date as date_from and the bill's own
# date field as need_date_to.
@example(
    case=search_case(
        ORDER_SEARCH,
        rows=[
            row_of("o", 1, biz_date=date(2026, 10, 9), need_date=date(2026, 11, 1)),
            row_of("o", 2, biz_date=date(2026, 10, 10), need_date=date(2026, 10, 9)),
        ],
        filters=[BIZ_DATE, "need_date"],
        args={"date_from": date(2026, 10, 9), "need_date_to": date(2026, 10, 9)},
    )
)
# A page past the last one is an empty page and no error, and the total still counts every
# record that matched (requirement 5.11).
@example(
    case=search_case(
        VENDOR_SEARCH,
        rows=[row_of("v", index, code=f"V000{index}") for index in (1, 2, 3)],
        args={PAGE_PARAM: 4, PAGE_SIZE_PARAM: 2},
    )
)
# A descending key over values of which some are empty, the id deciding the records whose
# keys are equal (requirement 5.14).
@example(
    case=search_case(
        VENDOR_SEARCH,
        rows=[
            row_of("v", 1, pay_days=30),
            row_of("v", 2, pay_days=None),
            row_of("v", 3, pay_days=30),
            row_of("v", 4, pay_days=0),
        ],
        sort=[("pay_days", True)],
    )
)
# Numbers compare by magnitude and not as text, which a negative amount and 0 make visible;
# two sort keys, the second deciding where the first is equal (requirement 5.14).
@example(
    case=search_case(
        ORDER_SEARCH,
        rows=[
            row_of("o", 1, status="draft", amount_with_tax=Decimal("22.60")),
            row_of("o", 2, status="draft", amount_with_tax=Decimal("-5.00")),
            row_of("o", 3, status="approved", amount_with_tax=Decimal("0")),
        ],
        sort=[(STATUS, False), (AMOUNT_WITH_TAX, True)],
    )
)
# A filter of every other type: an integer, a rate and a value no record holds
# (requirement 5.9).
@example(
    case=search_case(
        VENDOR_SEARCH,
        rows=[
            row_of("v", 1, pay_days=30, default_tax_rate=Decimal("0.13")),
            row_of("v", 2, pay_days=30, default_tax_rate=Decimal("0")),
        ],
        filters=["pay_days", "default_tax_rate", STATUS],
        args={"pay_days": 30, "default_tax_rate": Decimal("0.13"), STATUS: "enabled"},
    )
)
# A boolean filter over an empty table: a search that matched nothing is a successful, empty
# page (requirement 5.11).
@example(
    case=search_case(
        ORDER_SEARCH, filters=["price_includes_tax"], args={"price_includes_tax": False}
    )
)
def test_a_search_is_the_reference_conditions_order_and_page(
    store: SandboxStore, case: SearchCase
) -> None:
    domain = fixture_model()
    item = searched_item(domain, case)

    with content_in(store, {case.searched.target: case.rows}) as (uow, stored):
        page = run_search(uow, domain, item, case.args)

    rows = stored[case.searched.target]
    matching = [row for row in rows if search_matches(case, row)]
    number, size = asked_page(case.args)
    expected = page_items(ordered(matching, search_keys(case)), number, size)
    note(f"content: {[dict(row) for row in rows]}")

    # The records that matched, in the order the keys and the id put them in, cut to the page
    # the caller asked for (requirements 5.9, 5.11 and 5.14).
    assert [row_id(view_values(found)) for found in page.items] == [row_id(row) for row in expected]
    assert (page.total, page.page, page.page_size) == (len(matching), number, size)
    assert page.has_more == more_follows(len(matching), number, size)
    # And every record of the page carries the summary fields of its own record and no
    # others, in the order of requirement 5.10 and under the result keys of 13.3.
    summary = summary_fields(item, case, own_fields(item))
    assert [view_values(found) for found in page.items] == [
        summary_object(summary, row) for row in expected
    ]


# --- property 15b: the stock balance query (requirements 9.7 and 9.12) -------------------

# The materials and the warehouses a balance record may name: both are required ref fields of
# the bills that move stock (requirement 9.11), so the primitive never writes an empty one.
# The pools are small, so two records of one material differ by their warehouse alone and the
# id of the record decides where both are equal (requirement 9.12).
MATERIALS: Final[tuple[Value, ...]] = ("art_000001", "art_000002", "art_000009")
WAREHOUSES: Final[tuple[Value, ...]] = ("dep_000001", "dep_000002")

BALANCE_POOLS: Final[tuple[Pool, ...]] = (
    pool(MATERIAL_COLUMN, *MATERIALS),
    pool(WAREHOUSE_COLUMN, *WAREHOUSES),
    pool(QTY_BASE_COLUMN, Decimal("0"), Decimal("12"), Decimal("-3.5"), None),
)

# The order of the result: by material id, then by warehouse id, then by the id of the
# balance record, which :func:`row_order` adds as the last key (requirement 9.12).
BALANCE_KEYS: Final[tuple[Key, ...]] = (
    Key(read=reader(MATERIAL_COLUMN)),
    Key(read=reader(WAREHOUSE_COLUMN)),
)


@dataclass(frozen=True, slots=True)
class BalanceCase:
    """One example of the stock balance query: the balance records and the arguments."""

    rows: tuple[Row, ...]
    args: Mapping[str, Value]


@st.composite
def balance_args(draw: st.DrawFn) -> dict[str, Value]:
    """Draw the arguments of the balance query: the two filters and the page (9.7, 9.12).

    Both filters are optional and either may be a value no record holds; the page and the
    page size may be left out for the defaults of requirement 9.12.
    """
    args: dict[str, Value] = {}
    for name, values in ((MATERIAL_COLUMN, MATERIALS), (WAREHOUSE_COLUMN, WAREHOUSES)):
        wanted = None if draw(st.sampled_from(ABSENT)) else draw(st.sampled_from(values))
        if wanted is not None:
            args[name] = wanted
    for param, numbers in ((PAGE_PARAM, PAGES), (PAGE_SIZE_PARAM, PAGE_SIZES)):
        number = draw(st.sampled_from(numbers))
        if number is not None:
            args[param] = number
    return args


@st.composite
def balance_cases(draw: st.DrawFn) -> BalanceCase:
    """Draw one example: the content of the balance table and the arguments of the query."""
    rows = draw(row_lists("b", BALANCE_POOLS, draw(counts())))
    return BalanceCase(rows=rows, args=FrozenMap(draw(balance_args())))


def balance_case(rows: Sequence[Row] = (), args: Mapping[str, Value] = FrozenMap()) -> BalanceCase:
    """One balance example written out, its records given as mappings of their columns."""
    return BalanceCase(rows=tuple(rows), args=FrozenMap(args))


def balance_row(index: int, material: str, warehouse: str, qty: str | None = "1") -> Row:
    """One balance record: the pair it belongs to and the quantity on hand (req 9.7)."""
    return row_of(
        "b",
        index,
        material_id=material,
        warehouse_id=warehouse,
        qty_base=None if qty is None else Decimal(qty),
    )


def balance_triple(view: RecordView) -> tuple[Value, Value, Value]:
    """One item of the result: the material, the warehouse and the quantity (req 9.7).

    The two references come back as nested summaries, of which the id is what says which
    balance record the item was built from; the id of the record itself is no part of a
    result.
    """
    values = view_values(view)
    return (
        values[MATERIAL_COLUMN.removesuffix(ID_SUFFIX)],
        values[WAREHOUSE_COLUMN.removesuffix(ID_SUFFIX)],
        values[QTY_BASE_COLUMN],
    )


def row_triple(row: Row) -> tuple[Value, Value, Value]:
    """The same three values read off the balance record itself (requirement 9.7)."""
    return (row.get(MATERIAL_COLUMN), row.get(WAREHOUSE_COLUMN), row.get(QTY_BASE_COLUMN))


def balance_matches(args: Mapping[str, Value], row: Row) -> bool:
    """Whether ``row`` satisfies both filters of the balance query (requirement 9.7).

    The material and the warehouse are compared for equality and both are optional: a
    parameter the caller left out does not filter, and giving both selects the one pair that
    satisfies them.
    """
    return all(
        row.get(name) == wanted
        for name in (MATERIAL_COLUMN, WAREHOUSE_COLUMN)
        if (wanted := args.get(name)) is not None
    )


# Feature: erp-mock-mcp-m0, Property 15: 检索与查询的参考模型
@pytest.mark.req("9.7", "9.12")
@settings(max_examples=100)
@given(case=balance_cases())
# Both filters at once select the one pair that satisfies them (requirement 9.7).
@example(
    case=balance_case(
        rows=[
            balance_row(1, "art_000001", "dep_000001"),
            balance_row(2, "art_000001", "dep_000002"),
            balance_row(3, "art_000002", "dep_000001"),
        ],
        args={MATERIAL_COLUMN: "art_000001", WAREHOUSE_COLUMN: "dep_000002"},
    )
)
# By material and then by warehouse, whatever order the records were written in; a quantity
# on hand of 0 is returned as well (requirements 9.10 and 9.12).
@example(
    case=balance_case(
        rows=[
            balance_row(1, "art_000002", "dep_000002", "0"),
            balance_row(2, "art_000001", "dep_000002", "-3.5"),
            balance_row(3, "art_000001", "dep_000001", "12"),
        ]
    )
)
# Eleven records of one pair, where the id of the record decides, read by the page
# (requirement 9.12).
@example(
    case=balance_case(
        rows=[balance_row(index, "art_000001", "dep_000001") for index in range(1, 12)],
        args={PAGE_PARAM: 2, PAGE_SIZE_PARAM: 5},
    )
)
# A filter no record satisfies and a page past the last one: both are empty, successful pages
# (requirement 9.12).
@example(
    case=balance_case(
        rows=[balance_row(1, "art_000001", "dep_000001")], args={MATERIAL_COLUMN: "art_000009"}
    )
)
@example(case=balance_case(args={PAGE_PARAM: 2}))
def test_the_balance_query_is_the_reference_filters_order_and_page(
    store: SandboxStore, case: BalanceCase
) -> None:
    domain = fixture_model()
    query = domain.queries[BALANCE_QUERY]

    with content_in(store, {STOCK_BALANCE_TABLE: case.rows}) as (uow, stored):
        page = run_balance_query(uow, domain, query, case.args)

    rows = stored[STOCK_BALANCE_TABLE]
    matching = [row for row in rows if balance_matches(case.args, row)]
    number, size = asked_page(case.args)
    expected = page_items(ordered(matching, BALANCE_KEYS), number, size)
    note(f"content: {[dict(row) for row in rows]}")

    # The pairs the two filters select, by material id and then warehouse id, cut to the page
    # the caller asked for (requirements 9.7 and 9.12).
    assert [balance_triple(found) for found in page.items] == [row_triple(row) for row in expected]
    assert (page.total, page.page, page.page_size) == (len(matching), number, size)
    assert page.has_more == more_follows(len(matching), number, size)


# --- property 15c: a declarative query (requirements 5.13 and 10.10) ---------------------


@dataclass(frozen=True, slots=True)
class Source:
    """The content one declarative query reads and the level its records come from.

    ``vendors`` are the records a ref path and an ``exists`` read, ``headers`` the bills and
    ``lines`` their lines; a line names the bill it belongs to in the internal column of its
    table, and a line whose bill is not there reads an empty header (requirement 10.10).
    """

    level: FieldLevel
    vendors: tuple[Record, ...] = ()
    headers: tuple[Record, ...] = ()
    lines: tuple[Record, ...] = ()

    @property
    def rows(self) -> tuple[Record, ...]:
        """The source records: the lines of the bill, or its headers (requirement 5.13)."""
        return self.lines if self.level == "line" else self.headers

    def at(self, level: FieldLevel, row: Row) -> Row:
        """The record a path at ``level`` of the source record ``row`` reads (req 10.10).

        The source record itself at its own level, the header of its bill for a ``header.``
        path of a query over lines, and no record at all for a ``line.`` path of a query that
        reads no lines, which leaves every one of its values empty.
        """
        if level == self.level:
            return row
        if self.level == "line" and level == "header":
            return self.header_of(row.get(BILL_ID_COLUMN))
        return NO_ROW

    def header_of(self, bill_id: Value) -> Row:
        """The bill ``bill_id``, an empty record where the content has no such bill."""
        return next((row for row in self.headers if row_id(row) == bill_id), NO_ROW)

    def lines_of(self, row: Row) -> tuple[Row, ...]:
        """The lines of the bill ``row`` belongs to, which ``count`` reads (req 10.13)."""
        found = row.get(BILL_ID_COLUMN) if self.level == "line" else None
        bill_id = found if isinstance(found, str) else row_id(row)
        return tuple(line for line in self.lines if line.get(BILL_ID_COLUMN) == bill_id)

    def vendor_of(self, vendor_id: Value) -> Row:
        """The partner ``vendor_id``, an empty record where there is none (req 10.6)."""
        return next((row for row in self.vendors if row_id(row) == vendor_id), NO_ROW)


@dataclass(frozen=True, slots=True)
class Env:
    """What the reference evaluates one filter on, record by record (requirement 10.10).

    ``line`` is the source line, empty for a query over bill headers, ``header`` the bill it
    belongs to, ``lines`` the lines of that bill and ``source`` the content an ``exists`` and
    a ref path read. The anchor date is :data:`~tests.helpers.fixtures.ANCHOR_DATE`, the
    fixed clock of the fixture, which ``context.today`` evaluates to.
    """

    line: Row
    header: Row
    lines: tuple[Row, ...]
    source: Source


@dataclass(frozen=True, slots=True)
class Filter:
    """One filter of a query: the text the pack declares and what it means (req 10.10).

    The predicate is written next to the text rather than evaluated from it - a reference
    model of the expression language is property 9's - and answers False for an empty value,
    which makes a comparison unknown: only a record the filter is true for is selected
    (requirements 5.13 and 10.6).
    """

    text: str
    holds: Callable[[Env], bool]


# The filters an example may draw, each over the fixture's own configuration. A filter that
# names a ``line.`` path is only drawn for a query over a bill's lines, which is where
# loading step 5 allows one (requirement 10.10).
FILTERS: Final[Mapping[str, Filter]] = {
    "states": Filter(
        "header.status in ['approved', 'closed']",
        lambda env: env.header.get(STATUS) in ("approved", "closed"),
    ),
    "net": Filter(
        "header.price_includes_tax", lambda env: env.header.get("price_includes_tax") is True
    ),
    "until_today": Filter(
        "header.biz_date <= context.today",
        lambda env: on_or_before(env.header.get(BIZ_DATE), ANCHOR_DATE),
    ),
    "enabled_partner": Filter(
        "header.vendor.status == 'enabled'",
        lambda env: env.source.vendor_of(env.header.get("vendor_id")).get(STATUS) == "enabled",
    ),
    "one_line": Filter("count(lines) == 1", lambda env: len(env.lines) == 1),
    "any_disabled": Filter(
        "exists(vendor, status == 'disabled')",
        lambda env: any(row.get(STATUS) == "disabled" for row in env.source.vendors),
    ),
    "price": Filter("line.price > 10", lambda env: greater(env.line.get("price"), Decimal(10))),
    "both": Filter(
        "line.price > 10 and header.status == 'approved'",
        lambda env: (
            greater(env.line.get("price"), Decimal(10)) and env.header.get(STATUS) == "approved"
        ),
    ),
}

# The parameters an example may draw, by the level the ``maps_to`` of each names: the field a
# parameter is compared with and the entity a ref parameter refers to (requirement 5.13).
PARAMS: Final[Mapping[FieldLevel, tuple[QueryParamModel, ...]]] = {
    "header": (
        QueryParamModel(
            name="vendor_id", type="ref", maps_to=FieldRef("header", "vendor_id"), ref=VENDOR
        ),
        QueryParamModel(name="status", type="enum", maps_to=FieldRef("header", STATUS)),
        QueryParamModel(name="biz_date", type="date", maps_to=FieldRef("header", BIZ_DATE)),
    ),
    "line": (
        QueryParamModel(
            name="article_id", type="ref", maps_to=FieldRef("line", "article_id"), ref="article"
        ),
        QueryParamModel(name="price", type="price", maps_to=FieldRef("line", "price")),
    ),
}

# The values a parameter may be given, by its name; None leaves the parameter out, which
# leaves its condition out as well (requirement 5.13).
PARAM_VALUES: Final[Mapping[str, tuple[Value, ...]]] = {
    "vendor_id": ("v1", "v2", "v9"),
    "status": ("draft", "approved", "closed"),
    "biz_date": (date(2026, 10, 9), date(2026, 10, 10)),
    "article_id": ("art_000001", "art_000002"),
    "price": (Decimal("4"), Decimal("12.5")),
}

# The sort keys and the returned fields an example may draw, by level. ``amount`` is a field
# of both levels, so a query that returns both keeps the first one under that key
# (requirement 5.13).
SORTS: Final[Mapping[FieldLevel, tuple[str, ...]]] = {
    "header": (BIZ_DATE, NUMBER, STATUS),
    "line": ("price", "qty", "article_id"),
}
RETURNS: Final[Mapping[FieldLevel, tuple[str, ...]]] = {
    "header": (BIZ_DATE, NUMBER, "vendor_id", AMOUNT),
    "line": ("price", "qty", "article_id", AMOUNT),
}

# The content of the three tables of a query example. The partner records carry the status the
# ref path and the ``exists`` read; a bill names a partner of them or one that is not there;
# a line names a bill of them or one that is not there.
VENDOR_ROWS: Final[tuple[Pool, ...]] = (pool(STATUS, "enabled", "disabled", None),)
HEADER_ROWS: Final[tuple[Pool, ...]] = (
    pool(NUMBER, "SPO202610090001", "SPO202610090002", None),
    pool(STATUS, "draft", "approved", "closed", None),
    pool(BIZ_DATE, date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10), None),
    pool("price_includes_tax", True, False, None),
    pool("vendor_id", "v1", "v2", "v9", None),
    pool(AMOUNT, Decimal("0"), Decimal("22.60"), None),
)
LINE_ROWS: Final[tuple[Pool, ...]] = (
    pool("article_id", "art_000001", "art_000002", None),
    pool("price", Decimal("4"), Decimal("10"), Decimal("12.5"), None),
    pool("qty", Decimal("1"), Decimal("2"), None),
    pool(AMOUNT, Decimal("8"), Decimal("25.00"), None),
)

# How many records of each table a query example holds, and the history limits it may
# declare: None for a query that returns no history at all (requirement 5.13).
MAX_VENDORS: Final = 2
MAX_HEADERS: Final = 3
MAX_LINES: Final = 6
LIMITS: Final[tuple[int | None, ...]] = (None, 1, 2, 5)

# The bill a line may belong to that the content does not hold (requirement 10.10).
MISSING_BILL: Final = "o9"


@dataclass(frozen=True, slots=True)
class QueryCase:
    """One example of a declarative query: what it declares and what the sandbox holds.

    ``level`` is the records the query reads - the lines of the pricing bill or its headers -
    ``params`` the parameters it declares, at the level of its own source, and ``args`` the
    ones the caller gave; ``filter_name`` is the drawn filter of :data:`FILTERS`, ``sort`` the
    sort keys with their directions, and ``latest``, ``history`` and ``limit`` what it returns
    (requirement 5.13).
    """

    level: FieldLevel
    params: tuple[QueryParamModel, ...]
    args: Mapping[str, Value]
    filter_name: str | None
    sort: tuple[tuple[FieldLevel, str, bool], ...]
    latest: tuple[tuple[FieldLevel, str], ...]
    history: tuple[tuple[FieldLevel, str], ...]
    limit: int | None
    vendors: tuple[Row, ...] = ()
    headers: tuple[Row, ...] = ()
    lines: tuple[Row, ...] = ()

    @property
    def table(self) -> str:
        """The table the source records come from (requirement 5.13)."""
        return ORDER_LINE if self.level == "line" else ORDER

    @property
    def filter_text(self) -> str | None:
        """The filter text the query declares, None where it declares none."""
        return None if self.filter_name is None else FILTERS[self.filter_name].text


def query_model(case: QueryCase) -> QueryModel:
    """``case`` as the resolved query of a pack, the way loading step 5 builds one."""
    return QueryModel(
        tool="fx_p15_query",
        pack="fx_trade",
        file="tools/exposure.yaml",
        source=ORDER,
        source_level=case.level,
        params=FrozenMap((param.name, param) for param in case.params),
        filter=case.filter_text,
        sort=tuple(
            SortRef(FieldRef(level, name), descending=descending)
            for level, name, descending in case.sort
        ),
        latest=tuple(FieldRef(level, name) for level, name in case.latest),
        history=tuple(FieldRef(level, name) for level, name in case.history),
        history_limit=case.limit,
    )


def query_selects(case: QueryCase, source: Source, row: Row) -> bool:
    """Whether ``row`` is selected: every parameter equals and the filter is true (5.13).

    A parameter the caller left out does not filter; a query without a filter selects every
    record the parameters left, and a declared filter selects only a record it is true for.
    """
    for param in case.params:
        wanted = case.args.get(param.name)
        if wanted is None:
            continue
        if source.at(param.maps_to.level, row).get(param.maps_to.name) != wanted:
            return False
    if case.filter_name is None:
        return True
    env = Env(
        line=source.at("line", row),
        header=source.at("header", row),
        lines=source.lines_of(row),
        source=source,
    )
    return FILTERS[case.filter_name].holds(env)


def level_reader(source: Source, level: FieldLevel, name: str) -> Callable[[Row], Value]:
    """A reader of the field ``name`` at ``level`` of a source record (requirement 5.13)."""
    return lambda row: source.at(level, row).get(name)


def query_keys(case: QueryCase, source: Source) -> tuple[Key, ...]:
    """The sort keys of the query, each read from the level its path names (req 5.13)."""
    return tuple(
        Key(read=level_reader(source, level, name), descending=descending)
        for level, name, descending in case.sort
    )


def returned_fields(
    levels: Mapping[FieldLevel, Mapping[str, FieldModel]],
    refs: Sequence[tuple[FieldLevel, str]],
) -> dict[str, tuple[FieldLevel, FieldModel]]:
    """The fields ``refs`` names, each result key once, in that order (requirement 5.13).

    A path is read from the level it names, so a query over the lines of a bill returns fields
    of the line and of its header in one object; a field of both levels with one name is
    returned once, under that name, and a path that names no field of its level is left out.
    """
    found: dict[str, tuple[FieldLevel, FieldModel]] = {}
    for level, name in refs:
        declared = levels[level].get(name)
        if declared is not None:
            found.setdefault(result_key(declared), (level, declared))
    return found


def result_object(
    source: Source,
    levels: Mapping[FieldLevel, Mapping[str, FieldModel]],
    refs: Sequence[tuple[FieldLevel, str]],
    row: Row,
) -> dict[str, Value]:
    """One object of the result: the fields ``refs`` names, read off ``row`` (req 5.13)."""
    return {
        key: source.at(level, row).get(declared.name)
        for key, (level, declared) in returned_fields(levels, refs).items()
    }


def source_id(level: FieldLevel) -> tuple[tuple[FieldLevel, str], ...]:
    """The id of the source record, which every drawn result object returns first.

    Both levels declare an ``id``, so the result key of the source record's own id is taken by
    it and the id of its bill is not returned under that key (requirement 5.13).
    """
    return ((level, ID_COLUMN),)


@st.composite
def query_args(draw: st.DrawFn, params: Sequence[QueryParamModel]) -> dict[str, Value]:
    """Draw a value for each parameter of the query, or none at all (requirement 5.13).

    A value no record holds leaves the query without a match, which is a successful result
    with no ``latest`` and no ``history``.
    """
    args: dict[str, Value] = {}
    for param in params:
        values = PARAM_VALUES[param.name]
        wanted = None if draw(st.sampled_from(ABSENT)) else draw(st.sampled_from(values))
        if wanted is not None:
            args[param.name] = wanted
    return args


@st.composite
def returns(draw: st.DrawFn) -> tuple[tuple[FieldLevel, str], ...]:
    """Draw the fields one result object returns next to the id of the source record.

    Paths of both levels are drawn whichever level the source reads, since a ``line.`` path of
    a query over bill headers is allowed and returns null (requirement 5.13).
    """
    levels: tuple[FieldLevel, ...] = ("header", "line")
    return tuple(
        draw(
            st.lists(
                st.one_of(
                    *(
                        st.tuples(st.just(level), st.sampled_from(RETURNS[level]))
                        for level in levels
                    )
                ),
                unique=True,
                max_size=3,
            )
        )
    )


@st.composite
def query_cases(draw: st.DrawFn) -> QueryCase:
    """Draw one example: the source, the declaration, the content and the arguments."""
    level: FieldLevel = draw(st.sampled_from(("line", "header")))
    filters = [
        name
        for name, declared in FILTERS.items()
        if level == "line" or not declared.text.startswith("line.")
    ]
    params = tuple(
        draw(
            st.lists(st.sampled_from(PARAMS[level]), unique_by=lambda param: param.name, max_size=2)
        )
    )
    vendors = draw(row_lists("v", VENDOR_ROWS, draw(counts(MAX_VENDORS))))
    headers = draw(row_lists("o", HEADER_ROWS, draw(counts(MAX_HEADERS))))
    bills = pool(BILL_ID_COLUMN, *[row_id(row) for row in headers], MISSING_BILL)
    return QueryCase(
        level=level,
        params=params,
        args=FrozenMap(draw(query_args(params))),
        filter_name=draw(st.sampled_from([None, *filters])),
        sort=tuple(
            draw(
                st.lists(
                    st.tuples(st.just(level), st.sampled_from(SORTS[level]), st.booleans()),
                    unique_by=lambda key: key[1],
                    max_size=2,
                )
            )
        ),
        latest=(*source_id(level), *draw(returns())),
        history=(*source_id(level), *draw(returns())),
        limit=draw(st.sampled_from(LIMITS)),
        vendors=vendors,
        headers=headers,
        lines=draw(row_lists("l", LINE_ROWS, draw(counts(MAX_LINES)), extra=(bills,))),
    )


def query_case(
    *,
    level: FieldLevel = "line",
    params: Sequence[QueryParamModel] = (),
    args: Mapping[str, Value] = FrozenMap(),
    filter_name: str | None = None,
    sort: Sequence[tuple[FieldLevel, str, bool]] = (),
    extra: Sequence[tuple[FieldLevel, str]] = (),
    limit: int | None = 5,
    vendors: Sequence[Row] = (),
    headers: Sequence[Row] = (),
    lines: Sequence[Row] = (),
) -> QueryCase:
    """One query example written out; both result objects return the id and ``extra``."""
    return QueryCase(
        level=level,
        params=tuple(params),
        args=FrozenMap(args),
        filter_name=filter_name,
        sort=tuple(sort),
        latest=(*source_id(level), *extra),
        history=(*source_id(level), *extra),
        limit=limit,
        vendors=tuple(vendors),
        headers=tuple(headers),
        lines=tuple(lines),
    )


def header_row(index: int, **values: Value) -> Row:
    """One bill header of a written-out example."""
    return row_of("o", index, **values)


def line_row(index: int, bill: str, **values: Value) -> Row:
    """One line of a written-out example, belonging to the bill ``bill``."""
    return row_of("l", index, **{BILL_ID_COLUMN: bill, **values})


# Feature: erp-mock-mcp-m0, Property 15: 检索与查询的参考模型
@pytest.mark.req("5.13", "10.10")
@settings(max_examples=100)
@given(case=query_cases())
# The query of the fixture's own pack: the lines of the pricing bill, a parameter at each
# level, the states of the header and a sort over two of its fields (requirement 5.13).
@example(
    case=query_case(
        params=(PARAMS["header"][0], PARAMS["line"][0]),
        args={"vendor_id": "v1", "article_id": "art_000001"},
        filter_name="states",
        sort=(("header", BIZ_DATE, True), ("header", NUMBER, True)),
        extra=(("line", "price"), ("header", NUMBER)),
        vendors=[row_of("v", 1, status="enabled")],
        headers=[
            header_row(
                1,
                number="SPO202610090001",
                status="approved",
                biz_date=date(2026, 10, 9),
                vendor_id="v1",
            ),
            header_row(
                2,
                number="SPO202610090002",
                status="closed",
                biz_date=date(2026, 10, 10),
                vendor_id="v1",
            ),
        ],
        lines=[
            line_row(1, "o1", article_id="art_000001", price=Decimal("10")),
            line_row(2, "o1", article_id="art_000002", price=Decimal("4")),
            line_row(3, "o2", article_id="art_000001", price=Decimal("12.5")),
        ],
    )
)
# Without a filter and without a sort key the id of the source record alone decides, and
# ``l10`` comes before ``l2`` by code point (requirements 5.13 and 5.14).
@example(
    case=query_case(
        headers=[header_row(1)],
        lines=[line_row(index, "o1", price=Decimal("4")) for index in (1, 2, 10, 11)],
        extra=(("line", "price"),),
    )
)
# A query over bill headers whose result returns a ``line.`` path: there is no line to read,
# so the value is null (requirement 5.13).
@example(
    case=query_case(
        level="header",
        filter_name="net",
        extra=(("line", "price"), ("header", AMOUNT)),
        headers=[
            header_row(1, price_includes_tax=True, amount=Decimal("22.60")),
            header_row(2, price_includes_tax=False),
            header_row(3),
        ],
        lines=[line_row(1, "o1", price=Decimal("10"))],
    )
)
# ``amount`` is a field of both levels: the first path keeps the key and the second one is not
# returned (requirement 5.13).
@example(
    case=query_case(
        extra=(("line", AMOUNT), ("header", AMOUNT)),
        headers=[header_row(1, amount=Decimal("22.60"))],
        lines=[line_row(1, "o1", amount=Decimal("8"))],
    )
)
# A filter that is false or unknown for every record selects none, which leaves ``latest``
# null and ``history`` empty (requirements 5.13 and 10.6).
@example(
    case=query_case(
        filter_name="price",
        headers=[header_row(1)],
        lines=[line_row(1, "o1", price=None), line_row(2, "o1", price=Decimal("4"))],
    )
)
# A query that declares no history returns an empty one, whatever it selected (req 5.13).
@example(
    case=query_case(
        limit=None,
        headers=[header_row(1)],
        lines=[line_row(1, "o1", price=Decimal("12.5"))],
        extra=(("line", "price"),),
    )
)
# ``count(lines)`` reads the bill of the source record, a bill that is not there included, and
# ``exists`` reads a whole table (requirements 10.10 and 10.13).
@example(
    case=query_case(
        filter_name="one_line",
        headers=[header_row(1), header_row(2)],
        lines=[
            line_row(1, "o1"),
            line_row(2, "o2"),
            line_row(3, "o2"),
            line_row(4, MISSING_BILL),
        ],
    )
)
@example(
    case=query_case(
        filter_name="any_disabled",
        vendors=[row_of("v", 1, status="disabled")],
        headers=[header_row(1)],
        lines=[line_row(1, "o1")],
    )
)
# A ref path of the header expanded to the partner it names, which may not be there at all
# (requirements 10.6 and 10.10).
@example(
    case=query_case(
        filter_name="enabled_partner",
        vendors=[row_of("v", 1, status="enabled"), row_of("v", 2, status="disabled")],
        headers=[
            header_row(1, vendor_id="v1"),
            header_row(2, vendor_id="v2"),
            header_row(3, vendor_id="v9"),
        ],
        lines=[line_row(1, "o1"), line_row(2, "o2"), line_row(3, "o3")],
    )
)
# The business context: the anchor date of the fixture is the second of the three business
# dates, so the records of the third one are left out (requirement 10.10).
@example(
    case=query_case(
        level="header",
        filter_name="until_today",
        sort=(("header", BIZ_DATE, False),),
        extra=(("header", BIZ_DATE),),
        headers=[
            header_row(1, biz_date=date(2026, 10, 8)),
            header_row(2, biz_date=date(2026, 10, 9)),
            header_row(3, biz_date=date(2026, 10, 10)),
            header_row(4),
        ],
    )
)
def test_a_declarative_query_is_the_reference_selection_order_and_returns(
    store: SandboxStore, case: QueryCase
) -> None:
    domain = fixture_model()
    query = query_model(case)
    bill = domain.bills[ORDER]
    levels: Mapping[FieldLevel, Mapping[str, FieldModel]] = {
        "header": bill.header,
        "line": bill.lines,
        "record": FrozenMap(),
    }
    content = {VENDOR: case.vendors, ORDER: case.headers, ORDER_LINE: case.lines}

    with content_in(store, content) as (uow, stored):
        resolved = resolve_source(domain, query)
        assert resolved is not None
        tree = query_filter(query)
        found = selected_records(
            uow, domain, query, resolved, case.args, filter_tree=tree, anchor_date=ANCHOR_DATE
        )
        view = run_query(uow, domain, query, case.args, filter_tree=tree, anchor_date=ANCHOR_DATE)

    source = Source(
        level=case.level,
        vendors=stored[VENDOR],
        headers=stored[ORDER],
        lines=stored[ORDER_LINE],
    )
    selected = ordered(
        [row for row in source.rows if query_selects(case, source, row)], query_keys(case, source)
    )
    note(f"selected: {[row_id(row) for row in selected]}")

    # The records every parameter and the filter select, in the order the sort keys and the id
    # of the source record put them in (requirements 5.13 and 10.10).
    assert [row_id(record) for record in found] == [row_id(row) for row in selected]
    # ``latest`` is the first of them and ``history`` the first ``limit`` of them, both with
    # the fields their paths name (requirement 5.13).
    latest = None if view.latest is None else view_values(view.latest)
    assert latest == (result_object(source, levels, case.latest, selected[0]) if selected else None)
    # A query that declares no history returns an empty one, whatever it selected.
    history = (
        []
        if case.limit is None
        else [result_object(source, levels, case.history, row) for row in selected[: case.limit]]
    )
    assert [view_values(item) for item in view.history] == history
