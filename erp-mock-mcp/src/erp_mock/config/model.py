"""The resolved domain model: the configuration as the engine reads it (design.md 2.4).

Loading step 5 turns the validated documents of the business packs (:mod:`erp_mock.config.pack`)
into this model and resolves every name they reference (requirement 2.9). From there on the
tool generator, the storage, the expression checks and the result assembly read the domain
model instead of the documents, so no consumer derives the same thing twice (design.md data
model 1). Every object here is a ``frozen=True, slots=True`` dataclass whose collections are
tuples and FrozenMaps, so the loaded configuration rejects every modification attempt and
keeps its content (requirement 2.11).

The field view is the centre of the model: :class:`BillModel` merges the engine standard
fields of meta-model.md section 4 with the fields the pack declares into one ordered
sequence of :class:`FieldModel` per level (``header`` and ``lines``), and
:class:`EntityModel` does the same for the single level of an entity. What that merge
settles:

- the standard fields come first, in the order of meta-model.md section 4 - the fields of
  every bill, then those of pricing, then ``qty_base`` for lines that declare quantity
  semantics without pricing (requirement 1.4), and last the link field of a push whose
  source the bill is: ``linked_qty`` on the lines when the push's limit_by is qty,
  ``linked_amount`` on the header when it is amount;
- a declaration with the name of a standard field adds no field (requirement 1.5): it gives
  that standard field its ``label`` and, when the field is writable, its ``default``,
  ``default_from`` and ``default_context``, as ``tax_rate: { default_from: material.tax_rate }``
  does for a pricing line. What such a declaration may say at all is checked by step 5
  itself (requirement 1.6), which reports a different type, a default on a read-only
  standard field and any other attribute;
- the declared fields follow, in declaration order.

The view therefore does not say which names the pack wrote, nor in which order: a same-name
declaration leaves its standard field where the engine put it. ``declared_fields`` of an
entity and ``header_declared_fields`` and ``line_declared_fields`` of a bill keep that,
the names of the level in declaration order, same-name declarations included. The parameter
order of a generated tool is built from them (requirement 11.4), which is how a pack moves
a standard field - ``price_includes_tax``, a line's ``tax_rate`` - to the place it belongs
in its own tools.

:attr:`FieldModel.writable` is the engine's answer, never the pack's: of the standard fields
only the header's ``biz_date``, ``remark`` and ``price_includes_tax`` and the lines' ``qty``,
``price`` and ``tax_rate`` can be written by a write tool, every other standard field is
read-only (requirement 1.3); a declared field is writable unless it declares ``readonly``.
:attr:`FieldModel.required` stays what the pack declared: the fields R-GEN-002 requires at
submit (requirement 7.5) and the ``required`` of an inputSchema (requirement 11.12) add the
engine's own fields on their own terms, each at the place that states them.

What is not here: the internal columns of the storage (``_bill_id``, ``_close_mode``,
``_conversion``, requirement 5.12 keeps them out of every result), which the state layer
adds, and the parsed rule expressions, which live outside this layer - ``check``, ``value``
and ``filter`` are the text step 5 checks (requirement 10.2).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.base import BilingualLabel, Label
from erp_mock.config.fields import DEFAULT_MAX_LENGTH, FieldType
from erp_mock.config.pack import (
    BillOperation,
    BillStatus,
    CodeDef,
    EntityOperation,
    LineQuantityDef,
    Primitive,
    RuleErrorCode,
    WriteOperation,
    entity_standard_fields,
    header_standard_fields,
    line_standard_fields,
)

__all__ = [
    "ENGINE_RECORDS",
    "ENTITY_STATUSES",
    "LINKED_AMOUNT",
    "LINKED_QTY",
    "LINE_SUFFIX",
    "SIMPLE_STATUSES",
    "STANDARD_STATUSES",
    "STOCK_BALANCE",
    "STOCK_TXN",
    "BillModel",
    "ConversionModel",
    "DefaultFrom",
    "DomainModel",
    "EntityModel",
    "FieldLevel",
    "FieldModel",
    "FieldOrigin",
    "FieldRef",
    "LinkageModel",
    "QueryModel",
    "QueryParamModel",
    "RuleModel",
    "SearchModel",
    "SortRef",
    "StateMachine",
    "UnitConversionModel",
    "WritebackModel",
    "entity_standard_view",
    "header_standard_view",
    "line_standard_view",
]

# Where a field lives: the one level of an entity record, or the header or the lines of a
# bill, written ``header.<field>`` and ``line.<field>`` in a configuration
# (:data:`~erp_mock.config.pack.FieldPath`).
type FieldLevel = Literal["record", "header", "line"]
# Who the field comes from: the engine's standard fields of meta-model.md section 4, or the
# declaration of the business pack.
type FieldOrigin = Literal["standard", "declared"]
# The two preset state machines (meta-model.md section 6).
type StateMachine = Literal["standard", "simple"]

# The lines of a bill are named ``<bill>_line``, the name that takes part in the namespace
# of the instance (requirement 2.7) and names the line table (design.md data model 3).
LINE_SUFFIX: Final = "_line"

# The two records the engine keeps itself: the stock transactions of the inventory
# primitive and the stock balance of a material in a warehouse (requirement 9.1,
# design.md data model 3). Their names come from the engine and not from a pack, and a
# diff reports a change of them under these names (requirement 18.4), so they take part in
# the namespace of the instance as well: no entity, bill or bill line may be named like
# one of them (requirement 2.7).
STOCK_TXN: Final = "stock_txn"
STOCK_BALANCE: Final = "stock_balance"
ENGINE_RECORDS: Final[tuple[str, ...]] = (STOCK_TXN, STOCK_BALANCE)

# The states of each preset state machine, in the order they are reached; the values of the
# ``status`` field of a bill (meta-model.md section 6).
STANDARD_STATUSES: Final[tuple[BillStatus, ...]] = ("draft", "submitted", "approved", "closed")
SIMPLE_STATUSES: Final[tuple[BillStatus, ...]] = ("draft", "approved")
# The states of an entity whose ``has_status`` is true; a disabled record must not be used
# by a new bill (R-GEN-006, requirement 5.7).
ENTITY_STATUSES: Final = ("enabled", "disabled")

# The link field a push maintains on its source when the pack's writeback names no other
# one: the base unit quantity of the source line for a push limited by qty, the amount of
# the source bill for one limited by amount (meta-model.md section 7).
LINKED_QTY: Final = "linked_qty"
LINKED_AMOUNT: Final = "linked_amount"


# --- fields ------------------------------------------------------------------------------


# The enum values of a field that is no enum field, and the default of that attribute.
_NO_VALUES: Final[FrozenMap[str, BilingualLabel]] = FrozenMap()


@dataclass(frozen=True, slots=True)
class DefaultFrom:
    """A resolved ``default_from``: the default comes from a referenced record.

    ``material.purchase_unit_id`` on a line means: follow the line's ref field
    ``material_id`` (:attr:`ref_field`) to the record it refers to, which is a record of
    :attr:`entity`, and take its :attr:`field` (requirement 5.1). Step 5 resolves all
    three; the ref field may be a standard field, as ``org.name`` is.

    Such a default is read through a ref field that may have a default of its own, so the
    order the defaults of a level are applied in matters: ``default_order`` of
    :class:`EntityModel` and ``header_default_order`` and ``line_default_order`` of
    :class:`BillModel` hold the fields with a ``default_from``, each after the field its
    own default is read through. Step 5 sorts them and reports a cycle, so the order holds
    every such field of a valid instance.
    """

    ref_field: str
    entity: str
    field: str


@dataclass(frozen=True, slots=True)
class FieldModel:
    """One field of an entity or of one level of a bill, standard or declared.

    - ``type`` is one of the 12 field types of meta-model.md section 3, for a standard
      field the type the engine keeps it in.
    - ``writable`` says whether a write tool may write it (requirement 1.3): no standard
      field but the header's ``biz_date``, ``remark`` and ``price_includes_tax`` and the
      lines' ``qty``, ``price`` and ``tax_rate``, and every declared field that does not
      declare ``readonly``.
    - ``computed`` marks a value the engine derives from the other data and keeps up to
      date on every write: the amounts of pricing, ``qty_base``, and the link field a
      writeback maintains (requirement 6.1, 6.6 and 8.8).
    - ``required``, ``unique``, ``label``, ``default``, ``default_from`` and
      ``default_context`` are what the pack declared, together with the defaults the engine
      brings itself (``biz_date`` from the anchor date, ``org_id`` from the default
      organization, ``price_includes_tax`` true, an entity's ``status`` enabled,
      requirements 5.1 and 6.3).
    - ``ref`` is the entity or bill a ref field refers to, None for the header's
      ``source_bill_id``, whose target is the bill named by ``source_bill_type``, and None
      for a target step 5 could not resolve.
    - ``values`` are the allowed values of a qty, price, money or rate field
      (requirement 1.7), ``enum_values`` the codes and labels of an enum field, and
      ``max_length`` the length limit of a string or text field, the default of the type
      when the declaration gives none.
    """

    name: str
    type: FieldType
    origin: FieldOrigin
    writable: bool
    computed: bool = False
    label: Label | None = None
    required: bool = False
    unique: bool = False
    ref: str | None = None
    values: tuple[str, ...] = ()
    enum_values: FrozenMap[str, BilingualLabel] = _NO_VALUES
    max_length: int | None = None
    default: str | int | bool | None = None
    default_from: DefaultFrom | None = None
    default_context: Literal["today", "org", "user"] | None = None

    @property
    def column(self) -> str:
        """The column the value is stored in: the field's own name (design.md data model 3).

        Only the engine's internal columns, which are no fields, start with an underscore.
        """
        return self.name

    @property
    def nested(self) -> bool:
        """Whether a result returns the field as a nested object (requirement 13.3).

        True for every ref field, the header's ``source_bill_id`` included, whose key in a
        result is the name without ``_id``; the lines' ``source_line_id`` keeps its name
        and its string value, and so is not nested.
        """
        return self.type == "ref"


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A reference to one field of an object: its level and its name.

    How a configuration names a field of another object's level: ``header.warehouse_id``
    and ``line.qty_base`` of a linkage or a declarative query
    (:data:`~erp_mock.config.pack.FieldPath`), and ``source_line.linked_qty`` of a
    writeback. The level of a field of an entity is ``record``.
    """

    level: FieldLevel
    name: str


