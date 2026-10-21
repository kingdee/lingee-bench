"""The declarative query of an exposure: the records it selects, and its latest and history.

A query tool a business pack declares by ``source``, ``params``, ``filter``, ``sort`` and
``returns`` (requirement 5.13). What it does, in the slots of the pipeline design.md 4.1
assigns it; a query writes nothing, so every other slot passes it by:

=====================  ==================================================================
slot                   what this module contributes
=====================  ==================================================================
``references``         :func:`check_query_references`: every reference parameter of the
                       query names a record that is there (requirement 5.6)
``view``               :func:`run_query`: the records it selects, sorted, as ``latest``
                       and ``history`` (requirements 5.13 and 10.10)
=====================  ==================================================================

What it reads. ``source`` is an entity, the headers of a bill or its lines, written
``<bill>_line``; the resolved model keeps the object and the level apart, so
:class:`QuerySource` says which table the records come from and which field view each path
of the query resolves against (``source_level`` of
:class:`~erp_mock.config.model.QueryModel`). A ``header.<field>`` path reads the entity
record, the bill header, or the header of the bill a line belongs to; a ``line.<field>``
path reads the source line and has nothing to read in a query over bill headers or entity
records, where it is null - only the ``filter`` is kept from using ``line.`` there, by
loading step 5 (requirement 10.10).

Which records are selected (requirement 5.13):

1. every parameter the caller gave must equal the field it maps to, compared on the typed
   values of the storage layer, so a number compares by magnitude and a date
   chronologically; a parameter that was not given, or given as null, does not filter;
2. the ``filter`` must evaluate to true. Only true selects: a record the filter is false or
   unknown for is left out, which is the three-valued logic of requirement 10.6
   (:func:`~erp_mock.domain.rule_expr.evaluator.eval_expr`). A query without a filter
   selects every record the parameters left.

The filter is evaluated on its syntax tree, never on the text again: :func:`query_filter`
parses one and :func:`query_filters` every filter of an instance, which the engine does
once per loaded instance. Loading step 5 has already parsed the same text and checked that
it resolves, is well typed, is boolean and uses ``line.`` only where the source is a bill's
lines (``loader.step5_expr``, requirement 10.2); parsing is a pure function of the text, so
the tree here is the tree the loader checked. A caller that holds those trees - the loader's
``ParsedDomain`` - hands them to the engine instead, and domain must not import loader
(requirement 24.8), which is why this module can produce them on its own.

What the filter is evaluated on (requirement 10.10). :class:`RecordEnv` is the
:class:`~erp_mock.domain.rule_expr.evaluator.EvalEnv` of one source record:
``header.<field>`` is the record of the first paragraph, ``line.<field>`` the source line,
``context.today`` and ``context.period`` the anchor date and its month,
``context.user.<field>`` the current user's record, and a ref path is expanded to the record
it points at (:class:`StoredRecord`). ``sum(lines.F)`` and ``count(lines)`` read the lines
of the bill the record belongs to, and ``exists`` and ``sum_of`` read a whole table once per
call (:class:`QueryTables`) - the records of the sandbox as the call found them, a query
writing nothing (requirement 10.13). The rules step evaluates an enforced rule's check on
the bill the operation produced (requirement 10.5) and builds its own env over that
candidate; the two classes here are the env over stored records, which that step reads the
records of an ``exists`` through as well.

The order (requirement 5.13): the keys of ``sort``, each ascending unless the pack wrote
``desc``, and the id of the source record ascending as the last key, compared the way
requirement 5.14 compares (:func:`~erp_mock.domain.search.sorted_records`).

What comes back. ``latest`` is the fields of ``returns.latest`` of the first record, null
when no record was selected; ``history`` the fields of ``returns.history.fields`` of the
first ``limit`` records, the first one included, and an empty array for a query that
declares no history at all. A field is read from the level its path names, so a query over
the lines of a bill returns fields of the line and of its header in one object; a field of
both levels with one name is returned once, under that name (:func:`result_fields`). This is
the same rule, in the same order, as the ``latest`` and ``history`` of the outputSchema of
the query tool (``_query_keys`` of :mod:`erp_mock.tools.output_schema`), separate because
domain must not import tools (requirement 24.8).

What is not here: the query the inventory primitive provides, the stock balance of
requirements 9.7 and 9.12, whose parameters and result the primitive defines rather than
the exposure. The ``view`` slot sends it to that primitive instead
(:func:`~erp_mock.domain.inventory.run_balance_query`), so :func:`run_query` performs the
declared queries alone and raises NotImplementedError for any other (design.md 4.2).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

from erp_mock.common.errors import ErpError, ErrorCode
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
    SortRef,
)
from erp_mock.domain.crud import Reference, field_path, missing_references, not_found, own_fields
from erp_mock.domain.rule_expr.evaluator import Record as ExprRecord
from erp_mock.domain.rule_expr.evaluator import Value as ExprValue
from erp_mock.domain.rule_expr.evaluator import eval_expr
from erp_mock.domain.rule_expr.nodes import Expr
from erp_mock.domain.rule_expr.parser import parse_expr
from erp_mock.domain.search import SortKey, sorted_records
from erp_mock.domain.views import (
    QueryView,
    RecordView,
    ViewValue,
    bill_lines,
    field_item,
    ref_target,
    referenced,
    result_key,
)
from erp_mock.state.schema import BILL_ID_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "QuerySource",
    "QueryTables",
    "RecordEnv",
    "ResultField",
    "StoredRecord",
    "check_query_references",
    "query_filter",
    "query_filters",
    "query_references",
    "query_sort_keys",
    "resolve_source",
    "result_fields",
    "run_query",
    "selected_records",
    "user_record",
]

# The record a path with nothing to read reads: a ``line.<field>`` path of a query over
# bill headers or entity records, which names a field of the lines and has no line, so
# every such value is null (see the module docstring).
_NO_RECORD: Final[Record] = FrozenMap()


@dataclass(frozen=True, slots=True)
class QuerySource:
    """Where a declarative query reads its records from (requirement 5.13).

    ``item`` is the entity or the bill the exposure named and ``level`` the records it
    reads: an entity record (``record``), a bill header (``header``) or a bill line
    (``line``). :attr:`table` is the table those records live in; :attr:`header_fields` is
    the field view a ``header.<field>`` path resolves against, which for a line is the
    header of its bill, and :meth:`level_fields` the view of any level of the object.
    """

    item: EntityModel | BillModel
    level: FieldLevel

    @property
    def bill(self) -> BillModel | None:
        """The bill the query reads, None for a query over an entity."""
        return self.item if isinstance(self.item, BillModel) else None

    @property
    def table(self) -> str:
        """The table of the source records: the lines of a bill for a line query."""
        bill = self.bill
        return bill.line_name if bill is not None and self.level == "line" else self.item.name

    @property
    def fields(self) -> Mapping[str, FieldModel]:
        """The field view of the source records themselves."""
        return self.level_fields(self.level)

    @property
    def header_fields(self) -> Mapping[str, FieldModel]:
        """The field view of what ``header.<field>`` reads: a record or a bill header."""
        return own_fields(self.item)

    def level_fields(self, level: FieldLevel) -> Mapping[str, FieldModel]:
        """The field view of ``level`` of the source object, empty where it has none."""
        item = self.item
        if isinstance(item, BillModel):
            return item.level(level)
        return item.fields if level == "record" else FrozenMap()


@dataclass(frozen=True, slots=True)
class ResultField:
    """One field a query returns: the level its path named and the field itself (5.13)."""

    level: FieldLevel
    field: FieldModel


@dataclass(frozen=True, slots=True)
class StoredRecord:
    """One stored record an expression reads, the Record of the evaluator (req 10.10).

    ``fields`` is the field view of the record's own level, which is what a ref path is
    expanded through: the field says which entity or bill it refers to, and the record a
    header's ``source_bill_id`` points at is the bill its own ``source_bill_type`` names
    (requirement 13.3). A reference that is empty, or whose target the instance does not
    have, expands to None, which makes the path null (requirement 10.6).
    """

    uow: UnitOfWork
    domain: DomainModel
    fields: Mapping[str, FieldModel]
    record: Record

    def field(self, name: str) -> ExprValue:
        """The value of the field ``name``, None when it is null (requirement 10.6).

        An int field comes back as a Decimal: the type rules put int in the number class,
        which the evaluator computes and compares in (requirements 10.11 and 10.12).
        """
        return _expr_value(self.record.get(name))

    def ref(self, name: str) -> "StoredRecord | None":
        """The record the ref field ``name`` refers to, None when there is none (10.1)."""
        declared = self.fields.get(name)
        value = self.record.get(name)
        if declared is None or not isinstance(value, str):
            return None
        item = referenced(self.domain, ref_target(declared, self.record))
        if item is None:
            return None
        target = self.uow.get(item.name, value)
        if target is None:
            return None
        return StoredRecord(
            uow=self.uow, domain=self.domain, fields=own_fields(item), record=target
        )


@dataclass(slots=True)
class QueryTables:
    """The records an ``exists`` or a ``sum_of`` reads, read once per call (req 10.13).

    One instance per query call, shared by the env of every source record: a filter that
    names a target reads that table on the first record and the records are kept for the
    rest, so one call reads one table once. A bill is read as its headers, and a name the
    instance does not have reads nothing.
    """

    uow: UnitOfWork
    domain: DomainModel
    _cached: dict[str, tuple[StoredRecord, ...]] = field(default_factory=dict)

    def of(self, target: str) -> tuple[StoredRecord, ...]:
        """Every record of the entity or bill ``target``, by id."""
        cached = self._cached.get(target)
        if cached is None:
            item = referenced(self.domain, target)
            fields: Mapping[str, FieldModel] = FrozenMap() if item is None else own_fields(item)
            records = () if item is None else tuple(self.uow.select(item.name))
            cached = tuple(
                StoredRecord(uow=self.uow, domain=self.domain, fields=fields, record=record)
                for record in records
            )
            self._cached[target] = cached
        return cached


@dataclass(frozen=True, slots=True)
class RecordEnv:
    """What the filter of one source record is evaluated on (requirement 10.10).

    ``header_record`` is what ``header.<field>`` reads - the entity record, the bill header,
    or the header of the bill a line belongs to - and ``line_records`` the lines of that
    bill, which ``sum(lines.F)`` and ``count(lines)`` read and which are empty for a query
    over an entity. ``user_record`` is the current user's record, None where the instance
    configures none, and ``tables`` the records an ``exists`` or a ``sum_of`` reads. The
    source line itself is passed to the evaluator per record and is not held here.
    """

    anchor_date: date
    header_record: StoredRecord
    tables: QueryTables
    line_records: tuple[StoredRecord, ...] = ()
    user_record: StoredRecord | None = None

    @property
    def today(self) -> date:
        """The anchor date: the value of ``context.today`` (requirement 10.5)."""
        return self.anchor_date

    @property
    def header(self) -> ExprRecord:
        """The record that ``header.<field>`` reads (requirement 10.10)."""
        return self.header_record

    @property
    def lines(self) -> Sequence[ExprRecord]:
        """The lines of the bill the record belongs to, by line number (req 10.13)."""
        return self.line_records

    @property
    def user(self) -> ExprRecord | None:
        """The current user's record, None where the instance has none (requirement 10.6)."""
        return self.user_record

    def records(self, target: str) -> Sequence[ExprRecord]:
        """Every record of ``target``, for an ``exists`` or a ``sum_of`` (req 10.13)."""
        return self.tables.of(target)


