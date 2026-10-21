"""The unit_conversion primitive: the base unit quantity of a line and the factor it reads.

A bill whose lines declare quantity semantics measures a quantity in the unit of the line,
while the stock of the sandbox is kept in the base unit of the material (requirement 9.2).
This module is the one place that converts between the two, and the one place that checks
what such a conversion needs:

=====================  ==================================================================
slot                   what this module contributes
=====================  ==================================================================
``candidate``          :func:`assign_qty_base`: the ``qty_base`` of every line of the
                       record a write stores (requirement 6.6)
``unit_conversion``    :func:`check_unit_conversion`: a line whose unit converts to the
                       base unit (requirement 6.7), and the unit and factor of a
                       conversion record a write stores (requirement 6.11)
``view``               :func:`conversion_records`: the conversion records a material
                       carries in its own result (requirement 6.9)
=====================  ==================================================================

Nothing here is named by the engine. The measured entity is what the material reference of
the lines points at, the unit entity what their unit reference points at, and the base unit
field, the conversion records and their material, unit and factor fields are what that
entity declared in its ``unit_conversion`` (requirement 6.8); the engine test fixture
calls them ``article``, ``measure``, ``base_measure_id``, ``measure_link``,
``article_id``, ``alt_measure_id`` and ``ratio``, where the example pack calls them
``material``, ``unit``, ``base_unit_id``, ``unit_conversion``, ``material_id``,
``from_unit_id`` and ``factor``. :func:`resolve_line_quantity` resolves the whole set for
one bill, and a bill that declares no quantity semantics - a pricing bill of money alone -
resolves to None and keeps its ``qty_base`` empty.

The quantity itself (requirement 6.6): ``qty_base = round(qty × factor, 4)``, ROUND_HALF_UP
on the exact product, where ``qty`` is the standard ``qty`` of a pricing bill and the
declared quantity field of a bill without pricing (:attr:`LineQuantity.qty_field`), and
``factor`` is how many base units one unit of the line is: :data:`ONE` when the line's unit
is the base unit of its material, and the factor of the material's conversion record for
that unit otherwise. An empty material, unit or quantity leaves ``qty_base`` empty, and so
does a unit the material has no conversion record for - a line the write is about to be
rejected over.

That rejection is requirement 6.7, in the ``unit_conversion`` slot of the pipeline: after
the state of the bill and before the business rules (requirement 12.10), so a call against
a bill in the wrong state reports the state and a call that also breaks a rule reports the
missing conversion. A line whose material and unit are both set, whose unit is not the base
unit and whose material has no conversion record for it ends the call in VALIDATION_FAILED,
which no rule decides (requirement 13.8); the detail items name the unit field of each such
line by line order.

The write of a conversion record is checked in the same slot (requirement 6.11,
design.md 4.2): the factor has to be a value above 0, the unit has to be set and has to
differ from the base unit of its material, and the material must not already have a record
for that unit, which is the one of the four that is a CONFLICT. The checks run in the order
requirement 6.11 lists them and the first one that fails ends the call, so the error of a
record that breaks two of them is the same every time (requirement 12.9). An update is
checked on the merged record, since the candidate holds what the record will hold.

Field paths are those of the parameters the caller sent (design.md data model 6): the unit
field of a line as ``lines[<index>].<name>`` with the index counting from 0, and a field of
a conversion record by its own name.

Import direction. This module imports no other module of :mod:`erp_mock.domain`, because
two of them read it: :mod:`erp_mock.domain.crud` computes the ``qty_base`` of the candidate
through it and :mod:`erp_mock.domain.views` appends the conversion records of a material
through it. Two names it therefore repeats rather than imports:

- :data:`UNIT_CONVERSIONS`, the key the conversion records of a material take in its
  result, which is the ``UNIT_CONVERSIONS`` of :mod:`erp_mock.tools.output_schema` - the
  schema of the array and the array itself have to carry the same key, and domain must not
  import tools either (requirement 24.8);
- the ``lines`` of a field path, which is the ``LINES`` of :mod:`erp_mock.domain.views` and
  the ``LINES_PARAM`` of :mod:`erp_mock.tools.input_schema`.
"""

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from erp_mock.common.decimals import PLACES, WIDE, round_half_up
from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.config.model import BillModel, DomainModel, EntityModel, UnitConversionModel
from erp_mock.state.schema import ID_COLUMN
from erp_mock.state.store import Record, UnitOfWork, Value