@dataclass(frozen=True, slots=True)
class SortRef:
    """One sort key: the field and its direction; no direction means ascending (req 5.14)."""

    field: FieldRef
    descending: bool = False


@dataclass(frozen=True, slots=True)
class SearchModel:
    """The ``search`` of an entity or a bill, with every field resolved (req 5.9 to 5.14).

    Empty lists are the defaults of the declaration: without ``keyword_fields`` the search
    tool has no keyword parameter, without ``filters`` no filters, and without
    ``default_sort`` the records are ordered by id alone.
    """

    keyword_fields: tuple[FieldRef, ...] = ()
    filters: tuple[FieldRef, ...] = ()
    default_sort: tuple[SortRef, ...] = ()


# --- entities ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitConversionModel:
    """The ``unit_conversion`` of the measured entity, resolved (requirement 6.8).

    ``base_unit_field`` is the ref field of this entity that names the base unit and
    ``unit_entity`` the entity it refers to, the unit entity of the instance. ``entity``
    holds the conversion records, each of which names its material (``material_field``),
    its alternative unit (``unit_field``) and how many base units one of them is
    (``factor_field``). Step 5 checks the types of all five (requirement 6.12), so
    ``unit_entity`` is None when ``base_unit_field`` is no ref field.
    """

    base_unit_field: str
    unit_entity: str | None
    entity: str
    material_field: str
    unit_field: str
    factor_field: str