# --- the filter of a declarative query (requirement 10.2) --------------------------------


def query_filter(query: QueryModel) -> Expr | None:
    """The syntax tree of the ``filter`` of ``query``, None where it declares none.

    Raises :class:`~erp_mock.domain.rule_expr.errors.ExprError` for a text that does not
    parse, which loading step 5 reported and refused to start on (requirement 10.2).
    """
    return None if query.filter is None else parse_expr(query.filter)


def query_filters(domain: DomainModel) -> FrozenMap[str, Expr]:
    """The syntax tree of every declared filter of ``domain``, by query tool name.

    A query without a filter is absent, as is a primitive query, which declares none. Built
    once per loaded instance, so no call parses a filter text again (see the module
    docstring).
    """
    return FrozenMap(
        (query.tool, tree)
        for query in domain.queries.values()
        if (tree := query_filter(query)) is not None
    )


# --- the source of a declarative query (requirement 5.13) --------------------------------


def resolve_source(domain: DomainModel, query: QueryModel) -> QuerySource | None:
    """Where ``query`` reads its records from, None when the instance has no such object.

    None for a primitive query, which names no source, and for a declared source loading
    step 5 could not resolve, which it reported (requirement 2.9).
    """
    if query.source is None or query.source_level is None:
        return None
    item = referenced(domain, query.source)
    if item is None:
        return None
    if not isinstance(item, BillModel) and query.source_level != "record":
        return None
    return QuerySource(item=item, level=query.source_level)


