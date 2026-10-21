"""Configuration objects of a business pack (requirement 1, meta-model.md sections 3 to 9).

A business pack is a directory of YAML documents (business-pack.md section 3). Every kind of
document has a model here, and loading step 4 validates each parsed document against it,
reporting every pydantic error at its config path (design.md 2.3):

=====================  ====================  =================================
file                   document model        object model
=====================  ====================  =================================
pack.yaml              PackManifest
entities/*.yaml        EntityDef             key ``entity``
entities/*.yaml        BillDef               key ``bill``
conversions/*.yaml     ConversionFile        ConversionDef
writebacks/*.yaml      WritebackFile         WritebackDef
linkages/*.yaml        LinkageFile           InventoryLinkageDef
rules/*.yaml           RuleFile              EnforcedRuleDef | DescribedRuleDef
tools/exposure.yaml    ExposureDef
tools/descriptions/*   DescriptionFile
seed/generators.yaml   SeedGeneratorsFile
seed/shared/*.yaml     SharedDataFile
=====================  ====================  =================================

An entity, a bill, an exposure and a description document name their object in a key of the
document itself (``entity: supplier``), which is how step 4 tells an entity file from a bill
file. A conversion, a writeback, a linkage and a rule document nest their object under one
key, as meta-model.md writes them, so the ``*File`` model holds the object and every error
keeps the config path of the file (``conversion.limit_by``). ``seed/examples/`` is not read
by the framework (design.md data model 1).

What is checked here is the structure of one document on its own: required keys, undefined
keys (``extra_forbidden``), the type and form of every value, and the field declarations of
fields.py, including the names of ref fields at one level together with the engine standard
fields of that level (requirement 1.12, :func:`~erp_mock.config.fields.field_name_errors`).
A value that only names something, such as a bill, a field, a tool, a generator or a rule
expression, is a :data:`~erp_mock.config.base.Name` or a path of names here; whether the
thing exists, and everything that needs more than one document, is checked in later steps:

- step 2 and 3: requires_meta_model and the depends_on ranges, which are plain text here
  so that their syntax errors are reported in those steps (requirement 2.5);
- step 5: reference integrity (requirement 2.9), namespaces and prefixes (requirement 2.7),
  rule numbers (requirement 2.8), primitive use (requirement 2.6), same-name declarations
  of standard fields (requirement 1.6), quantity semantics and unit conversion
  (requirement 6.12), the fields of an inventory linkage by direction (requirement 9.3 and
  9.11), the operations of a simple bill (requirement 7.11), the context entities
  (requirement 1.14, including the ``default_flag`` of the org role), and the syntax, names
  and types of every rule expression (requirement 10.2);
- step 6: tool names, exclude and rename (requirement 11.2 and 11.14);
- step 7: description variants, missing or too long descriptions and forbidden content
  (requirement 3.1 to 3.4), which is why an empty description and an empty
  ``applies_to_tools`` are accepted here;
- step 8: the datasets built from the generators and the shared data (requirement 20).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self, get_args

from pydantic import (
    AfterValidator,
    Field,
    GetJsonSchemaHandler,
    StringConstraints,
    ValidatorFunctionWrapHandler,
    WithJsonSchema,
    WrapValidator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError, PydanticKnownError

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.versions import SemVer, VersionError
from erp_mock.config.base import (
    NAME_PATTERN,
    FrozenList,
    FrozenModel,
    Label,
    Name,
    NameMap,
    NonEmpty,
    NonEmptyStr,
    known_error,
    validation_error,
)
from erp_mock.config.fields import (
    FieldMap,
    FieldType,
    ScalarDefault,
    TypedFieldMap,
    field_name_errors,
)

__all__ = [
    "BILL_OPERATIONS",
    "DESCRIPTION_VARIANTS",
    "ENTITY_OPERATIONS",
    "GENERATORS",
    "PRIMITIVES",
    "VERSION_PATTERN",
    "AdjustDef",
    "BillDef",
    "BillOperation",
    "BillStatus",
    "ChoiceGenerator",
    "CodeDef",
    "CodePrefix",
    "CodeWordsGenerator",
    "CompanyNameGenerator",
    "ContextDef",
    "ConversionDef",
    "ConversionFile",
    "DecimalRangeGenerator",
    "DeclarativeQueryDef",
    "DescribedRuleDef",
    "DescriptionFile",
    "EnforcedRuleDef",
    "EntityDef",
    "EntityFieldPath",
    "EntityOperation",
    "ExposureDef",
    "ExprText",
    "FieldPath",
    "GeneratorDecl",
    "GeneratorObject",
    "IntRangeGenerator",
    "InventoryLinkageDef",
    "LineQuantityDef",
    "LinkageFile",
    "NumberDef",
    "PackManifest",
    "ParamPath",
    "PartnerDef",
    "PersonNameGenerator",
    "Primitive",
    "PrimitiveQueryDef",
    "QueryDef",
    "QueryHistoryDef",
    "QueryObject",
    "QueryParamDef",
    "QueryReturnsDef",
    "QuerySortKey",
    "RuleDef",
    "RuleErrorCode",
    "RuleFile",
    "RuleObject",
    "RulePrefix",
    "SearchDef",
    "SeedGeneratorsFile",
    "SharedDataFile",
    "SharedRecord",
    "ShortNameGenerator",
    "SortKey",
    "SourceLinePath",
    "SourcePath",
    "ToolDescription",
    "UnitConversionDef",
    "VersionText",
    "WriteOperation",
    "WritebackDef",
    "WritebackFile",
    "WritebackTarget",
    "entity_standard_fields",
    "header_standard_fields",
    "line_standard_fields",
]


# --- value types -------------------------------------------------------------------------

# A concrete version <major>.<minor>.<patch> (requirement 2.3); the grammar of
# common.versions.SemVer, which validates it.
VERSION_PATTERN: Final = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"


def _check_version(text: str) -> str:
    try:
        SemVer.parse(text)
    except VersionError as error:
        raise PydanticCustomError(
            "version_invalid",
            "{value} 不是 <主>.<次>.<修订> 形式的具体版本：{reason}",
            {"value": text, "reason": error.reason},
        ) from None
    return text


# A version of a pack or an instance config. The JSON Schema states the same grammar, which
# the model checks with SemVer.parse.
VersionText = Annotated[
    str,
    AfterValidator(_check_version),
    WithJsonSchema({"type": "string", "pattern": VERSION_PATTERN}),
]

# A prefix of generated codes and bill numbers, such as ``S`` or ``CGDD`` (requirement 4.3
# and 4.4): ASCII capitals and digits, starting with a capital, so a code and a number read
# as one token.
_PREFIX_PATTERN: Final = r"^[A-Z][A-Z0-9]*$"
CodePrefix = Annotated[str, StringConstraints(pattern=_PREFIX_PATTERN)]
# The rule number prefix of a pack (business-pack.md section 4), as in R-AP-001. Step 5
# rejects GEN, which is reserved for the engine rules (requirement 2.8).
RulePrefix = Annotated[str, StringConstraints(pattern=_PREFIX_PATTERN)]

# ``header.<field>`` or ``line.<field>``: how a linkage names a field of the bill it is
# attached to, and how a declarative query names a field of its source (requirement 9.11,
# requirement 5.13).
FieldPath = Annotated[str, StringConstraints(pattern=r"^(?:header|line)\.[a-z][a-z0-9_]*$")]
# A sort key of a search: a field name, optionally followed by a direction; no direction
# means ascending (requirement 5.14).
SortKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(?: (?:asc|desc))?$")]
# A sort key of a declarative query: a field path with an optional direction.
QuerySortKey = Annotated[
    str, StringConstraints(pattern=r"^(?:header|line)\.[a-z][a-z0-9_]*(?: (?:asc|desc))?$")
]
# ``source.<field>``: a field of the source bill's header (requirement 8.1).
SourcePath = Annotated[str, StringConstraints(pattern=r"^source\.[a-z][a-z0-9_]*$")]
# ``source_line.<field>``: a field of the source line a target line comes from.
SourceLinePath = Annotated[str, StringConstraints(pattern=r"^source_line\.[a-z][a-z0-9_]*$")]
# The target of a writeback: a field of the source header or of the source line
# (requirement 8.8).
WritebackTarget = Annotated[str, StringConstraints(pattern=r"^source(?:_line)?\.[a-z][a-z0-9_]*$")]
# ``<entity>.<field>``: the field a seed generator is bound to (design.md data model 1).
EntityFieldPath = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")]
# A parameter of a tool as a description file writes it: a top level parameter by its name,
# a parameter of a lines element as ``lines[].<name>`` (requirement 3.2).
ParamPath = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(?:\[\]\.[a-z][a-z0-9_]*)?$")
]

# A rule expression, checked at step 5 (requirement 10.2).
ExprText = NonEmptyStr

# The Name pattern, for the values that a validator checks itself. pydantic applies the
# patterns above with its Rust engine, which matches "$" only at the end of the text, as
# fullmatch does here.
_NAME: Final = re.compile(NAME_PATTERN)


@dataclass(frozen=True, slots=True)
class _ClosedKeys:
    """JSON Schema of a map with patterned keys: other keys are not allowed.

    The same marker that :data:`~erp_mock.config.base.NameMap` applies to its Name keys,
    for the maps whose keys are paths instead of names.
    """

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler(schema)
        if "patternProperties" in json_schema:
            json_schema["additionalProperties"] = False
        return json_schema


# --- states, operations and primitives ---------------------------------------------------

# The states of the preset state machines (meta-model.md section 6).
BillStatus = Literal["draft", "submitted", "approved", "closed"]
# The operations of an entity: the actions of tool-design.md section 2 that apply to a
# record of basic data.
EntityOperation = Literal["search", "get", "create", "update", "delete"]
# The operations of a bill. A simple bill must not declare submit, unsubmit, close or
# unclose; step 5 reports that (requirement 7.11).
BillOperation = Literal[
    "search",
    "get",
    "create",
    "update",
    "delete",
    "submit",
    "unsubmit",
    "approve",
    "unapprove",
    "close",
    "unclose",
]
# The operations a rule can be checked on (requirement 10.9); create covers a push too.
WriteOperation = Literal[
    "create",
    "update",
    "delete",
    "submit",
    "unsubmit",
    "approve",
    "unapprove",
    "close",
    "unclose",
]
ENTITY_OPERATIONS: Final[tuple[EntityOperation, ...]] = get_args(EntityOperation)
BILL_OPERATIONS: Final[tuple[BillOperation, ...]] = get_args(BillOperation)

# The engine primitives of M0 (requirement 2.6). A manifest that declares any other
# primitive, an M1 primitive included, fails at step 1.
Primitive = Literal[
    "crud",
    "numbering",
    "pricing",
    "unit_conversion",
    "state_machine",
    "conversion",
    "writeback",
    "inventory",
    "rule_expr",
]
PRIMITIVES: Final[tuple[Primitive, ...]] = get_args(Primitive)

# The description variants a pack may provide (tool-design.md section 3). Step 7 checks the
# variants of a manifest against this list and requires zh-detailed (requirement 3.1).
DESCRIPTION_VARIANTS: Final = ("zh-detailed", "zh-brief", "en-detailed", "en-brief")

# The error codes a pack rule may declare (requirement 10.7); each is an
# erp_mock.common.errors.ErrorCode. The codes the engine decides on its own (NOT_FOUND,
# LIMIT_EXCEEDED, INTERNAL_ERROR) cannot be declared.
RuleErrorCode = Literal["VALIDATION_FAILED", "BUSINESS_RULE_VIOLATION", "INVALID_STATE", "CONFLICT"]


# --- manifest ----------------------------------------------------------------------------


class PackManifest(FrozenModel):
    """``pack.yaml``: what a pack is, what it needs and which namespaces it takes.

    ``requires_meta_model`` and the ranges of ``depends_on`` are plain text here and parsed
    in steps 2 and 3 (requirement 2.5). ``description_variants`` may be empty and may name
    an unknown variant: step 7 reports both (requirement 3.1).
    """

    pack: Name
    version: VersionText
    title: Label
    requires_meta_model: NonEmptyStr
    depends_on: NameMap[NonEmptyStr] = FrozenMap()
    primitives: FrozenList[Primitive]
    rule_prefix: RulePrefix
    id_prefixes: FrozenList[Name]
    number_prefixes: FrozenList[CodePrefix]
    tool_prefixes: FrozenList[Name]
    description_variants: FrozenList[NonEmptyStr]
    license: NonEmptyStr
    maintainers: FrozenList[NonEmptyStr]


# --- engine standard fields --------------------------------------------------------------
# The standard fields of each level by name, mapping each to True when a result returns it
# as a nested object under the name without ``_id`` (requirement 13.3) and to False
# otherwise. The entity and bill models pass them to field_name_errors, so a declared field
# cannot take the key of a standard field (requirement 1.12). config.model builds the full
# field view, with types and writability, from the same lists (requirement 1.3).

_ENTITY_FIELDS: Final = ("id",)
_BILL_HEADER_FIELDS: Final[Mapping[str, bool]] = MappingProxyType(
    {
        "id": False,
        "number": False,
        "bill_type": False,
        "org_id": True,
        "biz_date": False,
        "status": False,
        "source_bill_type": False,
        "source_bill_id": True,
        "remark": False,
        "created_seq": False,
        "updated_seq": False,
    }
)
_BILL_HEADER_PRICING_FIELDS: Final = (
    "price_includes_tax",
    "amount",
    "tax_amount",
    "amount_with_tax",
)
_BILL_LINE_FIELDS: Final = ("id", "line_no", "source_line_id")
_BILL_LINE_PRICING_FIELDS: Final = (
    "qty",
    "qty_base",
    "price",
    "tax_rate",
    "amount",
    "tax_amount",
    "amount_with_tax",
)
_QTY_BASE_FIELD: Final = "qty_base"


def entity_standard_fields(*, code: bool, has_status: bool) -> FrozenMap[str, bool]:
    """The standard fields of an entity: ``id``, plus ``code`` and ``status`` when declared.

    ``code`` says whether the entity declares a code rule, so the engine assigns its code
    (requirement 4.3); ``has_status`` whether the engine keeps its status (requirement 5.10).
    """
    names = [*_ENTITY_FIELDS]
    if code:
        names.append("code")
    if has_status:
        names.append("status")
    return FrozenMap((name, False) for name in names)


def header_standard_fields(*, pricing: bool) -> FrozenMap[str, bool]:
    """The standard fields of a bill header (requirement 1.3, meta-model.md section 4).

    ``linked_amount`` is added at step 5, where it is known whether the bill is the source
    of a push whose limit_by is amount.
    """
    fields = dict(_BILL_HEADER_FIELDS)
    if pricing:
        fields.update((name, False) for name in _BILL_HEADER_PRICING_FIELDS)
    return FrozenMap(fields)


def line_standard_fields(*, pricing: bool, line_quantity: bool) -> FrozenMap[str, bool]:
    """The standard fields of a bill's lines (requirement 1.3 and 1.4).

    ``qty_base`` comes with pricing or with declared quantity semantics; ``linked_qty`` is
    added at step 5, as :func:`header_standard_fields` describes.
    """
    names = [*_BILL_LINE_FIELDS]
    if pricing:
        names.extend(_BILL_LINE_PRICING_FIELDS)
    elif line_quantity:
        names.append(_QTY_BASE_FIELD)
    return FrozenMap((name, False) for name in names)


# --- entity ------------------------------------------------------------------------------


class CodeDef(FrozenModel):
    """``code: { prefix: S, digits: 4 }``: how the engine generates codes (requirement 4.3).

    An entity without this key gets no generated code; its records carry the code written
    in the shared data or the dataset.
    """

    prefix: CodePrefix
    digits: Annotated[int, Field(ge=1)]


class SearchDef(FrozenModel):
    """``search``: the parameters and the order of a search tool (requirement 5.9 to 5.14).

    Every list may be omitted: without ``keyword_fields`` the tool has no keyword parameter,
    without ``filters`` no filters, and without ``default_sort`` the results are ordered by
    id alone.
    """

    keyword_fields: FrozenList[Name] = ()
    filters: FrozenList[Name] = ()
    default_sort: FrozenList[SortKey] = ()


class ContextDef(FrozenModel):
    """``context``: the entity is the org or the user context entity (requirement 1.8).

    ``default_flag`` names the bool field of the org context entity whose only record with
    the value true is the default organization. Step 5 checks that the org role declares it
    and that it names a bool field, and that each role is taken by exactly one entity
    (requirement 1.14).
    """

    role: Literal["org", "user"]
    default_flag: Name | None = None


class UnitConversionDef(FrozenModel):
    """``unit_conversion``: the base unit of an entity and its conversion records.

    Declared on the entity a bill's quantity semantics measures (requirement 6.8):
    ``base_unit_field`` is its ref field to the unit entity, ``entity`` the entity that
    holds the conversion records, and ``material_field``, ``unit_field`` and
    ``factor_field`` are the fields of such a record. Step 5 checks the types of all five
    (requirement 6.12).
    """

    base_unit_field: Name
    entity: Name
    material_field: Name
    unit_field: Name
    factor_field: Name


class EntityDef(FrozenModel):
    """An entity document: basic data, one record per row (meta-model.md section 3).

    ``shared`` is true for data the pack provides in ``seed/`` and false for data a dataset
    provides; ``has_status`` adds the engine's enabled and disabled status, which
    R-GEN-006 checks (requirement 5.7).
    """

    entity: Name
    label: Label | None = None
    id_prefix: Name
    code: CodeDef | None = None
    shared: bool = False
    has_status: bool = False
    context: ContextDef | None = None
    unit_conversion: UnitConversionDef | None = None
    operations: FrozenList[EntityOperation] = ()
    search: SearchDef | None = None
    fields: TypedFieldMap

    @model_validator(mode="after")
    def check_field_names(self) -> Self:
        """Check the ref field names against the standard fields of the entity (req 1.12)."""
        standard = entity_standard_fields(code=self.code is not None, has_status=self.has_status)
        errors = field_name_errors(self.fields, loc=("fields",), standard=standard)
        if errors:
            raise validation_error(errors)
        return self


# --- bill --------------------------------------------------------------------------------


class NumberDef(FrozenModel):
    """``number: { prefix: CGDD }``: the prefix of the bill numbers (requirement 4.4).

    A number is ``<prefix><biz_date as YYYYMMDD><4 digits>``, so only the prefix is
    declared.
    """

    prefix: CodePrefix


class PartnerDef(FrozenModel):
    """``partner: { field: supplier_id }``: the bill's partner field (meta-model.md 4)."""

    field: Name


