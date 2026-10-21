"""What the generator reads off the sandbox before it draws the next operation.

Requirement 25.2 asks the operation sequences to cover the successful and the failing path
of every operation, and design.md gets that from the state: every step first reads the
bills the sandbox holds, grouped by the state they are in, and the quota each source line
has left, and then draws an operation against what is there. The three readers here are
that step:

- :func:`read_bills` - one :class:`Held` per bill type of the surface, each carrying the id
  and the state of every bill the sandbox holds, so a sensible state operation finds a bill
  in the state it starts from and a noisy one finds a bill in another state
  (requirement 7.4);
- :func:`read_refs` - the records a reference may name, the enabled ones apart from the
  disabled ones, so a sensible value points at a record that is there and enabled and a
  noisy one at a disabled record (R-GEN-006) or at no record at all (requirement 5.6);
- :func:`read_source` - one source bill of one conversion with what each of its lines has
  left, the Pushable_Qty of the glossary, so a sensible push stays inside the quota and a
  noisy one goes over it (requirements 8.5 and 8.6).

Everything here reads through :class:`~erp_mock.state.store.UnitOfWork` and writes nothing,
and the quota comes from :mod:`erp_mock.domain.conversion` rather than from a model of its
own: these are the inputs of the generator, not the reference model of a property - what
the engine should have computed is each property's own business.
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Final

from erp_mock.common.decimals import PLACES
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import ENTITY_STATUSES, DomainModel
from erp_mock.domain.conversion import pushable_amount, pushable_qty
from erp_mock.domain.state_machine import bill_status
from erp_mock.domain.unit_conversion import QTY_BASE
from erp_mock.state.schema import BILL_ID_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, SandboxStore, UnitOfWork, Value
from tests.property.opseq.start import BillTools, PushTools, Start

__all__ = [
    "DISABLED",
    "ENABLED",
    "MISSING_NUMBER",
    "QTY_UNIT",
    "Held",
    "Refs",
    "Source",
    "SourceLine",
    "missing_id",
    "read_bills",
    "read_refs",
    "read_source",
]

# The two states of an entity that has them (requirement 5.7): a reference to a disabled
# record breaks R-GEN-006, which is one of the noise operations.
ENABLED: Final = ENTITY_STATUSES[0]
DISABLED: Final = ENTITY_STATUSES[1]

# The status field of an entity record, the same name a bill holds its state under
# (requirement 5.7, meta-model.md section 4).
_STATUS: Final = "status"

# The sequence number of an id no record answers: the counters of a sandbox start at 0 and
# a sequence of at most 50 operations never reaches this one, so ``<prefix>_999999`` is an
# internal ID of the right shape that nothing is stored under (requirement 4.1).
MISSING_NUMBER: Final = 999999

# The smallest quantity a quantity field holds, the unit a push goes over a quota by
# (requirement 4.7).
QTY_UNIT: Final = Decimal(1).scaleb(-PLACES["qty"])

# The id prefix of a reference whose entity the configuration does not name, which no
# reference of a valid instance has (:meth:`Refs.absent_id`).
_NO_ENTITY_PREFIX: Final = "nil"

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


def missing_id(prefix: str) -> str:
    """An internal ID of ``prefix`` that no record of the sandbox answers (req 4.1)."""
    return f"{prefix}_{MISSING_NUMBER:06d}"


# --- the bills the sandbox holds (requirement 7.4) ---------------------------------------


@dataclass(frozen=True, slots=True)
class Held:
    """The bills of one bill type the sandbox holds, each with the state it is in.

    ``bills`` is one pair per bill, the internal id and the state, in id order; a bill that
    was deleted is not among them. :meth:`in_state` and :meth:`outside` are the two sides
    of a state operation: the bills an action may run on, and the bills it may not
    (requirement 7.4).
    """

    tools: BillTools
    bills: tuple[tuple[str, str], ...] = ()

    @property
    def name(self) -> str:
        """The name of the bill type (design.md 3.1)."""
        return self.tools.name

    @property
    def ids(self) -> tuple[str, ...]:
        """Every bill of the type the sandbox holds, in id order."""
        return tuple(bill_id for bill_id, _ in self.bills)

    def in_state(self, status: str) -> tuple[str, ...]:
        """The bills that are in the state ``status``."""
        return tuple(bill_id for bill_id, state in self.bills if state == status)

    def outside(self, status: str) -> tuple[str, ...]:
        """The bills that are in any state but ``status`` (requirement 7.4)."""
        return tuple(bill_id for bill_id, state in self.bills if state != status)


def read_bills(store: SandboxStore, start: Start) -> tuple[Held, ...]:
    """The bills of every bill type of the surface, in the model order of the bills.

    One select per bill type, in one transaction, so the whole group is read as one state
    of the sandbox.
    """
    with store.transaction() as uow:
        return tuple(_held(uow, tools) for tools in start.bills)


def _held(uow: UnitOfWork, tools: BillTools) -> Held:
    """The bills of one type with their states (:class:`Held`)."""
    return Held(
        tools=tools,
        bills=tuple(
            (str(record[ID_COLUMN]), bill_status(record)) for record in uow.select(tools.name)
        ),
    )


# --- the records a reference may name (requirements 5.6 and 5.7) -------------------------


@dataclass(frozen=True, slots=True)
class Refs:
    """The records of each entity a reference may name, by entity name.

    ``enabled`` holds the ids a sensible value points at - every record of an entity
    without a status, and the enabled ones of an entity that has one - and ``disabled`` the
    ids a noisy value points at, which break R-GEN-006 (requirement 5.7). An entity the
    sandbox holds no record of is not a key of either. ``absent`` is one id per entity that
    no record answers, of the shape the ``id_prefix`` of that entity gives it
    (:func:`missing_id`, requirement 4.1).
    """

    enabled: FrozenMap[str, tuple[str, ...]] = FrozenMap()
    disabled: FrozenMap[str, tuple[str, ...]] = FrozenMap()
    absent: FrozenMap[str, str] = FrozenMap()

    def of(self, entity: str) -> tuple[str, ...]:
        """The enabled records of ``entity``, empty when the sandbox holds none."""
        return self.enabled.get(entity, ())

    def unusable(self, entity: str) -> tuple[str, ...]:
        """The disabled records of ``entity``, empty when the sandbox holds none."""
        return self.disabled.get(entity, ())

    def absent_id(self, entity: str) -> str:
        """An internal ID of ``entity`` that no record answers (requirements 4.1, 5.6)."""
        return self.absent.get(entity, missing_id(_NO_ENTITY_PREFIX))


def read_refs(store: SandboxStore, domain: DomainModel) -> Refs:
    """The records every entity of ``domain`` holds, the enabled apart from the disabled.

    One select per entity, in one transaction. The sequences write bills, so a session
    reads this pool once and keeps it (:meth:`~tests.property.opseq.session.Session.refs`).
    """
    enabled: dict[str, tuple[str, ...]] = {}
    disabled: dict[str, tuple[str, ...]] = {}
    absent: dict[str, str] = {}
    with store.transaction() as uow:
        for entity in domain.entities.values():
            records = uow.select(entity.name)
            enabled[entity.name] = tuple(
                str(record[ID_COLUMN]) for record in records if not _is_disabled(record)
            )
            disabled[entity.name] = tuple(
                str(record[ID_COLUMN]) for record in records if _is_disabled(record)
            )
            absent[entity.name] = missing_id(entity.id_prefix)
    return Refs(enabled=FrozenMap(enabled), disabled=FrozenMap(disabled), absent=FrozenMap(absent))


def _is_disabled(record: Record) -> bool:
    """Whether the entity record is disabled, which R-GEN-006 refuses (requirement 5.7)."""
    return record.get(_STATUS) == DISABLED


# --- what a source bill has left (requirements 8.5 and 8.6) ------------------------------


@dataclass(frozen=True, slots=True)
class SourceLine:
    """One line of a source bill and what a push may still take from it (req 8.5).

    - ``pushable``: the Pushable_Qty of the line, in the base unit of its material: its own
      ``qty_base`` minus what the bills of the conversion already occupy;
    - ``factor``: how many base units one unit of the line is, read back off the line
      itself as ``qty_base / qty`` and 1 for a line that measures nothing
      (requirement 6.6). A pushed line sends its quantity in the unit the conversion maps
      from this line, so the factor turns a base unit quota into the quantity a push may
      send (:attr:`takeable`).
    """

    id: str
    pushable: Decimal = _ZERO
    factor: Decimal = _ONE

    @property
    def takeable(self) -> Decimal:
        """The largest quantity a push of this line may send, in the line's own unit.

        The quota divided by the factor, rounded down to the 4 decimals of a quantity, so
        the quantity the caller sends is one a tool accepts and its base unit quantity is
        no more than the quota (requirement 4.7).
        """
        if self.pushable <= _ZERO:
            return _ZERO
        return (self.pushable / self.factor).quantize(QTY_UNIT, rounding=ROUND_DOWN)


@dataclass(frozen=True, slots=True)
class Source:
    """One source bill of one conversion: the state it is in and what is left of it.

    ``lines`` are its lines in line order, each with its own quota; ``amount`` is the
    Pushable_Amount of the bill for a conversion limited by amount and None for one limited
    by quantity (requirement 8.6).
    """

    id: str
    status: str = ""
    lines: tuple[SourceLine, ...] = ()
    amount: Decimal | None = None

    @property
    def by_amount(self) -> bool:
        """Whether the quota is the one amount of the bill rather than one per line (8.6)."""
        return self.amount is not None

    @property
    def free_lines(self) -> tuple[SourceLine, ...]:
        """The lines a push may still take something from (requirement 8.5)."""
        return tuple(line for line in self.lines if line.takeable > _ZERO)

    @property
    def usable_lines(self) -> tuple[SourceLine, ...]:
        """The lines a push may name while staying inside the quota (8.5 and 8.6).

        Every line of the bill when the quota is counted by amount, since the amount is the
        bill's and no line of it has one of its own; the lines that have something left
        when it is counted per line.
        """
        return self.lines if self.by_amount else self.free_lines

    @property
    def exhausted(self) -> bool:
        """Whether the bill has no quota left for a push at all (requirements 8.5, 8.6)."""
        if self.amount is not None:
            return self.amount <= _ZERO
        return not self.free_lines


def read_source(store: SandboxStore, push: PushTools, source_id: str) -> Source:
    """The source bill ``source_id`` of ``push`` with the quota of each of its lines.

    The quota is :func:`~erp_mock.domain.conversion.pushable_qty` per line, or
    :func:`~erp_mock.domain.conversion.pushable_amount` for the bill when the conversion is
    limited by amount; a bill the sandbox does not hold comes back with no lines and no
    state.
    """
    with store.transaction() as uow:
        header = uow.get(push.source.name, source_id)
        if header is None:
            return Source(id=source_id)
        lines = uow.select(push.source.line_name, where={BILL_ID_COLUMN: source_id})
        by_amount = push.conversion.limit_by == "amount"
        return Source(
            id=source_id,
            status=bill_status(header),
            lines=tuple(_source_line(uow, push, line) for line in lines),
            amount=(
                max(pushable_amount(uow, push.conversion, push.target, header), _ZERO)
                if by_amount
                else None
            ),
        )


def _source_line(uow: UnitOfWork, push: PushTools, line: Record) -> SourceLine:
    """One source line with its quota and its factor (:class:`SourceLine`)."""
    quota = (
        pushable_qty(uow, push.conversion, push.target, line)
        if push.conversion.limit_by == "qty"
        else _ZERO
    )
    return SourceLine(
        id=str(line[ID_COLUMN]),
        pushable=max(quota, _ZERO),
        factor=_factor(push.source.qty_field, line),
    )


def _factor(qty_field: str | None, line: Record) -> Decimal:
    """How many base units one unit of ``line`` is, 1 when it cannot be read (req 6.6)."""
    own = _decimal(line.get(qty_field)) if qty_field is not None else None
    base = _decimal(line.get(QTY_BASE))
    if own is None or base is None or own <= _ZERO or base <= _ZERO:
        return _ONE
    return base / own


def _decimal(value: Value) -> Decimal | None:
    """``value`` as the decimal it is, None for a column that holds something else."""
    return value if isinstance(value, Decimal) else None
