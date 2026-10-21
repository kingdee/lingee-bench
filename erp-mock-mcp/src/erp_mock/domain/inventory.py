"""The inventory primitive: the stock an approve moves, and the stock on hand it leaves.

A bill whose pack declares an ``inventory`` linkage moves stock when it is approved: one
stock transaction per line and warehouse, and the net change of each (material, warehouse)
on its balance (requirement 9.1). What this module contributes to the slots of the pipeline
(design.md 4.1):

=====================  ==================================================================
slot                   what this module contributes
=====================  ==================================================================
``write``              :func:`run_linkages`: the transactions and the balances of an
                       approve, R-GEN-007 decided before the first of them is written
                       (requirements 9.1, 9.2, 9.4, 9.5, 9.10); :func:`reverse_linkages`:
                       the same transactions removed again on an unapprove, and the
                       balance of a (material, warehouse) with no transaction left removed
                       with them (requirements 9.6 and 9.10)
``view``               :func:`run_balance_query`: the stock on hand the primitive query of
                       an exposure returns, filtered, ordered and paged (9.7 and 9.12)
=====================  ==================================================================

Both writes run in the transaction of the operation, after the state of the bill and before
the writebacks of an approve - an unapprove reverses the two in the opposite order
(requirements 7.6 and 7.7) - so an error of this module rolls back the state of the bill,
the stock and everything else the call wrote (requirement 7.10).

What a linkage produces (requirements 9.1 and 9.2). The ``direction`` decides the warehouse
and the sign of every line, and the quantity is always the base unit quantity of the line,
which the unit_conversion primitive computed when the line was written
(:data:`~erp_mock.domain.unit_conversion.QTY_BASE`, requirement 6.6) and which requirement
9.11 keeps a linkage from naming another field for:

============  =========================================  ============================
direction     warehouse of each transaction              quantity
============  =========================================  ============================
``in``        ``warehouse``                              ``+qty_base``
``out``       ``warehouse``                              ``-qty_base``
``transfer``  ``from_warehouse``, then ``to_warehouse``  ``-qty_base``, ``+qty_base``
``adjust``    ``warehouse``                              ``+`` or ``-`` by the header
============  =========================================  ============================

None of that is named by the engine: the warehouse fields, the enum field of an adjust and
the values that mean an increase come from the linkage
(:class:`~erp_mock.config.model.LinkageModel`), and the material of a line from the quantity
semantics of the bill (:func:`~erp_mock.domain.unit_conversion.resolve_line_quantity`). The
columns of the two stock tables are the engine's own, fixed names (design.md data model 3).

The id of a transaction is ``<source line id>:<warehouse id>:<direction>``, with ``in`` for
the transaction that takes ``+qty_base`` and ``out`` for the one that takes ``-qty_base``
(requirement 18.8), so no counter takes part in it: an unapprove followed by the approve of
an unchanged bill writes exactly the ids it removed, and a diff of the two states shows
nothing. The id of a balance record is ``<material id>:<warehouse id>``.

R-GEN-007 (requirements 9.4 and 9.5). The moves of the whole operation are computed first,
then the net change of each (material, warehouse), and the check compares the balance the
operation started from - 0 where the pair has no record - plus that net change against 0.
Only the sum decides: a line that would take a pair below 0 on its own does not break the
rule when another line of the same bill puts it back, and the order of the lines changes
nothing (requirement 9.9). A violation is BUSINESS_RULE_VIOLATION with the rule id
``R-GEN-007`` and one detail item per pair that would go below 0, by material id and then
warehouse id, each carrying the two ids and the quantity on hand before the operation; no
item names an input field (design.md data model 6).

Which balance records exist (requirement 9.10). A balance record belongs to a (material,
warehouse) that has at least one transaction: an approve creates it with the first
transaction of that pair, even where the net change is 0, and an unapprove removes it with
the last one. A balance of 0 whose pair still has transactions stays.

The stock balance query (requirements 9.7 and 9.12) is the one query of M0 a primitive
provides rather than an exposure: its parameters are the material and the warehouse, both
optional and compared for equality, plus the page; its items carry the material, the
warehouse, the base unit of the material and the quantity on hand in that base unit, by
material id and then warehouse id with the record id as the last key, paged the way a
search is paged (:mod:`erp_mock.domain.search`). A pair without a balance record is not in
the result and a balance of 0 is.

Two sets of names this module repeats rather than imports, because domain must not import
tools (requirement 24.8): the parameters of the balance query, which are the two columns of
a balance record (:data:`BALANCE_PARAMS`, the ``BALANCE_PARAMS`` of
:mod:`erp_mock.tools.input_schema`), and the keys of one item of its result, which are those
two columns and the base unit field without their ``_id``, then ``qty_base``
(:func:`balance_view`, ``_primitive_result`` of :mod:`erp_mock.tools.output_schema`). The
material entity and the warehouse entity the summaries of an item are resolved against are
read from the configuration the same way there (:func:`material_entity`,
:func:`warehouse_entity`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from erp_mock.common.decimals import PLACES, WIDE, format_decimal, round_half_up
from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import (
    BillModel,
    DomainModel,
    EntityModel,
    FieldRef,
    LinkageModel,
    QueryModel,
)
from erp_mock.domain.search import (
    DEFAULT_PAGE,
    DEFAULT_PAGE_SIZE,
    PAGE_PARAM,
    PAGE_SIZE_PARAM,
    SortKey,
    page_number,
    sorted_records,
)
from erp_mock.domain.unit_conversion import QTY_BASE, resolve_line_quantity
from erp_mock.domain.views import PageView, RecordView, ViewValue, bill_lines, ref_summary
from erp_mock.state.schema import (
    ID_COLUMN,
    MATERIAL_COLUMN,
    QTY_BASE_COLUMN,
    QTY_DELTA_COLUMN,
    SOURCE_BILL_TYPE_COLUMN,
    SOURCE_LINE_COLUMN,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    WAREHOUSE_COLUMN,
)
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "BALANCE",
    "BALANCE_PARAMS",
    "IN",
    "INVENTORY",
    "NEGATIVE_STOCK_REASON",
    "NEGATIVE_STOCK_RULE",
    "OUT",
    "StockChange",
    "StockMove",
    "TxnDirection",
    "balance_id",
    "balance_view",
    "bill_moves",
    "check_negative_stock",
    "linkage_moves",
    "linkages_of",
    "material_entity",
    "matching_balances",
    "net_changes",
    "reverse_linkages",
    "run_balance_query",
    "run_linkages",
    "stock_balance",
    "stored_moves",
    "txn_id",
    "warehouse_entity",
]

# The engine rule this module decides: no (material, warehouse) is left below 0
# (requirement 9.4).
NEGATIVE_STOCK_RULE: Final = "R-GEN-007"

# The reason of design.md data model 6 this module reports; its items name no input field
# and carry the two ids and the quantity on hand before the operation.
NEGATIVE_STOCK_REASON: Final = "negative_stock"

# The direction of one transaction, the last segment of its id: ``in`` for the transaction
# that takes +qty_base and ``out`` for the one that takes -qty_base (requirement 18.8).
type TxnDirection = Literal["in", "out"]

IN: Final[TxnDirection] = "in"
OUT: Final[TxnDirection] = "out"

# The primitive and the query of the one primitive query of M0, the stock balance
# (requirement 9.7); the same pair the tool generator reads from the exposure.
INVENTORY: Final = "inventory"
BALANCE: Final = "balance"

# The two filters of that query, the columns of a balance record (see the module docstring).
BALANCE_PARAMS: Final[tuple[str, str]] = (MATERIAL_COLUMN, WAREHOUSE_COLUMN)

# The operation an inventory linkage runs on, which an unapprove reverses (9.1, 9.6).
_APPROVE: Final = "approve"

# The separator of the two ids this module builds (requirements 18.8 and 9.10).
_ID_SEPARATOR: Final = ":"

# The suffix a ref field's name ends with and a result key drops (requirement 13.3).
_ID_SUFFIX: Final = "_id"

# The precision a quantity is kept at (requirement 4.7).
_QTY_PLACES: Final = PLACES["qty"]
_ZERO: Final = Decimal(0)

# The first key of a detail item that names no input field (design.md data model 6).
_REASON_KEY: Final = "reason"


def txn_id(source_line_id: str, warehouse_id: str, direction: TxnDirection) -> str:
    """The id of one stock transaction (requirement 18.8).

    ``<source line id>:<warehouse id>:<direction>``, which no counter takes part in, so the
    same bill moving the same stock again writes the same ids.
    """
    return _ID_SEPARATOR.join((source_line_id, warehouse_id, direction))


def balance_id(material_id: str, warehouse_id: str) -> str:
    """The id of the balance of one (material, warehouse) (design.md data model 3)."""
    return _ID_SEPARATOR.join((material_id, warehouse_id))


@dataclass(frozen=True, slots=True)
class StockMove:
    """One stock transaction, as a linkage produces it or as the sandbox holds it (9.1).

    ``qty_delta`` is what the transaction adds to the balance of (``material_id``,
    ``warehouse_id``): ``+qty_base`` or ``-qty_base`` of the line it comes from
    (requirement 9.2). ``id`` is the id of requirement 18.8, which :func:`linkage_moves`
    builds and a stored transaction brings along, and ``linkage`` the linkage that produced
    the move, which the writes of the move are recorded under (requirement 17.4).
    """

    id: str
    material_id: str
    warehouse_id: str
    qty_delta: Decimal
    source_bill_type: str
    source_line_id: str
    linkage: str

    @property
    def place(self) -> tuple[str, str]:
        """The (material, warehouse) the move changes, the key its net change is kept by."""
        return (self.material_id, self.warehouse_id)

    @property
    def row(self) -> FrozenMap[str, Value]:
        """The transaction as the stock_txn table holds it (design.md data model 3)."""
        items: tuple[tuple[str, Value], ...] = (
            (ID_COLUMN, self.id),
            (MATERIAL_COLUMN, self.material_id),
            (WAREHOUSE_COLUMN, self.warehouse_id),
            (QTY_DELTA_COLUMN, self.qty_delta),
            (SOURCE_BILL_TYPE_COLUMN, self.source_bill_type),
            (SOURCE_LINE_COLUMN, self.source_line_id),
        )
        return FrozenMap(items)

    @property
    def reversed(self) -> "StockMove":
        """The same transaction with the opposite sign: what removing it does (req 9.6)."""
        return StockMove(
            id=self.id,
            material_id=self.material_id,
            warehouse_id=self.warehouse_id,
            qty_delta=_qty(-self.qty_delta),
            source_bill_type=self.source_bill_type,
            source_line_id=self.source_line_id,
            linkage=self.linkage,
        )


@dataclass(frozen=True, slots=True)
class StockChange:
    """What one operation does to the balance of one (material, warehouse) (req 9.5).

    ``before`` is the quantity on hand the operation started from, 0 where the pair has no
    balance record, and ``delta`` the net change of the whole operation on it - the sum of
    the ``qty_delta`` of its moves and not the value of any single one. ``linkage`` is the
    linkage of the first move that touched the pair, which the write of the balance is
    recorded under (requirement 17.4).
    """

    material_id: str
    warehouse_id: str
    before: Decimal
    delta: Decimal
    linkage: str

    @property
    def place(self) -> tuple[str, str]:
        """The (material, warehouse) of the change, which the changes are ordered by (9.4)."""
        return (self.material_id, self.warehouse_id)

    @property
    def id(self) -> str:
        """The id of the balance record of the pair (design.md data model 3)."""
        return balance_id(self.material_id, self.warehouse_id)

    @property
    def after(self) -> Decimal:
        """The quantity on hand the operation leaves: ``before`` plus ``delta`` (req 9.5)."""
        return _qty(WIDE.add(self.before, self.delta))

    @property
    def negative(self) -> bool:
        """Whether the pair would be left below 0, which breaks R-GEN-007 (9.4, 9.5)."""
        return self.after < 0

    @property
    def row(self) -> FrozenMap[str, Value]:
        """The balance as the stock_balance table holds it (design.md data model 3)."""
        items: tuple[tuple[str, Value], ...] = (
            (ID_COLUMN, self.id),
            (MATERIAL_COLUMN, self.material_id),
            (WAREHOUSE_COLUMN, self.warehouse_id),
            (QTY_BASE_COLUMN, self.after),
        )
        return FrozenMap(items)


# --- the moves of one bill (requirements 9.1 and 9.2) ------------------------------------


def linkages_of(domain: DomainModel, bill: str) -> tuple[LinkageModel, ...]:
    """The inventory linkages of the bill type ``bill``, in model order (requirement 9.1).

    Every linkage of that bill whose ``on`` is approve, which in M0 is every linkage there
    is; empty for a bill type whose packs declare none, which moves no stock at all.
    """
    return tuple(
        linkage
        for linkage in domain.linkages.values()
        if linkage.bill == bill and linkage.on == _APPROVE
    )


def linkage_moves(
    domain: DomainModel,
    linkage: LinkageModel,
    bill: BillModel,
    header: Record,
    lines: Sequence[Record],
) -> tuple[StockMove, ...]:
    """The transactions ``linkage`` produces for ``header`` and its ``lines`` (9.1, 9.2).

    One move per line for an in, an out and an adjust, two per line for a transfer - the
    warehouse the stock leaves first - in line order. Every move carries the material of the
    line, the warehouse the linkage's field names at its own level and the base unit
    quantity of the line with the sign of the direction; a line whose base unit quantity is
    empty counts as 0 and still gets its transaction (requirement 9.1).

    Raises RuntimeError when the linkage or the bill does not resolve - a warehouse field
    the bill does not have, a bill without quantity semantics, an adjust whose enum value is
    in neither list - all of which loading step 5 reports (requirements 9.3 and 9.11).
    """
    quantity = resolve_line_quantity(domain, bill)
    if quantity is None:
        raise RuntimeError(f"the lines of {bill.name} measure nothing, so {linkage.id} cannot run")
    warehouses = _warehouse_directions(linkage, bill, header)
    material_field = quantity.material_field
    qty_field = QTY_BASE if linkage.qty is None else linkage.qty.name
    moves: list[StockMove] = []
    for line in lines:
        material_id = _require_id(line.get(material_field), linkage, material_field)
        line_id = _id_of(line)
        qty = line.get(qty_field)
        base = qty if isinstance(qty, Decimal) else _ZERO
        for ref, direction in warehouses:
            warehouse_id = _require_id(_at_level(ref, header, line), linkage, ref.name)
            moves.append(
                StockMove(
                    id=txn_id(line_id, warehouse_id, direction),
                    material_id=material_id,
                    warehouse_id=warehouse_id,
                    qty_delta=_qty(base if direction == IN else -base),
                    source_bill_type=bill.name,
                    source_line_id=line_id,
                    linkage=linkage.id,
                )
            )
    return tuple(moves)


def bill_moves(
    uow: UnitOfWork, domain: DomainModel, bill: BillModel, header: Record
) -> tuple[StockMove, ...]:
    """Every transaction the approve of the bill ``header`` writes (requirement 9.1).

    The moves of each linkage of the bill type, in model order of the linkages and in line
    order within each; empty for a bill type that declares none.
    """
    linkages = linkages_of(domain, bill.name)
    if not linkages:
        return ()
    lines = bill_lines(uow, bill, _id_of(header))
    return tuple(
        move for linkage in linkages for move in linkage_moves(domain, linkage, bill, header, lines)
    )


def stored_moves(
    uow: UnitOfWork, domain: DomainModel, bill: BillModel, header: Record
) -> tuple[StockMove, ...]:
    """The transactions the sandbox holds for the bill ``header``, by id (requirement 9.6).

    What an unapprove removes: every stock transaction whose source bill type is this bill
    type and whose source line is one of this bill's lines, read from the sandbox rather
    than computed again, so the balances are reversed by exactly what the approve wrote.
    Each one is attributed to the linkage that would produce it, which is where the writes
    of a reversal are recorded (requirement 17.4).
    """
    line_ids = {_id_of(line) for line in bill_lines(uow, bill, _id_of(header))}
    causes = {move.id: move.linkage for move in bill_moves(uow, domain, bill, header)}
    linkages = linkages_of(domain, bill.name)
    first = linkages[0].id if linkages else ""
    return tuple(
        _stored_move(record, causes.get(_id_of(record), first))
        for record in uow.select(STOCK_TXN_TABLE, where={SOURCE_BILL_TYPE_COLUMN: bill.name})
        if _text(record.get(SOURCE_LINE_COLUMN)) in line_ids
    )


# --- the net change of one operation and R-GEN-007 (requirements 9.4 and 9.5) ------------


def stock_balance(uow: UnitOfWork, material_id: str, warehouse_id: str) -> Decimal:
    """The quantity on hand of one (material, warehouse), 0 without a record (req 9.5).

    A pair that has no balance record counts as 0, which is what the first receipt into a
    warehouse adds to.
    """
    record = uow.get(STOCK_BALANCE_TABLE, balance_id(material_id, warehouse_id))
    if record is None:
        return _ZERO
    qty = record.get(QTY_BASE_COLUMN)
    return qty if isinstance(qty, Decimal) else _ZERO


def net_changes(uow: UnitOfWork, moves: Sequence[StockMove]) -> tuple[StockChange, ...]:
    """The net change of each (material, warehouse) of ``moves``, in report order (9.5).

    One change per pair the moves touch, by material id and then warehouse id
    (requirement 9.4), each carrying the quantity on hand the operation started from and
    the sum of the ``qty_delta`` of its moves. The order of the moves does not change the
    result, which is what makes an approve independent of the order of its lines
    (requirement 9.9).
    """
    deltas: dict[tuple[str, str], Decimal] = {}
    causes: dict[tuple[str, str], str] = {}
    for move in moves:
        deltas[move.place] = _qty(WIDE.add(deltas.get(move.place, _ZERO), move.qty_delta))
        causes.setdefault(move.place, move.linkage)
    return tuple(
        StockChange(
            material_id=material_id,
            warehouse_id=warehouse_id,
            before=stock_balance(uow, material_id, warehouse_id),
            delta=delta,
            linkage=causes[(material_id, warehouse_id)],
        )
        for (material_id, warehouse_id), delta in sorted(deltas.items())
    )


def check_negative_stock(
    domain: DomainModel, changes: Sequence[StockChange], *, reverse: bool = False
) -> None:
    """R-GEN-007: no (material, warehouse) is left below 0 (requirements 9.4 and 9.5).

    Raises BUSINESS_RULE_VIOLATION with the rule id ``R-GEN-007`` and one detail item per
    pair whose quantity on hand before the operation plus the net change of the operation
    is below 0, in the order of ``changes`` - by material id and then warehouse id. Each
    item carries the two ids and that quantity on hand and names no input field;
    ``reverse`` says whether the operation is an unapprove, which only the message differs
    by.
    """
    short = tuple(change for change in changes if change.negative)
    if not short:
        return
    first = short[0]
    message = (
        f"{_action_text(reverse)}后{_material_label(domain)}记录 {first.material_id}"
        f" 在仓库 {first.warehouse_id} 的即时库存将小于 0："
        f"操作前为 {_qty_text(first.before)}，本次变化 {_qty_text(first.delta)}"
    )
    raise ErpError(
        ErrorCode.BUSINESS_RULE_VIOLATION,
        _with_rest(message, len(short), "个（物料，仓库）的即时库存同样将小于 0"),
        rule_id=NEGATIVE_STOCK_RULE,
        details=tuple(_negative_detail(change) for change in short),
    )


# --- the writes of an approve and of an unapprove (requirements 9.1, 9.6 and 9.10) -------


def run_linkages(uow: UnitOfWork, domain: DomainModel, bill: BillModel, header: Record) -> None:
    """Step 9 of an approve: the stock the linkages of ``bill`` move (req 9.1, 9.2).

    The moves of every linkage are computed first and R-GEN-007 is decided on their net
    change (:func:`check_negative_stock`), so a bill that would leave a (material,
    warehouse) below 0 writes no transaction at all. Afterwards each move becomes one
    transaction and each pair takes its new quantity on hand, the balance record of a pair
    that had none being created with its first transaction (requirement 9.10). A bill type
    without a linkage writes nothing.
    """
    moves = bill_moves(uow, domain, bill, header)
    if not moves:
        return
    changes = net_changes(uow, moves)
    check_negative_stock(domain, changes)
    for move in moves:
        with uow.cause(move.linkage):
            uow.insert(STOCK_TXN_TABLE, move.row)
    _write_balances(uow, changes)


def reverse_linkages(uow: UnitOfWork, domain: DomainModel, bill: BillModel, header: Record) -> None:
    """Step 9 of an unapprove: the stock the approve moved, moved back (9.6, 9.10).

    Every transaction the sandbox holds for this bill is removed and its ``qty_delta`` taken
    off the balance of its (material, warehouse); R-GEN-007 is decided on the net change
    first, so an unapprove that would leave a pair below 0 removes nothing
    (requirement 9.4). A pair with no transaction left loses its balance record, and one
    that keeps a transaction keeps the record, a quantity on hand of 0 included
    (requirement 9.10).
    """
    if not linkages_of(domain, bill.name):
        return
    stored = stored_moves(uow, domain, bill, header)
    changes = net_changes(uow, tuple(move.reversed for move in stored))
    check_negative_stock(domain, changes, reverse=True)
    for move in stored:
        with uow.cause(move.linkage):
            uow.delete(STOCK_TXN_TABLE, move.id)
    _write_balances(uow, changes, drop_empty=True)


# --- the stock balance query (requirements 9.7 and 9.12) ---------------------------------


def material_entity(domain: DomainModel) -> EntityModel | None:
    """The material entity of the instance: the one that declares unit_conversion (6.8).

    The entity the quantity semantics of a measured bill refer to, which names the base unit
    field and holds the conversion records; the first in model order where an instance
    declares more than one, and None where none does.
    """
    return next((entity for entity in domain.entities.values() if entity.unit_conversion), None)


def warehouse_entity(domain: DomainModel) -> str | None:
    """The warehouse entity of the instance: what a linkage's warehouse field refers to.

    The warehouse of an in, an out and an adjust and the two warehouses of a transfer are
    required ref fields of their bill (requirement 9.11), so the first one that resolves
    names the entity the stock is counted per; None where no linkage resolves.
    """
    for linkage in domain.linkages.values():
        for ref in (linkage.warehouse, linkage.from_warehouse, linkage.to_warehouse):
            if ref is None:
                continue
            field = domain.fields_of(linkage.bill, ref.level).get(ref.name)
            if field is not None and field.ref is not None:
                return field.ref
    return None


def matching_balances(uow: UnitOfWork, args: Mapping[str, Value]) -> list[Record]:
    """The balance records the arguments of the query select (requirement 9.7).

    The material and the warehouse are compared for equality and both are optional: a
    parameter the caller left out or sent as null does not filter, and giving both selects
    the one pair that satisfies them. Only pairs that have a balance record take part, a
    quantity on hand of 0 included (requirement 9.10).

    Raises KeyError for an argument that names no parameter of the query, which a generated
    tool does not send (requirement 11.4).
    """
    unknown = sorted(set(args) - {*BALANCE_PARAMS, PAGE_PARAM, PAGE_SIZE_PARAM})
    if unknown:
        raise KeyError(f"the stock balance query has no parameter {', '.join(unknown)}")
    where = {name: value for name in BALANCE_PARAMS if (value := args.get(name)) is not None}
    return uow.select(STOCK_BALANCE_TABLE, where=FrozenMap(where))


def balance_view(uow: UnitOfWork, domain: DomainModel, record: Record) -> RecordView:
    """One item of the result: the two references, the base unit and the quantity (9.7).

    The material and the warehouse come back as the nested summary of the record they name
    (requirement 13.3), under their column names without ``_id``; the base unit of the
    material follows under the name the material entity gave its base unit field, and is
    left out altogether for an instance whose material entity declares no unit conversion.
    ``qty_base`` is the quantity on hand, in that base unit.
    """
    material_id = _text(record.get(MATERIAL_COLUMN))
    material = material_entity(domain)
    items: list[tuple[str, ViewValue]] = [
        (
            _ref_key(MATERIAL_COLUMN),
            ref_summary(uow, domain, None if material is None else material.name, material_id),
        ),
        (
            _ref_key(WAREHOUSE_COLUMN),
            ref_summary(uow, domain, warehouse_entity(domain), _text(record.get(WAREHOUSE_COLUMN))),
        ),
    ]
    conversion = None if material is None else material.unit_conversion
    if material is not None and conversion is not None:
        stored = uow.get(material.name, material_id)
        unit_id = None if stored is None else stored.get(conversion.base_unit_field)
        summary = (
            ref_summary(uow, domain, conversion.unit_entity, unit_id)
            if isinstance(unit_id, str)
            else None
        )
        items.append((_ref_key(conversion.base_unit_field), summary))
    items.append((QTY_BASE_COLUMN, record.get(QTY_BASE_COLUMN)))
    return RecordView(FrozenMap(items))


def run_balance_query(
    uow: UnitOfWork, domain: DomainModel, query: QueryModel, args: Mapping[str, Value]
) -> PageView:
    """Step 10 of the stock balance query: the page of quantities on hand (9.7, 9.12).

    The records the arguments select (:func:`matching_balances`), ordered by material id and
    then warehouse id with the record id as the last key, cut to the page the caller asked
    for - page 1 and 20 records where it named none - each one as one item
    (:func:`balance_view`). A query that matched nothing and a page past the last one both
    return an empty ``items`` and the total of what matched (requirement 9.12).

    Raises RuntimeError for a query another primitive provides: the stock balance is the one
    primitive query of M0, and loading step 5 reports every other (requirement 2.6).
    """
    if (query.primitive, query.query) != (INVENTORY, BALANCE):
        raise RuntimeError(
            f"the inventory primitive has no query {query.query!r} for the tool {query.tool}"
        )
    ordered = sorted_records(matching_balances(uow, args), _BALANCE_SORT)
    page = page_number(args, PAGE_PARAM, DEFAULT_PAGE)
    page_size = page_number(args, PAGE_SIZE_PARAM, DEFAULT_PAGE_SIZE)
    start = (page - 1) * page_size
    items = tuple(
        balance_view(uow, domain, record) for record in ordered[start : start + page_size]
    )
    return PageView(items=items, total=len(ordered), page=page, page_size=page_size)


def _material_key(record: Record) -> Value:
    """The material of one balance record, the first sort key of the result (req 9.12)."""
    return record.get(MATERIAL_COLUMN)


def _warehouse_key(record: Record) -> Value:
    """The warehouse of one balance record, the second sort key of the result (req 9.12)."""
    return record.get(WAREHOUSE_COLUMN)


# The order of the result: the material, then the warehouse, then the id of the record
# itself, which :func:`~erp_mock.domain.search.sorted_records` adds as the last key
# (requirements 9.12 and 5.14).
_BALANCE_SORT: Final[tuple[SortKey, ...]] = (
    SortKey(value=_material_key),
    SortKey(value=_warehouse_key),
)


# --- internals ---------------------------------------------------------------------------


def _write_balances(
    uow: UnitOfWork, changes: Sequence[StockChange], *, drop_empty: bool = False
) -> None:
    """The new quantity on hand of each pair, in the order of ``changes`` (9.1, 9.10).

    A pair that has no balance record gets one, so the first transaction of a (material,
    warehouse) creates it even where the net change is 0. ``drop_empty`` is what an
    unapprove passes: a pair whose transactions are all gone by now loses its record, while
    one that still has a transaction keeps it with its new quantity.
    """
    for change in changes:
        with uow.cause(change.linkage):
            if drop_empty and not _has_txns(uow, change):
                uow.delete(STOCK_BALANCE_TABLE, change.id)
            elif uow.get(STOCK_BALANCE_TABLE, change.id) is None:
                uow.insert(STOCK_BALANCE_TABLE, change.row)
            else:
                uow.update(STOCK_BALANCE_TABLE, change.id, {QTY_BASE_COLUMN: change.after})


def _has_txns(uow: UnitOfWork, change: StockChange) -> bool:
    """Whether the pair of ``change`` still has a stock transaction (requirement 9.10).

    Read after the transactions of the operation were removed, so what it sees is what the
    pair is left with.
    """
    return bool(
        uow.select(
            STOCK_TXN_TABLE,
            where={MATERIAL_COLUMN: change.material_id, WAREHOUSE_COLUMN: change.warehouse_id},
        )
    )


def _warehouse_directions(
    linkage: LinkageModel, bill: BillModel, header: Record
) -> tuple[tuple[FieldRef, TxnDirection], ...]:
    """The warehouse field and the direction of each transaction of one line (req 9.2).

    One pair for an in, an out and an adjust, two for a transfer - the warehouse the stock
    leaves first. Raises RuntimeError for a linkage that does not resolve, which loading
    step 5 reports (requirements 9.3 and 9.11).
    """
    match linkage.direction:
        case "in":
            return ((_require_ref(linkage, linkage.warehouse, "warehouse_field"), IN),)
        case "out":
            return ((_require_ref(linkage, linkage.warehouse, "warehouse_field"), OUT),)
        case "transfer":
            return (
                (_require_ref(linkage, linkage.from_warehouse, "from_warehouse_field"), OUT),
                (_require_ref(linkage, linkage.to_warehouse, "to_warehouse_field"), IN),
            )
        case "adjust":
            return (
                (
                    _require_ref(linkage, linkage.warehouse, "warehouse_field"),
                    _adjust_direction(linkage, bill, header),
                ),
            )


def _adjust_direction(linkage: LinkageModel, bill: BillModel, header: Record) -> TxnDirection:
    """Which way an adjust moves the stock of every line (requirement 9.2).

    The value of the enum field of the header decides: a value the linkage lists under
    ``increase`` takes +qty_base and one under ``decrease`` -qty_base. Raises RuntimeError
    for a value in neither list and for a linkage that names no such field, both of which
    loading step 5 reports (requirement 9.3).
    """
    ref = _require_ref(linkage, linkage.adjust_field, "adjust.field")
    value = _text(header.get(ref.name))
    if value in linkage.increase:
        return IN
    if value in linkage.decrease:
        return OUT
    raise RuntimeError(
        f"{linkage.id} does not say which way {bill.name} moves stock for {ref.name} {value!r}"
    )


def _at_level(ref: FieldRef, header: Record, line: Record) -> Value:
    """The value ``ref`` names, read from the header or from the line (requirement 9.11)."""
    return line.get(ref.name) if ref.level == "line" else header.get(ref.name)


def _require_ref(linkage: LinkageModel, ref: FieldRef | None, declared: str) -> FieldRef:
    """``ref``, which the linkage has to declare; a missing one is a programming error."""
    if ref is None:
        raise RuntimeError(f"the {linkage.direction} linkage {linkage.id} declares no {declared}")
    return ref


def _require_id(value: Value, linkage: LinkageModel, name: str) -> str:
    """``value`` as the internal id it has to be (requirements 7.5 and 9.11).

    The material of a line and the warehouse of a linkage are required fields, so a bill
    that left draft has both of them (R-GEN-002); an empty one is a programming error.
    """
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{linkage.id} needs {name}, which the bill left empty")
    return value


def _stored_move(record: Record, linkage: str) -> StockMove:
    """One stock transaction the sandbox holds, with the linkage that wrote it (9.6)."""
    qty = record.get(QTY_DELTA_COLUMN)
    return StockMove(
        id=_id_of(record),
        material_id=_text(record.get(MATERIAL_COLUMN)),
        warehouse_id=_text(record.get(WAREHOUSE_COLUMN)),
        qty_delta=qty if isinstance(qty, Decimal) else _ZERO,
        source_bill_type=_text(record.get(SOURCE_BILL_TYPE_COLUMN)),
        source_line_id=_text(record.get(SOURCE_LINE_COLUMN)),
        linkage=linkage,
    )


def _negative_detail(change: StockChange) -> FrozenMap[str, str]:
    """One detail item of R-GEN-007, in the key order of design.md data model 6 (9.4)."""
    items: tuple[tuple[str, str], ...] = (
        (_REASON_KEY, NEGATIVE_STOCK_REASON),
        (MATERIAL_COLUMN, change.material_id),
        (WAREHOUSE_COLUMN, change.warehouse_id),
        (QTY_BASE_COLUMN, _qty_text(change.before)),
    )
    return FrozenMap(items)


def _qty(value: Decimal) -> Decimal:
    """``value`` at the precision of a quantity, never -0 (requirements 4.7 and 4.8)."""
    return round_half_up(value, _QTY_PLACES)


def _qty_text(value: Decimal) -> str:
    """A quantity as a detail item and a message write it (requirement 4.8)."""
    return format_decimal("qty", value)


def _ref_key(name: str) -> str:
    """The key a reference takes in a result: its column name without ``_id`` (13.3)."""
    return name.removesuffix(_ID_SUFFIX)


def _material_label(domain: DomainModel) -> str:
    """The Chinese label of the material entity, empty where it has none (req 13.7)."""
    material = material_entity(domain)
    if material is None or material.label is None:
        return ""
    return material.label.zh


def _action_text(reverse: bool) -> str:
    """The operation a message names: the unapprove that reverses, or the approve (13.7)."""
    return "反审核" if reverse else "审核"


def _with_rest(message: str, total: int, rest: str) -> str:
    """``message``, saying how many further items of the same kind there are."""
    return message if total < 2 else f"{message}；另有 {total - 1} {rest}"


def _text(value: Value) -> str:
    """``value`` as the text it is, the empty string for anything else."""
    return value if isinstance(value, str) else ""


def _id_of(record: Mapping[str, Value]) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""
