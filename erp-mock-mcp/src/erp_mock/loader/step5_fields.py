"""Step 5, part three: the fields, the quantity semantics, the linkages and the defaults.

``loader.step5_model`` resolved every name a document writes; this part settles that what
those names reached fits the engine that runs them. What it checks:

- the same-name declarations of the engine standard fields (requirement 1.6): such a
  declaration adds no field of its own, it gives that standard field its label and, when
  the field is writable, its defaults (requirement 1.5). So the type it writes must be the
  type of that standard field, a read-only standard field takes no default at all, no
  other field attribute may be written, and the value of a ``default`` must fit the type -
  which is known only here, a declaration without ``type`` writing none
  (:class:`~erp_mock.config.fields.StandardOverride`). A declaration without ``type``
  whose name is no standard field belongs to no field at all and is reported too, every
  other field declaration stating one of the 12 field types (requirement 1.2);
- the quantity semantics of a bill's lines and the unit conversion of the entity they
  measure (requirements 1.4 and 6.12): the material and unit fields are ref fields of the
  lines to entities; the quantity field is declared by a bill without pricing alone, a
  pricing bill measuring its standard ``qty``, and it is a qty field; and the measured
  entity declares a base unit field, a required ref field to the unit entity the lines
  name, whose conversion records name their material and their alternative unit with ref
  fields to those two entities and their factor with a numeric field;
- the inventory linkages (requirements 9.3 and 9.11): the warehouse fields the direction
  calls for are declared and are required ref fields of the bill, and the ones it does not
  call for are not declared; the quantity is the lines' ``qty_base``, so the lines have to
  declare quantity semantics; and an adjust linkage names a required enum field of the
  header whose values its ``increase`` and ``decrease`` divide between them, each once;
- the simple state machine (requirement 7.11): a bill with two states knows neither
  submit nor close, so it declares neither of them nor their reverse, and a source that
  cannot be closed cannot be closed automatically either - no writeback of a push whose
  source it is declares ``auto_close_source_when``;
- the order the ``default_from`` defaults of each level are applied in
  (:class:`~erp_mock.config.model.DefaultFrom`): such a default is read through a ref
  field of the same level, which may have a default of its own, so the fields are sorted
  by that dependency and the order is kept in the model (design.md 4.2). Fields that
  depend on each other in a circle have no such order and give one error for the whole
  cycle, locating every field of it (requirement 2.2).

Which check reads what: a declaration is what the pack wrote, which only the documents
show - the model has merged a same-name declaration into its standard field and holds a
linkage field the direction does not call for next to the ones it does - so whether
something was declared at all is read off the documents step 4 validated. What a
declaration reached is read off the model: the merged field view of each level, with the
type, the writability and the target of every field. A name that did not resolve leaves
None in the model and was reported by ``loader.step5_model``, so the check that would read
it is skipped and no item is reported twice.

:func:`check` returns the model with the order of the defaults filled in, which is what
step 5 hands on; everything it finds goes into the collector of the step, so one run
reports it together with the errors of the other parts (requirement 2.2).
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from pydantic import TypeAdapter, ValidationError

from erp_mock.common.decimals import PLACES
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.base import DECIMAL_PATTERN
from erp_mock.config.fields import DateText, FieldType, PeriodText, StandardOverride, TypedField
from erp_mock.config.model import (
    BillModel,
    DomainModel,
    EntityModel,
    FieldLevel,
    FieldModel,
)
from erp_mock.config.pack import (
    AdjustDef,
    BillDef,
    BillOperation,
    EntityDef,
    InventoryLinkageDef,
    LineQuantityDef,
)
from erp_mock.loader.errors import Location, StepErrors
from erp_mock.loader.step4 import LINKAGE_KEY, WRITEBACK_KEY, RawPack

__all__ = ["check"]

# The config path of the fields of each level, as a document writes them: the one level of
# an entity and the two of a bill (:mod:`erp_mock.config.pack`).
FIELDS_KEY: Final = "fields"
HEADER_KEY: Final = "header"
LINES_KEY: Final = "lines"
_LEVEL_KEYS: Final[Mapping[FieldLevel, str]] = {
    "record": FIELDS_KEY,
    "header": HEADER_KEY,
    "line": LINES_KEY,
}
# How an error names the fields of each level.
_LEVEL_NAMES: Final[Mapping[FieldLevel, str]] = {
    "record": "字段",
    "header": "表头字段",
    "line": "分录字段",
}

# The items of a bill, an entity and a linkage this part reports at.
QUANTITY_KEY: Final = "line_quantity"
UNIT_CONVERSION_KEY: Final = "unit_conversion"
OPERATIONS_KEY: Final = "operations"
AUTO_CLOSE_ITEM: Final = "auto_close_source_when"
DEFAULT_FROM_ITEM: Final = "default_from"

# What a same-name declaration of a standard field may say (requirement 1.6): the type of
# that field, its label and the three attributes that give it a default. The items are
# written as a document writes them, so an error names what the author has to remove.
_ALLOWED_ITEMS: Final[tuple[str, ...]] = (
    "type",
    "label",
    "default",
    DEFAULT_FROM_ITEM,
    "default_context",
)
# The attributes that give a field a default, which only a writable standard field takes.
_DEFAULT_ITEMS: Final[tuple[str, ...]] = ("default", DEFAULT_FROM_ITEM, "default_context")
# The one field attribute whose key differs from its name in the model.
_ITEM_KEYS: Final[Mapping[str, str]] = {"max_length": "maxLength"}
# The business context values each field type takes as its default (requirement 5.1): the
# anchor date for a date field, the default organization and the current user for a ref
# field; every other type takes none (:mod:`erp_mock.config.fields`).
_CONTEXT_VALUES: Final[Mapping[FieldType, tuple[str, ...]]] = {
    "date": ("today",),
    "ref": ("org", "user"),
}

# The types the factor of a conversion record may have (requirement 6.12).
FACTOR_TYPES: Final[tuple[FieldType, ...]] = ("int", "qty", "price", "money", "rate")

# The operations of the two states a simple bill does not have (requirement 7.11,
# meta-model.md section 6).
SIMPLE_FORBIDS: Final[tuple[BillOperation, ...]] = ("submit", "unsubmit", "close", "unclose")

# The quantity an inventory linkage reads: the base unit quantity of the line
# (requirement 9.11), which the lines of a bill with quantity semantics carry.
QTY_BASE: Final = "qty_base"
# The items that name the warehouses of a linkage, and which of them each direction uses
# (requirement 9.3). ``adjust`` names the field that says which way the stock moves.
WAREHOUSE_ITEM: Final = "warehouse_field"
FROM_WAREHOUSE_ITEM: Final = "from_warehouse_field"
TO_WAREHOUSE_ITEM: Final = "to_warehouse_field"
ADJUST_ITEM: Final = "adjust"
_WAREHOUSE_ITEMS: Final[tuple[str, ...]] = (WAREHOUSE_ITEM, FROM_WAREHOUSE_ITEM, TO_WAREHOUSE_ITEM)
_DIRECTION_ITEMS: Final[Mapping[str, tuple[str, ...]]] = {
    "in": (WAREHOUSE_ITEM,),
    "out": (WAREHOUSE_ITEM,),
    "adjust": (WAREHOUSE_ITEM, ADJUST_ITEM),
    "transfer": (FROM_WAREHOUSE_ITEM, TO_WAREHOUSE_ITEM),
}

# How a linkage writes a field of the header or of the lines of its bill
# (:data:`~erp_mock.config.pack.FieldPath`).
_PATH_LEVELS: Final[Mapping[str, FieldLevel]] = {"header": "header", "line": "line"}

# The value types a default is checked against: the ones of step 4, so a default of a
# declaration without ``type`` is read exactly as a typed declaration would have been
# (:mod:`erp_mock.config.fields`).
_DATE_TEXT: Final = TypeAdapter(DateText)
_PERIOD_TEXT: Final = TypeAdapter(PeriodText)
_DECIMAL_TEXT: Final = re.compile(DECIMAL_PATTERN)


def check(packs: Sequence[RawPack], domain: DomainModel, errors: StepErrors) -> DomainModel:
    """Run this part of step 5 on ``packs`` and ``domain``; return the model with the order.

    ``packs`` are the packs instance.yaml references, in its order, with the documents
    step 4 validated, and ``domain`` is the model ``loader.step5_model`` built from them.
    The returned model is that model with the order of the ``default_from`` defaults of
    every level filled in; this part of the step passed when ``errors`` stayed empty.
    """
    docs = _Docs(packs)
    _check_standard_fields(docs, domain, errors)
    _check_quantity_semantics(docs, domain, errors)
    _check_unit_conversions(domain, errors)
    _check_linkages(docs, domain, errors)
    _check_simple_machines(docs, domain, errors)
    return _with_default_order(domain, errors)


# --- the documents the model was built from ----------------------------------------------


@dataclass(frozen=True, slots=True)
class _Doc[D]:
    """One declared object: the pack that declares it, its file and the document."""

    pack: str
    file: str
    document: D


class _Docs:
    """The entity and bill documents of the packs by name, and the linkage documents.

    The names are collected the way the model's are (``loader.step5_model``), in the order
    of instance.yaml and then of the file path, keeping the first declaration of a name, so
    a document found here is the one the model was built from; a second declaration of the
    same name is a namespace conflict ``loader.step5_namespace`` reports. The linkages are
    taken as they come, a check of one reading its own document alone.
    """

    __slots__ = ("bills", "entities", "linkages")

    def __init__(self, packs: Sequence[RawPack]) -> None:
        self.entities: Mapping[str, _Doc[EntityDef]] = _by_name(
            (_Doc(pack.name, doc.file, doc.document) for pack in packs for doc in pack.entities),
            lambda document: document.entity,
        )
        self.bills: Mapping[str, _Doc[BillDef]] = _by_name(
            (_Doc(pack.name, doc.file, doc.document) for pack in packs for doc in pack.bills),
            lambda document: document.bill,
        )
        self.linkages: tuple[_Doc[InventoryLinkageDef], ...] = tuple(
            _Doc(pack.name, doc.file, doc.document) for pack in packs for doc in pack.linkages
        )


def _by_name[D](docs: Iterable[_Doc[D]], name_of: Callable[[D], str]) -> dict[str, _Doc[D]]:
    """The documents by the name each of them declares, keeping the first of a name."""
    found: dict[str, _Doc[D]] = {}
    for doc in docs:
        found.setdefault(name_of(doc.document), doc)
    return found


# --- one level of fields ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Owner:
    """One level of fields of the model, with what an error about such a field needs.

    ``kind`` and ``name`` are how an error names the entity or the bill, ``pack`` and
    ``file`` where it is declared, and ``fields`` the field view of ``level``, which holds
    the standard fields of that level merged with the declared ones.
    """

    kind: str
    name: str
    pack: str
    file: str
    level: FieldLevel
    fields: FrozenMap[str, FieldModel]

    def field(self, name: str) -> str:
        """How an error names one field of the level: ``单据 <bill> 的分录字段 qty``."""
        return f"{self.kind} {self.name} 的{_LEVEL_NAMES[self.level]} {name}"

    def path(self, name: str) -> str:
        """The config path of one field of the level: ``lines.qty``."""
        return f"{_LEVEL_KEYS[self.level]}.{name}"


def _entity_owner(entity: EntityModel) -> _Owner:
    """The one level of an entity as the owner of its fields."""
    return _Owner("实体", entity.name, entity.pack, entity.file, "record", entity.fields)


def _bill_owner(bill: BillModel, level: FieldLevel) -> _Owner:
    """One level of a bill as the owner of its fields."""
    return _Owner("单据", bill.name, bill.pack, bill.file, level, bill.level(level))


# --- the same-name declarations of the standard fields (requirement 1.6) -----------------


def _check_standard_fields(docs: _Docs, domain: DomainModel, errors: StepErrors) -> None:
    """Check every declaration with the name of a standard field (requirement 1.6).

    Every level of every entity and bill: an entity has one, a bill its header and its
    lines, and each of them merges the standard fields of that level with the declared
    ones (``loader.step5_model``).
    """
    for name, doc in docs.entities.items():
        entity = domain.entities.get(name)
        if entity is not None:
            _check_level(_entity_owner(entity), doc.document.fields, errors)
    for name, bill_doc in docs.bills.items():
        bill = domain.bills.get(name)
        if bill is None:
            continue
        _check_level(_bill_owner(bill, "header"), bill_doc.document.header, errors)
        _check_level(_bill_owner(bill, "line"), bill_doc.document.lines, errors)


def _check_level(
    owner: "_Owner", declared: Mapping[str, TypedField | StandardOverride], errors: StepErrors
) -> None:
    """Check the declarations of one level against the field view they produced."""
    for name, field in declared.items():
        standard = owner.fields.get(name)
        if standard is None:
            # A declaration without ``type`` whose name is no standard field: it gives no
            # standard field its defaults and declares no field of its own, since every
            # field declaration states its type (requirements 1.2 and 1.5).
            reason = f"{owner.field(name)} 不是引擎标准字段，字段声明必须写 type"
            errors.add_at(owner.pack, owner.file, owner.path(name), reason)
        elif standard.origin == "standard":
            _check_override(owner, field, standard, errors)


def _check_override(
    owner: "_Owner",
    declared: TypedField | StandardOverride,
    standard: FieldModel,
    errors: StepErrors,
) -> None:
    """Check one same-name declaration of the standard field ``standard`` (req 1.6).

    What it may say: the type of that standard field, its label and the three attributes
    that give it a default, those only when the field is writable; a default must fit the
    type of the standard field, which a declaration without ``type`` does not write. Every
    error names the entity or bill and the field (requirement 1.6).
    """
    what = owner.field(standard.name)
    path = owner.path(standard.name)
    written = declared.model_fields_set
    # A declaration that writes another type is one error, and its default is then left
    # alone: that value belongs to the type the author wrote, not to the engine's.
    kind = None if isinstance(declared, StandardOverride) else declared.type
    another_type = kind is not None and kind != standard.type
    if another_type:
        reason = f"{what} 是引擎标准字段，同名声明的类型 {kind} 与它的类型 {standard.type} 不同"
        errors.add_at(owner.pack, owner.file, f"{path}.type", reason)
    extra = sorted(item for item in written if item not in _ALLOWED_ITEMS)
    if extra:
        keys = "、".join(_ITEM_KEYS.get(item, item) for item in extra)
        reason = f"{what} 是引擎标准字段，同名声明只能写 {'、'.join(_ALLOWED_ITEMS)}，不能写 {keys}"
        errors.add_at(owner.pack, owner.file, path, reason)
    defaults = [item for item in _DEFAULT_ITEMS if item in written]
    if not defaults:
        return
    if not standard.writable:
        reason = f"{what} 是只读的引擎标准字段，不能用同名声明声明缺省值（{'、'.join(defaults)}）"
        errors.add_at(owner.pack, owner.file, path, reason)
        return
    if declared.default is not None and not another_type:
        problem = _default_problem(standard, declared.default)
        if problem is not None:
            reason = f"{what} 的缺省值与标准字段的类型 {standard.type} 不符：{problem}"
            errors.add_at(owner.pack, owner.file, f"{path}.default", reason)
    context = getattr(declared, "default_context", None)
    if context is not None and context not in _CONTEXT_VALUES.get(standard.type, ()):
        reason = f"{what} 的类型 {standard.type} 不接受 default_context {context}"
        errors.add_at(owner.pack, owner.file, f"{path}.default_context", reason)


def _default_problem(standard: FieldModel, value: str | int | bool) -> str | None:
    """Why ``value`` does not fit the type of ``standard``, None when it does.

    A declaration without ``type`` writes a string, an integer or a boolean
    (:data:`~erp_mock.config.fields.ScalarDefault`); whether that value is a constant of
    the type the engine keeps the standard field in is settled here, with the value types
    of step 4 (requirement 1.11).
    """
    kind = standard.type
    match kind:
        case "bool":
            return None if isinstance(value, bool) else "必须是布尔值"
        case "int":
            return None if isinstance(value, int) and not isinstance(value, bool) else "必须是整数"
        case "string" | "text":
            if not isinstance(value, str):
                return "必须是字符串"
            limit = standard.max_length
            return None if limit is None or len(value) <= limit else f"长度超过 {limit}"
        case "enum":
            if isinstance(value, str) and value in standard.enum_values:
                return None
            return f"必须是 {'、'.join(standard.enum_values)} 之一"
        case "date":
            return None if _fits(_DATE_TEXT, value) else "必须是 YYYY-MM-DD 形式的有效日期"
        case "period":
            return None if _fits(_PERIOD_TEXT, value) else "必须是 YYYY-MM 形式的会计期间"
        case "qty" | "price" | "money" | "rate":
            if not isinstance(value, str) or _DECIMAL_TEXT.fullmatch(value) is None:
                return "必须是十进制字符串"
            places = PLACES[kind]
            written = len(value.partition(".")[2])
            return None if written <= places else f"小数位数超过 {kind} 字段的精度 {places} 位"
        case "ref":
            return None if isinstance(value, str) else "必须是记录的内部 ID"


def _fits(adapter: TypeAdapter[str], value: str | int | bool) -> bool:
    """Whether ``value`` is a text of the value type ``adapter`` validates."""
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


# --- the quantity semantics of a bill's lines (requirements 1.4 and 6.12) ----------------


def _check_quantity_semantics(docs: _Docs, domain: DomainModel, errors: StepErrors) -> None:
    """Check the ``line_quantity`` of every bill that declares one (requirements 1.4, 6.12).

    It names fields of the lines, which ``loader.step5_model`` resolved, so a bill whose
    model holds no quantity semantics either declares none or wrote a name that did not
    resolve, and is skipped here.
    """
    for name, doc in docs.bills.items():
        bill = domain.bills.get(name)
        if bill is None or bill.line_quantity is None:
            continue
        quantity = bill.line_quantity
        _check_quantity_field(doc, bill, quantity, errors)
        material = _ref_entity(doc, bill, quantity.material_field, "material_field", domain, errors)
        unit = _ref_entity(doc, bill, quantity.unit_field, "unit_field", domain, errors)
        if material is not None and unit is not None:
            _check_measured_entity(doc, domain.entities[material], unit, quantity, errors)


def _check_quantity_field(
    doc: _Doc[BillDef], bill: BillModel, quantity: LineQuantityDef, errors: StepErrors
) -> None:
    """The quantity the lines measure (requirements 1.4 and 6.6).

    A bill with pricing measures its standard ``qty`` and declares no quantity field; a
    bill without pricing declares one, and it is a qty field of its lines.
    """
    path = f"{QUANTITY_KEY}.qty_field"
    if bill.pricing:
        if quantity.qty_field is not None:
            reason = (
                f"单据 {bill.name} 启用了 pricing，分录数量语义不声明数量字段（数量取标准字段 qty）"
            )
            errors.add_at(doc.pack, doc.file, path, reason)
        return
    if quantity.qty_field is None:
        reason = f"单据 {bill.name} 没有启用 pricing，分录数量语义必须声明 qty 类型的数量字段"
        errors.add_at(doc.pack, doc.file, path, reason)
        return
    field = bill.lines.get(quantity.qty_field)
    if field is not None and field.type != "qty":
        reason = (
            f"单据 {bill.name} 的分录数量语义的数量字段 {field.name}"
            f" 的类型是 {field.type}，必须是 qty"
        )
        errors.add_at(doc.pack, doc.file, path, reason)


def _ref_entity(
    doc: _Doc[BillDef],
    bill: BillModel,
    name: str,
    item: str,
    domain: DomainModel,
    errors: StepErrors,
) -> str | None:
    """The entity the ref field ``name`` of the lines refers to (requirement 6.12).

    None when the field is no ref field to an entity, which is reported, or when its
    target did not resolve, which ``loader.step5_model`` reported.
    """
    path = f"{QUANTITY_KEY}.{item}"
    what = f"单据 {bill.name} 的分录数量语义的字段 {name}"
    field = bill.lines.get(name)
    if field is None:
        return None
    if field.type != "ref":
        errors.add_at(doc.pack, doc.file, path, f"{what} 的类型是 {field.type}，必须是 ref")
        return None
    if field.ref is None:
        return None
    if field.ref not in domain.entities:
        errors.add_at(doc.pack, doc.file, path, f"{what} 引用的 {field.ref} 不是实体")
        return None
    return field.ref


def _check_measured_entity(
    doc: _Doc[BillDef],
    material: EntityModel,
    unit: str,
    quantity: LineQuantityDef,
    errors: StepErrors,
) -> None:
    """The measured entity declares a base unit of the unit entity the lines name (req 6.12).

    Without a base unit no factor can be read for a line at all, and a base unit that is a
    record of another entity than the unit of a line leaves the factors of that material
    inapplicable to the lines. The second is one error of two places: the field of the
    bill that names the unit entity, and the base unit field of the entity.
    """
    what = f"单据 {doc.document.bill} 的分录数量语义"
    conversion = material.unit_conversion
    if conversion is None:
        reason = (
            f"{what}的物料引用字段 {quantity.material_field} 指向的实体"
            f" {material.name} 没有声明基本单位字段（{UNIT_CONVERSION_KEY}）"
        )
        errors.add_at(doc.pack, doc.file, f"{QUANTITY_KEY}.material_field", reason)
        return
    if conversion.unit_entity is None or conversion.unit_entity == unit:
        # A base unit field that is no ref field is reported at the entity itself.
        return
    places = (
        Location(doc.file, f"{QUANTITY_KEY}.unit_field"),
        Location(material.file, f"{UNIT_CONVERSION_KEY}.base_unit_field"),
    )
    reason = (
        f"{what}的计量单位引用字段 {quantity.unit_field} 指向实体 {unit}，"
        f"而物料实体 {material.name} 的基本单位字段 {conversion.base_unit_field}"
        f" 指向实体 {conversion.unit_entity}"
    )
    errors.add(doc.pack, places, reason)


# --- the unit conversion of a measured entity (requirement 6.12) -------------------------


def _check_unit_conversions(domain: DomainModel, errors: StepErrors) -> None:
    """Check the ``unit_conversion`` of every entity that declares one (requirement 6.12).

    The base unit field belongs to the entity itself and the other three to the entity
    that holds its conversion records, each of which says how many base units one of its
    alternative units is (requirement 6.6). ``loader.step5_model`` resolved all five, so an
    entity whose model holds no unit conversion declares none or wrote a name that did not
    resolve.
    """
    for entity in domain.entities.values():
        conversion = entity.unit_conversion
        if conversion is None:
            continue
        base = entity.fields.get(conversion.base_unit_field)
        if base is not None and (base.type != "ref" or not base.required):
            reason = (
                f"实体 {entity.name} 的基本单位字段 {conversion.base_unit_field}"
                f" 必须是必填的 ref 字段，当前为{_state(base)}的 {base.type} 字段"
            )
            errors.add_at(
                entity.pack, entity.file, f"{UNIT_CONVERSION_KEY}.base_unit_field", reason
            )
        records = domain.entities.get(conversion.entity)
        if records is None:
            continue
        _check_record_ref(entity, records, conversion.material_field, "material_field", errors)
        if base is not None and base.type == "ref" and base.ref is not None:
            _check_record_ref(
                entity, records, conversion.unit_field, "unit_field", errors, target=base.ref
            )
        factor = records.fields.get(conversion.factor_field)
        if factor is not None and factor.type not in FACTOR_TYPES:
            reason = (
                f"换算关系实体 {records.name} 的换算系数字段 {factor.name} 的类型"
                f" {factor.type} 不是 {'、'.join(FACTOR_TYPES)} 之一"
            )
            errors.add_at(entity.pack, entity.file, f"{UNIT_CONVERSION_KEY}.factor_field", reason)


def _check_record_ref(
    entity: EntityModel,
    records: EntityModel,
    name: str,
    item: str,
    errors: StepErrors,
    target: str | None = None,
) -> None:
    """One ref field of a conversion record (requirement 6.12).

    ``target`` is the entity it must refer to, the measured entity itself for the material
    field and the entity of the base unit for the alternative unit. The error is reported
    at the ``unit_conversion`` of the measured entity, which is where the field is named.
    """
    expected = entity.name if target is None else target
    field = records.fields.get(name)
    if field is None or (field.type == "ref" and field.ref == expected):
        return
    current = f"ref: {field.ref}" if field.type == "ref" else field.type
    reason = (
        f"换算关系实体 {records.name} 的字段 {name} 必须是指向实体 {expected} 的 ref 字段，"
        f"当前为 {current}"
    )
    errors.add_at(entity.pack, entity.file, f"{UNIT_CONVERSION_KEY}.{item}", reason)


def _state(field: FieldModel) -> str:
    """How an error says whether a field is required."""
    return "必填" if field.required else "非必填"


# --- the inventory linkages (requirements 9.3 and 9.11) ----------------------------------


def _check_linkages(docs: _Docs, domain: DomainModel, errors: StepErrors) -> None:
    """Check every inventory linkage against its direction and its bill (req 9.3, 9.11)."""
    for doc in docs.linkages:
        bill = domain.bills.get(doc.document.bill)
        if bill is None:
            # The bill did not resolve and was reported by ``loader.step5_model``.
            continue
        _check_warehouses(doc, bill, errors)
        _check_linkage_quantity(doc, bill, docs, errors)
        _check_adjust(doc, bill, errors)


def _check_warehouses(doc: _Doc[InventoryLinkageDef], bill: BillModel, errors: StepErrors) -> None:
    """The warehouse fields of the linkage's direction (requirements 9.3 and 9.11).

    The direction decides which of them apply: one warehouse for in, out and adjust, the
    two warehouses of a transfer. Each of those must be declared and be a required ref
    field of the bill, and the ones the direction does not use must not be declared, a
    stock transaction being written for the warehouses of the direction alone.
    """
    linkage = doc.document
    applies = _DIRECTION_ITEMS[linkage.direction]
    for item in _WAREHOUSE_ITEMS:
        text = _warehouse_item(linkage, item)
        if item not in applies:
            if text is not None:
                reason = f"direction 为 {linkage.direction} 的库存联动不声明 {item}"
                errors.add_at(doc.pack, doc.file, f"{LINKAGE_KEY}.{item}", reason)
            continue
        if text is None:
            reason = f"direction 为 {linkage.direction} 的库存联动必须声明 {item}"
            errors.add_at(doc.pack, doc.file, LINKAGE_KEY, reason)
            continue
        field = _bill_field(bill, text)
        if field is None or (field.type == "ref" and field.required):
            # A path that did not resolve was reported by ``loader.step5_model``.
            continue
        reason = (
            f"库存联动的仓库字段 {text} 必须是单据 {bill.name} 中必填的 ref 字段，"
            f"当前为{_state(field)}的 {field.type} 字段"
        )
        errors.add_at(doc.pack, doc.file, f"{LINKAGE_KEY}.{item}", reason)


def _check_linkage_quantity(
    doc: _Doc[InventoryLinkageDef], bill: BillModel, docs: _Docs, errors: StepErrors
) -> None:
    """The quantity of a linkage: the ``qty_base`` of measured lines (requirement 9.11)."""
    declared = docs.bills.get(bill.name)
    if declared is not None and declared.document.line_quantity is None:
        reason = (
            f"单据 {bill.name} 的分录没有声明数量语义（{QUANTITY_KEY}），库存联动取不到基本单位数量"
        )
        errors.add_at(doc.pack, doc.file, f"{LINKAGE_KEY}.bill", reason)
    qty_field = doc.document.qty_field
    if qty_field != f"line.{QTY_BASE}":
        reason = f"库存联动的数量字段必须是 line.{QTY_BASE}，不能是 {qty_field}"
        errors.add_at(doc.pack, doc.file, f"{LINKAGE_KEY}.qty_field", reason)


def _check_adjust(doc: _Doc[InventoryLinkageDef], bill: BillModel, errors: StepErrors) -> None:
    """The ``adjust`` of a linkage: the enum field its values divide (requirement 9.3).

    An adjust linkage reads the direction of its lines off one value of the bill, so that
    field is a required enum field of the header and every one of its values says one
    direction: ``increase`` and ``decrease`` list them all, each exactly once.
    """
    linkage = doc.document
    adjust = linkage.adjust
    if adjust is None:
        if linkage.direction == "adjust":
            reason = f"direction 为 adjust 的库存联动必须声明 {ADJUST_ITEM}"
            errors.add_at(doc.pack, doc.file, LINKAGE_KEY, reason)
        return
    if linkage.direction != "adjust":
        reason = f"direction 为 {linkage.direction} 的库存联动不声明 {ADJUST_ITEM}"
        errors.add_at(doc.pack, doc.file, f"{LINKAGE_KEY}.{ADJUST_ITEM}", reason)
        return
    field = _bill_field(bill, adjust.field)
    if field is None:
        return
    level = _PATH_LEVELS[adjust.field.partition(".")[0]]
    if level != "header" or field.type != "enum" or not field.required:
        reason = (
            f"库存联动的增减方向字段 {adjust.field} 必须是单据 {bill.name} 表头中必填的"
            f" enum 字段，当前为{_state(field)}的 {field.type} 类型{_LEVEL_NAMES[level]}"
        )
        errors.add_at(doc.pack, doc.file, f"{LINKAGE_KEY}.{ADJUST_ITEM}.field", reason)
        return
    _check_adjust_values(doc, field, adjust, errors)


def _check_adjust_values(
    doc: _Doc[InventoryLinkageDef], field: FieldModel, adjust: AdjustDef, errors: StepErrors
) -> None:
    """``increase`` and ``decrease`` list the values of ``field``, each once (req 9.3)."""
    path = f"{LINKAGE_KEY}.{ADJUST_ITEM}"
    listed = [*adjust.increase, *adjust.decrease]
    problems = [
        (
            [value for value in dict.fromkeys(listed) if value not in field.enum_values],
            "不是该字段的取值",
        ),
        (
            [value for value in field.enum_values if value not in listed],
            "没有列在 increase 或 decrease 中",
        ),
        (
            [value for value in dict.fromkeys(adjust.increase) if value in adjust.decrease],
            "同时列在 increase 和 decrease 中",
        ),
    ]
    for values, problem in problems:
        if values:
            reason = (
                f"增减方向字段 {field.name} 的取值与增减方向的对应关系不一致："
                f"{'、'.join(values)} {problem}"
            )
            errors.add_at(doc.pack, doc.file, path, reason)


def _warehouse_item(linkage: InventoryLinkageDef, item: str) -> str | None:
    """The field path one warehouse item of ``linkage`` writes, None when it writes none."""
    written = {
        WAREHOUSE_ITEM: linkage.warehouse_field,
        FROM_WAREHOUSE_ITEM: linkage.from_warehouse_field,
        TO_WAREHOUSE_ITEM: linkage.to_warehouse_field,
    }
    return written[item]


def _bill_field(bill: BillModel, text: str) -> FieldModel | None:
    """The field a ``header.<field>`` or ``line.<field>`` path names, None when unknown."""
    prefix, _, name = text.partition(".")
    level = _PATH_LEVELS.get(prefix)
    return None if level is None else bill.level(level).get(name)


# --- the simple state machine (requirement 7.11) -----------------------------------------


def _check_simple_machines(docs: _Docs, domain: DomainModel, errors: StepErrors) -> None:
    """Check what a bill with two states must not declare (requirement 7.11).

    A simple bill has draft and approved alone, so it knows neither submit nor close; and
    a source that cannot be closed cannot be closed automatically, so no writeback of a
    push whose source it is declares ``auto_close_source_when``.
    """
    for name, doc in docs.bills.items():
        bill = domain.bills.get(name)
        if bill is None or bill.state_machine != "simple":
            continue
        for index, operation in enumerate(doc.document.operations):
            if operation in SIMPLE_FORBIDS:
                reason = (
                    f"单据 {bill.name} 的 state_machine 为 simple，"
                    f"没有 {'、'.join(SIMPLE_FORBIDS)} 操作，不能声明 {operation}"
                )
                errors.add_at(doc.pack, doc.file, f"{OPERATIONS_KEY}[{index}]", reason)
    for writeback in domain.writebacks.values():
        if writeback.auto_close_source_when is None:
            continue
        conversion = domain.conversions.get(writeback.conversion)
        source = None if conversion is None else domain.bills.get(conversion.source)
        if source is None or source.state_machine != "simple":
            continue
        reason = (
            f"单据 {source.name} 的 state_machine 为 simple，没有 closed 状态，"
            f"writeback {writeback.id} 不能声明 {AUTO_CLOSE_ITEM}"
        )
        errors.add_at(writeback.pack, writeback.file, f"{WRITEBACK_KEY}.{AUTO_CLOSE_ITEM}", reason)


# --- the order the defaults are applied in -----------------------------------------------


def _with_default_order(domain: DomainModel, errors: StepErrors) -> DomainModel:
    """Return ``domain`` with the order of the ``default_from`` defaults of every level.

    One order per level: the fields with a ``default_from``, each after the field its own
    default is read through (:class:`~erp_mock.config.model.DefaultFrom`). Fields that
    depend on each other in a circle get no place in that order and are reported, one
    error per cycle (requirement 2.2).
    """
    entities = FrozenMap(
        (name, _ordered_entity(entity, errors)) for name, entity in domain.entities.items()
    )
    bills = FrozenMap((name, _ordered_bill(bill, errors)) for name, bill in domain.bills.items())
    return replace(domain, entities=entities, bills=bills)


def _ordered_entity(entity: EntityModel, errors: StepErrors) -> EntityModel:
    """``entity`` with the order of the defaults of its one level."""
    return replace(entity, default_order=_order(_entity_owner(entity), errors))


def _ordered_bill(bill: BillModel, errors: StepErrors) -> BillModel:
    """``bill`` with the order of the defaults of each of its two levels."""
    return replace(
        bill,
        header_default_order=_order(_bill_owner(bill, "header"), errors),
        line_default_order=_order(_bill_owner(bill, "line"), errors),
    )


def _order(owner: _Owner, errors: StepErrors) -> tuple[str, ...]:
    """The order the defaults of one level are applied in; report every cycle it holds."""
    # A field with a ``default_from`` reads it through one ref field of the same level, so
    # it depends on that field, and only when that field has a default of its own: a ref
    # field the caller fills, or a constant or the context fills, is there before this
    # pass runs (design.md 4.2).
    sources = {
        name: field.default_from.ref_field
        for name, field in owner.fields.items()
        if field.default_from is not None
    }
    names = tuple(sources)
    depends = {name: ref for name, ref in sources.items() if ref in sources}
    placed = _sorted(names, depends)
    for cycle in _cycles(names, depends, frozenset(placed)):
        _report_cycle(owner, cycle, errors)
    return placed


def _sorted(names: Sequence[str], depends: Mapping[str, str]) -> tuple[str, ...]:
    """``names`` with every field after the one it depends on; a cycle is left out.

    ``depends`` holds the dependencies inside ``names``, at most one per field, so the
    fields that depend on none of them come first, in the order of the field view, and
    each of the others follows as soon as the field it depends on is placed.
    """
    successors: dict[str, list[str]] = {}
    for name, ref in depends.items():
        successors.setdefault(ref, []).append(name)
    ready = [name for name in names if name not in depends]
    placed: list[str] = []
    while ready:
        name = ready.pop(0)
        placed.append(name)
        ready.extend(successors.get(name, ()))
    return tuple(placed)


def _cycles(
    names: Sequence[str], depends: Mapping[str, str], placed: frozenset[str]
) -> list[tuple[str, ...]]:
    """The cycles among the fields of ``names`` that :func:`_sorted` left out.

    Every field left out depends on a field that was left out too, and on exactly one, so
    following the dependencies from it walks into a cycle and then round it. Each cycle is
    one error: every field of a walk is taken as seen, so a field that only leads into a
    cycle adds none of its own.
    """
    found: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for start in names:
        if start in placed or start in seen:
            continue
        walked: dict[str, int] = {}
        name = start
        while name not in placed and name not in seen and name not in walked:
            walked[name] = len(walked)
            name = depends[name]
        if name in walked:
            found.append(tuple(walked)[walked[name] :])
        seen.update(walked)
    return found


def _report_cycle(owner: _Owner, cycle: Sequence[str], errors: StepErrors) -> None:
    """Record one cycle of ``default_from`` dependencies, locating each field of it."""
    places = [Location(owner.file, f"{owner.path(name)}.{DEFAULT_FROM_ITEM}") for name in cycle]
    reason = f"{owner.field('、'.join(cycle))} 的 {DEFAULT_FROM_ITEM} 构成循环依赖"
    errors.add(owner.pack, places, reason)