@dataclass(frozen=True, slots=True)
class EntityModel:
    """An entity: basic data, one record per row (meta-model.md section 3).

    ``pack`` and ``file`` are where it is declared, so a later step can report an error at
    that document. ``fields`` is the field view of the record, standard fields first:
    ``id``, the generated ``code`` when ``code`` is declared, and ``status`` when
    ``has_status`` is true. ``context`` says whether the entity is the organization or the
    user context entity, and ``default_flag`` names the bool field whose only true record
    is the default organization (requirement 1.8). ``default_order`` is the order the
    ``default_from`` defaults of the record are applied in (:data:`DefaultFrom`), and
    ``declared_fields`` the declaration order of the level (see the module docstring).
    """

    name: str
    pack: str
    file: str
    id_prefix: str
    fields: FrozenMap[str, FieldModel]
    label: Label | None = None
    code: CodeDef | None = None
    shared: bool = False
    has_status: bool = False
    context: Literal["org", "user"] | None = None
    default_flag: str | None = None
    unit_conversion: UnitConversionModel | None = None
    operations: tuple[EntityOperation, ...] = ()
    search: SearchModel | None = None
    default_order: tuple[str, ...] = ()
    declared_fields: tuple[str, ...] = ()


# --- bills -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BillModel:
    """A bill: a header and its lines (meta-model.md section 4).

    ``header`` and ``lines`` are the two field views, each merging the standard fields of
    the level with the declared ones (see the module docstring). ``line_quantity`` is the
    quantity semantics of the lines (requirement 1.4) and :attr:`qty_field` the quantity it
    measures, the standard ``qty`` of a pricing bill or the declared field of a bill
    without pricing. ``header_default_order`` and ``line_default_order`` are the order the
    ``default_from`` defaults of each level are applied in (:data:`DefaultFrom`), and
    ``header_declared_fields`` and ``line_declared_fields`` the declaration order of each
    level (see the module docstring).
    """

    name: str
    pack: str
    file: str
    id_prefix: str
    number_prefix: str
    state_machine: StateMachine
    header: FrozenMap[str, FieldModel]
    lines: FrozenMap[str, FieldModel]
    label: Label | None = None
    pricing: bool = False
    partner_field: str | None = None
    line_quantity: LineQuantityDef | None = None
    operations: tuple[BillOperation, ...] = ()
    search: SearchModel | None = None
    header_default_order: tuple[str, ...] = ()
    line_default_order: tuple[str, ...] = ()
    header_declared_fields: tuple[str, ...] = ()
    line_declared_fields: tuple[str, ...] = ()

    @property
    def line_name(self) -> str:
        """``<bill>_line``, the name of the lines (requirement 2.7, design.md 3)."""
        return f"{self.name}{LINE_SUFFIX}"

    @property
    def statuses(self) -> tuple[BillStatus, ...]:
        """The states of the bill's preset state machine (meta-model.md section 6)."""
        return STANDARD_STATUSES if self.state_machine == "standard" else SIMPLE_STATUSES

    @property
    def qty_field(self) -> str | None:
        """The quantity field of the lines, None without quantity semantics (req 6.6).

        The standard ``qty`` when pricing is on, otherwise the ``qty_field`` the
        ``line_quantity`` declares.
        """
        if self.line_quantity is None:
            return None
        return "qty" if self.pricing else self.line_quantity.qty_field

    def level(self, level: FieldLevel) -> FrozenMap[str, FieldModel]:
        """The field view of ``header`` or ``line``; ``record`` has no fields on a bill."""
        match level:
            case "header":
                return self.header
            case "line":
                return self.lines
            case "record":
                return FrozenMap()


