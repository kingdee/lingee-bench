"""Instance configuration, tool profiles and initial datasets (business-pack.md section 8).

An instance configuration is the deployment unit of a service: a directory holding
``instance.yaml``, a ``profiles/`` directory and a ``datasets/`` directory. Each kind of
document has a model here:

=====================  =============
file                   model
=====================  =============
instance.yaml          InstanceDef
profiles/*.yaml        ProfileDef
datasets/*.yaml        DatasetDef
=====================  =============

As in pack.py, what is checked here is the structure of one document on its own: required
keys, undefined keys (``extra_forbidden``), and the type and form of every value. Loading
step 1 validates instance.yaml and reports its errors as instance configuration errors
(requirement 2.12); step 4 validates the profiles and datasets named by instance.yaml
(requirement 2.13). Everything that needs more than one document, or the packs, comes
later:

- step 4: ``profiles`` holds semantic alone, and that profile's surface,
  description_variant and include_deprecated have the fixed M0 values (requirement 2.13),
  which is why ``surface`` also accepts generic and ``description_variant`` is text here;
- step 5: default_profile is in profiles, default_dataset is in datasets, and the ``packs``
  of a profile and the ``include_shared`` of a dataset are packs instance.yaml references
  (requirement 2.10); step 6 checks the tool names of include_tools and exclude_tools
  (requirement 2.10);
- step 8 and the Seed_Builder: the current user is a record of the user context entity
  (requirement 2.10), and everything a dataset names - kinds, fields, generators against
  the field type, aliases (prefix equal to the kind, declared before use), the codes of
  shared data, the categories of the date expressions of date fields, the status values and
  the kinds of ``bulk`` (requirement 20.6).

The clock is checked here, so step 1 reports each error under its config path below
``clock`` (requirement 16.3): ``mode`` is today or fixed, ``anchor_date`` is a date of the
calendar and is required when the mode is fixed, and ``timezone`` is a name of the IANA
time zone database, compared case-sensitively against the zones that zoneinfo and the
tzdata package provide.

A dataset record is a mapping, as seed-data.md section 2 writes it: the entity or bill
under the reserved key ``kind``, the written field values, and the lines of a bill under
``lines``. The shape of a value says what it is - a str, int or bool is the value itself, a
mapping is a generator call (``{ gen: company_name, industry: 机械 }``) and a list is the
lines of the record (the Seed_Builder checks that such a key is ``lines`` and that the kind
is a bill). A value of any other shape, a YAML float included, is one error at the field:
decimals and dates are text, as everywhere in a configuration.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Final, Literal, Self, TypeGuard
from zoneinfo import available_timezones

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    StringConstraints,
    TypeAdapter,
    ValidatorFunctionWrapHandler,
    WithJsonSchema,
    WrapValidator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError

from erp_mock.common.date_expr import Category, DateExprError, parse_date_expr
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.base import (
    NAME_PATTERN,
    FrozenList,
    FrozenModel,
    Label,
    Name,
    NameMap,
    NonEmptyStr,
    known_error,
    validation_error,
)
from erp_mock.config.fields import DateText, ScalarDefault
from erp_mock.config.pack import GeneratorDecl, GeneratorObject, VersionText

__all__ = [
    "ALIAS_PATTERN",
    "CONFIG_NAME_PATTERN",
    "DEFAULT_CLOSED_BEFORE",
    "INTERNAL_ID_PATTERN",
    "PERIOD_EXPR_PATTERN",
    "Alias",
    "BulkDef",
    "ClockDef",
    "ClockMode",
    "ConfigName",
    "DatasetDef",
    "DatasetRecord",
    "FieldValue",
    "FieldValueDecl",
    "InstanceDef",
    "InternalId",
    "LineRecord",
    "PeriodExpr",
    "PeriodsDef",
    "ProfileDef",
    "RecordValue",
    "RecordValueDecl",
    "Surface",
    "TimezoneName",
]


# --- value types -------------------------------------------------------------------------

# The name of an instance configuration, of a profile or of an initial dataset, as the
# deployer chooses it (``p2p-demo``, ``purchase-basic``). Such a name appears in an address
# (``/p/<profile>/mcp``, ``/d/<dataset>/mcp``, requirement 14.2) and in the directories of
# the snapshot cache (``snapshots/<dataset>@<version>/``, design.md data model 2), so it is
# written like the name of a Named_Snapshot (requirement 19.20): lowercase letters, digits,
# hyphens and underscores, starting with a letter or a digit, at most 64 code points. Pack
# names, entity names and every other name of the meta model stay
# :data:`~erp_mock.config.base.Name`.
CONFIG_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,63}$"
ConfigName = Annotated[str, StringConstraints(pattern=CONFIG_NAME_PATTERN)]

# An internal ID as the Numbering_Primitive assigns it (requirement 4.1):
# ``<id_prefix>_<sequence>``, the sequence padded with leading zeros to at least 6 digits,
# such as ``emp_000001``. A current user is written this way and is never an alias or a
# code (requirement 20.16); step 8 checks that the record exists (requirement 2.10).
INTERNAL_ID_PATTERN: Final = r"^[a-z][a-z0-9_]*_[0-9]{6,}$"
InternalId = Annotated[str, StringConstraints(pattern=INTERNAL_ID_PATTERN)]

# The alias of a dataset record, ``<kind>:<name>`` such as ``supplier:main``
# (seed-data.md section 2). Aliases are unique because they are mapping keys: YAML reading
# rejects a repeated key at its line (loader.yaml_io), which is the "repeated alias" of
# requirement 20.6. The Seed_Builder checks the prefix against the record's kind.
ALIAS_PATTERN: Final = r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$"
Alias = Annotated[str, StringConstraints(pattern=ALIAS_PATTERN)]

# ``month(N)``, the only expression of the period category (requirement 16.4). The grammar
# of common.date_expr, which validates it; the JSON Schema states the same.
PERIOD_EXPR_PATTERN: Final = r"^month\((?:0|-?[1-9][0-9]*)\)$"


def _check_period_expr(text: str) -> str:
    # Only the text is checked. Its value needs an anchor date, and a legal text whose
    # month falls outside the years 0001 to 9999 is a DateExprRangeError of the Clock at
    # build time, not an error of the configuration.
    try:
        parse_date_expr(text, Category.PERIOD)
    except DateExprError as error:
        raise PydanticCustomError(
            "period_expr_invalid",
            "{value} 不是期间类日期表达式（如 month(0)、month(-1)）：位置 {position} 处无法解析",
            {"value": text, "position": error.position},
        ) from None
    return text


PeriodExpr = Annotated[
    str,
    AfterValidator(_check_period_expr),
    WithJsonSchema({"type": "string", "pattern": PERIOD_EXPR_PATTERN}),
]


@lru_cache(maxsize=1)
def _zone_names() -> frozenset[str]:
    """The time zone names of zoneinfo's search path and of the tzdata package."""
    return frozenset(available_timezones())


