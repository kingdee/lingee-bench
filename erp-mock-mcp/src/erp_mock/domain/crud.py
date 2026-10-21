"""The crud primitive: the record a write builds, its defaults, references and uniqueness.

What a create, an update and a delete do, in the slots of the pipeline design.md 4.1
assigns them. The slot functions of :mod:`erp_mock.domain.engine` call the functions of
this module, so the order the errors of one call are reported in stays with the pipeline:

=====================  ==================================================================
slot                   what this module contributes
=====================  ==================================================================
``references``         :func:`check_input_references`: the record the operation works on
                       and every reference the caller wrote exist, all of them collected
                       into one NOT_FOUND (requirement 5.6)
``pushed_lines``       :func:`check_lines_editable`: an update does not replace the lines
                       of a bill a push generated (requirement 5.4)
``candidate``          :func:`build_create_candidate`, :func:`build_update_candidate` and
                       :func:`build_push_candidate`: the record to store - the caller's
                       values, the defaults, the identifiers, the amounts and the base unit
                       quantities (requirements 5.1 to 5.3, 8.1, 8.2, 4.1 to 4.4, 6.1 to
                       6.4 and 6.6)
``rules``              :func:`check_disabled_references`: R-GEN-006 over the references
                       this call writes (requirement 5.7)
``uniqueness``         :func:`check_uniqueness`: no other record holds a unique value
                       this call writes (requirement 5.8)
``write``              :func:`write_candidate` and :func:`delete_record`: the record and
                       its lines (requirement 5.5)
``view``               :func:`candidate_view`: the object a get of the record returns
                       (requirement 5.16)
=====================  ==================================================================

Defaults are filled in three rounds, in this order (requirement 5.1, design.md 4.2):

1. what the caller wrote, and for a push what the mapping of the conversion brings;
2. ``default``, the constant the pack declared, and ``default_context``, which takes the
   anchor date (``today``), the default organization (``org``) or the current user
   (``user``);
3. ``default_from``, which follows a ref field of the same level to the record it points
   at and takes one field of it, as a line's unit takes the purchase unit of its material.

A field an earlier round filled is left alone, so the caller beats every default and a
constant beats the context: a bill's ``biz_date`` and ``org_id`` fall back to the anchor
date and the default organization exactly where the pack declared no default of its own,
and an entity with ``has_status`` is created enabled (requirement 5.1). An empty value
counts as not filled, which is what "the caller did not fill it" means for a parameter the
caller may send as null.

A push is the one write where that last sentence does not hold: the fields its mapping and
its ``required_line_inputs`` cover take no default at all, and one the mapping brought
empty stays empty (requirement 8.2). :func:`build_push_candidate` is therefore given the
values of each level with those fields already settled - present in the map, their value
empty where the source had none - and rounds 2 and 3 skip every field the map names.

Round 3 runs last and in the order loading step 5 sorted the fields into
(``default_order`` of an entity, ``header_default_order`` and ``line_default_order`` of a
bill, :class:`~erp_mock.config.model.DefaultFrom`): a default read through a ref field
that has a default of its own is applied after that field, and a cycle between them was
rejected at load time, so the order holds every such field of a valid instance.

What the engine writes itself, never the caller: the internal id, an entity's generated
code and a bill's number from :mod:`erp_mock.domain.numbering`, the bill type, the draft
state of a new bill (requirement 7.1), the line numbers and the line ids
(requirement 4.2), the amounts of a pricing bill from :mod:`erp_mock.domain.pricing` and
the ``qty_base`` of a measured line from :mod:`erp_mock.domain.unit_conversion`. The
business date the number of a bill is built from is the one of the candidate, defaults
included (requirement 4.4).

References. Two checks over two different sets, in two different slots:

- the caller's references are checked for existence in the ``references`` slot, before
  anything is built, so a wrong id is reported as NOT_FOUND with the field path it came
  from (requirement 5.6) and before the state of a bill is looked at (requirement 12.1);
- the references the call *writes* - the caller's, those a mapping brought and those a
  default brought - are checked against R-GEN-006 in the ``rules`` slot: none of them may
  point at a disabled piece of basic data (requirement 5.7). A reference a bill already
  holds and this call does not write is not in that set, so it is not checked.

Field paths are those of the parameters the caller sent (design.md data model 6): a field
of the record or of the header by its own name, a field of a line as
``lines[<index>].<name>`` with the index counting from 0, so the line numbered ``n`` is at
``lines[n - 1]`` (requirements 5.6 and 10.7).

An update changes a record the sandbox holds (requirements 5.2 and 5.3): the fields the
caller named take its values and every other field stays as it is, so no default is filled
a second time and the number of a bill does not follow a changed business date
(requirement 4.4). ``lines`` replaces the lines of a bill as a whole - the line numbers run
from 1 again and the line ids are built from them (requirements 5.3 and 4.2), the new lines
taking the defaults of a create - while an update that carries no lines leaves the stored
ones in place. The computed fields are recomputed on the merged bill afterwards, the
``qty_base`` of every line and the amounts included, so an update of the header alone moves
the amounts of every line: a different ``price_includes_tax`` splits the same total
differently (requirements 5.2, 6.1 and 6.6).

A candidate therefore says two things: what the record holds after the write
(:attr:`Candidate.values`) and what this call writes into it (:attr:`Candidate.changes`).
The second is the set R-GEN-006 and the uniqueness check look at, so a reference or a
unique value the record already held and this call does not write is none of their business
(requirements 5.7 and 5.8).

Uniqueness is the last check before the write (requirement 12.11), so a call that breaks a
business rule and a unique field at once reports the rule. A value is compared with the
same field of the other records of its own table exactly as it is written - the comparison
is case sensitive - and an empty value takes no part in it (requirement 5.8).

A delete removes the record (requirement 5.5): a bill goes with every one of its lines, so
a get of its id afterwards ends in NOT_FOUND and no search lists it again. Requirement 5
does not state the delete of an entity record; the design settles it so that the references
of a sandbox always point at a record that is there (design.md 4.2): a record another
record refers to ends the delete in CONFLICT with the reason ``referenced``, and every
other record is removed.

What is not here: the state a bill must be in for an update or a delete, which R-GEN-001
decides in the ``bill_status`` slot before any of this runs (requirements 7.3 and 12.1),
and what a push reads and maps before :func:`build_push_candidate` builds its draft, which
is the conversion primitive's (:mod:`erp_mock.domain.conversion`).
"""

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import BillModel, DomainModel, EntityModel, FieldModel
from erp_mock.domain.numbering import FIRST_LINE_NO, line_id, next_code, next_id, next_number
from erp_mock.domain.pricing import (
    PRICE_INCLUDES_TAX,
    LineInput,
    default_price_includes_tax,
    price_bill,
)
from erp_mock.domain.unit_conversion import assign_qty_base
from erp_mock.domain.views import (
    LINES,
    RecordView,
    bill_lines,
    default_org,
    record_view,
    referenced,
)
from erp_mock.state.schema import BILL_ID_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "DISABLED_REASON",
    "DISABLED_RULE",
    "DUPLICATE_REASON",
    "LINES_NOT_EDITABLE_REASON",
    "NOT_FOUND_REASON",
    "REFERENCED_REASON",
    "Arguments",
    "Candidate",
    "Reference",
    "Referring",
    "UniqueValue",
    "build_create_candidate",
    "build_push_candidate",
    "build_update_candidate",
    "candidate_view",
    "check_disabled_references",
    "check_input_references",
    "check_lines_editable",
    "check_not_referenced",
    "check_uniqueness",
    "delete_record",
    "disabled_references",
    "field_path",
    "input_references",
    "level_references",
    "line_path",
    "missing_references",
    "not_found",
    "own_fields",
    "referring_record",
    "target_reference",
    "unique_conflicts",
    "unique_values",
    "write_candidate",
    "written_references",
]