# --- conversion, writeback and linkage ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConversionModel:
    """A push from a source bill to a target bill draft, resolved (meta-model.md 7).

    ``header_mapping`` and ``line_mapping`` map a field of the target to the field of the
    source header or source line it takes its value from, the ``source.`` and
    ``source_line.`` prefixes removed (requirement 8.1). ``link_field`` is the field of the
    source the push's quota is counted against and the writeback keeps up to date: the
    target of the writeback of this conversion, or :data:`LINKED_QTY` and
    :data:`LINKED_AMOUNT` when no writeback names one; it lives on the source's lines when
    ``limit_by`` is qty and on its header when ``limit_by`` is amount (requirement 8.5
    and 8.6).
    """

    id: str
    pack: str
    file: str
    source: str
    target: str
    require_source_status: BillStatus
    limit_by: Literal["qty", "amount"]
    link_field: FieldRef
    line_selection: Literal["by_source_line_id"] = "by_source_line_id"
    header_mapping: FrozenMap[str, str] = FrozenMap()
    line_mapping: FrozenMap[str, str] = FrozenMap()
    required_line_inputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WritebackModel:
    """The update of the source bill when a pushed bill is approved (meta-model.md 7).

    ``target`` is the field of the source the value is added to, on its header for a
    ``source.<field>`` target and on its lines for a ``source_line.<field>`` one, which
    also decides whether ``value`` is evaluated once or per target line (requirement 8.8).
    ``value`` is the rule expression as written; step 5 checks it (requirement 10.2).
    """

    id: str
    pack: str
    file: str
    conversion: str
    target: FieldRef
    value: str
    on: Literal["approve"] = "approve"
    op: Literal["add"] = "add"
    reverse_on: Literal["unapprove"] = "unapprove"
    auto_close_source_when: Literal["all_lines_linked"] | None = None