class LineQuantityDef(FrozenModel):
    """``line_quantity``: the quantity semantics of a bill's lines (requirement 1.4).

    ``material_field`` and ``unit_field`` are the ref fields to the measured entity and to
    the unit entity; ``qty_field`` names the qty field a bill without pricing measures,
    while a bill with pricing uses the standard ``qty``. The engine adds the read-only
    ``qty_base`` to such lines. Step 5 checks the fields and the combination with pricing
    (requirement 6.12).
    """

    material_field: Name
    unit_field: Name
    qty_field: Name | None = None


class BillDef(FrozenModel):
    """A bill document: a header and its lines (meta-model.md section 4).

    ``state_machine`` is required and selects one of the two preset state machines; a
    document without it, or with any other value, is a step 4 error that names the bill
    (requirement 1.9). ``pricing`` turns on the quantity, price and amount fields of the
    pricing primitive (requirement 6.1).
    """

    bill: Name
    label: Label | None = None
    id_prefix: Name
    number: NumberDef
    state_machine: Literal["standard", "simple"]
    pricing: bool = False
    partner: PartnerDef | None = None
    header: FieldMap
    lines: FieldMap
    line_quantity: LineQuantityDef | None = None
    operations: FrozenList[BillOperation] = ()
    search: SearchDef | None = None

    @model_validator(mode="after")
    def check_field_names(self) -> Self:
        """Check the ref field names of both levels against their standard fields (req 1.12)."""
        errors = [
            *field_name_errors(
                self.header,
                loc=("header",),
                standard=header_standard_fields(pricing=self.pricing),
            ),
            *field_name_errors(
                self.lines,
                loc=("lines",),
                standard=line_standard_fields(
                    pricing=self.pricing, line_quantity=self.line_quantity is not None
                ),
            ),
        ]
        if errors:
            raise validation_error(errors)
        return self