__all__ = [
    "DUPLICATE_REASON",
    "INVALID_UNIT_REASON",
    "MISSING_REASON",
    "NOT_POSITIVE_REASON",
    "ONE",
    "QTY_BASE",
    "UNIT_CONVERSIONS",
    "LineQuantity",
    "assign_qty_base",
    "check_conversion_record",
    "check_line_conversions",
    "check_unit_conversion",
    "conversion_declarations",
    "conversion_factor",
    "conversion_record",
    "conversion_records",
    "line_qty_base",
    "missing_conversions",
    "qty_base",
    "resolve_line_quantity",
]

# The key the conversion records of a material take in its own result (requirement 6.9);
# the same key the schema of that array carries (see the module docstring).
UNIT_CONVERSIONS: Final = "unit_conversions"

# The standard field of a measured line the primitive keeps (meta-model.md section 4).
QTY_BASE: Final = "qty_base"

# The factor of a line whose unit is the base unit of its material (requirement 6.6).
ONE: Final = Decimal(1)

# The reasons of design.md data model 6 this module reports: a unit the material has no
# conversion record for (requirement 6.7) and, for the write of a conversion record
# (requirement 6.11), a factor that is not above 0, a unit that is empty or is the base
# unit, and a unit the material already has a record for. The table of the design lists
# ``unit_conversion_missing`` and ``duplicate``, the latter being the reason the crud
# primitive reports for requirement 5.8 as well; the other two are reasons of their own,
# as requirement 6.11 is not in that table.
MISSING_REASON: Final = "unit_conversion_missing"
NOT_POSITIVE_REASON: Final = "must_be_positive"
INVALID_UNIT_REASON: Final = "invalid_conversion_unit"
DUPLICATE_REASON: Final = "duplicate"

# The decimal places of a quantity (requirement 4.7).
_QTY_PLACES: Final = PLACES["qty"]

# The keys of one detail item (design.md data model 6).
_FIELD_KEY: Final = "field"
_REASON_KEY: Final = "reason"

# The first segment of the field path of a line (see the module docstring).
_LINES: Final = "lines"


@dataclass(frozen=True, slots=True)
class LineQuantity:
    """What the lines of one bill measure, resolved (requirements 1.4 and 6.8).

    ``material_field``, ``unit_field`` and ``qty_field`` are the three fields of a line the
    primitive reads: the reference to the material, the reference to the unit the quantity
    is given in, and the quantity itself. ``material`` is the entity the material reference
    points at and ``conversion`` the unit conversion that entity declared, which names its
    base unit field and the records that hold its factors.
    """

    material_field: str
    unit_field: str
    qty_field: str
    material: EntityModel
    conversion: UnitConversionModel


def resolve_line_quantity(domain: DomainModel, bill: BillModel) -> LineQuantity | None:
    """What the lines of ``bill`` measure, None when they measure nothing (req 6.8).

    None for a bill that declares no quantity semantics, and for one whose material
    reference points at an entity the instance does not have or at one that declares no
    unit conversion; loading step 5 reports both of the latter (requirement 6.12), so a
    valid instance resolves every bill that declares quantity semantics.
    """
    quantity = bill.line_quantity
    qty_field = bill.qty_field
    if quantity is None or qty_field is None:
        return None
    field = bill.lines.get(quantity.material_field)
    material = None if field is None or field.ref is None else domain.entities.get(field.ref)
    if material is None or material.unit_conversion is None:
        return None
    return LineQuantity(
        material_field=quantity.material_field,
        unit_field=quantity.unit_field,
        qty_field=qty_field,
        material=material,
        conversion=material.unit_conversion,
    )


# --- the base unit quantity of a line (requirement 6.6) ----------------------------------


def qty_base(qty: Decimal, factor: Decimal) -> Decimal:
    """``round(qty × factor, 4)``, ROUND_HALF_UP on the exact product (requirement 6.6).

    The product itself is exact - two field values have at most 30 significant digits each
    (:data:`~erp_mock.common.decimals.WIDE`) - so the only rounding is the one to the
    precision of a quantity, and the result has exponent -4 and is never -0.
    """
    return round_half_up(WIDE.multiply(qty, factor), _QTY_PLACES)