@dataclass(frozen=True, slots=True)
class LinkageModel:
    """An inventory linkage of one bill, with every field resolved (requirement 9).

    Which fields apply follows ``direction``: ``warehouse`` for in, out and adjust,
    ``from_warehouse`` and ``to_warehouse`` for transfer, and ``adjust_field`` with
    ``increase`` and ``decrease`` for adjust, whose values must partition the values of
    that enum field (requirement 9.2 and 9.3). ``qty`` is the base unit quantity of the
    line (requirement 9.11). A field step 5 could not resolve is None.
    """

    id: str
    pack: str
    file: str
    bill: str
    primitive: Literal["inventory"]
    direction: Literal["in", "out", "transfer", "adjust"]
    qty: FieldRef | None
    on: Literal["approve"] = "approve"
    warehouse: FieldRef | None = None
    from_warehouse: FieldRef | None = None
    to_warehouse: FieldRef | None = None
    adjust_field: FieldRef | None = None
    increase: tuple[str, ...] = ()
    decrease: tuple[str, ...] = ()


# --- rules and queries -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleModel:
    """A pack rule, enforced by the engine or described to the model (meta-model.md 9).

    An enforced rule declares ``bill``, ``on``, ``check``, ``error_code``, the optional
    ``field`` of its detail item and ``message``; the engine evaluates ``check`` after each
    operation of ``on`` and ends the call with that code, this rule's id and ``message.zh``
    when it is false (requirement 10.7). A described rule declares ``description`` and
    ``applies_to_tools`` instead: the engine does not check it, and step 7 requires its
    ``description.zh`` to appear verbatim in the zh-detailed description of each of those
    tools (requirement 3.6). ``check`` is the text step 5 checks (requirement 10.2).
    """

    id: str
    pack: str
    file: str
    enforcement: Literal["enforced", "described"]
    bill: str | None = None
    on: tuple[WriteOperation, ...] = ()
    check: str | None = None
    error_code: RuleErrorCode | None = None
    field: FieldRef | None = None
    message: Label | None = None
    description: Label | None = None
    applies_to_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryParamModel:
    """One parameter of a declarative query (requirement 5.13).

    ``maps_to`` is the field of the source record the value is compared with, and ``ref``
    the entity a ref parameter refers to.
    """

    name: str
    type: FieldType
    maps_to: FieldRef
    ref: str | None = None
    required: bool = False