# --- conversion and writeback ------------------------------------------------------------


class ConversionDef(FrozenModel):
    """A push from a source bill to a target bill draft (meta-model.md section 7).

    ``header_mapping`` maps a field of the target header to ``source.<field>`` and
    ``line_mapping`` a field of a target line to ``source_line.<field>``;
    ``required_line_inputs`` are the fields the caller gives per line, and they are the only
    parameters of the push tool's lines (requirement 11.12). ``limit_by`` selects the
    quota R-GEN-005 enforces: by the base unit quantity of the source line, or by the
    amount of the source bill (requirement 8.5 and 8.6).
    """

    id: Name
    source: Name
    target: Name
    require_source_status: BillStatus
    limit_by: Literal["qty", "amount"]
    line_selection: Literal["by_source_line_id"] = "by_source_line_id"
    header_mapping: NameMap[SourcePath] = FrozenMap()
    line_mapping: NameMap[SourceLinePath] = FrozenMap()
    required_line_inputs: Annotated[FrozenList[Name], NonEmpty()]


class ConversionFile(FrozenModel):
    """``conversions/<id>.yaml``: one conversion under the key ``conversion``."""

    conversion: ConversionDef


class WritebackDef(FrozenModel):
    """The update of a source bill when a pushed bill is approved (meta-model.md section 7).

    ``value`` is a rule expression evaluated per target line for a ``source_line.<field>``
    target and once for a ``source.<field>`` target (requirement 8.8); step 5 checks it.
    ``auto_close_source_when`` closes the source when every line is linked, and leaving it
    out means the source is never closed automatically (requirement 8.13).
    """

    id: Name
    conversion: Name
    on: Literal["approve"]
    target: WritebackTarget
    op: Literal["add"]
    value: ExprText
    reverse_on: Literal["unapprove"] = "unapprove"
    auto_close_source_when: Literal["all_lines_linked"] | None = None


