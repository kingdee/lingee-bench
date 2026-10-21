"""The result of one operation, as typed values with resolved references (design.md 4.1).

Every operation of the engine ends in a view: the values a result carries, each in the
Python type of its field - str, int, bool, Decimal, ``datetime.date`` - and every
reference already resolved into the summary of the record it points at. The tools layer
turns a view into the JSON of a tool result (design.md 5.4), so nothing here formats a
number or a date and the engine stays independent of how a result is written.

What a view holds:

- :func:`record_view`, which a get returns and every write operation returns as well
  (requirements 5.16, 13.2, 13.9): every non-internal field of the record, an empty value
  included, and for a bill its lines by line number (requirement 5.12). The keys are the
  result keys of the field view, so a ref field appears without its ``_id``
  (requirement 13.3) and the internal columns of the storage - ``_bill_id``,
  ``_close_mode``, ``_conversion`` - appear nowhere.
- the conversion records of a material, last in its own object and under the key
  :data:`~erp_mock.domain.unit_conversion.UNIT_CONVERSIONS`: the unit and the factor of
  each, by unit id (requirement 6.9). The records come from the unit_conversion primitive
  (:func:`~erp_mock.domain.unit_conversion.conversion_records`), which decides what a
  material carries; only the object of the material itself gets them, so a reference to a
  material in another object and a search result do not.
- :func:`ref_summary`: the nested object of one reference - ``id``, then ``code`` and
  ``name`` as far as the referenced entity declares them, and ``id`` and ``number`` for a
  bill (requirement 13.3).
- :func:`field_item` and :func:`field_view`, one key and one object of a chosen set of
  fields: the summary fields of a search result (requirement 5.10) and the fields a
  declarative query returns, which a query over a bill's lines reads from two records
  (requirement 5.13). :mod:`erp_mock.domain.search` and :mod:`erp_mock.domain.query`
  decide which fields those are.
- :func:`business_context_view`: the anchor date, the month it falls in, the default
  organization and the current user, in that order (requirement 16.10).

The result keys follow the same rules as the outputSchema the tool generator writes:
:func:`result_key` is the ``result_key`` of :mod:`erp_mock.tools.output_schema`,
:func:`summary_keys` its ``entity_ref_schema`` and ``bill_ref_schema``, and :data:`LINES`
its ``LINES_PARAM``. The two are separate because domain must not import tools
(requirement 24.8); the result assembly builds every result object in the order of the
schema of its tool and validates it against that schema, which is where the two meet
(requirement 13.1, design.md 5.4).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from erp_mock.common.date_expr import MonthPeriod, eval_date_expr
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import (
    BillModel,
    DomainModel,
    EntityModel,
    FieldModel,
    UnitConversionModel,
)
from erp_mock.domain.unit_conversion import UNIT_CONVERSIONS, conversion_records
from erp_mock.state.schema import BILL_ID_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "BUSINESS_DATE",
    "CURRENT_PERIOD",
    "LINES",
    "ORG",
    "USER",
    "PageView",
    "QueryView",
    "RecordView",
    "RefSummary",
    "ViewValue",
    "bill_lines",
    "bill_view",
    "business_context_view",
    "conversion_view",
    "default_org",
    "entity_view",
    "field_item",
    "field_view",
    "record_view",
    "ref_summary",
    "ref_target",
    "referenced",
    "result_key",
    "summary_keys",
]

# The key a bill's lines take in its result (requirement 13.2), the name the parameter of
# a write tool has as well (``LINES_PARAM`` of erp_mock.tools.input_schema).
LINES: Final = "lines"

# The four keys of the business context, in the order requirement 16.10 returns them.
BUSINESS_DATE: Final = "business_date"
CURRENT_PERIOD: Final = "current_period"
ORG: Final = "org"
USER: Final = "user"

# The standard fields this module reads by name (meta-model.md section 4): the two a
# summary carries next to the id, the line number a bill's lines are ordered by, and the
# pair that says which bill a pushed bill comes from.
_CODE: Final = "code"
_NAME: Final = "name"
_NUMBER: Final = "number"
_LINE_NO: Final = "line_no"
_SOURCE_BILL_ID: Final = "source_bill_id"
_SOURCE_BILL_TYPE: Final = "source_bill_type"

# The suffix a ref field's name ends with and a result key drops (requirement 13.3).
_ID_SUFFIX: Final = "_id"

# The month the anchor date falls in, the ``current_period`` of the business context
# (requirement 16.10); the period expression ``month(0)`` evaluates to its ``YYYY-MM``.
_ANCHOR_MONTH: Final = MonthPeriod(0)


@dataclass(frozen=True, slots=True)
class RefSummary:
    """The nested object one reference returns (requirement 13.3).

    ``fields`` are its keys in result order: ``id``, then ``code`` and ``name`` as far as
    the referenced entity declares them, and ``id`` and ``number`` for a bill
    (:func:`summary_keys`). The id is the value of the reference itself, so a summary
    always carries it; a record that is not there leaves the other keys empty.
    """

    fields: FrozenMap[str, Value]


# One value of a result: the typed value of a field, the summary of a reference, or an
# array of objects - the lines of a bill (requirement 13.2) and the conversion records of
# a material (requirement 6.9). An empty value is None, which belongs to Value.
type ViewValue = Value | RefSummary | tuple[RecordView, ...]


@dataclass(frozen=True, slots=True)
class RecordView:
    """One object of a result: its keys in result order, their values typed.

    ``fields`` keeps the order the keys are written in, which for a record is the order of
    the field view of its level (requirement 13.1).
    """

    fields: FrozenMap[str, ViewValue]


@dataclass(frozen=True, slots=True)
class PageView:
    """One page of records, what a search returns (requirement 5.11).

    ``items`` are the records of the page, in the order the search sorted them;
    ``total`` is how many records matched altogether and ``page`` and ``page_size`` the
    page they come from, the values actually used.
    """

    items: tuple[RecordView, ...]
    total: int
    page: int
    page_size: int

    @property
    def has_more(self) -> bool:
        """Whether records follow this page (requirement 5.11)."""
        return self.page * self.page_size < self.total


@dataclass(frozen=True, slots=True)
class QueryView:
    """The result of a declarative query: ``latest`` and ``history`` (requirement 5.13).

    ``latest`` is the first matching record, None when none matched, and ``history`` the
    first few of them, an empty tuple when none matched. For an opt-in paginated query,
    ``history`` holds the requested candidate page and ``has_more`` describes records
    after it. The tools layer may shorten that page to fit the byte budget and updates
    the continuation using the number of complete records actually emitted.
    """

    latest: RecordView | None = None
    history: tuple[RecordView, ...] = ()
    # None keeps the legacy query contract. Paginated queries use a zero-based offset.
    offset: int | None = None
    has_more: bool = False


def result_key(field: FieldModel) -> str:
    """The key ``field`` takes in a result: a ref field without its ``_id`` (req 13.3)."""
    return field.name.removesuffix(_ID_SUFFIX) if field.nested else field.name


def referenced(domain: DomainModel, target: str | None) -> EntityModel | BillModel | None:
    """The entity or bill ``target`` names, None when the instance has no such object.

    A target is None where loading step 5 could not resolve the reference, and names no
    object where a record's ``source_bill_type`` holds a bill the instance no longer has.
    """
    if target is None:
        return None
    bill = domain.bills.get(target)
    return bill if bill is not None else domain.entities.get(target)


def summary_keys(item: EntityModel | BillModel | None) -> tuple[str, ...]:
    """The keys the summary of a reference to ``item`` carries, in order (req 13.3).

    ``id``, ``code`` and ``name`` for an entity, each of the latter two only where the
    entity declares it as a field of its own that is no reference itself; ``id`` and
    ``number`` for a bill; the id alone for a target that is not there.
    """
    if isinstance(item, BillModel):
        return (ID_COLUMN, _NUMBER)
    if item is None:
        return (ID_COLUMN,)
    declared = [name for name in (_CODE, _NAME) if _is_scalar(item.fields.get(name))]
    return (ID_COLUMN, *declared)


def ref_summary(
    uow: UnitOfWork, domain: DomainModel, target: str | None, record_id: str
) -> RefSummary:
    """The summary of the record ``record_id`` of ``target`` (requirement 13.3).

    The record is read from the sandbox, so the summary carries the code, the name or the
    number it holds right now. A record that is not there - a reference the engine cannot
    produce, since a referenced record cannot be deleted - keeps the id and leaves the
    other keys empty.
    """
    item = referenced(domain, target)
    record = None if item is None else uow.get(item.name, record_id)
    return _summary(item, record_id, record)


def entity_view(
    uow: UnitOfWork, domain: DomainModel, entity: EntityModel, record: Record
) -> RecordView:
    """The view of one entity record: its fields, then what a material carries (5.12, 6.9).

    A material - an entity that declares a unit conversion - carries its conversion records
    after its own fields (:func:`conversion_view`); every other entity returns its fields
    alone.
    """
    items = _field_items(uow, domain, entity.fields, record)
    conversion = entity.unit_conversion
    if conversion is not None:
        items.append((UNIT_CONVERSIONS, conversion_view(uow, domain, conversion, _id_of(record))))
    return RecordView(FrozenMap(items))


def conversion_view(
    uow: UnitOfWork, domain: DomainModel, conversion: UnitConversionModel, material_id: str
) -> tuple[RecordView, ...]:
    """The conversion records of the material ``material_id``, by unit id (req 6.9).

    One object per record, each carrying the conversion unit as a nested summary and the
    factor as the value it holds, under the result keys of the two fields the pack declared
    and in that order - the keys of the array in the outputSchema of the tool
    (``_conversion_array`` of :mod:`erp_mock.tools.output_schema`). Nothing else of the
    record is returned, its own id and its material included, and a material without a
    conversion record returns an empty array.
    """
    records = conversion_records(uow, conversion, material_id)
    if not records:
        return ()
    entity = domain.entities.get(conversion.entity)
    declared: Mapping[str, FieldModel] = FrozenMap() if entity is None else entity.fields
    shown = {
        name: field
        for name in (conversion.unit_field, conversion.factor_field)
        if (field := declared.get(name)) is not None
    }
    return tuple(
        RecordView(FrozenMap(_field_items(uow, domain, shown, record))) for record in records
    )


def bill_view(uow: UnitOfWork, domain: DomainModel, bill: BillModel, header: Record) -> RecordView:
    """The view of one bill: its header fields, then its lines (requirement 5.12).

    The lines are read from the sandbox and ordered by line number; each one carries every
    non-internal field of a line.
    """
    items = _field_items(uow, domain, bill.header, header)
    lines = tuple(
        RecordView(FrozenMap(_field_items(uow, domain, bill.lines, line)))
        for line in bill_lines(uow, bill, _id_of(header))
    )
    items.append((LINES, lines))
    return RecordView(FrozenMap(items))


def record_view(
    uow: UnitOfWork, domain: DomainModel, item: EntityModel | BillModel, record: Record
) -> RecordView:
    """The view a get of ``record`` returns, a bill's lines included (requirement 5.12)."""
    if isinstance(item, BillModel):
        return bill_view(uow, domain, item, record)
    return entity_view(uow, domain, item, record)