# The arguments of one level: the values the caller sent for the record, for the header or
# for one line, already typed and rounded by the tools layer (design.md 5.2).
type Arguments = Mapping[str, Value]

# The reasons of design.md data model 6 this module reports: a reference that is not there
# (requirement 5.6), one that points at disabled basic data (5.7), the lines of a bill a
# push generated (5.4), a unique value another record holds (5.8), and - the one reason the
# table does not list, since requirement 5 does not state the delete of an entity record -
# a record another record refers to (design.md 4.2).
NOT_FOUND_REASON: Final = "not_found"
DISABLED_REASON: Final = "disabled_reference"
LINES_NOT_EDITABLE_REASON: Final = "lines_not_editable"
DUPLICATE_REASON: Final = "duplicate"
REFERENCED_REASON: Final = "referenced"

# The engine rule that keeps disabled basic data out of what a call writes (req 5.7).
DISABLED_RULE: Final = "R-GEN-006"

# The key a detail item names its input field under (design.md data model 6).
_FIELD_KEY: Final = "field"
_REASON_KEY: Final = "reason"

# The standard fields this module writes or reads by name (meta-model.md section 4): the
# generated code of an entity, the type, the state, the number and the business date of a
# bill header, the line number of a line, and the bill a pushed header comes from.
_CODE: Final = "code"
_BILL_TYPE: Final = "bill_type"
_STATUS: Final = "status"
_NUMBER: Final = "number"
_BIZ_DATE: Final = "biz_date"
_LINE_NO: Final = "line_no"
_SOURCE_BILL_TYPE: Final = "source_bill_type"
_SOURCE_BILL_ID: Final = "source_bill_id"

# The three fields of a pricing line the amounts are computed from (requirement 6.1).
_QTY: Final = "qty"
_PRICE: Final = "price"
_TAX_RATE: Final = "tax_rate"

# The state every new bill starts in, the first state of both preset state machines
# (requirement 7.1).
_DRAFT: Final = "draft"
# The value the status of a disabled piece of basic data holds (requirement 5.7).
_DISABLED: Final = "disabled"


@dataclass(frozen=True, slots=True)
class Reference:
    """One reference a call names: where it came from and what it points at.

    ``path`` is the field path of the parameter the value came from (:func:`field_path`,
    :func:`line_path`), ``target`` the entity or bill the field refers to - None where
    loading step 5 could not resolve it - and ``id`` the value itself.
    """

    path: str
    target: str | None
    id: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """The record a write operation will store, with its lines (design.md 4.1 step 5).

    ``values`` are the values of the record itself - of the header for a bill - after the
    write, and ``lines`` those of its lines in line number order, each already carrying its
    id, its line number and the internal column that names its bill. Every key is a column
    of the table of its level, so the values reach storage as they are.

    ``changes`` are the fields this call writes into a record the sandbox already holds -
    the caller's own values and the computed fields that were recomputed with them - and
    None for a record this call creates, which writes every value it carries;
    :attr:`written` is the one of the two that applies. ``writes_lines`` says whether the
    call writes the lines of the bill: true for a create and for an update that replaces
    them, false for an update that leaves them in place (requirements 5.3, 5.7 and 5.8).
    """

    item: EntityModel | BillModel
    values: FrozenMap[str, Value]
    lines: tuple[FrozenMap[str, Value], ...] = ()
    changes: FrozenMap[str, Value] | None = None
    writes_lines: bool = True

    @property
    def id(self) -> str:
        """The internal id of the record, which a candidate always carries."""
        return _id_of(self.values)

    @property
    def bill(self) -> BillModel | None:
        """The bill the candidate belongs to, None for an entity record."""
        return self.item if isinstance(self.item, BillModel) else None

    @property
    def is_new(self) -> bool:
        """Whether the record is one this call creates rather than one it changes."""
        return self.changes is None

    @property
    def written(self) -> Mapping[str, Value]:
        """What this call writes into the record: every value of a new one (5.7, 5.8)."""
        return self.values if self.changes is None else self.changes