class WritebackFile(FrozenModel):
    """``writebacks/<id>.yaml``: one writeback under the key ``writeback``."""

    writeback: WritebackDef


# --- inventory linkage -------------------------------------------------------------------


class AdjustDef(FrozenModel):
    """``adjust``: which values of a header enum field increase and which decrease stock.

    Declared by an adjust linkage (requirement 9.2 and 9.3). Step 5 checks that ``field``
    is a required enum field of the header and that ``increase`` and ``decrease`` partition
    its values.
    """

    field: FieldPath
    increase: Annotated[FrozenList[Name], NonEmpty()]
    decrease: Annotated[FrozenList[Name], NonEmpty()]


class InventoryLinkageDef(FrozenModel):
    """A linkage whose primitive is inventory (meta-model.md section 8, requirement 9).

    ``direction`` decides which warehouse fields apply: ``warehouse_field`` for in, out and
    adjust, ``from_warehouse_field`` and ``to_warehouse_field`` for transfer, and ``adjust``
    for adjust. All four are optional here and checked against the direction at step 5
    (requirement 9.3); ``qty_field`` must be the lines' ``qty_base`` (requirement 9.11).
    M0 implements no other linkage primitive, so any other value of ``primitive`` is a
    step 4 error (requirement 2.6).
    """

    id: Name
    bill: Name
    primitive: Literal["inventory"]
    on: Literal["approve"]
    direction: Literal["in", "out", "transfer", "adjust"]
    warehouse_field: FieldPath | None = None
    from_warehouse_field: FieldPath | None = None
    to_warehouse_field: FieldPath | None = None
    qty_field: FieldPath
    adjust: AdjustDef | None = None