def conversion_record(
    uow: UnitOfWork, conversion: UnitConversionModel, material_id: str, unit_id: str
) -> Record | None:
    """The record that converts ``unit_id`` of ``material_id``, None when there is none.

    The conversion record of one (material, unit) pair; a write never stores a second one
    for the same pair (requirement 6.11), and where a sandbox holds several the first by id
    is taken, so a read is deterministic either way.
    """
    records = uow.select(
        conversion.entity,
        where={conversion.material_field: material_id, conversion.unit_field: unit_id},
    )
    return records[0] if records else None


def conversion_factor(
    uow: UnitOfWork, quantity: LineQuantity, material_id: str, unit_id: str
) -> Decimal | None:
    """How many base units one ``unit_id`` of ``material_id`` is (requirement 6.6).

    :data:`ONE` when ``unit_id`` is the base unit of the material, and the factor of its
    conversion record for that unit otherwise; None when the material is not there, when it
    has no conversion record for the unit - the line requirement 6.7 rejects the write over
    - and when the factor of that record is empty, which no write of the engine leaves
    behind (requirement 6.11).
    """
    conversion = quantity.conversion
    material = uow.get(quantity.material.name, material_id)
    if material is None:
        return None
    if material.get(conversion.base_unit_field) == unit_id:
        return ONE
    record = conversion_record(uow, conversion, material_id, unit_id)
    return None if record is None else _factor_of(record, conversion)


def line_qty_base(
    uow: UnitOfWork, quantity: LineQuantity, line: Mapping[str, Value]
) -> Decimal | None:
    """The ``qty_base`` of one line of a measured bill (requirement 6.6).

    None when the material, the unit or the quantity of the line is empty, and when the
    unit has no factor (:func:`conversion_factor`).
    """
    material_id = line.get(quantity.material_field)
    unit_id = line.get(quantity.unit_field)
    qty = line.get(quantity.qty_field)
    if not isinstance(material_id, str) or not isinstance(unit_id, str):
        return None
    if not isinstance(qty, Decimal):
        return None
    factor = conversion_factor(uow, quantity, material_id, unit_id)
    return None if factor is None else qty_base(qty, factor)


def assign_qty_base(
    uow: UnitOfWork,
    domain: DomainModel,
    bill: BillModel,
    lines: Iterable[MutableMapping[str, Value]],
) -> None:
    """Step 5 of a write: the ``qty_base`` of every line of the candidate (req 6.6).

    ``lines`` are the lines the write will store, which take the value in place. A bill
    whose lines measure nothing keeps the field empty: a pricing bill without quantity
    semantics has the column and no material to read a factor through
    (:func:`resolve_line_quantity`).
    """
    quantity = resolve_line_quantity(domain, bill)
    if quantity is None:
        return
    for line in lines:
        line[QTY_BASE] = line_qty_base(uow, quantity, line)


# --- the checks of the unit_conversion slot (requirements 6.7 and 6.11) ------------------


def check_unit_conversion(
    uow: UnitOfWork,
    domain: DomainModel,
    item: EntityModel | BillModel,
    values: Mapping[str, Value],
    lines: Sequence[Mapping[str, Value]] = (),
) -> None:
    """Step 6 of a write: what the unit conversion of the record requires (6.7, 6.11).

    ``item`` is the entity or bill the candidate belongs to, ``values`` the record it will
    store and ``lines`` its lines. A bill is checked line by line against the conversion
    records its materials hold (:func:`check_line_conversions`) and an entity record
    against what a conversion record may carry (:func:`check_conversion_record`); an entity
    that holds no conversion records and a bill that measures nothing are checked for
    nothing.
    """
    if isinstance(item, BillModel):
        check_line_conversions(uow, domain, item, lines)
        return
    check_conversion_record(uow, domain, item, values)


def missing_conversions(
    uow: UnitOfWork, quantity: LineQuantity, lines: Sequence[Mapping[str, Value]]
) -> tuple[str, ...]:
    """The field paths of the lines whose unit does not convert, by line (req 6.7).

    One path per line whose material and unit are both set, whose unit is not the base unit
    of that material and whose material has no conversion record for it; the path names the
    unit field of the line, whose index counts from 0, so the line numbered ``n`` is at
    ``lines[n - 1]``.
    """
    return tuple(
        _line_path(index, quantity.unit_field)
        for index, line in enumerate(lines)
        if _without_conversion(uow, quantity, line)
    )