@dataclass(frozen=True, slots=True)
class Referring:
    """One record that refers to another one: where the reference is (design.md 4.2).

    ``table`` is the table the referring record is in - an entity, a bill or the lines of a
    bill - ``id`` its internal id and ``field`` the ref field the reference stands in.
    """

    table: str
    id: str
    field: str


@dataclass(frozen=True, slots=True)
class UniqueValue:
    """One value of a unique field a call writes (requirement 5.8).

    ``path`` is the field path of the parameter it came from (:func:`field_path`,
    :func:`line_path`), ``table`` the table of its level, ``field`` the column it is
    compared in and ``value`` the value itself, which is never empty.
    """

    path: str
    table: str
    field: str
    value: Value


# --- field paths (design.md data model 6) ------------------------------------------------


def field_path(name: str) -> str:
    """The path of a field of the record or of the header: its own name (req 5.6)."""
    return name


def line_path(index: int, name: str) -> str:
    """The path of ``name`` in the line at ``index``, counting from 0 (requirement 5.6)."""
    return f"{LINES}[{index}].{name}"


def own_fields(item: EntityModel | BillModel) -> FrozenMap[str, FieldModel]:
    """The field view of the record itself: an entity's fields, a bill's header."""
    return item.header if isinstance(item, BillModel) else item.fields


# --- the references of one call (requirements 5.6 and 5.7) -------------------------------


def target_reference(item: EntityModel | BillModel, record_id: str) -> Reference:
    """The reference to the record an operation works on (requirements 5.6 and 11.4).

    Its path is the parameter the id came from, which for the record an update, a delete, a
    get or a state operation works on is ``id``.
    """
    return Reference(path=field_path(ID_COLUMN), target=item.name, id=record_id)


def input_references(
    item: EntityModel | BillModel,
    values: Arguments,
    lines: Sequence[Arguments] | None = None,
    *,
    record_id: str | None = None,
) -> tuple[Reference, ...]:
    """The references the caller named, in the order their paths are reported (req 5.6).

    ``record_id`` is the record the operation works on, whose own existence comes first
    (:func:`target_reference`); None for a create, which works on no existing record. The
    ref fields of the record or of the header follow, in field view order, then those of
    each line in line order; a field the caller left out or sent empty brings no reference.
    Only the fields of the level are read, so a key that names none is ignored here and
    reported by the candidate builder.
    """
    found = [] if record_id is None else [target_reference(item, record_id)]
    found.extend(level_references(own_fields(item), values, field_path))
    if isinstance(item, BillModel):
        for index, line in enumerate(lines or ()):
            found.extend(level_references(item.lines, line, _line_paths(index)))
    return tuple(found)


def written_references(candidate: Candidate) -> tuple[Reference, ...]:
    """The references ``candidate`` writes, in path order (requirement 5.7).

    Every ref field of the record or of the header this call writes, then those of each
    line where the call writes the lines: the caller's references, those a mapping brought
    and those a default brought, which together are the set R-GEN-006 looks at. A reference
    a record already held and this call does not write is not among them. The
    ``source_bill_id`` of a pushed header refers to the bill its own ``source_bill_type``
    names.
    """
    item = candidate.item
    bill = candidate.bill
    found = list(
        level_references(own_fields(item), candidate.written, field_path, candidate.values)
    )
    if bill is not None and candidate.writes_lines:
        for index, line in enumerate(candidate.lines):
            found.extend(level_references(bill.lines, line, _line_paths(index), line))
    return tuple(found)


def missing_references(
    uow: UnitOfWork, domain: DomainModel, references: Sequence[Reference]
) -> tuple[Reference, ...]:
    """The references of ``references`` that no record answers (requirement 5.6).

    A deleted record and a record of another type count as absent, which is what reading
    the id in the table of the declared target amounts to; a reference whose target the
    instance does not have is absent as well.
    """
    return tuple(ref for ref in references if _record_of(uow, domain, ref) is None)


def disabled_references(
    uow: UnitOfWork, domain: DomainModel, references: Sequence[Reference]
) -> tuple[Reference, ...]:
    """The references of ``references`` that point at disabled basic data (req 5.7).

    Only an entity whose ``has_status`` is true can be disabled, so a reference to a bill,
    to an entity without a status or to a record that is not there is none of them.
    """
    return tuple(ref for ref in references if _is_disabled(uow, domain, ref))


def check_input_references(
    uow: UnitOfWork,
    domain: DomainModel,
    item: EntityModel | BillModel,
    values: Arguments,
    lines: Sequence[Arguments] | None = None,
    *,
    record_id: str | None = None,
) -> None:
    """Step 1 of a write: the record and the caller's references exist (req 5.6).

    ``record_id`` is the record the operation works on, which an update and a delete name
    and a create does not. Raises one NOT_FOUND for all of them at once, its details
    carrying the field path of each (:func:`not_found`).
    """
    named = input_references(item, values, lines, record_id=record_id)
    missing = missing_references(uow, domain, named)
    if missing:
        raise not_found(domain, missing)


def check_disabled_references(uow: UnitOfWork, domain: DomainModel, candidate: Candidate) -> None:
    """Step 7 of a write: R-GEN-006 over the references it writes (requirement 5.7).

    Raises BUSINESS_RULE_VIOLATION with the rule id R-GEN-006 and one detail item per
    reference that points at disabled basic data, each naming its field path.
    """
    disabled = disabled_references(uow, domain, written_references(candidate))
    if not disabled:
        return
    first = disabled[0]
    message = (
        f"{first.path} 引用的{_label(domain, first.target)}记录 {first.id} 已停用，本次写入不能引用"
    )
    raise ErpError(
        ErrorCode.BUSINESS_RULE_VIOLATION,
        _with_rest(message, len(disabled), "处引用已停用的记录"),
        rule_id=DISABLED_RULE,
        details=_details(_paths(disabled), DISABLED_REASON),
    )