# --- the references of a query (requirement 5.6) -----------------------------------------


def query_references(query: QueryModel, args: Mapping[str, Value]) -> tuple[Reference, ...]:
    """The references the caller gave, in parameter order (requirement 5.6).

    One per reference parameter the caller gave a value for; the path is the parameter's own
    name. A parameter that was left out or sent as null names no record.
    """
    return tuple(
        Reference(path=field_path(param.name), target=param.ref, id=value)
        for param in query.params.values()
        if param.ref is not None and isinstance(value := args.get(param.name), str)
    )


def check_query_references(
    uow: UnitOfWork, domain: DomainModel, query: QueryModel, args: Mapping[str, Value]
) -> None:
    """Step 1 of a query: every reference parameter names a record (requirement 5.6).

    Raises one NOT_FOUND for all of them at once, its details carrying the field path of
    each (:func:`~erp_mock.domain.crud.not_found`).
    """
    missing = missing_references(uow, domain, query_references(query, args))
    if missing:
        raise not_found(domain, missing)


# --- the records a query selects (requirements 5.13 and 10.10) ---------------------------


def selected_records(
    uow: UnitOfWork,
    domain: DomainModel,
    query: QueryModel,
    source: QuerySource,
    args: Mapping[str, Value],
    *,
    filter_tree: Expr | None,
    anchor_date: date,
    current_user_id: str | None = None,
) -> list[Record]:
    """The source records the parameters and the filter select, in order (5.13, 10.10).

    Every record of the source table is read, the parameters are compared with the fields
    they map to, the filter is evaluated on the record, and what is left is sorted by
    ``sort`` with the id of the source record as the last key.
    """
    reader = _Reader(
        uow=uow,
        domain=domain,
        source=source,
        anchor_date=anchor_date,
        current_user_id=current_user_id,
    )
    return _selected(reader, query, args, filter_tree)