def bill_lines(uow: UnitOfWork, bill: BillModel, bill_id: str) -> tuple[Record, ...]:
    """The lines of the bill ``bill_id``, by line number ascending (requirement 5.12).

    The line ids of a bill are built from its id and the line number (requirement 4.2), so
    their code point order is not the order of the numbers; the lines are sorted by the
    number itself, and a line without one comes last.
    """
    return tuple(sorted(uow.select(bill.line_name, where={BILL_ID_COLUMN: bill_id}), key=_line_key))


def default_org(uow: UnitOfWork, entity: EntityModel) -> Record | None:
    """The default organization: the record whose default flag is true (requirement 1.8).

    ``entity`` is the organization context entity of the instance
    (:attr:`~erp_mock.config.model.DomainModel.org_entity`). Loading step 8 requires the
    initial dataset to hold exactly one such record; where a sandbox holds several, the
    first by id is taken, and where it holds none the result is None.
    """
    if entity.default_flag is None:
        return None
    records = uow.select(entity.name, where={entity.default_flag: True})
    return records[0] if records else None


def business_context_view(
    uow: UnitOfWork, domain: DomainModel, anchor_date: date, current_user_id: str | None
) -> RecordView:
    """The business context of the sandbox, in the order of requirement 16.10.

    ``business_date`` is the anchor date of the sandbox and ``current_period`` the month it
    falls in; ``org`` is the summary of the default organization and ``user`` that of the
    current user, both read from the sandbox as it is now. Either one is None - null in the
    result - where the instance declares no such context entity, where the sandbox holds no
    default organization, or where no current user is configured.
    """
    org_entity = domain.org_entity
    user_entity = domain.user_entity
    org_record = None if org_entity is None else default_org(uow, org_entity)
    org = None if org_record is None else _summary(org_entity, _id_of(org_record), org_record)
    user = (
        None
        if user_entity is None or current_user_id is None
        else ref_summary(uow, domain, user_entity.name, current_user_id)
    )
    items: tuple[tuple[str, ViewValue], ...] = (
        (BUSINESS_DATE, anchor_date),
        (CURRENT_PERIOD, eval_date_expr(_ANCHOR_MONTH, anchor_date)),
        (ORG, org),
        (USER, user),
    )
    return RecordView(FrozenMap(items))


