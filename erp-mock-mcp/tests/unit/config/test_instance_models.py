"""erp_mock.config.instance: the instance configuration, its profiles and its datasets.

The examples of business-pack.md section 8, tool-design.md section 8 and seed-data.md
section 2 are parsed here with loader.yaml_io, the way loading step 1 reads instance.yaml
and step 4 reads a profile or a dataset, and validated against their models. The other
documents are built from the minimal required keys with :func:`instance_doc`,
:func:`profile_doc` and :func:`dataset_doc`, which replace or add one key at a time.

Errors are asserted with their pydantic ``loc``, which is the config path the loader
reports (``("clock", "anchor_date")`` is ``clock.anchor_date``).
"""

import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.base import Label, Loc
from erp_mock.config.instance import (
    DEFAULT_CLOSED_BEFORE,
    BulkDef,
    ClockDef,
    DatasetDef,
    InstanceDef,
    PeriodsDef,
    ProfileDef,
)
from erp_mock.config.pack import (
    CompanyNameGenerator,
    DecimalRangeGenerator,
    ShortNameGenerator,
)
from erp_mock.loader.yaml_io import parse_yaml

FILE = Path("instance.yaml")


def parse(text: str) -> Any:
    return parse_yaml(textwrap.dedent(text), FILE)


def load[M: BaseModel](model: type[M], text: str) -> M:
    """Validate the YAML document ``text`` as ``model``, as a loading step validates it."""
    return model.model_validate(parse(text))