class LinkageFile(FrozenModel):
    """``linkages/<id>.yaml``: one linkage under the key ``linkage``."""

    linkage: InventoryLinkageDef


# --- rules -------------------------------------------------------------------------------


class EnforcedRuleDef(FrozenModel):
    """A rule the engine checks on the operations of ``on`` (meta-model.md section 9).

    ``check`` is a rule expression, checked at step 5 and evaluated on the bill after the
    operation (requirement 10.5); when it is false, the call ends with ``error_code``, the
    rule's id and ``message.zh``, and ``field`` gives the field path of the detail item
    (requirement 10.7). The id is text here: step 5 checks it against the pack's
    rule_prefix (requirement 2.8).
    """

    id: NonEmptyStr
    bill: Name
    enforcement: Literal["enforced"]
    on: Annotated[FrozenList[WriteOperation], NonEmpty()]
    check: ExprText
    error_code: RuleErrorCode
    field: Name | None = None
    message: Label


class DescribedRuleDef(FrozenModel):
    """A rule the engine does not enforce (meta-model.md section 9).

    ``description.zh`` must appear verbatim in the zh-detailed description of every tool of
    ``applies_to_tools``, which step 7 checks; it also reports an empty ``applies_to_tools``
    (requirement 3.6 and 3.8), so an empty list is accepted here.
    """

    id: NonEmptyStr
    enforcement: Literal["described"]
    description: Label
    applies_to_tools: FrozenList[Name] = ()