def not_found(domain: DomainModel, missing: Sequence[Reference]) -> ErpError:
    """The NOT_FOUND of the references of ``missing`` (requirement 5.6).

    One detail item per reference, in the order they were collected, each naming its field
    path; the message names the first path, its id and the Chinese label of what it refers
    to, so it carries the path of a detail item (requirement 13.7). Raises ValueError for
    an empty ``missing``: an error says what was not there.
    """
    if not missing:
        raise ValueError("a NOT_FOUND needs at least one reference that is not there")
    first = missing[0]
    message = f"找不到 {first.path} 为 {first.id} 的{_label(domain, first.target)}记录"
    return ErpError(
        ErrorCode.NOT_FOUND,
        _with_rest(message, len(missing), "处引用不存在"),
        details=_details(_paths(missing), NOT_FOUND_REASON),
    )


# --- the lines of a pushed bill (requirement 5.4) ----------------------------------------


def check_lines_editable(bill: BillModel, record: Record) -> None:
    """Step 4 of an update that carries lines: no push generated the bill (req 5.4).

    The lines of a bill a push generated answer to the source lines they come from, so an
    update does not replace them: such a call ends in VALIDATION_FAILED, which no rule
    decides (requirement 13.8), and its one detail item names the ``lines`` parameter. A
    bill whose ``source_bill_id`` is empty is the caller's own and goes through.
    """
    source = record.get(_SOURCE_BILL_ID)
    if not isinstance(source, str):
        return
    raise ErpError(
        ErrorCode.VALIDATION_FAILED,
        f"{_item_label(bill)} {_id_of(record)} 由 {source} 下推生成，不能用 {LINES} 替换其分录",
        details=_details((LINES,), LINES_NOT_EDITABLE_REASON),
    )


# --- building the record of a write (requirements 5.1 to 5.3, 4.1 to 4.4 and 6) ----------


def build_create_candidate(
    uow: UnitOfWork,
    domain: DomainModel,
    item: EntityModel | BillModel,
    values: Arguments,
    lines: Sequence[Arguments] | None = None,
    *,
    anchor_date: date,
    current_user_id: str | None = None,
) -> Candidate:
    """Step 5 of a create: the record to store (requirements 5.1, 4.1 to 4.4 and 6).

    The three rounds of defaults run per level (see the module docstring), then the
    identifiers and the computed fields are assigned: the internal id of the record, an
    entity's generated code, a bill's type, draft state and number, the line numbers and
    line ids, the ``qty_base`` of a measured line and, for a pricing bill, the amounts of
    every line and the sums of the header. The counters are raised in the transaction of
    the operation, so a later step that fails returns them to where they were and the next
    successful create gets the same id (requirement 4.12).

    Raises KeyError for an argument that names no writable field of its level and for
    lines on an entity, neither of which a generated tool can send.
    """
    defaults = _Defaults(uow=uow, domain=domain, anchor_date=anchor_date, user_id=current_user_id)
    if not isinstance(item, BillModel):
        if lines:
            raise KeyError(f"the entity {item.name} has no {LINES}")
        record = defaults.level(item.fields, item.default_order, values, item.name)
        record[ID_COLUMN] = next_id(uow, item)
        code = next_code(uow, item)
        if code is not None:
            record[_CODE] = code
        return Candidate(item=item, values=FrozenMap(record))
    header = defaults.level(item.header, item.header_default_order, values, item.name)
    line_values = [
        defaults.level(item.lines, item.line_default_order, line, item.line_name)
        for line in lines or ()
    ]
    return _new_bill(uow, domain, item, header, line_values, anchor_date=anchor_date)


def build_push_candidate(
    uow: UnitOfWork,
    domain: DomainModel,
    bill: BillModel,
    values: Arguments,
    lines: Sequence[Arguments] = (),
    *,
    anchor_date: date,
    current_user_id: str | None = None,
) -> Candidate:
    """Step 5 of a push: the target bill draft to store (requirements 8.1, 8.2 and 6).

    ``values`` and ``lines`` are what the conversion primitive settled for the header and
    for each line: the fields the mapping of the conversion brings, the
    ``required_line_inputs`` of the caller, and the fields the push writes itself - the
    source bill type, the source bill, the source line of each line and the internal column
    that names the conversion (:mod:`erp_mock.domain.conversion`). Every key of them names
    a column of its level, a read-only standard field included, and a key whose value is
    empty is a field the mapping or the inputs cover and the source left empty.

    Rounds 2 and 3 of the defaults then run over the fields those maps do *not* name, so a
    covered field keeps the value it was given, an empty one included (requirement 8.2),
    and the rest of the draft is built exactly as a create builds it: the internal id, the
    bill type, the draft state, the number from the business date of the candidate, the
    line numbers and line ids, the base unit quantities and the amounts. A required field
    that is left empty does not stop the push; R-GEN-002 asks for it at submit
    (requirements 8.2 and 7.5).
    """
    defaults = _Defaults(uow=uow, domain=domain, anchor_date=anchor_date, user_id=current_user_id)
    header = defaults.fill(bill.header, bill.header_default_order, dict(values))
    line_values = [defaults.fill(bill.lines, bill.line_default_order, dict(line)) for line in lines]
    return _new_bill(uow, domain, bill, header, line_values, anchor_date=anchor_date)