def query_sort_keys(
    source: QuerySource, sort: Sequence[SortRef], header: Callable[[Record], Record]
) -> tuple[SortKey, ...]:
    """The sort keys of ``sort``, each read from the level its path names (req 5.13).

    ``header`` gives the header record of a source record, which a ``header.<field>`` key of
    a query over a bill's lines is read from. A key that names no field of its level is left
    out, as a field loading step 5 could not resolve.
    """
    return tuple(
        SortKey(value=_level_reader(source, ref.field, header), descending=ref.descending)
        for ref in sort
        if ref.field.name in source.level_fields(ref.field.level)
    )


# --- what a query returns (requirement 5.13) ---------------------------------------------


def result_fields(source: QuerySource, refs: Sequence[FieldRef]) -> tuple[ResultField, ...]:
    """The fields ``refs`` names, in that order, each result key once (requirement 5.13).

    A path is read from the level it names - the lines of the source bill for a ``line.``
    path and the header for a ``header.`` one - so a query over the lines of a bill returns
    fields of both. A field of both levels with one name is returned once, under that name,
    and a path that names no field of its level is left out.
    """
    found: dict[str, ResultField] = {}
    for ref in refs:
        declared = source.level_fields(ref.level).get(ref.name)
        if declared is not None:
            found.setdefault(result_key(declared), ResultField(level=ref.level, field=declared))
    return tuple(found.values())


# --- the query itself --------------------------------------------------------------------