RuleObject = EnforcedRuleDef | DescribedRuleDef

_RULE_MODELS: Final[Mapping[str, type[RuleObject]]] = MappingProxyType(
    {"enforced": EnforcedRuleDef, "described": DescribedRuleDef}
)


def _expected(names: Sequence[str]) -> str:
    """The ``expected`` of a literal_error, written as pydantic writes it."""
    quoted = [repr(name) for name in names]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + f" or {quoted[-1]}"


def _select[M: FrozenModel](value: object, key: str, models: Mapping[str, type[M]]) -> M:
    """Validate ``value`` with the model that its ``key`` selects.

    A missing key is a ``missing`` error and an unknown value a ``literal_error``, both at
    ``key``; the selected model's errors stay below the document, as fields.py selects a
    field model by its type.
    """
    for model in models.values():
        if isinstance(value, model):
            return value
    if not isinstance(value, dict):
        raise PydanticKnownError("dict_type")
    if key not in value:
        raise validation_error([known_error((key,), "missing", value)])
    tag = value[key]
    selected = models.get(tag) if isinstance(tag, str) else None
    if selected is None:
        expected = _expected(list(models))
        raise validation_error([known_error((key,), "literal_error", tag, expected=expected)])
    return selected.model_validate(value)


def _validate_rule(value: object, handler: ValidatorFunctionWrapHandler) -> RuleObject:
    return _select(value, "enforcement", _RULE_MODELS)


# A rule, selected by its enforcement. The wrapped union gives the JSON Schema and the
# serialization, as in fields.py.
RuleDef = Annotated[RuleObject, WrapValidator(_validate_rule)]


class RuleFile(FrozenModel):
    """``rules/<id>.yaml``: one rule under the key ``rule``."""

    rule: RuleDef


# --- tool exposure -----------------------------------------------------------------------