def field_item(
    uow: UnitOfWork, domain: DomainModel, field: FieldModel, record: Record
) -> tuple[str, ViewValue]:
    """The result key and value of ``field`` read from ``record`` (requirements 5.12, 13.3).

    A ref field comes back as the summary of the record it points at, under its name
    without ``_id``; every other field as the typed value it holds. A field the record has
    no value for keeps its key and gets None, so no key is left out (requirement 13.4).
    """
    value = record.get(field.name)
    if not field.nested:
        return (field.name, value)
    if isinstance(value, str):
        return (result_key(field), ref_summary(uow, domain, ref_target(field, record), value))
    return (result_key(field), None)


def field_view(
    uow: UnitOfWork, domain: DomainModel, fields: Iterable[FieldModel], record: Record
) -> RecordView:
    """One result object built from ``fields`` of ``record``, in that order.

    The object of a chosen set of fields rather than of a whole record: the summary fields
    of a search result (requirement 5.10) and the fields a declarative query returns
    (requirement 5.13); :func:`record_view` is the whole record.
    """
    return RecordView(FrozenMap(field_item(uow, domain, field, record) for field in fields))


def _field_items(
    uow: UnitOfWork, domain: DomainModel, fields: Mapping[str, FieldModel], record: Record
) -> list[tuple[str, ViewValue]]:
    """The result keys and values of ``fields``, in field view order (requirement 5.12)."""
    return [field_item(uow, domain, field, record) for field in fields.values()]