def check_line_conversions(
    uow: UnitOfWork, domain: DomainModel, bill: BillModel, lines: Sequence[Mapping[str, Value]]
) -> None:
    """Step 6 of a write to a bill: every line's unit converts (requirement 6.7).

    Raises VALIDATION_FAILED, which no rule decides (requirement 13.8), with one detail
    item per line that has no factor to read, in line order; the message names the path of
    the first of them (requirement 13.7).
    """
    quantity = resolve_line_quantity(domain, bill)
    if quantity is None:
        return
    missing = missing_conversions(uow, quantity, lines)
    if not missing:
        return
    label = _label(quantity.material)
    message = f"{missing[0]} 的计量单位不是该{label}的基本单位，且没有以它为换算单位的换算关系"
    raise ErpError(
        ErrorCode.VALIDATION_FAILED,
        _with_rest(message, len(missing), "处的计量单位缺少换算关系"),
        details=_details(missing, MISSING_REASON),
    )


def conversion_declarations(
    domain: DomainModel, entity: str
) -> tuple[tuple[EntityModel, UnitConversionModel], ...]:
    """The measured entities whose conversion records ``entity`` holds (req 6.8).

    Every entity that declared ``entity`` as the holder of its conversion records, in model
    order, each with that declaration: it names the fields of a record of ``entity`` the
    checks of requirement 6.11 read. Empty for an entity that holds no conversion records,
    which is every entity of a pack that declares no unit conversion at all.
    """
    found: list[tuple[EntityModel, UnitConversionModel]] = []
    for measured in domain.entities.values():
        conversion = measured.unit_conversion
        if conversion is not None and conversion.entity == entity:
            found.append((measured, conversion))
    return tuple(found)


def check_conversion_record(
    uow: UnitOfWork, domain: DomainModel, entity: EntityModel, values: Mapping[str, Value]
) -> None:
    """Step 6 of a write to an entity record: what a conversion record may carry (6.11).

    ``values`` is the record the write will store, which for an update is the merged one.
    The factor, then the unit, then the records the material already has, as requirement
    6.11 lists them; raises VALIDATION_FAILED for the first three and CONFLICT for a unit
    the material already has a record for, each with one detail item naming the field.
    An entity that holds no conversion records is checked for nothing
    (:func:`conversion_declarations`).
    """
    for measured, conversion in conversion_declarations(domain, entity.name):
        _check_factor(values, conversion)
        _check_unit(uow, values, measured, conversion)


# --- the conversion records of one material (requirement 6.9) ----------------------------


def conversion_records(
    uow: UnitOfWork, conversion: UnitConversionModel, material_id: str
) -> tuple[Record, ...]:
    """The conversion records of the material ``material_id``, by unit id (req 6.9).

    Every record of the material, ordered by the internal id of its conversion unit; a
    record whose unit is empty comes last, and records that share a unit keep the order of
    their own ids. Empty for a material that has no conversion record, which is returned as
    an empty array, the base unit never being added to it.
    """
    records = uow.select(conversion.entity, where={conversion.material_field: material_id})
    return tuple(sorted(records, key=lambda record: _unit_key(record, conversion)))


# --- internals ---------------------------------------------------------------------------


def _check_factor(values: Mapping[str, Value], conversion: UnitConversionModel) -> None:
    """The factor of a conversion record is a value above 0 (requirement 6.11)."""
    factor = _factor_value(values.get(conversion.factor_field))
    if factor is not None and factor > 0:
        return
    path = conversion.factor_field
    raise ErpError(
        ErrorCode.VALIDATION_FAILED,
        f"{path} 是 1 个换算单位折合的基本单位数量，必须大于 0",
        details=_details((path,), NOT_POSITIVE_REASON),
    )