class QueryParamDef(FrozenModel):
    """A parameter of a declarative query (tool-design.md section 2).

    ``maps_to`` names the field of the source record the value is compared with; a record is
    selected when every given parameter equals its field and the filter is true
    (requirement 5.13). A ref parameter declares the entity it refers to, as a ref field
    does (requirement 1.12).
    """

    type: FieldType
    ref: Name | None = None
    required: bool = False
    maps_to: FieldPath

    @model_validator(mode="after")
    def check_ref(self) -> Self:
        """``ref`` belongs to a ref parameter and to no other type (requirement 1.12)."""
        if self.type == "ref" and self.ref is None:
            raise validation_error([known_error(("ref",), "missing", None)])
        if self.type != "ref" and self.ref is not None:
            raise validation_error([known_error(("ref",), "extra_forbidden", self.ref)])
        return self


class QueryHistoryDef(FrozenModel):
    """History fields and row limit; opt-in pagination makes the limit a per-page cap."""

    fields: Annotated[FrozenList[FieldPath], NonEmpty()]
    limit: Annotated[int, Field(ge=1)]
    paginate: bool = False


class QueryReturnsDef(FrozenModel):
    """``returns``: the fields of the first matching record, and optionally of the history.

    Without ``history`` the query returns only ``latest`` (requirement 5.13).
    """

    latest: Annotated[FrozenList[FieldPath], NonEmpty()]
    history: QueryHistoryDef | None = None


class DeclarativeQueryDef(FrozenModel):
    """A query declared by source, params, filter, sort and returns (requirement 5.13).

    ``source`` is an entity, a bill or a bill's lines (``<bill>_line``); ``filter`` is a
    rule expression that may use ``line.`` only when the source is a bill's lines
    (requirement 10.10). Step 5 resolves the source and checks the filter.
    """

    tool: Name
    source: Name
    params: NameMap[QueryParamDef] = FrozenMap()
    filter: ExprText | None = None
    sort: FrozenList[QuerySortKey] = ()
    returns: QueryReturnsDef

    @model_validator(mode="after")
    def check_pagination_params(self) -> Self:
        """Pagination controls must not shadow business filters."""
        history = self.returns.history
        if history is not None and history.paginate:
            errors = [
                known_error(("params", name), "extra_forbidden", self.params[name])
                for name in ("offset", "limit")
                if name in self.params
            ]
            if errors:
                raise validation_error(errors)
        return self


class PrimitiveQueryDef(FrozenModel):
    """A query a primitive provides, such as the stock balance of the inventory primitive.

    ``primitive`` and ``query`` name it; step 5 checks that the primitive provides that
    query and that the pack declares the primitive (requirement 2.6), and the primitive
    itself defines the parameters and the result (requirement 9.7).
    """

    tool: Name
    primitive: Primitive
    query: Name


QueryObject = DeclarativeQueryDef | PrimitiveQueryDef


def _validate_query(value: object, handler: ValidatorFunctionWrapHandler) -> QueryObject:
    if isinstance(value, DeclarativeQueryDef | PrimitiveQueryDef):
        return value
    if not isinstance(value, dict):
        raise PydanticKnownError("dict_type")
    # A primitive query names its primitive; everything else is declarative and needs a
    # source, as a declaration without a type is a standard override in fields.py.
    model = PrimitiveQueryDef if "primitive" in value else DeclarativeQueryDef
    return model.model_validate(value)


# A query of exposure.yaml, selected by the presence of ``primitive``.
QueryDef = Annotated[QueryObject, WrapValidator(_validate_query)]


class ExposureDef(FrozenModel):
    """``tools/exposure.yaml``: which tools the pack exposes (tool-design.md section 2).

    The generated tools are those of the operations, the conversions and ``queries``;
    ``exclude`` drops tools and ``rename`` renames them, both named by the generated name
    (requirement 11.1). ``source_short_names`` gives the short name of a source bill used
    in a push tool's name and in its ``<short name>_id`` parameter, which defaults to the
    source bill's name (requirement 11.13). Step 6 checks the resulting names.
    """

    pack: Name
    exclude: FrozenList[Name] = ()
    rename: NameMap[Name] = FrozenMap()
    source_short_names: NameMap[Name] = FrozenMap()
    queries: FrozenList[QueryDef] = ()


# --- tool descriptions -------------------------------------------------------------------


class ToolDescription(FrozenModel):
    """The description of one tool and of its parameters, as written for one variant.

    Both go into tools/list verbatim (requirement 3.7). An empty or missing text is
    accepted here and reported at step 7 with the variant, the tool and the parameter path
    (requirement 3.2).
    """

    description: str = ""
    params: Annotated[FrozenMap[ParamPath, str], _ClosedKeys()] = FrozenMap()