def errors(model: type[BaseModel], text: str) -> list[tuple[Loc, str]]:
    """The ``(loc, type)`` of every error of the YAML document ``text`` as a ``model``."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(parse(text))
    return [(error["loc"], error["type"]) for error in caught.value.errors()]


def document(required: dict[str, str], overrides: dict[str, str]) -> str:
    """A YAML document of ``required``, with ``overrides`` replacing or adding keys."""
    return "".join(f"{key}: {value}\n" for key, value in (required | overrides).items())


# --- instance.yaml (requirement 2.12) ----------------------------------------------------

# The instance.yaml of business-pack.md section 8, with the keys M0 restricts written as
# the document writes them: the model accepts several profiles and datasets, and step 4 is
# what requires profiles to hold semantic alone (requirement 2.13).
INSTANCE_EXAMPLE = """
    instance: p2p-demo
    version: 1.0.0
    title: { zh: 采购到付款示例 }
    packs:
      core: 1.0.0
      purchase: 1.0.0
      inventory: 1.0.0
    profiles: [semantic, semantic-brief, generic]
    default_profile: semantic
    datasets: [purchase-basic, ap-month-end]
    default_dataset: purchase-basic
    current_user: emp_000001
    clock:
      mode: today
      timezone: Asia/Shanghai
    """

INSTANCE_KEYS = {
    "instance": "p2p-demo",
    "version": "1.0.0",
    "title": "{ zh: 采购到付款示例 }",
    "packs": "{ core: 1.0.0 }",
    "profiles": "[semantic]",
    "default_profile": "semantic",
    "datasets": "[purchase-basic]",
    "default_dataset": "purchase-basic",
}


def instance_doc(**overrides: str) -> str:
    """An instance.yaml with the required keys, ``overrides`` replacing or adding keys."""
    return document(INSTANCE_KEYS, overrides)


@pytest.mark.req("2.12", "16.3")
def test_instance_example() -> None:
    instance = load(InstanceDef, INSTANCE_EXAMPLE)
    assert (instance.instance, instance.version) == ("p2p-demo", "1.0.0")
    assert instance.title == Label(zh="采购到付款示例")
    # The packs keep the written order, which orders the packs of the instance elsewhere.
    assert list(instance.packs) == ["core", "purchase", "inventory"]
    assert instance.packs["purchase"] == "1.0.0"
    assert instance.profiles == ("semantic", "semantic-brief", "generic")
    assert instance.datasets == ("purchase-basic", "ap-month-end")
    assert (instance.default_profile, instance.default_dataset) == ("semantic", "purchase-basic")
    assert instance.current_user == "emp_000001"
    assert instance.clock == ClockDef(mode="today", timezone="Asia/Shanghai")


@pytest.mark.req("2.12", "16.3")
def test_instance_defaults() -> None:
    instance = load(InstanceDef, instance_doc())
    # Without a current user every dataset must set its own (requirement 2.10, step 8).
    assert instance.current_user is None
    # Without a clock the anchor date is the current date of the local time zone.
    assert instance.clock == ClockDef()
    assert (instance.clock.mode, instance.clock.anchor_date, instance.clock.timezone) == (
        "today",
        None,
        None,
    )


@pytest.mark.req("2.12")
def test_instance_required_keys() -> None:
    assert errors(InstanceDef, "{}\n") == [
        (("instance",), "missing"),
        (("version",), "missing"),
        (("title",), "missing"),
        (("packs",), "missing"),
        (("profiles",), "missing"),
        (("default_profile",), "missing"),
        (("datasets",), "missing"),
        (("default_dataset",), "missing"),
    ]


@pytest.mark.req("2.12")
@pytest.mark.parametrize(
    ("key", "value", "loc", "error_type"),
    [
        ("instance", "P2P-Demo", ("instance",), "string_pattern_mismatch"),
        ("instance", "-demo", ("instance",), "string_pattern_mismatch"),
        ("instance", "'p2p demo'", ("instance",), "string_pattern_mismatch"),
        ("instance", "1", ("instance",), "string_type"),
        ("version", "'1.0'", ("version",), "version_invalid"),
        ("version", "'>=1.0.0'", ("version",), "version_invalid"),
        ("version", "'01.0.0'", ("version",), "version_invalid"),
        ("version", "1.0", ("version",), "string_type"),  # a YAML float
        ("title", "采购到付款示例", ("title",), "model_type"),
        ("title", "{ en: P2P }", ("title", "zh"), "missing"),
        # A pack reference is a concrete version, never a range (requirement 2.3).
        ("packs", "{ core: '>=1.0.0' }", ("packs", "core"), "version_invalid"),
        ("packs", "{ core: 1 }", ("packs", "core"), "string_type"),
        ("packs", "{ Core: 1.0.0 }", ("packs", "Core", "[key]"), "string_pattern_mismatch"),
        ("packs", "[core]", ("packs",), "dict_type"),
        ("profiles", "semantic", ("profiles",), "tuple_type"),
        ("profiles", "[Semantic]", ("profiles", 0), "string_pattern_mismatch"),
        ("datasets", "{ purchase-basic: 1 }", ("datasets",), "tuple_type"),
        ("default_profile", "''", ("default_profile",), "string_pattern_mismatch"),
        # The current user is an internal ID, never an alias or a code (req 20.16).
        ("current_user", "'employee:buyer'", ("current_user",), "string_pattern_mismatch"),
        ("current_user", "emp_1", ("current_user",), "string_pattern_mismatch"),
        ("current_user", "E00001", ("current_user",), "string_pattern_mismatch"),
        ("current_user", "1", ("current_user",), "string_type"),
        ("clock", "today", ("clock",), "model_type"),
        ("admin_token", "secret", ("admin_token",), "extra_forbidden"),
    ],
)
def test_instance_value_errors(key: str, value: str, loc: Loc, error_type: str) -> None:
    assert errors(InstanceDef, instance_doc(**{key: value})) == [(loc, error_type)]


@pytest.mark.req("2.12")
def test_every_error_of_the_instance_is_reported() -> None:
    document = instance_doc(packs="{ core: '>=1.0.0', purchase: 1 }", current_user="emp_1")
    assert errors(InstanceDef, document) == [
        (("packs", "core"), "version_invalid"),
        (("packs", "purchase"), "string_type"),
        (("current_user",), "string_pattern_mismatch"),
    ]


# --- clock (requirement 16.3) ------------------------------------------------------------


@pytest.mark.req("16.3")
def test_clock_fixed_mode() -> None:
    clock = load(ClockDef, "mode: fixed\nanchor_date: '2026-10-09'\n")
    assert (clock.mode, clock.anchor_date, clock.timezone) == ("fixed", "2026-10-09", None)


@pytest.mark.req("16.3")
@pytest.mark.parametrize("name", ["Asia/Shanghai", "UTC", "Europe/Berlin", "Etc/GMT+8"])
def test_clock_accepts_iana_time_zone_names(name: str) -> None:
    assert load(ClockDef, f"timezone: {name}\n").timezone == name


@pytest.mark.req("16.3")
@pytest.mark.parametrize(
    ("clock", "loc", "error_type"),
    [
        ("mode: utc\n", ("mode",), "literal_error"),
        ("mode: Fixed\n", ("mode",), "literal_error"),
        ("mode: TODAY\n", ("mode",), "literal_error"),
        ("mode: null\n", ("mode",), "literal_error"),
        # Mode fixed needs the date it fixes the anchor to.
        ("mode: fixed\n", ("anchor_date",), "missing"),
        ("mode: fixed\nanchor_date: null\n", ("anchor_date",), "missing"),
        ("anchor_date: '2026-02-30'\n", ("anchor_date",), "date_invalid"),
        ("anchor_date: '2026-13-01'\n", ("anchor_date",), "date_invalid"),
        ("anchor_date: '0000-01-01'\n", ("anchor_date",), "date_invalid"),
        ("anchor_date: '2026-2-3'\n", ("anchor_date",), "string_pattern_mismatch"),
        ("anchor_date: 2026/10/09\n", ("anchor_date",), "string_pattern_mismatch"),
        ("anchor_date: today\n", ("anchor_date",), "string_pattern_mismatch"),
        ("anchor_date: 20261009\n", ("anchor_date",), "string_type"),
        # Time zone names are compared case-sensitively against the IANA database.
        ("timezone: asia/shanghai\n", ("timezone",), "timezone_invalid"),
        ("timezone: ASIA/SHANGHAI\n", ("timezone",), "timezone_invalid"),
        ("timezone: Asia/Shanghai/\n", ("timezone",), "timezone_invalid"),
        ("timezone: '+08:00'\n", ("timezone",), "timezone_invalid"),
        ("timezone: ''\n", ("timezone",), "string_too_short"),
        ("timezone: 8\n", ("timezone",), "string_type"),
        ("offset: 8\n", ("offset",), "extra_forbidden"),
    ],
)
def test_clock_errors(clock: str, loc: Loc, error_type: str) -> None:
    assert errors(ClockDef, clock) == [(loc, error_type)]


@pytest.mark.req("16.3")
def test_clock_errors_are_located_below_clock_in_the_instance() -> None:
    assert errors(InstanceDef, instance_doc(clock="{ mode: fixed }")) == [
        (("clock", "anchor_date"), "missing")
    ]
    document = instance_doc(clock="{ mode: today, timezone: Mars/Phobos }")
    assert errors(InstanceDef, document) == [(("clock", "timezone"), "timezone_invalid")]


@pytest.mark.req("16.3")
def test_unknown_time_zone_error_names_the_value() -> None:
    with pytest.raises(ValidationError) as caught:
        load(ClockDef, "timezone: Mars/Phobos\n")
    (error,) = caught.value.errors()
    assert error["ctx"] == {"value": "Mars/Phobos"}
    assert "Mars/Phobos" in error["msg"]


# --- profile (requirement 2.13) ----------------------------------------------------------

# The profile of tool-design.md section 8.
PROFILE_EXAMPLE = """
    profile: semantic
    version: 1.0.0
    surface: semantic
    description_variant: zh-detailed
    packs: [core, purchase, inventory]
    include_tools: []
    exclude_tools: []
    include_deprecated: false
    """

PROFILE_KEYS = {
    "profile": "semantic",
    "version": "1.0.0",
    "surface": "semantic",
    "description_variant": "zh-detailed",
    "packs": "[core]",
}


def profile_doc(**overrides: str) -> str:
    """A profile with the required keys, ``overrides`` replacing or adding keys."""
    return document(PROFILE_KEYS, overrides)


@pytest.mark.req("2.13")
def test_profile_example() -> None:
    profile = load(ProfileDef, PROFILE_EXAMPLE)
    assert (profile.profile, profile.version) == ("semantic", "1.0.0")
    assert (profile.surface, profile.description_variant) == ("semantic", "zh-detailed")
    # tools/list returns the tools pack by pack, in this order (requirement 11.9).
    assert profile.packs == ("core", "purchase", "inventory")
    assert (profile.include_tools, profile.exclude_tools) == ((), ())
    assert profile.include_deprecated is False


@pytest.mark.req("2.13")
def test_profile_defaults() -> None:
    profile = load(ProfileDef, profile_doc())
    # No include_tools means every tool of the selected packs (requirement 11.10).
    assert (profile.include_tools, profile.exclude_tools) == ((), ())
    assert profile.include_deprecated is False


@pytest.mark.req("2.13")
def test_profile_tool_selection() -> None:
    profile = load(
        ProfileDef,
        profile_doc(
            include_tools="[supplier_search, supplier_get]",
            exclude_tools="[supplier_delete]",
            include_deprecated="true",
        ),
    )
    assert profile.include_tools == ("supplier_search", "supplier_get")
    assert profile.exclude_tools == ("supplier_delete",)
    assert profile.include_deprecated is True


@pytest.mark.req("2.13")
def test_profile_values_m0_restricts_are_structurally_valid() -> None:
    # Step 4 reports a surface, a variant or an include_deprecated other than M0's
    # (requirement 2.13), so such a document is well formed here.
    profile = load(
        ProfileDef,
        profile_doc(profile="generic", surface="generic", description_variant="en-brief"),
    )
    assert (profile.profile, profile.surface) == ("generic", "generic")
    assert profile.description_variant == "en-brief"


@pytest.mark.req("2.13")
def test_profile_required_keys() -> None:
    assert errors(ProfileDef, "{}\n") == [
        (("profile",), "missing"),
        (("version",), "missing"),
        (("surface",), "missing"),
        (("description_variant",), "missing"),
        (("packs",), "missing"),
    ]


@pytest.mark.req("2.13")
@pytest.mark.parametrize(
    ("key", "value", "loc", "error_type"),
    [
        ("profile", "Semantic", ("profile",), "string_pattern_mismatch"),
        ("version", "'1.0'", ("version",), "version_invalid"),
        ("surface", "legacy", ("surface",), "literal_error"),
        ("surface", "Semantic", ("surface",), "literal_error"),
        ("description_variant", "''", ("description_variant",), "string_too_short"),
        ("description_variant", "[zh-detailed]", ("description_variant",), "string_type"),
        ("packs", "core", ("packs",), "tuple_type"),
        ("packs", "[Core]", ("packs", 0), "string_pattern_mismatch"),
        ("include_tools", "supplier_search", ("include_tools",), "tuple_type"),
        ("exclude_tools", "[supplier-delete]", ("exclude_tools", 0), "string_pattern_mismatch"),
        ("include_deprecated", "'false'", ("include_deprecated",), "bool_type"),
        ("include_deprecated", "no", ("include_deprecated",), "bool_type"),  # YAML 1.2: a str
        ("include_deprecated", "0", ("include_deprecated",), "bool_type"),
        ("tools", "[supplier_search]", ("tools",), "extra_forbidden"),
    ],
)
def test_profile_value_errors(key: str, value: str, loc: Loc, error_type: str) -> None:
    assert errors(ProfileDef, profile_doc(**{key: value})) == [(loc, error_type)]


# --- dataset (requirement 20.1) ----------------------------------------------------------

# The dataset of seed-data.md section 2, whose generators are the built-in ones of
# design.md data model 1 (the document's ``gen: price`` is not one of them).
DATASET_EXAMPLE = """
    dataset: purchase-basic
    version: 1.0.0
    random_seed: 20261009
    include_shared: [core, gl]
    current_user: emp_000001
    periods: { closed_before: "month(0)" }
    entities:
      supplier:main:
        kind: supplier
        name: { gen: company_name, industry: 机械 }
        short_name: { gen: short_name }
        status: enabled
      material:bolt_m8:
        kind: material
        name: M8 不锈钢螺栓
        material_kind: raw
        purchase_unit: PCS
        tax_rate: "0.13"
      purchase_order:history_last:
        kind: purchase_order
        supplier: "supplier:main"
        biz_date: month(-1).day(12)
        status: closed
        lines:
          - material: "material:bolt_m8"
            qty: "5000"
            price: { gen: decimal_range, min: "0.70", max: "0.95" }
    bulk:
      - { kind: supplier, count: 50 }
      - { kind: material, count: 200 }
    """

DATASET_KEYS = {"dataset": "purchase-basic", "version": "1.0.0", "random_seed": "20261009"}


def dataset_doc(**overrides: str) -> str:
    """A dataset with the required keys, ``overrides`` replacing or adding keys."""
    return document(DATASET_KEYS, overrides)


def record_doc(record: str, alias: str = "supplier:main") -> str:
    """A dataset whose ``entities`` holds the single record ``record`` under ``alias``."""
    return dataset_doc(entities=f"{{ '{alias}': {record} }}")


@pytest.mark.req("20.1")
def test_dataset_example() -> None:
    dataset = load(DatasetDef, DATASET_EXAMPLE)
    assert (dataset.dataset, dataset.version) == ("purchase-basic", "1.0.0")
    assert dataset.random_seed == 20261009
    assert dataset.include_shared == ("core", "gl")
    assert dataset.current_user == "emp_000001"
    assert dataset.periods == PeriodsDef(closed_before="month(0)")
    assert dataset.bulk == (
        BulkDef(kind="supplier", count=50),
        BulkDef(kind="material", count=200),
    )
    # The records keep the written order, which is the order they are created in
    # (requirement 20.17).
    assert list(dataset.entities) == [
        "supplier:main",
        "material:bolt_m8",
        "purchase_order:history_last",
    ]
    supplier = dataset.entities["supplier:main"]
    assert isinstance(supplier, FrozenMap)
    assert list(supplier) == ["kind", "name", "short_name", "status"]
    assert supplier["kind"] == "supplier"
    assert supplier["name"] == CompanyNameGenerator(gen="company_name", industry="机械")
    assert supplier["short_name"] == ShortNameGenerator(gen="short_name")
    assert supplier["status"] == "enabled"
    # A field named like the reserved key kind is written <kind value>_kind (req 20.16).
    assert dataset.entities["material:bolt_m8"]["material_kind"] == "raw"


@pytest.mark.req("20.1")
def test_dataset_example_lines() -> None:
    lines = load(DatasetDef, DATASET_EXAMPLE).entities["purchase_order:history_last"]["lines"]
    assert isinstance(lines, tuple)
    (line,) = lines
    assert list(line) == ["material", "qty", "price"]
    assert line["material"] == "material:bolt_m8"
    assert line["qty"] == "5000"  # decimals are text, as everywhere in a configuration
    price = line["price"]
    assert isinstance(price, DecimalRangeGenerator)
    assert (price.min, price.max) == ("0.70", "0.95")


@pytest.mark.req("20.1")
def test_dataset_defaults() -> None:
    dataset = load(DatasetDef, dataset_doc())
    assert dataset.include_shared == ()
    assert dataset.current_user is None
    assert dataset.entities == FrozenMap()
    assert dataset.bulk == ()
    # Every period before the month of the anchor date is closed (seed-data.md 4).
    assert dataset.periods.closed_before == DEFAULT_CLOSED_BEFORE == "month(0)"


@pytest.mark.req("20.1")
def test_dataset_required_keys() -> None:
    assert errors(DatasetDef, "{}\n") == [
        (("dataset",), "missing"),
        (("version",), "missing"),
        (("random_seed",), "missing"),
    ]


@pytest.mark.req("20.1")
@pytest.mark.parametrize("seed", ["0", "1", "20261009", "4294967296"])
def test_random_seed_is_a_non_negative_integer(seed: str) -> None:
    dataset = load(DatasetDef, dataset_doc(random_seed=seed))
    assert dataset.random_seed == int(seed)


@pytest.mark.req("20.1")
@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        ("-1", "greater_than_equal"),
        ("'20261009'", "int_type"),
        ("1.5", "int_type"),
        ("true", "int_type"),
        ("null", "int_type"),
    ],
)
def test_random_seed_errors(value: str, error_type: str) -> None:
    assert errors(DatasetDef, dataset_doc(random_seed=value)) == [(("random_seed",), error_type)]


@pytest.mark.req("20.1")
@pytest.mark.parametrize("expr", ["month(0)", "month(-1)", "month(1)", "month(-12)", "month(240)"])
def test_closed_before_takes_a_period_expression(expr: str) -> None:
    dataset = load(DatasetDef, dataset_doc(periods=f"{{ closed_before: '{expr}' }}"))
    assert dataset.periods.closed_before == expr


@pytest.mark.req("20.1")
@pytest.mark.parametrize(
    "expr",
    [
        "today",  # a date expression, not a period one
        "month(0).start",
        "month(-1).day(12)",
        "quarter(0)",
        "year(0)",
        "Month(0)",
        "month(-0)",
        "month(01)",
        "month()",
        "'month(0) '",
    ],
)
def test_closed_before_rejects_anything_else(expr: str) -> None:
    assert errors(DatasetDef, dataset_doc(periods=f"{{ closed_before: {expr} }}")) == [
        (("periods", "closed_before"), "period_expr_invalid")
    ]


@pytest.mark.req("20.1")
def test_closed_before_error_names_the_value_and_the_position() -> None:
    with pytest.raises(ValidationError) as caught:
        load(DatasetDef, dataset_doc(periods="{ closed_before: 'month(0).start' }"))
    (error,) = caught.value.errors()
    # Requirement 16.6: the position is where no legal period expression continues.
    assert error["ctx"] == {"value": "month(0).start", "position": 9}


@pytest.mark.req("20.1")
def test_periods_holds_closed_before_alone() -> None:
    assert errors(DatasetDef, dataset_doc(periods="{ closed_after: 'month(0)' }")) == [
        (("periods", "closed_after"), "extra_forbidden")
    ]
    assert errors(DatasetDef, dataset_doc(periods="'month(0)'")) == [(("periods",), "model_type")]


@pytest.mark.req("20.1")
@pytest.mark.parametrize(
    "alias",
    ["supplier", "Supplier:main", "supplier:Main", "supplier:", ":main", "supplier:main:extra"],
)
def test_entities_keys_are_aliases(alias: str) -> None:
    assert errors(DatasetDef, record_doc("{ kind: supplier }", alias=alias)) == [
        (("entities", alias, "[key]"), "string_pattern_mismatch")
    ]


@pytest.mark.req("20.1")
def test_entities_keys_accept_the_alias_of_any_kind() -> None:
    dataset = load(
        DatasetDef,
        dataset_doc(
            entities="{ unit_conversion:cup_box: { kind: unit_conversion }, m:a: { kind: m } }"
        ),
    )
    assert list(dataset.entities) == ["unit_conversion:cup_box", "m:a"]


@pytest.mark.req("20.1")
@pytest.mark.parametrize(
    ("record", "loc", "error_type"),
    [
        ("{ name: x }", ("kind",), "missing"),
        ("{ kind: 1 }", ("kind",), "string_type"),
        ("{ kind: Supplier }", ("kind",), "string_pattern_mismatch"),
        ("supplier", (), "dict_type"),
        ("{ kind: supplier, tax_rate: 0.13 }", ("tax_rate",), "field_value"),  # a YAML float
        ("{ kind: supplier, name: null }", ("name",), "field_value"),
        ("{ kind: supplier, Name: x }", ("Name", "[key]"), "string_pattern_mismatch"),
        ("{ kind: supplier, name: { industry: 机械 } }", ("name", "gen"), "missing"),
        ("{ kind: supplier, name: { gen: full_name } }", ("name", "gen"), "literal_error"),
        ("{ kind: supplier, name: { gen: int_range, min: 1 } }", ("name", "max"), "missing"),
        ("{ kind: supplier, name: { gen: company_name, size: 3 } }", ("name", "size"), "extra_forbidden"),
        ("{ kind: purchase_order, lines: [1] }", ("lines", 0), "dict_type"),
        ("{ kind: purchase_order, lines: [{ qty: 1.5 }] }", ("lines", 0, "qty"), "field_value"),
        ("{ kind: purchase_order, lines: [{ qty: { gen: nope } }] }", ("lines", 0, "qty", "gen"), "literal_error"),
    ],
)  # fmt: skip
def test_record_errors_are_located_below_the_alias(record: str, loc: Loc, error_type: str) -> None:
    assert errors(DatasetDef, record_doc(record)) == [
        (("entities", "supplier:main", *loc), error_type)
    ]


@pytest.mark.req("20.1")
def test_record_values_keep_their_written_type() -> None:
    record = load(
        DatasetDef,
        record_doc(
            "{ kind: supplier, name: 示例供应商, payment_term_days: 30, is_default: true,"
            ' default_tax_rate: "0.13", biz_date: month(-1).day(12) }'
        ),
    ).entities["supplier:main"]
    assert record["name"] == "示例供应商"
    assert record["payment_term_days"] == 30
    assert record["is_default"] is True
    # Decimals and relative dates are text; the Seed_Builder evaluates the expressions.
    assert record["default_tax_rate"] == "0.13"
    assert record["biz_date"] == "month(-1).day(12)"


@pytest.mark.req("20.1", "20.16")
def test_pushed_record_and_empty_lines() -> None:
    entities = load(
        DatasetDef,
        dataset_doc(
            entities=(
                "{ purchase_receipt:first: { kind: purchase_receipt,"
                ' source: "purchase_order:history_last",'
                ' lines: [{ source_line: 1, qty: "500", warehouse: WH01 }] },'
                " purchase_order:empty: { kind: purchase_order, lines: [] } }"
            )
        ),
    ).entities
    receipt = entities["purchase_receipt:first"]
    assert receipt["source"] == "purchase_order:history_last"
    lines = receipt["lines"]
    assert isinstance(lines, tuple)
    (line,) = lines
    # A pushed line names its source line by line number (requirement 20.16).
    assert line["source_line"] == 1
    assert entities["purchase_order:empty"]["lines"] == ()


@pytest.mark.req("20.1")
@pytest.mark.parametrize(
    ("item", "loc", "error_type"),
    [
        ("{ count: 50 }", ("kind",), "missing"),
        ("{ kind: supplier }", ("count",), "missing"),
        ("{ kind: supplier, count: 0 }", ("count",), "greater_than_equal"),
        ("{ kind: supplier, count: -5 }", ("count",), "greater_than_equal"),
        ("{ kind: supplier, count: '50' }", ("count",), "int_type"),
        ("{ kind: supplier, count: 1.5 }", ("count",), "int_type"),
        ("{ kind: Supplier, count: 5 }", ("kind",), "string_pattern_mismatch"),
        ("{ kind: supplier, count: 5, prefix: S }", ("prefix",), "extra_forbidden"),
    ],
)
def test_bulk_items_name_an_entity_and_a_positive_count(
    item: str, loc: Loc, error_type: str
) -> None:
    assert errors(DatasetDef, dataset_doc(bulk=f"[{item}]")) == [(("bulk", 0, *loc), error_type)]


@pytest.mark.req("20.1")
def test_bulk_keeps_the_order_of_the_list() -> None:
    dataset = load(
        DatasetDef,
        dataset_doc(bulk="[{ kind: supplier, count: 50 }, { kind: material, count: 1 }]"),
    )
    assert [(item.kind, item.count) for item in dataset.bulk] == [("supplier", 50), ("material", 1)]


@pytest.mark.req("20.1")
def test_every_error_of_the_dataset_is_reported() -> None:
    document = dataset_doc(
        random_seed="-1",
        include_shared="[Core]",
        periods="{ closed_before: today }",
        entities=(
            "{ supplier:main: { kind: supplier, name: { gen: nope } },"
            " material:bolt: { name: M8 }, unit:pcs: { kind: unit, factor: 1.5 } }"
        ),
        bulk="[{ kind: supplier, count: 0 }]",
    )
    assert errors(DatasetDef, document) == [
        (("random_seed",), "greater_than_equal"),
        (("include_shared", 0), "string_pattern_mismatch"),
        (("periods", "closed_before"), "period_expr_invalid"),
        (("entities", "supplier:main", "name", "gen"), "literal_error"),
        (("entities", "material:bolt", "kind"), "missing"),
        (("entities", "unit:pcs", "factor"), "field_value"),
        (("bulk", 0, "count"), "greater_than_equal"),
    ]


@pytest.mark.req("20.1")
def test_the_kind_of_a_record_is_checked_after_its_values() -> None:
    # The reserved key is checked once every value of the record is valid, so a record with
    # an unusable value reports that value alone.
    assert errors(DatasetDef, record_doc("{ tax_rate: 0.13 }")) == [
        (("entities", "supplier:main", "tax_rate"), "field_value")
    ]


# --- JSON Schema -------------------------------------------------------------------------


@pytest.mark.req("2.12", "2.13", "20.1")
def test_json_schema_matches_the_models() -> None:
    instance = InstanceDef.model_json_schema(mode="validation")
    assert instance["additionalProperties"] is False
    assert instance["required"] == [
        "instance",
        "version",
        "title",
        "packs",
        "profiles",
        "default_profile",
        "datasets",
        "default_dataset",
    ]
    # Pack names other than Name are rejected by the model and by the schema.
    assert instance["properties"]["packs"]["additionalProperties"] is False
    clock = instance["$defs"]["ClockDef"]
    assert clock["properties"]["mode"]["enum"] == ["today", "fixed"]
    assert clock["properties"]["anchor_date"]["anyOf"][0]["format"] == "date"

    profile = ProfileDef.model_json_schema(mode="validation")
    assert profile["properties"]["surface"]["enum"] == ["semantic", "generic"]
    assert profile["properties"]["include_deprecated"]["default"] is False

    dataset = DatasetDef.model_json_schema(mode="validation")
    assert dataset["required"] == ["dataset", "version", "random_seed"]
    assert dataset["properties"]["random_seed"]["minimum"] == 0
    assert dataset["$defs"]["BulkDef"]["properties"]["count"]["minimum"] == 1
    closed_before = dataset["$defs"]["PeriodsDef"]["properties"]["closed_before"]
    assert closed_before["default"] == "month(0)"
    assert closed_before["pattern"] == r"^month\((?:0|-?[1-9][0-9]*)\)$"
    entities = dataset["properties"]["entities"]
    assert entities["additionalProperties"] is False  # keys other than an alias
    record = entities["patternProperties"]["^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$"]
    assert record["additionalProperties"] is False  # field names other than Name
    value = record["patternProperties"]["^[a-z][a-z0-9_]*$"]["anyOf"]
    assert value[:3] == [{"type": "string"}, {"type": "integer"}, {"type": "boolean"}]
    assert [choice["$ref"].removeprefix("#/$defs/") for choice in value[3:-1]] == [
        "CompanyNameGenerator",
        "ShortNameGenerator",
        "PersonNameGenerator",
        "ChoiceGenerator",
        "IntRangeGenerator",
        "DecimalRangeGenerator",
        "CodeWordsGenerator",
    ]
    assert value[-1]["type"] == "array"  # the lines of a bill