@dataclass(frozen=True, slots=True)
class QueryModel:
    """A query tool of a pack: declared by source and returns, or provided by a primitive.

    A primitive query names the ``primitive`` and its ``query`` (the stock balance of the
    inventory primitive, requirement 9.7), and the primitive defines the parameters and the
    result. A declarative query names its ``source`` and the ``source_level`` it reads: the
    records of an entity (``record``), the headers of a bill (``header``) or its lines
    (``line``, written ``<bill>_line``). ``filter`` is the text step 5 checks
    (requirement 10.2), ``latest`` the fields of the first matching record and ``history``
    the fields of the first ``history_limit`` of them (requirement 5.13). When
    ``history_paginated`` is enabled, that limit bounds a requested page instead;
    offset/limit can traverse the whole matching sequence.
    """

    tool: str
    pack: str
    file: str
    primitive: Primitive | None = None
    query: str | None = None
    source: str | None = None
    source_level: FieldLevel | None = None
    params: FrozenMap[str, QueryParamModel] = FrozenMap()
    filter: str | None = None
    sort: tuple[SortRef, ...] = ()
    latest: tuple[FieldRef, ...] = ()
    history: tuple[FieldRef, ...] = ()
    history_limit: int | None = None
    history_paginated: bool = False


# --- the domain model --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainModel:
    """Every configuration object of the instance, resolved and keyed by its name.

    The order of each map is the order of the business packs in instance.yaml and, inside a
    pack, the path of the declaring file; queries are keyed by their tool name and keep the
    order of ``exposure.yaml``. A name declared twice keeps its first declaration, and
    step 5 reports the namespace conflict (requirement 2.7).
    """

    entities: FrozenMap[str, EntityModel] = FrozenMap()
    bills: FrozenMap[str, BillModel] = FrozenMap()
    conversions: FrozenMap[str, ConversionModel] = FrozenMap()
    writebacks: FrozenMap[str, WritebackModel] = FrozenMap()
    linkages: FrozenMap[str, LinkageModel] = FrozenMap()
    rules: FrozenMap[str, RuleModel] = FrozenMap()
    queries: FrozenMap[str, QueryModel] = FrozenMap()

    def context_entities(self, role: Literal["org", "user"]) -> tuple[EntityModel, ...]:
        """The entities declared as the ``role`` context entity, in model order (req 1.14)."""
        return tuple(entity for entity in self.entities.values() if entity.context == role)

    @property
    def org_entity(self) -> EntityModel | None:
        """The organization context entity, None unless exactly one declares that role."""
        return _single(self.context_entities("org"))

    @property
    def user_entity(self) -> EntityModel | None:
        """The user context entity, None unless exactly one declares that role."""
        return _single(self.context_entities("user"))

    def fields_of(self, name: str, level: FieldLevel) -> FrozenMap[str, FieldModel]:
        """The field view of ``level`` of the entity or bill ``name``, empty when unknown."""
        bill = self.bills.get(name)
        if bill is not None:
            return bill.level(level)
        entity = self.entities.get(name)
        if entity is not None and level == "record":
            return entity.fields
        return FrozenMap()


def _single(entities: Sequence[EntityModel]) -> EntityModel | None:
    """The one entity of ``entities``, or None when there is none or more than one."""
    return entities[0] if len(entities) == 1 else None