def _check_unit(
    uow: UnitOfWork,
    values: Mapping[str, Value],
    measured: EntityModel,
    conversion: UnitConversionModel,
) -> None:
    """The unit of a conversion record: set, not the base unit, not taken (req 6.11).

    A record whose material is empty is checked against neither the base unit nor the other
    records of that material, since there is no material to read either from; the material
    field of a conversion record is declared required, so a generated tool sends one.
    """
    path = conversion.unit_field
    unit_id = values.get(conversion.unit_field)
    if not isinstance(unit_id, str):
        raise ErpError(
            ErrorCode.VALIDATION_FAILED,
            f"{path} 是换算单位，不能为空",
            details=_details((path,), INVALID_UNIT_REASON),
        )
    material_id = values.get(conversion.material_field)
    if not isinstance(material_id, str):
        return
    label = _label(measured)
    material = uow.get(measured.name, material_id)
    if material is not None and material.get(conversion.base_unit_field) == unit_id:
        raise ErpError(
            ErrorCode.VALIDATION_FAILED,
            f"{path} 是{label}记录 {material_id} 的基本单位，不需要换算关系",
            details=_details((path,), INVALID_UNIT_REASON),
        )
    taken = _record_with_unit(uow, conversion, material_id, unit_id, _id_of(values))
    if taken is not None:
        raise ErpError(
            ErrorCode.CONFLICT,
            f"{label}记录 {material_id} 已有换算单位为 {unit_id} 的换算关系 {taken}",
            details=_details((path,), DUPLICATE_REASON),
        )


def _record_with_unit(
    uow: UnitOfWork,
    conversion: UnitConversionModel,
    material_id: str,
    unit_id: str,
    record_id: str,
) -> str | None:
    """The id of another record of ``material_id`` with the unit ``unit_id`` (req 6.11).

    The record the write itself stores is not one of the others, so an update that leaves
    the unit of a record as it is goes through.
    """
    for record in uow.select(
        conversion.entity,
        where={conversion.material_field: material_id, conversion.unit_field: unit_id},
    ):
        found = _id_of(record)
        if found != record_id:
            return found
    return None


def _without_conversion(uow: UnitOfWork, quantity: LineQuantity, line: Mapping[str, Value]) -> bool:
    """Whether the unit of ``line`` has no factor to read (requirement 6.7).

    False for a line whose material or unit is empty, for one whose unit is the base unit of
    its material, and for one whose material is not there, which the references slot
    reported as NOT_FOUND before this check ran (requirement 5.6).
    """
    material_id = line.get(quantity.material_field)
    unit_id = line.get(quantity.unit_field)
    if not isinstance(material_id, str) or not isinstance(unit_id, str):
        return False
    conversion = quantity.conversion
    material = uow.get(quantity.material.name, material_id)
    if material is None or material.get(conversion.base_unit_field) == unit_id:
        return False
    return conversion_record(uow, conversion, material_id, unit_id) is None


def _factor_of(record: Record, conversion: UnitConversionModel) -> Decimal | None:
    """The factor of the conversion record ``record``, None when it is empty."""
    return _factor_value(record.get(conversion.factor_field))


def _factor_value(value: Value) -> Decimal | None:
    """``value`` as a factor, None when it is empty or holds no number.

    The factor field is one of int, qty, price, money and rate (requirement 6.12), so an
    int column and a decimal column both reach this.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    return value if isinstance(value, Decimal) else None


def _unit_key(record: Record, conversion: UnitConversionModel) -> tuple[int, str]:
    """The sort key of one conversion record: its unit id, an empty one last (req 6.9)."""
    unit_id = record.get(conversion.unit_field)
    return (0, unit_id) if isinstance(unit_id, str) else (1, "")


def _line_path(index: int, name: str) -> str:
    """The path of ``name`` in the line at ``index``, counting from 0 (requirement 6.7)."""
    return f"{_LINES}[{index}].{name}"


def _details(paths: Iterable[str], reason: str) -> tuple[Mapping[str, str], ...]:
    """One detail item per field path, the reason after it (design.md data model 6)."""
    return tuple({_FIELD_KEY: path, _REASON_KEY: reason} for path in paths)


def _label(item: EntityModel) -> str:
    """The Chinese label of ``item``, empty where it has none."""
    return "" if item.label is None else item.label.zh


def _with_rest(message: str, total: int, rest: str) -> str:
    """``message``, saying how many further items of the same kind there are."""
    return message if total < 2 else f"{message}；另有 {total - 1} {rest}"


def _id_of(record: Mapping[str, Value]) -> str:
    """The id of ``record``; every record table keeps a text id that is NOT NULL."""
    record_id = record.get(ID_COLUMN)
    return record_id if isinstance(record_id, str) else ""