def run_query(
    uow: UnitOfWork,
    domain: DomainModel,
    query: QueryModel,
    args: Mapping[str, Value],
    *,
    filter_tree: Expr | None = None,
    anchor_date: date,
    current_user_id: str | None = None,
) -> QueryView:
    """The ``latest`` and the ``history`` of the declarative query ``query`` (req 5.13).

    ``filter_tree`` is the syntax tree of the query's filter, which the caller holds - the
    engine parses every filter of its instance once (:func:`query_filters`) - and None for a
    query that declares none; ``anchor_date`` and ``current_user_id`` are the business
    context the filter reads (requirement 10.5).

    Raises NotImplementedError for a query a primitive provides, which the ``view`` slot
    sends to that primitive instead (:func:`~erp_mock.domain.inventory.run_balance_query`
    for the stock balance of requirements 9.7 and 9.12), and RuntimeError for a declared
    source the instance does not have, which loading step 5 reported.
    """
    if query.primitive is not None:
        raise NotImplementedError(
            f"the {query.primitive} primitive performs the query {query.query!r}, not this one"
        )
    source = resolve_source(domain, query)
    if source is None:
        raise RuntimeError(f"the query {query.tool} has no source the instance declares")
    reader = _Reader(
        uow=uow,
        domain=domain,
        source=source,
        anchor_date=anchor_date,
        current_user_id=current_user_id,
    )
    records = _selected(reader, query, args, filter_tree)
    offset: int | None = None
    page_limit = query.history_limit or 1
    if query.history_paginated:
        raw_offset = args.get("offset", 0)
        raw_limit = args.get("limit", min(50, page_limit))
        raw_offset = 0 if raw_offset is None else raw_offset
        raw_limit = min(50, page_limit) if raw_limit is None else raw_limit
        if (
            isinstance(raw_offset, bool)
            or not isinstance(raw_offset, int)
            or raw_offset < 0
            or isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or not 1 <= raw_limit <= page_limit
        ):
            raise ErpError(
                ErrorCode.VALIDATION_FAILED, "offset 须为非负整数，limit 须在查询页大小范围内"
            )
        offset, page_limit = raw_offset, raw_limit
    if not records:
        return QueryView(offset=offset)
    header = reader.header_of
    latest = _result_view(
        uow, domain, source, result_fields(source, query.latest), records[0], header
    )
    limit = query.history_limit
    if limit is None:
        return QueryView(latest=latest)
    fields = result_fields(source, query.history)
    selected = records[:limit] if offset is None else records[offset : offset + page_limit]
    history = tuple(
        _result_view(uow, domain, source, fields, record, header) for record in selected
    )
    return QueryView(
        latest=latest,
        history=history,
        offset=offset,
        has_more=offset is not None and offset + len(history) < len(records),
    )


# --- internals ---------------------------------------------------------------------------


def _selected(
    reader: "_Reader", query: QueryModel, args: Mapping[str, Value], filter_tree: Expr | None
) -> list[Record]:
    """The records ``reader`` selects for ``query``, sorted (requirements 5.13, 10.10).

    One reader per call, so the bills, the user and the tables the filter reads are read
    once however many source records there are.
    """
    source = reader.source
    matching = [
        record
        for record in reader.uow.select(source.table)
        if reader.matches(query, args, record) and reader.selects(filter_tree, record)
    ]
    return sorted_records(matching, query_sort_keys(source, query.sort, reader.header_of))


@dataclass(slots=True)
class _Headers:
    """The header record and the lines of each source record, read once per bill (5.13).

    A source line names the bill it belongs to in the internal column of its table; a bill
    header and an entity record are their own header record.
    """

    uow: UnitOfWork
    source: QuerySource
    _headers: dict[str, Record] = field(default_factory=dict)
    _lines: dict[str, tuple[Record, ...]] = field(default_factory=dict)

    def of(self, record: Record) -> Record:
        """The record a ``header.<field>`` path of ``record`` reads."""
        bill = self.source.bill
        bill_id = self._bill_id(record)
        if bill is None or bill_id is None:
            return record
        cached = self._headers.get(bill_id)
        if cached is None:
            found = self.uow.get(bill.name, bill_id)
            # A stored line belongs to a stored bill; an empty record keeps every
            # ``header.`` path null where a sandbox lost one.
            cached = found if found is not None else _NO_RECORD
            self._headers[bill_id] = cached
        return cached

    def lines_of(self, record: Record) -> tuple[Record, ...]:
        """The lines of the bill ``record`` belongs to, by line number (req 10.13)."""
        bill = self.source.bill
        if bill is None:
            return ()
        bill_id = self._bill_id(record) or _id_of(record)
        cached = self._lines.get(bill_id)
        if cached is None:
            cached = bill_lines(self.uow, bill, bill_id)
            self._lines[bill_id] = cached
        return cached

    def _bill_id(self, record: Record) -> str | None:
        """The bill a source line belongs to, None where the records are not lines."""
        if self.source.level != "line":
            return None
        bill_id = record.get(BILL_ID_COLUMN)
        return bill_id if isinstance(bill_id, str) else None