def build_update_candidate(
    uow: UnitOfWork,
    domain: DomainModel,
    item: EntityModel | BillModel,
    record: Record,
    values: Arguments,
    lines: Sequence[Arguments] | None = None,
    *,
    anchor_date: date,
    current_user_id: str | None = None,
) -> Candidate:
    """Step 5 of an update: the record after the change (requirements 5.2 and 5.3).

    ``record`` is the stored record the update changes, which the ``references`` slot read.
    The caller's values take the fields they name and every other field of the record stays
    as it is, defaults included: nothing is filled a second time, and the number of a bill
    stays what it was even where the business date changes (requirements 5.2 and 4.4). A
    value the caller sent empty counts as not sent, as it does for a create, and leaves the
    field at its value.

    ``lines`` replaces the lines of the bill as a whole: the lines of the result answer to
    the array one by one, their numbers run from 1 again and their ids are built from those
    numbers, and each of them takes the defaults of a create (requirements 5.3 and 4.2).
    None leaves the stored lines in place. Either way the computed fields are recomputed on
    the merged bill - the ``qty_base`` of every line and the amounts - so an update of the
    header alone moves the amounts of every line (requirements 5.2 and 6.6).

    Raises KeyError for an argument that names no writable field of its level and for
    lines on an entity, neither of which a generated tool can send.
    """
    merged = dict(record)
    changes = _from_input(own_fields(item), values, item.name)
    merged.update(changes)
    if not isinstance(item, BillModel):
        if lines:
            raise KeyError(f"the entity {item.name} has no {LINES}")
        return Candidate(item=item, values=FrozenMap(merged), changes=FrozenMap(changes))
    bill_id = _id_of(record)
    if lines is None:
        line_values = [dict(line) for line in bill_lines(uow, item, bill_id)]
    else:
        defaults = _Defaults(
            uow=uow, domain=domain, anchor_date=anchor_date, user_id=current_user_id
        )
        line_values = _number_lines(
            item,
            bill_id,
            [
                defaults.level(item.lines, item.line_default_order, line, item.line_name)
                for line in lines
            ],
        )
    assign_qty_base(uow, domain, item, line_values)
    if item.pricing:
        changes.update(_assign_amounts(item, merged, line_values))
    return Candidate(
        item=item,
        values=FrozenMap(merged),
        lines=tuple(FrozenMap(line) for line in line_values),
        changes=FrozenMap(changes),
        writes_lines=lines is not None,
    )


def write_candidate(uow: UnitOfWork, candidate: Candidate) -> None:
    """Step 9 of a create or an update: the record and, for a bill, its lines.

    A new record is inserted with every value it carries; a column it does not carry - the
    internal columns of a bill, a computed field no primitive has filled - is stored as
    NULL (requirement 18.5). An update writes the fields the call changes
    (:attr:`Candidate.changes`) and leaves every other column of the row as it is.

    The lines of a bill are written as a whole: the stored ones go and the lines of the
    candidate take their place. That is what requirement 5.3 asks of an update that carries
    lines, and it carries the recomputed amounts of one that carries none; a line whose
    values did not change is still not part of what the call wrote, because the net changes
    compare the row the transaction started with against the row it ends with
    (requirement 17.4).
    """
    if candidate.is_new:
        uow.insert(candidate.item.name, candidate.values)
    else:
        uow.update(candidate.item.name, candidate.id, candidate.written)
    bill = candidate.bill
    if bill is None:
        return
    if not candidate.is_new:
        for stored in bill_lines(uow, bill, candidate.id):
            uow.delete(bill.line_name, _id_of(stored))
    for line in candidate.lines:
        uow.insert(bill.line_name, line)


def candidate_view(uow: UnitOfWork, domain: DomainModel, candidate: Candidate) -> RecordView:
    """Step 10 of a write: the object a get of the record returns (requirement 5.16).

    Read back from the sandbox after the write and after the operation sequence numbers
    were stamped, so a create and an update return exactly what a get right after them
    returns, the values as storage holds them included.
    """
    record = uow.get(candidate.item.name, candidate.id)
    if record is None:
        raise RuntimeError(f"the write did not store {candidate.item.name} {candidate.id}")
    return record_view(uow, domain, candidate.item, record)


# --- uniqueness (requirement 5.8) --------------------------------------------------------


def unique_values(candidate: Candidate) -> tuple[UniqueValue, ...]:
    """The values of unique fields ``candidate`` writes, in path order (req 5.8).

    The unique fields of the record or of the header this call writes, in field view order,
    then those of each line where the call writes the lines. An empty value takes no part
    in the comparison, so none is among them.
    """
    item = candidate.item
    bill = candidate.bill
    found = list(_level_unique(own_fields(item), candidate.written, item.name, field_path))
    if bill is not None and candidate.writes_lines:
        for index, line in enumerate(candidate.lines):
            found.extend(_level_unique(bill.lines, line, bill.line_name, _line_paths(index)))
    return tuple(found)


def unique_conflicts(uow: UnitOfWork, candidate: Candidate) -> tuple[UniqueValue, ...]:
    """The values of :func:`unique_values` another record holds too (requirement 5.8).

    A value is compared with the same field of the other records of its own table, exactly
    as it is written, so the comparison is case sensitive. The records this write replaces
    are not among the others: the record an update changes itself, and every stored line of
    a bill whose lines the call writes as a whole. Two lines of one call that carry the same
    value conflict with each other, the later of the two being reported.
    """
    conflicts: list[UniqueValue] = []
    seen: set[tuple[str, str, Value]] = set()
    for unique in unique_values(candidate):
        key = (unique.table, unique.field, unique.value)
        if key in seen or _value_taken(uow, candidate, unique):
            conflicts.append(unique)
        seen.add(key)
    return tuple(conflicts)