# --- the engine standard fields ----------------------------------------------------------
# One specification per standard field of meta-model.md section 4, read by the three view
# functions below. Which names a level has, and in which order, stays with
# :func:`~erp_mock.config.pack.entity_standard_fields` and its two siblings, which step 4
# already checks the declared field names against (requirement 1.12); the specifications
# here add the type, the writability (requirement 1.3) and the engine's own defaults, and
# the link field of a push comes last, the level deciding its type.


@dataclass(frozen=True, slots=True)
class _Standard:
    """The engine's part of one standard field: everything but its name."""

    type: FieldType
    writable: bool = False
    computed: bool = False
    max_length: int | None = None
    default: str | int | bool | None = None
    default_context: Literal["today", "org", "user"] | None = None

    def field(
        self,
        name: str,
        *,
        ref: str | None = None,
        enum_values: FrozenMap[str, BilingualLabel] = _NO_VALUES,
    ) -> FieldModel:
        """The standard field ``name`` as a :class:`FieldModel`.

        ``ref`` is the target of a standard ref field and ``enum_values`` the values of a
        standard enum field, both of which depend on the object the field belongs to.
        """
        return FieldModel(
            name=name,
            type=self.type,
            origin="standard",
            writable=self.writable,
            computed=self.computed,
            ref=ref,
            enum_values=enum_values,
            max_length=self.max_length,
            default=self.default,
            default_context=self.default_context,
        )


def _labels(*values: tuple[str, str, str]) -> FrozenMap[str, BilingualLabel]:
    """The enum values of a standard field: ``(code, zh, en)`` each."""
    return FrozenMap((code, BilingualLabel(zh=zh, en=en)) for code, zh, en in values)


# The labels of the engine's own enum values; the values themselves are the codes
# (requirement 13.4), so these serve messages and description drafts only.
_BILL_STATUS_LABELS: Final = _labels(
    ("draft", "草稿", "Draft"),
    ("submitted", "已提交", "Submitted"),
    ("approved", "已审核", "Approved"),
    ("closed", "已关闭", "Closed"),
)
_ENTITY_STATUS_LABELS: Final = _labels(
    ("enabled", "启用", "Enabled"), ("disabled", "停用", "Disabled")
)

_ID: Final = _Standard(type="string")
_SEQ: Final = _Standard(type="int")
_MONEY: Final = _Standard(type="money", computed=True)

_ENTITY_STANDARD: Final[Mapping[str, _Standard]] = MappingProxyType(
    {
        "id": _ID,
        # The engine generates the code of an entity that declares a code rule
        # (requirement 4.3); an entity without one declares ``code`` as a field of its own.
        "code": _ID,
        # An entity record is created enabled unless the caller says otherwise (req 5.1).
        "status": _Standard(type="enum", writable=True, default="enabled"),
    }
)

_HEADER_STANDARD: Final[Mapping[str, _Standard]] = MappingProxyType(
    {
        "id": _ID,
        "number": _ID,
        "bill_type": _ID,
        # The default organization of the instance (requirement 5.1); the entity it refers
        # to is the org context entity, which step 5 passes in.
        "org_id": _Standard(type="ref", default_context="org"),
        "biz_date": _Standard(type="date", writable=True, default_context="today"),
        "status": _Standard(type="enum"),
        "source_bill_type": _ID,
        # The bill it refers to is the one ``source_bill_type`` names, so the target is
        # known per record and not per field.
        "source_bill_id": _Standard(type="ref"),
        "remark": _Standard(type="text", writable=True, max_length=DEFAULT_MAX_LENGTH["text"]),
        "created_seq": _SEQ,
        "updated_seq": _SEQ,
        # Pricing (requirement 6.1 to 6.4); without a declared default the price includes
        # tax (requirement 6.3).
        "price_includes_tax": _Standard(type="bool", writable=True, default=True),
        "amount": _MONEY,
        "tax_amount": _MONEY,
        "amount_with_tax": _MONEY,
    }
)