class DescriptionFile(FrozenModel):
    """``tools/descriptions/<variant>.yaml``: the descriptions of one variant.

    ``variant`` is text here, so step 7 reports an unknown variant name together with the
    other description errors (requirement 3.1); :data:`DESCRIPTION_VARIANTS` lists the
    known names.
    """

    variant: NonEmptyStr
    tools: NameMap[ToolDescription] = FrozenMap()


# --- seed configuration ------------------------------------------------------------------


class CompanyNameGenerator(FrozenModel):
    """A company name; ``industry`` narrows the words it is built from (seed-data.md 1)."""

    gen: Literal["company_name"]
    industry: NonEmptyStr | None = None


class ShortNameGenerator(FrozenModel):
    """A short name derived from the record's name."""

    gen: Literal["short_name"]


class PersonNameGenerator(FrozenModel):
    """A person's name."""

    gen: Literal["person_name"]


class ChoiceGenerator(FrozenModel):
    """One of ``values``, drawn from the dataset's random sequence."""

    gen: Literal["choice"]
    values: Annotated[FrozenList[ScalarDefault], NonEmpty()]


class IntRangeGenerator(FrozenModel):
    """An integer between ``min`` and ``max``, both included."""

    gen: Literal["int_range"]
    min: int
    max: int


class DecimalRangeGenerator(FrozenModel):
    """A decimal between ``min`` and ``max``, both included and written as text."""

    gen: Literal["decimal_range"]
    min: NonEmptyStr
    max: NonEmptyStr


class CodeWordsGenerator(FrozenModel):
    """A text built from ``words``, for names and specifications."""

    gen: Literal["code_words"]
    words: Annotated[FrozenList[NonEmptyStr], NonEmpty()]


GeneratorObject = (
    CompanyNameGenerator
    | ShortNameGenerator
    | PersonNameGenerator
    | ChoiceGenerator
    | IntRangeGenerator
    | DecimalRangeGenerator
    | CodeWordsGenerator
)

_GENERATOR_MODELS: Final[Mapping[str, type[GeneratorObject]]] = MappingProxyType(
    {
        "company_name": CompanyNameGenerator,
        "short_name": ShortNameGenerator,
        "person_name": PersonNameGenerator,
        "choice": ChoiceGenerator,
        "int_range": IntRangeGenerator,
        "decimal_range": DecimalRangeGenerator,
        "code_words": CodeWordsGenerator,
    }
)
# The built-in generators (design.md data model 1), in declaration order.
GENERATORS: Final[tuple[str, ...]] = tuple(_GENERATOR_MODELS)


def _validate_generator(value: object, handler: ValidatorFunctionWrapHandler) -> GeneratorObject:
    return _select(value, "gen", _GENERATOR_MODELS)


# A generator with its parameters, selected by ``gen``; a dataset writes the same thing for
# a field value (requirement 20.3).
GeneratorDecl = Annotated[GeneratorObject, WrapValidator(_validate_generator)]


class SeedGeneratorsFile(FrozenModel):
    """``seed/generators.yaml``: the generator of each entity field (design.md data model 1).

    Keys are written ``<entity>.<field>``. A field without a generator keeps its default
    value when the Seed_Builder generates a record (requirement 20.4). Step 5 resolves the
    entities and fields.
    """

    generators: Annotated[FrozenMap[EntityFieldPath, GeneratorDecl], _ClosedKeys()] = FrozenMap()


_KIND_KEY: Final = "kind"


def _check_kind(record: FrozenMap[str, ScalarDefault]) -> FrozenMap[str, ScalarDefault]:
    """Check the reserved ``kind`` key of a shared data record (requirement 20.16)."""
    if _KIND_KEY not in record:
        raise validation_error([known_error((_KIND_KEY,), "missing", dict(record))])
    kind = record[_KIND_KEY]
    if not isinstance(kind, str):
        raise validation_error([known_error((_KIND_KEY,), "string_type", kind)])
    if _NAME.fullmatch(kind) is None:
        error = known_error((_KIND_KEY,), "string_pattern_mismatch", kind, pattern=NAME_PATTERN)
        raise validation_error([error])
    return record


# One record of shared data: its entity under the reserved key ``kind``, then the written
# field values. Refs are written as the code of the referenced record and a field named
# ``kind`` as ``<kind value>_kind`` (requirement 20.16); decimals and dates are text, as
# everywhere in a configuration.
SharedRecord = Annotated[NameMap[ScalarDefault], AfterValidator(_check_kind)]


class SharedDataFile(FrozenModel):
    """``seed/shared/<name>.yaml``: the Shared_Data records a pack provides.

    The Seed_Builder creates them through the engine before the dataset's own records, in
    the order they are written (requirement 20.17).
    """

    records: FrozenList[SharedRecord]