def check_uniqueness(uow: UnitOfWork, candidate: Candidate) -> None:
    """Step 8 of a write: no other record holds a value it writes (requirement 5.8).

    Raises CONFLICT, which no rule decides (requirement 13.8), with one detail item per
    colliding field in path order; the message names the path of the first of them
    (requirement 13.7).
    """
    conflicts = unique_conflicts(uow, candidate)
    if not conflicts:
        return
    first = conflicts[0]
    message = f"{first.path} 的值 {first.value} 与现有的{_item_label(candidate.item)}记录重复"
    raise ErpError(
        ErrorCode.CONFLICT,
        _with_rest(message, len(conflicts), "处值重复"),
        details=_details(_paths(conflicts), DUPLICATE_REASON),
    )


# --- deleting a record (requirement 5.5, design.md 4.2) ----------------------------------


def referring_record(
    uow: UnitOfWork, domain: DomainModel, entity: EntityModel, record_id: str
) -> Referring | None:
    """The first record that refers to the record ``record_id`` of ``entity``, or None.

    Every ref field that refers to ``entity`` is looked at, in the order of the domain
    model: the fields of each entity, then the header and the lines of each bill. The
    record itself is not among the answers, since a reference it holds goes with it.
    """
    for table, fields in _record_levels(domain):
        for declared in fields.values():
            if declared.type != "ref" or declared.ref != entity.name:
                continue
            for record in uow.select(table, where={declared.name: record_id}):
                found = _id_of(record)
                if table == entity.name and found == record_id:
                    continue
                return Referring(table=table, id=found, field=declared.name)
    return None


def check_not_referenced(
    uow: UnitOfWork, domain: DomainModel, entity: EntityModel, record_id: str
) -> None:
    """A record another record refers to is not deleted (design.md 4.2).

    Raises CONFLICT with the reason ``referenced`` and one detail item naming the ``id``
    parameter the record came from; the message says which record holds the reference.
    """
    referring = referring_record(uow, domain, entity, record_id)
    if referring is None:
        return
    path = field_path(ID_COLUMN)
    raise ErpError(
        ErrorCode.CONFLICT,
        f"{path} 为 {record_id} 的{_item_label(entity)}记录被 {referring.table}"
        f" {referring.id} 的 {referring.field} 引用，不能删除",
        details=_details((path,), REFERENCED_REASON),
    )


def delete_record(
    uow: UnitOfWork, domain: DomainModel, item: EntityModel | BillModel, record: Record
) -> None:
    """Step 9 of a delete: the record and, for a bill, its lines (requirement 5.5).

    A bill is removed with every one of its lines, so a get of its id afterwards ends in
    NOT_FOUND and no search lists it again; the id and the number it used are not handed
    out a second time (requirements 4.1 and 4.4). An entity record is removed where no
    other record refers to it (:func:`check_not_referenced`).
    """
    record_id = _id_of(record)
    if isinstance(item, BillModel):
        for line in bill_lines(uow, item, record_id):
            uow.delete(item.line_name, _id_of(line))
    else:
        check_not_referenced(uow, domain, item, record_id)
    uow.delete(item.name, record_id)


# --- the three rounds of defaults (requirement 5.1) --------------------------------------


@dataclass(slots=True)
class _Defaults:
    """What the defaults of one call are read from: the sandbox and the business context.

    One instance per operation. The default organization is read once and kept, so every
    level of one call gets the same ``org`` (requirement 1.8).
    """

    uow: UnitOfWork
    domain: DomainModel
    anchor_date: date
    user_id: str | None
    _org_id: str | None = None
    _org_read: bool = False

    def level(
        self,
        fields: Mapping[str, FieldModel],
        default_order: Sequence[str],
        values: Arguments,
        owner: str,
    ) -> dict[str, Value]:
        """The values of one level after the three rounds of defaults (requirement 5.1).

        ``owner`` is the name the level is stored under, which an error about an argument
        that names no field of it reports. A value the caller sent empty counts as not
        sent, so it takes a default like a value the caller left out.
        """
        return self.fill(fields, default_order, _from_input(fields, values, owner))

    def fill(
        self,
        fields: Mapping[str, FieldModel],
        default_order: Sequence[str],
        filled: dict[str, Value],
    ) -> dict[str, Value]:
        """Rounds 2 and 3 over ``filled``, whose keys round 1 settled (req 5.1 and 8.2).

        Every field ``filled`` names is left exactly as it is, an empty value included: for
        a create those are the values the caller sent, for a push the fields the mapping and
        the ``required_line_inputs`` of the conversion cover, which take no default even
        where the source left them empty (requirement 8.2). Fills and returns ``filled``.
        """
        self._apply_constants(fields, filled)
        self._apply_default_from(fields, default_order, filled)
        return filled

    def org_id(self) -> str | None:
        """The id of the default organization, None where the sandbox holds none (1.8)."""
        if not self._org_read:
            entity = self.domain.org_entity
            record = None if entity is None else default_org(self.uow, entity)
            self._org_id = None if record is None else _id_of(record)
            self._org_read = True
        return self._org_id

    def _apply_constants(self, fields: Mapping[str, FieldModel], filled: dict[str, Value]) -> None:
        """Round 2: the declared constant, then the business context (requirement 5.1).

        A field that carries both keeps the constant, so the engine's own ``today`` and
        ``org`` apply exactly where the pack declared no default of its own.
        """
        for name, declared in fields.items():
            if name in filled:
                continue
            value = _constant(declared)
            if value is None:
                value = self._context_value(declared)
            if value is not None:
                filled[name] = value

    def _apply_default_from(
        self,
        fields: Mapping[str, FieldModel],
        default_order: Sequence[str],
        filled: dict[str, Value],
    ) -> None:
        """Round 3: a field of the referenced record, in ``default_order`` (requirement 5.1).

        A reference that is empty or points at a record that is not there leaves the field
        empty, as does an empty value in the referenced record.
        """
        for name in default_order:
            source = fields[name].default_from
            if source is None or name in filled:
                continue
            reference = filled.get(source.ref_field)
            if not isinstance(reference, str):
                continue
            record = self.uow.get(source.entity, reference)
            value = None if record is None else record.get(source.field)
            if value is not None:
                filled[name] = value

    def _context_value(self, declared: FieldModel) -> Value:
        """The ``default_context`` of ``declared``: the date, the org or the user (5.1)."""
        match declared.default_context:
            case "today":
                return self.anchor_date
            case "org":
                return self.org_id()
            case "user":
                return self.user_id
            case None:
                return None