@dataclass(slots=True)
class _Reader:
    """One query call: the headers, the lines, the user and the tables its filter reads.

    One instance per call, so every source record is compared and evaluated against the
    same business context and each bill and each table is read once.
    """

    uow: UnitOfWork
    domain: DomainModel
    source: QuerySource
    anchor_date: date
    current_user_id: str | None = None
    _headers: _Headers = field(init=False)
    _tables: QueryTables = field(init=False)
    _user: StoredRecord | None = field(init=False)

    def __post_init__(self) -> None:
        self._headers = _Headers(uow=self.uow, source=self.source)
        self._tables = QueryTables(uow=self.uow, domain=self.domain)
        self._user = user_record(self.uow, self.domain, self.current_user_id)

    def header_of(self, record: Record) -> Record:
        """The record a ``header.<field>`` path of ``record`` reads (requirement 10.10)."""
        return self._headers.of(record)

    def matches(self, query: QueryModel, args: Mapping[str, Value], record: Record) -> bool:
        """Whether every parameter the caller gave equals the field it maps to (5.13)."""
        return all(
            self._equal(param, value, record)
            for param in query.params.values()
            if (value := args.get(param.name)) is not None
        )

    def selects(self, filter_tree: Expr | None, record: Record) -> bool:
        """Whether the filter is true for ``record``; false and unknown are not (10.6).

        A query without a filter selects every record.
        """
        if filter_tree is None:
            return True
        return eval_expr(filter_tree, self.env(record), self._line(record)) is True

    def env(self, record: Record) -> RecordEnv:
        """What the filter of ``record`` is evaluated on (requirement 10.10)."""
        return RecordEnv(
            anchor_date=self.anchor_date,
            header_record=self._stored(self.source.header_fields, self._headers.of(record)),
            tables=self._tables,
            line_records=tuple(
                self._stored(self.source.level_fields("line"), line)
                for line in self._headers.lines_of(record)
            ),
            user_record=self._user,
        )

    def _equal(self, param: QueryParamModel, value: Value, record: Record) -> bool:
        """Whether the field ``param`` maps to holds ``value`` (requirement 5.13)."""
        read = _record_at(self.source, param.maps_to.level, record, self.header_of)
        return read.get(param.maps_to.name) == value

    def _line(self, record: Record) -> StoredRecord | None:
        """The record ``line.<field>`` reads: the source record of a line query (10.10)."""
        if self.source.level != "line":
            return None
        return self._stored(self.source.fields, record)

    def _stored(self, fields: Mapping[str, FieldModel], record: Record) -> StoredRecord:
        """``record`` as an expression reads it, with the field view of its level."""
        return StoredRecord(uow=self.uow, domain=self.domain, fields=fields, record=record)


def _result_view(
    uow: UnitOfWork,
    domain: DomainModel,
    source: QuerySource,
    fields: Sequence[ResultField],
    record: Record,
    header: Callable[[Record], Record],
) -> RecordView:
    """One object of a query result: ``fields`` of ``record`` and of its header (5.13)."""
    items: list[tuple[str, ViewValue]] = [
        field_item(uow, domain, found.field, _record_at(source, found.level, record, header))
        for found in fields
    ]
    return RecordView(FrozenMap(items))


def _record_at(
    source: QuerySource, level: FieldLevel, record: Record, header: Callable[[Record], Record]
) -> Record:
    """The record a path at ``level`` of the source record ``record`` reads (req 5.13).

    The source record itself at its own level, the header of its bill for a
    ``header.<field>`` path of a query over lines, and no record at all for a
    ``line.<field>`` path of a query that reads no lines (:data:`_NO_RECORD`).
    """
    if level == source.level:
        return record
    if source.level == "line" and level == "header":
        return header(record)
    return _NO_RECORD


def _level_reader(
    source: QuerySource, ref: FieldRef, header: Callable[[Record], Record]
) -> Callable[[Record], Value]:
    """A reader of the field ``ref`` names of a source record (requirements 5.13, 5.14)."""
    return lambda record: _record_at(source, ref.level, record, header).get(ref.name)


def _expr_value(value: Value) -> ExprValue:
    """``value`` as an expression reads it: an int field is a number (req 10.11, 10.12).

    A bool is an int to Python but a boolean to the type rules, so it stays a bool; every
    other stored type is a value of the expression classes already.
    """
    if isinstance(value, bool):
        return value
    return Decimal(value) if isinstance(value, int) else value


def user_record(
    uow: UnitOfWork, domain: DomainModel, current_user_id: str | None
) -> StoredRecord | None:
    """The current user's record, None where the instance configures none (req 1.8).

    What every ``context.user.<field>`` path reads, whichever expression names it: the
    filter of a query, the check of a rule, the value of a writeback (requirement 10.5).
    """
    entity = domain.user_entity
    if entity is None or current_user_id is None:
        return None
    record = uow.get(entity.name, current_user_id)
    if record is None:
        return None
    return StoredRecord(uow=uow, domain=domain, fields=entity.fields, record=record)


def _id_of(record: Record) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""