_LINE_STANDARD: Final[Mapping[str, _Standard]] = MappingProxyType(
    {
        "id": _ID,
        "line_no": _SEQ,
        # The line of the source bill a pushed line comes from; a result returns the line
        # ID itself, not a nested object (requirement 13.3).
        "source_line_id": _ID,
        "qty": _Standard(type="qty", writable=True),
        "qty_base": _Standard(type="qty", computed=True),
        "price": _Standard(type="price", writable=True),
        "tax_rate": _Standard(type="rate", writable=True),
        "amount": _MONEY,
        "tax_amount": _MONEY,
        "amount_with_tax": _MONEY,
    }
)

# The link field of a push: the base unit quantity on a line, the amount on a header
# (meta-model.md section 7). The engine's writeback keeps it, so it is computed.
_LINK_FIELD: Final[Mapping[FieldLevel, _Standard]] = MappingProxyType(
    {
        "header": _MONEY,
        "line": _Standard(type="qty", computed=True),
    }
)


def entity_standard_view(*, code: bool, has_status: bool) -> tuple[FieldModel, ...]:
    """The standard fields of an entity, in order (requirement 1.3).

    ``code`` says whether the entity declares a code rule, so the engine generates its
    code, and ``has_status`` whether it keeps the engine's enabled and disabled status, the
    one standard field of an entity a write tool may write (requirement 5.1).
    """
    names = entity_standard_fields(code=code, has_status=has_status)
    labels = {"status": _ENTITY_STATUS_LABELS}
    return tuple(
        _ENTITY_STANDARD[name].field(name, enum_values=labels.get(name, _NO_VALUES))
        for name in names
    )


def header_standard_view(
    *,
    pricing: bool,
    state_machine: StateMachine = "standard",
    org_entity: str | None = None,
    link_fields: Sequence[str] = (),
) -> tuple[FieldModel, ...]:
    """The standard fields of a bill header, in order (requirement 1.3).

    ``pricing`` adds the amounts of the pricing primitive, ``state_machine`` decides the
    values of ``status``, ``org_entity`` is the organization context entity ``org_id``
    refers to (None when the instance does not declare exactly one), and ``link_fields``
    are the fields the writebacks of the pushes whose source this bill is maintain on its
    header, :data:`LINKED_AMOUNT` by default.
    """
    statuses = STANDARD_STATUSES if state_machine == "standard" else SIMPLE_STATUSES
    labels = {"status": _status_labels(statuses)}
    refs = {"org_id": org_entity}
    return (
        *(
            _HEADER_STANDARD[name].field(
                name, ref=refs.get(name), enum_values=labels.get(name, _NO_VALUES)
            )
            for name in header_standard_fields(pricing=pricing)
        ),
        *(_LINK_FIELD["header"].field(name) for name in link_fields),
    )


def line_standard_view(
    *, pricing: bool, line_quantity: bool = False, link_fields: Sequence[str] = ()
) -> tuple[FieldModel, ...]:
    """The standard fields of a bill's lines, in order (requirements 1.3 and 1.4).

    ``pricing`` adds the quantity, price and amounts of the pricing primitive, and
    ``line_quantity`` adds ``qty_base`` alone to the lines of a bill without pricing that
    declares quantity semantics. ``link_fields`` are the fields the writebacks of the
    pushes whose source this bill is maintain on its lines, :data:`LINKED_QTY` by default.
    """
    names = line_standard_fields(pricing=pricing, line_quantity=line_quantity)
    return (
        *(_LINE_STANDARD[name].field(name) for name in names),
        *(_LINK_FIELD["line"].field(name) for name in link_fields),
    )


def _status_labels(statuses: Sequence[BillStatus]) -> FrozenMap[str, BilingualLabel]:
    """The labels of ``statuses``, in their order."""
    return FrozenMap((status, _BILL_STATUS_LABELS[status]) for status in statuses)