def _check_timezone(name: str) -> str:
    # available_timezones() matches names exactly, as requirement 16.3 requires;
    # ZoneInfo(name) would accept "asia/shanghai" on a case-insensitive file system. Only
    # membership is read, so the order of the set never reaches the configuration.
    if name not in _zone_names():
        raise PydanticCustomError(
            "timezone_invalid", "{value} 不是 IANA 时区数据库中的时区名", {"value": name}
        )
    return name


# A time zone name such as Asia/Shanghai or UTC (requirement 16.3).
TimezoneName = Annotated[NonEmptyStr, AfterValidator(_check_timezone)]

# The Name pattern, for the ``kind`` of a record, which a validator checks itself.
_NAME: Final = re.compile(NAME_PATTERN)


@dataclass(frozen=True, slots=True)
class _ClosedKeys:
    """JSON Schema of a map with patterned keys: other keys are not allowed.

    The marker :data:`~erp_mock.config.base.NameMap` applies to its Name keys, here for the
    map whose keys are aliases (the same helper as in pack.py).
    """

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler(schema)
        if "patternProperties" in json_schema:
            json_schema["additionalProperties"] = False
        return json_schema


# --- instance configuration --------------------------------------------------------------

ClockMode = Literal["today", "fixed"]


class ClockDef(FrozenModel):
    """``clock``: how a sandbox gets its anchor date (requirement 16.1, seed-data.md 4).

    ``today`` reads the system time once, when the sandbox is created, and takes the date
    in ``timezone``, the local time zone of the service process when none is given;
    ``fixed`` takes ``anchor_date`` and therefore requires it (requirement 16.3). A date
    given for one sandbox (the Admin API, ``--anchor-date``) wins over both, so
    ``anchor_date`` stays allowed with mode today.
    """

    mode: ClockMode = "today"
    anchor_date: DateText | None = None
    timezone: TimezoneName | None = None

    @model_validator(mode="after")
    def check_anchor_date(self) -> Self:
        """Mode fixed needs ``anchor_date``: a missing one is an error at it (req 16.3)."""
        if self.mode == "fixed" and self.anchor_date is None:
            raise validation_error([known_error(("anchor_date",), "missing", None)])
        return self