def ref_target(field: FieldModel, record: Record) -> str | None:
    """The entity or bill ``field`` of ``record`` refers to (requirement 13.3).

    The ``ref`` of the field, or, for the header's ``source_bill_id``, the bill its own
    record names in ``source_bill_type``; None where loading step 5 could not resolve the
    target.
    """
    if field.ref is not None:
        return field.ref
    if field.origin == "standard" and field.name == _SOURCE_BILL_ID:
        bill_type = record.get(_SOURCE_BILL_TYPE)
        return bill_type if isinstance(bill_type, str) else None
    return None


def _summary(
    item: EntityModel | BillModel | None, record_id: str, record: Record | None
) -> RefSummary:
    """The summary of ``record``, carrying the keys of ``item`` (requirement 13.3)."""
    fields: Mapping[str, Value] = FrozenMap() if record is None else record
    items: list[tuple[str, Value]] = [
        (key, record_id if key == ID_COLUMN else fields.get(key)) for key in summary_keys(item)
    ]
    return RefSummary(FrozenMap(items))


def _is_scalar(field: FieldModel | None) -> bool:
    """Whether ``field`` is a field of its own that a summary returns as a value."""
    return field is not None and not field.nested


def _id_of(record: Record) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""


def _line_key(line: Record) -> tuple[int, int, str]:
    """The sort key of one line: its line number, a line without one last (req 5.12)."""
    line_no = line.get(_LINE_NO)
    # A bool is an int to Python but not to an int column (state.store.to_stored).
    if isinstance(line_no, int) and not isinstance(line_no, bool):
        return (0, line_no, _id_of(line))
    return (1, 0, _id_of(line))
