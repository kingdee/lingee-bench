"""Field declarations of entities and bills (meta-model.md section 3, requirement 1).

A field declaration is a mapping of field attributes. Its ``type`` key selects the model
that validates it, one model per family of the 12 field types (requirement 1.2):

=======================  ============  ==============================================
type                     model         keys of the type
=======================  ============  ==============================================
string, text             StringField   maxLength (default 100 and 500), default
int                      IntField      default
bool                     BoolField     default
enum                     EnumField     values (required), default
date                     DateField     default, default_context (today)
period                   PeriodField   default
qty, price, money, rate  DecimalField  values, default
ref                      RefField      ref (required), default, default_context (org, user)
=======================  ============  ==============================================

Every model also has label, required, readonly, unique and default_from (FieldCommon), so
each type accepts the 7 field attributes of meta-model.md section 3 where they apply to
it, plus the keys of its own type. Any other key is an undefined key (``extra_forbidden``),
a key of another type included, such as ``maxLength`` on an int field (requirement 1.11).

Values must fit the field type (requirement 1.11):

- ``default`` is a constant of the field type: a string of at most the field's maxLength
  code points; an int; a bool; an enum code listed in ``values``; a calendar date
  ``YYYY-MM-DD``; a period ``YYYY-MM``; a decimal string with at most the decimals of the
  type (common.decimals.PLACES), and one of ``values`` when they are listed; for ref, the
  internal ID of the default record. Decimals are strings so that their value stays
  exact; the strict models reject floats. Relative dates are not constants: a date field
  defaults to the anchor date with ``default_context: today``. Date expressions such as
  ``today+3d`` belong to datasets (requirement 16.4), not to field declarations.
- ``default_context`` takes a value from the business context, so it is declared only by
  the types of those values: ``today`` (the anchor date) for date fields, ``org`` and
  ``user`` (the internal IDs of the default organization and the current user) for ref
  fields. Step 5 checks that such a ref field refers to the context entity.
- ``default_from`` is written ``<ref field without _id>.<field>``, such as
  ``material.purchase_unit_id``; step 5 resolves it.
- ``values`` of a qty, price, money or rate field is a non-empty list of decimal strings
  with at most the decimals of the type, written digits counted (``"0.130"`` has 3), and
  constrains the write tools' input (requirement 1.7).
- ``values`` of an enum field is a non-empty mapping from codes matching
  ``^[a-z][a-z0-9_]*$`` to labels with both ``zh`` and ``en`` (requirement 1.13). Codes are
  unique because they are mapping keys: YAML reading rejects a repeated key at its line
  (loader.yaml_io).

Selecting the model: :data:`TypedFieldDecl` requires ``type`` (``missing`` at
``(..., "type")``) and reports an unknown type as ``literal_error`` at ``(..., "type")``.
:data:`FieldDecl`, for a bill's header and lines, also accepts a declaration without
``type``, the :class:`StandardOverride` of a standard field. Every error of a declaration is
located below the field: ``("header", "delivery_date", "default")``. (A pydantic tagged
union would insert its tag into that location, so the selection is done by hand.)

Field maps: :data:`TypedFieldMap` (entity fields) and :data:`FieldMap` (bill header and
lines) keep the document order, require :data:`~erp_mock.config.base.Name` keys and check
the names of the declared ref fields with :func:`field_name_errors` (requirement 1.12).
The entity and bill models call :func:`field_name_errors` again with the engine standard
fields of the same level.
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self, get_args

from pydantic import (
    AfterValidator,
    Field,
    StringConstraints,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WithJsonSchema,
    WrapValidator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError, PydanticKnownError

from erp_mock.common.decimals import PLACES, DecimalKind
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.base import (
    BilingualLabel,
    DecimalText,
    FrozenList,
    FrozenModel,
    Label,
    Loc,
    Name,
    NameMap,
    NonEmpty,
    custom_error,
    known_error,
    validation_error,
)

__all__ = [
    "DEFAULT_MAX_LENGTH",
    "FIELD_TYPES",
    "BoolField",
    "DateField",
    "DateText",
    "DecimalField",
    "DefaultFromPath",
    "EnumField",
    "FieldCommon",
    "FieldDecl",
    "FieldMap",
    "FieldType",
    "IntField",
    "PeriodField",
    "PeriodText",
    "RefField",
    "ScalarDefault",
    "StandardOverride",
    "StringField",
    "StringKind",
    "TypedField",
    "TypedFieldDecl",
    "TypedFieldMap",
    "field_name_errors",
]

FieldType = Literal[
    "string",
    "text",
    "int",
    "bool",
    "enum",
    "date",
    "period",
    "qty",
    "price",
    "money",
    "rate",
    "ref",
]
# The 12 field types of meta-model.md section 3, in the order of its table.
FIELD_TYPES: Final[tuple[FieldType, ...]] = get_args(FieldType)

StringKind = Literal["string", "text"]
# maxLength of a string or text field that declares none (meta-model.md section 3).
DEFAULT_MAX_LENGTH: Final[Mapping[StringKind, int]] = MappingProxyType({"string": 100, "text": 500})

_ID_SUFFIX: Final = "_id"


# --- value types -------------------------------------------------------------------------


def _check_calendar_date(text: str) -> str:
    # The pattern has fixed the form; fromisoformat rejects month 13, February 30, year 0.
    try:
        date.fromisoformat(text)
    except ValueError:
        raise PydanticCustomError(
            "date_invalid", "{value} 不是有效的日期", {"value": text}
        ) from None
    return text


_DATE_PATTERN: Final = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
# A calendar date written YYYY-MM-DD. The JSON Schema adds format date, which a format
# checker validates like the model.
DateText = Annotated[
    str,
    StringConstraints(pattern=_DATE_PATTERN),
    AfterValidator(_check_calendar_date),
    WithJsonSchema({"type": "string", "pattern": _DATE_PATTERN, "format": "date"}),
]
# A period written YYYY-MM, the pattern of period parameters (requirement 11.11).
PeriodText = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
# ``<ref field without _id>.<field of the referenced entity>``, resolved at step 5.
DefaultFromPath = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")]


def _scalar_default(value: object, handler: ValidatorFunctionWrapHandler) -> object:
    try:
        return handler(value)
    except ValidationError:
        # One error instead of one per member of the union.
        raise PydanticCustomError("scalar_type", "缺省值必须是字符串、整数或布尔值") from None


# The default of a declaration without type: a str, int or bool, never a float. Step 5
# checks it against the type of the standard field.
ScalarDefault = Annotated[str | int | bool, WrapValidator(_scalar_default)]


# --- field models ------------------------------------------------------------------------


class FieldCommon(FrozenModel):
    """The field attributes that every field type accepts (meta-model.md section 3).

    default and default_context depend on the type and are declared by each model.
    """

    label: Label | None = None
    required: bool = False
    readonly: bool = False
    unique: bool = False
    default_from: DefaultFromPath | None = None


class StringField(FieldCommon):
    """A string or text field; ``maxLength`` limits its length in code points."""

    type: StringKind
    max_length: Annotated[int, Field(ge=1)] | None = Field(default=None, alias="maxLength")
    default: str | None = None

    @property
    def effective_max_length(self) -> int:
        """The declared maxLength, or the default of the type (100 or 500)."""
        return DEFAULT_MAX_LENGTH[self.type] if self.max_length is None else self.max_length

    @model_validator(mode="after")
    def check_default(self) -> Self:
        limit = self.effective_max_length
        if self.default is not None and len(self.default) > limit:
            error = known_error(("default",), "string_too_long", self.default, max_length=limit)
            raise validation_error([error])
        return self


class IntField(FieldCommon):
    """An integer field."""

    type: Literal["int"]
    default: int | None = None


class BoolField(FieldCommon):
    """A boolean field."""

    type: Literal["bool"]
    default: bool | None = None


class EnumField(FieldCommon):
    """An enum field: ``values`` maps each code to its zh and en labels, in order."""

    type: Literal["enum"]
    values: Annotated[NameMap[BilingualLabel], NonEmpty()]
    default: Name | None = None

    @model_validator(mode="after")
    def check_default(self) -> Self:
        if self.default is not None and self.default not in self.values:
            raise validation_error([_not_in_values(self.default)])
        return self


class DateField(FieldCommon):
    """A date field; ``default_context: today`` defaults it to the anchor date."""

    type: Literal["date"]
    default: DateText | None = None
    default_context: Literal["today"] | None = None


class PeriodField(FieldCommon):
    """An accounting period field, YYYY-MM."""

    type: Literal["period"]
    default: PeriodText | None = None


class DecimalField(FieldCommon):
    """A qty, price, money or rate field, optionally limited to the listed ``values``."""

    type: DecimalKind
    values: Annotated[FrozenList[DecimalText], NonEmpty()] | None = None
    default: DecimalText | None = None

    @property
    def places(self) -> int:
        """Decimal places of the type: qty 4, price 6, money 2, rate 4."""
        return PLACES[self.type]

    @model_validator(mode="after")
    def check_values(self) -> Self:
        errors = [
            self._places_error(("values", index), value)
            for index, value in enumerate(self.values or ())
            if _places(value) > self.places
        ]
        if self.default is not None:
            if _places(self.default) > self.places:
                errors.append(self._places_error(("default",), self.default))
            elif self.values is not None and Decimal(self.default) not in {
                Decimal(value) for value in self.values
            }:
                errors.append(_not_in_values(self.default))
        if errors:
            raise validation_error(errors)
        return self

    def _places_error(self, loc: Loc, value: str) -> InitErrorDetails:
        return custom_error(
            loc,
            "decimal_places",
            "{value} 的小数位数超过 {type} 字段的精度 {places} 位",
            value,
            value=value,
            type=self.type,
            places=self.places,
        )


class RefField(FieldCommon):
    """A reference to a record of the entity or bill named by ``ref``.

    Its name ends with ``_id``; results return it under the name without ``_id``
    (requirement 13.3). ``default_context`` org and user give the default organization
    and the current user.
    """

    type: Literal["ref"]
    ref: Name
    default: str | None = None
    default_context: Literal["org", "user"] | None = None


class StandardOverride(FieldCommon):
    """A same-name declaration of an engine standard field without ``type``.

    Allowed in a bill's header and lines only (requirement 1.5), for instance
    ``tax_rate: { default_from: material.tax_rate }``. It accepts the 7 field attributes
    and nothing else; ``default`` is a str, int or bool, and its type is not known before
    step 5 resolves the standard field.

    Step 5 decides what such a declaration may say (requirement 1.6): only label and the
    default attributes, defaults only for a writable standard field, and a default that
    fits the standard field's type. It reads the declared attributes from
    ``model_fields_set``, as for a typed same-name declaration, so required, readonly and
    unique are accepted here and reported there with the bill and field name.
    """

    default: ScalarDefault | None = None
    default_context: Literal["today", "org", "user"] | None = None


def _places(text: str) -> int:
    """Number of written decimals of a decimal string."""
    return len(text.partition(".")[2])


def _not_in_values(value: str) -> InitErrorDetails:
    return custom_error(
        ("default",), "default_not_in_values", "缺省值 {value} 不在 values 中", value, value=value
    )


# --- selecting the model by type ---------------------------------------------------------

TypedField = (
    StringField
    | IntField
    | BoolField
    | EnumField
    | DateField
    | PeriodField
    | DecimalField
    | RefField
)

_TYPED_MODELS: Final[Mapping[str, type[TypedField]]] = MappingProxyType(
    {
        "string": StringField,
        "text": StringField,
        "int": IntField,
        "bool": BoolField,
        "enum": EnumField,
        "date": DateField,
        "period": PeriodField,
        "qty": DecimalField,
        "price": DecimalField,
        "money": DecimalField,
        "rate": DecimalField,
        "ref": RefField,
    }
)
_TYPED_CLASSES: Final = (
    StringField,
    IntField,
    BoolField,
    EnumField,
    DateField,
    PeriodField,
    DecimalField,
    RefField,
)
# The expected value of a literal_error, written as pydantic writes it.
_EXPECTED_TYPES: Final = ", ".join(repr(t) for t in FIELD_TYPES[:-1]) + f" or {FIELD_TYPES[-1]!r}"


def _typed_field(value: object) -> TypedField:
    if isinstance(value, _TYPED_CLASSES):
        return value
    if not isinstance(value, dict):
        raise PydanticKnownError("dict_type")
    if "type" not in value:
        raise validation_error([known_error(("type",), "missing", value)])
    kind = value["type"]
    model = _TYPED_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        error = known_error(("type",), "literal_error", kind, expected=_EXPECTED_TYPES)
        raise validation_error([error])
    # The model's errors are located below the declaration, as raised.
    return model.model_validate(value)


def _typed_field_or_override(value: object) -> TypedField | StandardOverride:
    if isinstance(value, StandardOverride):
        return value
    if isinstance(value, dict) and "type" not in value:
        return StandardOverride.model_validate(value)
    return _typed_field(value)


# The wrapped union schema is not used for validation: it gives the JSON Schema (anyOf the
# models, which exclude each other through type and additionalProperties) and the
# serialization.
def _validate_typed(value: object, handler: ValidatorFunctionWrapHandler) -> TypedField:
    return _typed_field(value)


def _validate_typed_or_override(
    value: object, handler: ValidatorFunctionWrapHandler
) -> TypedField | StandardOverride:
    return _typed_field_or_override(value)


# A field declaration with type: the field of an entity.
TypedFieldDecl = Annotated[TypedField, WrapValidator(_validate_typed)]
# A field declaration of a bill's header or lines: with type, or a StandardOverride.
FieldDecl = Annotated[TypedField | StandardOverride, WrapValidator(_validate_typed_or_override)]


# --- names of ref fields -----------------------------------------------------------------

_NO_STANDARD_FIELDS: Final[Mapping[str, bool]] = FrozenMap()


def field_name_errors(
    fields: Mapping[str, TypedField | StandardOverride],
    *,
    loc: Loc = (),
    standard: Mapping[str, bool] = _NO_STANDARD_FIELDS,
) -> list[InitErrorDetails]:
    """Return the name errors of the ref fields of one level (requirement 1.12).

    ``fields`` are the declared fields of an entity, a bill header or bill lines, and
    ``loc`` is their location (``("header",)``); every error is located at a field,
    ``loc + (<field name>,)``. ``standard`` lists the engine standard fields of the same
    level, mapping each name to True when results return it without ``_id`` like a ref
    (``org_id``, ``source_bill_id``) and to False otherwise (``number``, ``status``); a
    declared field with the name of a standard field is that field.

    Errors, in declaration order and then in the order of ``standard``:

    - ``ref_name_suffix``: a ref field whose name does not end with ``_id``;
    - ``ref_name_conflict`` at the ref field: its name without ``_id``, the key under
      which results return it (requirement 13.3), is the name of another field of the
      level, declared or standard. A StandardOverride of a standard ref field counts as
      a ref field;
    - ``ref_name_conflict`` at a declared field that has the name of an undeclared
      standard ref field without ``_id``, such as ``org`` next to ``org_id``.
    """
    names = fields.keys() | standard.keys()
    errors: list[InitErrorDetails] = []
    for name, decl in fields.items():
        is_ref = isinstance(decl, RefField)
        if not (is_ref or (isinstance(decl, StandardOverride) and standard.get(name, False))):
            continue
        if not name.endswith(_ID_SUFFIX):
            if is_ref:
                message = "ref 类型字段的名称必须以 _id 结尾"
                errors.append(custom_error((*loc, name), "ref_name_suffix", message, decl))
            continue
        key = name.removesuffix(_ID_SUFFIX)
        if key in names:
            message = "去掉 _id 后的名称 {key} 与同层的字段 {key} 重名"
            errors.append(custom_error((*loc, name), "ref_name_conflict", message, decl, key=key))
    for name, nested in standard.items():
        if not nested or name in fields or not name.endswith(_ID_SUFFIX):
            continue
        key = name.removesuffix(_ID_SUFFIX)
        if key in fields:
            message = "字段名与标准字段 {standard} 去掉 _id 后的名称相同"
            error = custom_error(
                (*loc, key), "ref_name_conflict", message, fields[key], standard=name
            )
            errors.append(error)
    return errors


def _check_field_names[M: Mapping[str, TypedField | StandardOverride]](fields: M) -> M:
    errors = field_name_errors(fields)
    if errors:
        raise validation_error(errors)
    return fields


# The fields of an entity, in document order.
TypedFieldMap = Annotated[NameMap[TypedFieldDecl], AfterValidator(_check_field_names)]
# The header or lines fields of a bill, in document order.
FieldMap = Annotated[NameMap[FieldDecl], AfterValidator(_check_field_names)]