def _from_input(
    fields: Mapping[str, FieldModel], values: Arguments, owner: str
) -> dict[str, Value]:
    """Round 1: what the caller wrote, every field it left empty still missing.

    Raises KeyError for a key that names no writable field of the level: a generated tool
    offers a parameter for the writable fields alone (requirement 1.3).
    """
    unknown = sorted(name for name in values if not _is_writable(fields.get(name)))
    if unknown:
        raise KeyError(f"{owner} has no writable field named {', '.join(unknown)}")
    return {name: value for name, value in values.items() if value is not None}


def _is_writable(declared: FieldModel | None) -> bool:
    """Whether ``declared`` is a field a write tool may write (requirement 1.3)."""
    return declared is not None and declared.writable


def _constant(declared: FieldModel) -> Value:
    """The declared ``default`` of ``declared`` as a typed value, None where it has none.

    Loading step 5 checked the constant against the type of the field, so a value of
    another Python type is a programming error and raises TypeError.
    """
    written = declared.default
    if written is None:
        return None
    match declared.type:
        case "date":
            return date.fromisoformat(_text(declared, written))
        case "qty" | "price" | "money" | "rate":
            return Decimal(_text(declared, written))
        case "int":
            if not isinstance(written, int) or isinstance(written, bool):
                raise TypeError(_wrong_default(declared, "int"))
            return written
        case "bool":
            if not isinstance(written, bool):
                raise TypeError(_wrong_default(declared, "bool"))
            return written
        case _:
            return _text(declared, written)


# --- the identifiers and the amounts the engine writes itself ----------------------------


def _assign_bill_fields(
    uow: UnitOfWork, bill: BillModel, header: dict[str, Value], anchor_date: date
) -> None:
    """The standard header fields of a new bill: its type, its state and its number.

    A new bill is a draft (requirement 7.1) and its number is built from the business date
    of the candidate, which the defaults have filled; a bill left without one takes the
    anchor date for it, the default requirement 5.1 gives that field.
    """
    header[_BILL_TYPE] = bill.name
    header[_STATUS] = _DRAFT
    biz_date = header.get(_BIZ_DATE)
    header[_NUMBER] = next_number(
        uow, bill, biz_date if isinstance(biz_date, date) else anchor_date
    )


def _new_bill(
    uow: UnitOfWork,
    domain: DomainModel,
    bill: BillModel,
    header: dict[str, Value],
    lines: list[dict[str, Value]],
    *,
    anchor_date: date,
) -> Candidate:
    """The candidate of a bill this call creates (requirements 4.1 to 4.4, 6.1 and 6.6).

    ``header`` and ``lines`` are the values of the two levels after their defaults; what is
    added here is what the engine writes itself and no caller and no mapping reaches: the
    internal id, the bill type, the draft state and the number of the header, the numbers
    and ids of the lines, the base unit quantity of every measured line and, for a pricing
    bill, the amounts of the lines and the sums of the header. A create and a push both end
    here, so a pushed draft is numbered and computed exactly like a created one
    (requirements 5.1 and 8.1).
    """
    bill_id = next_id(uow, bill)
    header[ID_COLUMN] = bill_id
    _assign_bill_fields(uow, bill, header, anchor_date)
    _number_lines(bill, bill_id, lines)
    assign_qty_base(uow, domain, bill, lines)
    if bill.pricing:
        _assign_amounts(bill, header, lines)
    return Candidate(
        item=bill, values=FrozenMap(header), lines=tuple(FrozenMap(line) for line in lines)
    )


def _number_lines(
    bill: BillModel, bill_id: str, lines: list[dict[str, Value]]
) -> list[dict[str, Value]]:
    """The numbers and ids of the lines a write stores, in place (requirements 5.3, 4.2).

    The lines answer to the array of the call one by one and in the same order, their
    numbers run from :data:`~erp_mock.domain.numbering.FIRST_LINE_NO` without a gap and
    their ids are built from the bill id and those numbers; each of them carries the
    internal column that names its bill. What a create and a push assign, and an update
    that replaces the lines of a bill as a whole (requirement 5.3).
    """
    for index, line in enumerate(lines):
        line_no = FIRST_LINE_NO + index
        line[ID_COLUMN] = line_id(bill_id, line_no)
        line[_LINE_NO] = line_no
        line[BILL_ID_COLUMN] = bill_id
    return lines


def _assign_amounts(
    bill: BillModel, header: dict[str, Value], lines: Sequence[dict[str, Value]]
) -> dict[str, Value]:
    """The amounts of a pricing bill: every line, then the header sums (req 6.1 to 6.4).

    ``price_includes_tax`` is the value the header carries - what the rounds of a create
    filled or what an update merged - and the default of requirement 6.3 where there is
    none. Returns the header fields it wrote, which an update counts among its changes.
    """
    written: dict[str, Value] = {}
    flag = header.get(PRICE_INCLUDES_TAX)
    if not isinstance(flag, bool):
        flag = default_price_includes_tax(bill)
        header[PRICE_INCLUDES_TAX] = flag
        written[PRICE_INCLUDES_TAX] = flag
    amounts = price_bill((_line_input(line) for line in lines), price_includes_tax=flag)
    for line, priced in zip(lines, amounts.lines, strict=True):
        line.update(priced.values())
    header.update(amounts.values())
    written.update(amounts.values())
    return written