class InstanceDef(FrozenModel):
    """``instance.yaml``: the packs, profiles and datasets of a service instance.

    ``packs`` pins each pack to a concrete version, never a range (requirement 2.3), and
    keeps the written order, which orders the packs of the instance everywhere else, the
    sorting of configuration errors included (design.md 2.3). ``profiles`` and ``datasets``
    name the definitions in ``profiles/`` and ``datasets/``, and the two defaults select
    the profile and the dataset of a client that asks for neither (requirement 15.1).
    ``current_user`` is the current user of every dataset that does not set its own; it may
    be left out only when each dataset does set it (requirement 2.10).
    """

    instance: ConfigName
    version: VersionText
    title: Label
    packs: NameMap[VersionText]
    profiles: FrozenList[ConfigName]
    default_profile: ConfigName
    datasets: FrozenList[ConfigName]
    default_dataset: ConfigName
    current_user: InternalId | None = None
    clock: ClockDef = ClockDef()


# --- profile -----------------------------------------------------------------------------

# The two tool surfaces of tool-design.md section 8; M0 serves semantic.
Surface = Literal["semantic", "generic"]


class ProfileDef(FrozenModel):
    """``profiles/<name>.yaml``: which tools a session sees (tool-design.md section 8).

    ``packs`` selects the packs whose tools the profile exposes, in the order tools/list
    returns them; an empty ``include_tools`` means every tool of those packs, and
    ``exclude_tools`` drops tools from that set (requirement 11.10). M0 serves one profile,
    whose surface is semantic, description_variant zh-detailed and include_deprecated
    false; step 4 checks those three values (requirement 2.13), so the other surface and
    the other variants of :data:`~erp_mock.config.pack.DESCRIPTION_VARIANTS` are
    structurally valid here.
    """

    profile: ConfigName
    version: VersionText
    surface: Surface
    description_variant: NonEmptyStr
    packs: FrozenList[Name]
    include_tools: FrozenList[Name] = ()
    exclude_tools: FrozenList[Name] = ()
    include_deprecated: bool = False


# --- dataset records ---------------------------------------------------------------------

# A value written in a record, or the generator call that produces it (requirement 20.3).
# The generator models of pack.py validate the call, so an unknown generator or a parameter
# that does not fit it is an error of the document (requirement 2.13) instead of a build
# failure (requirement 20.6).
FieldValue = ScalarDefault | GeneratorObject

# A TypeAdapter does not inherit the configuration of a model, so strictness is stated for
# the parts of a record that are validated through one.
_STRICT: Final = ConfigDict(strict=True)
_GENERATOR: Final = TypeAdapter[GeneratorObject](GeneratorDecl, config=_STRICT)

# The written values: exactly a str, an int or a bool. A YAML float is none of them.
_SCALAR_TYPES: Final = (str, int, bool)


def _is_scalar(value: object) -> TypeGuard[str | int | bool]:
    return type(value) in _SCALAR_TYPES


def _validate_field_value(value: object, handler: ValidatorFunctionWrapHandler) -> FieldValue:
    """Validate one written field value: the value itself, or a generator call."""
    if _is_scalar(value):
        return value
    if isinstance(value, GeneratorObject):
        return value
    if isinstance(value, dict):
        # The generator's errors stay below the field, as raised.
        return _GENERATOR.validate_python(value)
    raise PydanticCustomError("field_value", "字段值必须是字符串、整数、布尔值或生成器声明")


