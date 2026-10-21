"""Base types of the configuration models (design.md 2.2).

Every configuration model derives from :class:`FrozenModel`. Validation is strict: ``"1"``
is not an int, ``1`` is not a str, and a YAML float such as ``0.13`` is neither, so decimal
values are written as strings (:data:`DecimalText`) and keep their exact value. A key the
model does not define is an error, and a validated model is frozen. Collections are tuples
(:data:`FrozenList`) and FrozenMaps (:data:`NameMap`), so nothing in a validated model can
be modified (requirement 2.11).

Errors are pydantic errors located by ``loc`` tuples relative to the validated document,
such as ``("header", "delivery_date", "default")``; the loader turns them into config
paths (``header.delivery_date.default``). Checks that go beyond field types build their
errors with :func:`custom_error` or :func:`known_error` at a ``loc`` and raise them
together through :func:`validation_error`, so they are located the same way.

Inside a validator, pydantic turns a raised ValidationError back into its line errors and
puts the location of the validated value in front of each ``loc``.
"""

from collections.abc import Mapping, Sequence, Sized
from dataclasses import dataclass
from typing import Annotated, Any, Final, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    StringConstraints,
    ValidationError,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, InitErrorDetails, PydanticCustomError, PydanticKnownError
from pydantic_core import core_schema as cs

from erp_mock.common.frozen import FrozenMap

__all__ = [
    "DECIMAL_PATTERN",
    "NAME_PATTERN",
    "BilingualLabel",
    "DecimalText",
    "FrozenList",
    "FrozenModel",
    "Label",
    "Loc",
    "Name",
    "NameMap",
    "NonEmpty",
    "NonEmptyStr",
    "custom_error",
    "known_error",
    "validation_error",
]

# Names of entities, bills, fields and enum codes (requirements 1.11 and 1.13).
NAME_PATTERN: Final = r"^[a-z][a-z0-9_]*$"
# Decimal strings, as accepted for tool input (requirement 12.5).
DECIMAL_PATTERN: Final = r"^-?[0-9]+(\.[0-9]+)?$"


class FrozenModel(BaseModel):
    """Base of every configuration model: frozen, strict, no undefined keys.

    Keys are matched by alias where a field has one (``maxLength``), never by the Python
    attribute name.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, populate_by_name=False)


# Patterns use pydantic's default Rust regex engine, where "$" matches only at the end of
# the text, and [0-9] matches ASCII digits only.
Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
DecimalText = Annotated[str, StringConstraints(pattern=DECIMAL_PATTERN)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]

# A location of an error: keys and list indices from the document root (or from the value
# a validator checks) down to the offending value.
Loc = tuple[str | int, ...]

_T = TypeVar("_T")


def _list_to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


# A YAML list, validated into a tuple. Strict mode accepts only a tuple for a tuple field,
# so the list is converted first; its items are then validated strictly. Any other input,
# a string or a set included, is a tuple_type error.
FrozenList = Annotated[tuple[_T, ...], BeforeValidator(_list_to_tuple)]


@dataclass(frozen=True, slots=True)
class _ClosedKeys:
    """JSON Schema of a map with :data:`Name` keys: other keys are not allowed.

    pydantic describes the keys with ``patternProperties`` only, which lets JSON Schema
    accept any other key; the model rejects such a key (``string_pattern_mismatch`` at
    ``(<key>, "[key]")``), and ``additionalProperties: false`` makes the schema agree.
    """

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler(schema)
        if "patternProperties" in json_schema:
            json_schema["additionalProperties"] = False
        return json_schema


# A YAML mapping with Name keys, validated into a FrozenMap in document order.
NameMap = Annotated[FrozenMap[Name, _T], _ClosedKeys()]


def _check_non_empty[S: Sized](value: S) -> S:
    if len(value) == 0:
        field_type = "Dictionary" if isinstance(value, Mapping) else "List"
        raise PydanticKnownError(
            "too_short", {"field_type": field_type, "min_length": 1, "actual_length": 0}
        )
    return value


@dataclass(frozen=True, slots=True)
class NonEmpty:
    """``Annotated`` marker: the tuple or map must have at least one item.

    An empty value is a ``too_short`` error at the value itself. The check runs after the
    items have been validated, so a list whose items are all invalid reports the items
    only. The JSON Schema gets ``minItems`` or ``minProperties``; pydantic's
    ``min_length`` would give a FrozenMap or a converted tuple ``minLength`` instead.
    """

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return cs.no_info_after_validator_function(_check_non_empty, handler(source))

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler(schema)
        match json_schema.get("type"):
            case "array":
                json_schema["minItems"] = 1
            case "object":
                json_schema["minProperties"] = 1
        return json_schema


class Label(FrozenModel):
    """Display name of an object or field: ``{ zh: 名称 }`` or ``{ zh: 供应商, en: Supplier }``."""

    zh: NonEmptyStr
    en: NonEmptyStr | None = None


class BilingualLabel(FrozenModel):
    """Display name with both languages, as every enum value has (requirement 1.13)."""

    zh: NonEmptyStr
    en: NonEmptyStr


def custom_error(
    loc: Loc, error_type: str, message: str, input_value: object, /, **context: object
) -> InitErrorDetails:
    """An error of type ``error_type`` at ``loc``, about the input ``input_value``.

    ``message`` is a template whose ``{name}`` placeholders are filled from ``context``;
    the context is also the ``ctx`` of the error. The other parameters are positional, so
    any name can be a context key.
    """
    error = PydanticCustomError(error_type, message, dict(context) or None)
    return {"type": error, "loc": loc, "input": input_value}


def known_error(
    loc: Loc, error_type: str, input_value: object, /, **context: object
) -> InitErrorDetails:
    """An error of a pydantic error type, such as ``missing``, at ``loc``.

    ``context`` is the ``ctx`` the error type needs, such as ``max_length`` for
    ``string_too_long``.
    """
    error: InitErrorDetails = {"type": error_type, "loc": loc, "input": input_value}
    if context:
        error["ctx"] = dict(context)
    return error


def validation_error(errors: Sequence[InitErrorDetails], title: str = "config") -> ValidationError:
    """A ValidationError holding ``errors``, to be raised; ``errors`` must not be empty.

    Raised inside a validator, the errors keep their order and are located below the
    validated value.
    """
    if not errors:
        raise ValueError("a ValidationError needs at least one error")
    return ValidationError.from_exception_data(title, list(errors))