def _line_input(line: Arguments) -> LineInput:
    """The quantity, the price and the tax rate of one line of a pricing bill (req 6.1)."""
    return LineInput(
        qty=_decimal(line.get(_QTY)),
        price=_decimal(line.get(_PRICE)),
        tax_rate=_decimal(line.get(_TAX_RATE)),
    )


# --- internals ---------------------------------------------------------------------------


def _line_paths(index: int) -> Callable[[str], str]:
    """The path builder of the line at ``index`` (requirement 5.6)."""
    return lambda name: line_path(index, name)


def level_references(
    fields: Mapping[str, FieldModel],
    values: Arguments,
    path: Callable[[str], str],
    record: Arguments | None = None,
) -> Iterator[Reference]:
    """The references ``values`` holds, in field view order, their paths from ``path``.

    ``record`` is the record the values belong to, which a header's ``source_bill_id``
    takes its target from; None where no such record is at hand, which leaves that one
    reference without a target. The field view of the level decides both which values count
    as references and the order they are reported in, so a caller that reports one level at
    a time - the lines of a push, one by one - gets the same order as one that reports a
    whole record (requirement 5.6).
    """
    for name, declared in fields.items():
        value = values.get(name)
        if declared.type != "ref" or not isinstance(value, str):
            continue
        target = declared.ref if declared.ref is not None else _source_bill_type(record)
        yield Reference(path=path(name), target=target, id=value)


def _level_unique(
    fields: Mapping[str, FieldModel],
    values: Mapping[str, Value],
    table: str,
    path: Callable[[str], str],
) -> Iterator[UniqueValue]:
    """The unique values ``values`` holds, in field view order (requirement 5.8).

    ``table`` is the table the value is compared in and ``path`` builds the field path of
    its level; an empty value takes no part in the comparison and is left out.
    """
    for name, declared in fields.items():
        value = values.get(name)
        if declared.unique and value is not None:
            yield UniqueValue(path=path(name), table=table, field=name, value=value)


def _value_taken(uow: UnitOfWork, candidate: Candidate, unique: UniqueValue) -> bool:
    """Whether a record this write does not replace holds ``unique`` (requirement 5.8)."""
    stored = uow.select(unique.table, where={unique.field: unique.value})
    return any(not _replaced(candidate, unique.table, record) for record in stored)


def _replaced(candidate: Candidate, table: str, record: Record) -> bool:
    """Whether this write replaces ``record``, whose value is then none of another's.

    The record an update changes, and every line of the bill whose lines it writes as a
    whole: a line the write does not store again is gone when the call ends.
    """
    if table == candidate.item.name:
        return _id_of(record) == candidate.id
    return record.get(BILL_ID_COLUMN) == candidate.id


def _record_levels(domain: DomainModel) -> Iterator[tuple[str, FrozenMap[str, FieldModel]]]:
    """Every table of configured records and its field view, in domain model order.

    The entities, then the header and the lines of each bill. The engine's own stock tables
    are not among them: they hold no configured field, and the inventory primitive keeps
    them (design.md data model 3).
    """
    for entity in domain.entities.values():
        yield entity.name, entity.fields
    for bill in domain.bills.values():
        yield bill.name, bill.header
        yield bill.line_name, bill.lines


def _source_bill_type(record: Arguments | None) -> str | None:
    """The bill a header's ``source_bill_id`` refers to: its own ``source_bill_type``."""
    if record is None:
        return None
    bill_type = record.get(_SOURCE_BILL_TYPE)
    return bill_type if isinstance(bill_type, str) else None


def _record_of(uow: UnitOfWork, domain: DomainModel, ref: Reference) -> Record | None:
    """The record ``ref`` points at, None when it is not there (requirement 5.6)."""
    item = referenced(domain, ref.target)
    return None if item is None else uow.get(item.name, ref.id)


def _is_disabled(uow: UnitOfWork, domain: DomainModel, ref: Reference) -> bool:
    """Whether ``ref`` points at a disabled piece of basic data (requirement 5.7)."""
    item = referenced(domain, ref.target)
    if not isinstance(item, EntityModel) or not item.has_status:
        return False
    record = uow.get(item.name, ref.id)
    return record is not None and record.get(_STATUS) == _DISABLED


def _details(paths: Iterable[str], reason: str) -> tuple[Mapping[str, str], ...]:
    """One detail item per field path, the reason after it (design.md data model 6)."""
    return tuple({_FIELD_KEY: path, _REASON_KEY: reason} for path in paths)


def _paths(found: Iterable[Reference | UniqueValue]) -> tuple[str, ...]:
    """The field paths of ``found``, in the order they were collected (requirement 5.6)."""
    return tuple(item.path for item in found)


def _label(domain: DomainModel, target: str | None) -> str:
    """The Chinese label of the entity or bill ``target``, empty where it has none."""
    return _item_label(referenced(domain, target))


def _item_label(item: EntityModel | BillModel | None) -> str:
    """The Chinese label of ``item``, empty where it has none or there is no such item."""
    return "" if item is None or item.label is None else item.label.zh


def _with_rest(message: str, total: int, rest: str) -> str:
    """``message``, saying how many further items of the same kind there are."""
    return message if total < 2 else f"{message}；另有 {total - 1} {rest}"


def _text(declared: FieldModel, written: str | int | bool) -> str:
    """The declared default of ``declared`` as the text its type is written in."""
    if not isinstance(written, str):
        raise TypeError(_wrong_default(declared, "str"))
    return written


def _wrong_default(declared: FieldModel, expected: str) -> str:
    return (
        f"{declared.name} is a {declared.type} field and takes a {expected} default,"
        f" got {type(declared.default).__name__}: {declared.default!r}"
    )


def _decimal(value: Value) -> Decimal | None:
    """``value`` as the decimal of a qty, price or rate field, None when it is empty."""
    return value if isinstance(value, Decimal) else None


def _id_of(record: Arguments) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""