# A field value of a record or of one of its lines, selected by its shape. The wrapped
# union schema is not used for validation: it gives the JSON Schema and the serialization,
# as in fields.py.
FieldValueDecl = Annotated[FieldValue, WrapValidator(_validate_field_value)]

# One line of a bill: its written field values, in document order. A line generated by a
# push names its source line with ``source_line`` (requirement 20.16).
LineRecord = NameMap[FieldValueDecl]

_LINES: Final = TypeAdapter[tuple[LineRecord, ...]](FrozenList[LineRecord], config=_STRICT)

RecordValue = FieldValue | tuple[LineRecord, ...]


def _validate_record_value(value: object, handler: ValidatorFunctionWrapHandler) -> RecordValue:
    """Validate one value of a record: a field value, or the lines of a bill."""
    if isinstance(value, list | tuple):
        return _LINES.validate_python(value)
    return _validate_field_value(value, handler)


RecordValueDecl = Annotated[RecordValue, WrapValidator(_validate_record_value)]

_KIND_KEY: Final = "kind"


def _check_kind(record: FrozenMap[str, RecordValue]) -> FrozenMap[str, RecordValue]:
    """Check the reserved ``kind`` key of a dataset record (requirement 20.16)."""
    if _KIND_KEY not in record:
        raise validation_error([known_error((_KIND_KEY,), "missing", dict(record))])
    kind = record[_KIND_KEY]
    if not isinstance(kind, str):
        raise validation_error([known_error((_KIND_KEY,), "string_type", kind)])
    if _NAME.fullmatch(kind) is None:
        error = known_error((_KIND_KEY,), "string_pattern_mismatch", kind, pattern=NAME_PATTERN)
        raise validation_error([error])
    return record


# One record of ``entities``: the entity or bill under the reserved key ``kind``, then the
# written field values and, for a bill, its ``lines``. Refs are written without the ``_id``
# suffix, their value being an alias or the code of the referenced record, a field named
# ``kind`` is written ``<kind value>_kind``, and a bill generated by a push names its
# source with ``source`` (requirement 20.16).
DatasetRecord = Annotated[NameMap[RecordValueDecl], AfterValidator(_check_kind)]


# --- dataset -----------------------------------------------------------------------------

# The ``closed_before`` of a dataset that declares no periods: every period before the
# month of the anchor date is closed (requirement 20.1).
DEFAULT_CLOSED_BEFORE: Final = "month(0)"


class PeriodsDef(FrozenModel):
    """``periods``: which accounting periods are closed (requirement 20.1).

    Periods are calendar months and the month of the anchor date is the current period;
    periods before ``closed_before`` are closed, so the default ``month(0)`` closes every
    period before the anchor month and ``month(-1)`` leaves the previous month open
    (seed-data.md section 4). ``closed_before`` is the only key of ``periods``.
    """

    closed_before: PeriodExpr = DEFAULT_CLOSED_BEFORE


class BulkDef(FrozenModel):
    """One item of ``bulk``: ``count`` background records of the entity ``kind``.

    The Seed_Builder creates them after the records of ``entities``, in the order of the
    list and without aliases; a field with a generator gets a generated value and every
    other field its default (requirement 20.4). ``kind`` must be an entity whose ``shared``
    is false, which the Seed_Builder checks (requirement 20.6).
    """

    kind: Name
    count: Annotated[int, Field(ge=1)]


class DatasetDef(FrozenModel):
    """``datasets/<name>.yaml``: an initial dataset (requirement 20.1, seed-data.md 2).

    ``random_seed`` seeds the single pseudo random sequence of a build, so it is a
    non-negative integer (requirement 20.3). The Seed_Builder creates, in this order, the
    Shared_Data of the packs of ``include_shared``, the records of ``entities`` with their
    aliases, and the records of ``bulk``, each group in its written order
    (requirement 20.17); leaving one of the three out means none. ``current_user``
    overrides the current user of the instance configuration.
    """

    dataset: ConfigName
    version: VersionText
    random_seed: Annotated[int, Field(ge=0)]
    include_shared: FrozenList[Name] = ()
    current_user: InternalId | None = None
    periods: PeriodsDef = PeriodsDef()
    entities: Annotated[FrozenMap[Alias, DatasetRecord], _ClosedKeys()] = FrozenMap()
    bulk: FrozenList[BulkDef] = ()
