"""erp_mock.tools.input_schema: what every generated tool takes (requirement 11.3 to 11.12).

Two halves:

- the engine test fixture, loaded through step 6, is read for what a real configuration
  generates: the parameters of each kind of tool and their order (requirement 11.4), the two
  parameters of a date filter (requirement 11.5), the limits of ``page_size`` and ``lines``
  (requirement 11.6), the ``required`` list (requirement 11.12), and - over every tool of
  both packs - that no schema uses a keyword outside the compatible subset of
  tool-design.md section 4, that every object is closed, and that the whole thing is a valid
  JSON Schema (requirement 11.3);
- the type of one parameter (requirement 11.11) is read off :func:`field_schema` with a
  field of each of the 12 types, including the cases the fixture has no write tool for.

Schemas are compared after a round trip through canonical JSON, the text tools/list
carries: that turns the FrozenMaps and tuples into the dicts and lists a client sees, and
anything the generator cannot serialize fails right here.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from erp_mock.common.jsonio import JsonValue, dumps_compact
from erp_mock.config.fields import FieldType
from erp_mock.config.model import FieldLevel, FieldModel, FieldRef, SearchModel
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET, ToolSpec
from erp_mock.tools.input_schema import (
    KEYWORDS,
    MAX_LINES,
    MAX_PAGE_SIZE,
    MIN_LINES,
    MIN_PAGE,
    PERIOD_PATTERN,
    STANDARD_ORDER,
    bill_input,
    field_schema,
    schema_object,
)
from tests.helpers.fixtures import fixture_domain, load_fixture

# The 14 JSON Schema keywords of tool-design.md section 4 (requirement 11.3), written out
# here so that the module under test is compared with the specification rather than with
# itself.
COMPATIBLE_KEYWORDS: Final = frozenset(
    {
        "type",
        "properties",
        "required",
        "enum",
        "items",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "maxLength",
        "pattern",
        "format",
        "description",
        "additionalProperties",
    }
)

# The actions whose tool takes the id of one record and nothing else (requirement 11.4).
ID_ONLY_ACTIONS: Final = frozenset(
    {"get", "delete", "submit", "unsubmit", "approve", "unapprove", "close", "unclose"}
)

# A schema as a client reads it: parsed JSON, so dicts and lists.
type Json = Mapping[str, Any]


@pytest.fixture(scope="module")
def specs() -> Mapping[str, ToolSpec]:
    """Every tool the fixture exposes, by name, as step 6 generated it."""
    tools = load_fixture(6).tools
    assert tools is not None
    return {spec.name: spec for spec in tools.tools}


def as_json(schema: JsonValue) -> Json:
    """A schema as a client reads it: the canonical JSON text of tools/list, parsed back."""
    parsed = json.loads(dumps_compact(schema))
    assert isinstance(parsed, dict)
    return parsed


def schema_of(specs: Mapping[str, ToolSpec], tool: str) -> Json:
    """The inputSchema of ``tool``, parsed."""
    return as_json(specs[tool].input_schema)


def properties(schema: Json) -> Json:
    """The ``properties`` of a schema object."""
    found = schema["properties"]
    assert isinstance(found, dict)
    return found


def lines_items(schema: Json) -> Json:
    """The ``items`` of the ``lines`` array of a write tool."""
    found = properties(schema)["lines"]["items"]
    assert isinstance(found, dict)
    return found


def names(schema: Json) -> list[str]:
    """The parameter names of a schema object, in declaration order."""
    return list(properties(schema))


def required(schema: Json) -> list[str]:
    """The required parameters of a schema object; an empty list when it has none."""
    return list(schema.get("required", []))


def nodes(schema: Json, path: str = "") -> Iterator[tuple[str, Json]]:
    """Every schema in ``schema``, the top level included, with the path it sits at."""
    yield path, schema
    for param, item in schema.get("properties", {}).items():
        if isinstance(item, dict):
            yield from nodes(item, f"{path}.{param}")
    items = schema.get("items")
    if isinstance(items, dict):
        yield from nodes(items, f"{path}[]")


def field(field_type: FieldType, **overrides: Any) -> FieldModel:
    """One declared, writable field of ``field_type``, for the type mapping."""
    return FieldModel(name="x", type=field_type, origin="declared", writable=True, **overrides)


# --- the compatible subset (requirement 11.3) --------------------------------------------


@pytest.mark.req("11.3")
def test_the_keywords_are_the_compatible_subset_of_tool_design() -> None:
    assert len(KEYWORDS) == 14
    assert len(set(KEYWORDS)) == len(KEYWORDS)
    assert frozenset(KEYWORDS) == COMPATIBLE_KEYWORDS


@pytest.mark.req("11.3")
def test_every_schema_uses_only_those_keywords_and_closes_every_object(
    specs: Mapping[str, ToolSpec],
) -> None:
    for name, spec in specs.items():
        for path, node in nodes(as_json(spec.input_schema)):
            where = f"{name}{path}"
            assert set(node) <= COMPATIBLE_KEYWORDS, where
            # The description of each parameter is put in by task 9.4, verbatim from the
            # variant of the profile (requirement 3.7); step 6 writes none.
            assert "description" not in node, where
            if node.get("type") == "object":
                # The top level and the lines element are the only objects (req 11.3).
                assert path in {"", ".lines[]"}, where
                assert node["additionalProperties"] is False, where


@pytest.mark.req("11.3")
def test_every_input_schema_is_a_valid_json_schema(specs: Mapping[str, ToolSpec]) -> None:
    for name, spec in specs.items():
        schema = as_json(spec.input_schema)

        assert schema["type"] == "object", name
        Draft202012Validator.check_schema(schema)


@pytest.mark.req("11.3")
def test_the_builtin_tool_takes_no_parameters(specs: Mapping[str, ToolSpec]) -> None:
    schema = schema_of(specs, BUSINESS_CONTEXT_GET)

    assert schema == {"type": "object", "properties": {}, "additionalProperties": False}
    assert specs[BUSINESS_CONTEXT_GET].param_paths == ()


@pytest.mark.req("11.3")
@pytest.mark.parametrize("keyword", ["$ref", "oneOf", "minLength", "exclusiveMinimum", "title"])
def test_schema_object_rejects_a_keyword_outside_the_subset(keyword: str) -> None:
    with pytest.raises(ValueError, match=keyword.replace("$", r"\$")):
        schema_object(type="string", **{keyword: "x"})


@pytest.mark.req("11.3")
def test_schema_object_writes_the_keys_in_the_order_of_the_keyword_list() -> None:
    found = schema_object(additionalProperties=False, required=("a",), type="object", properties={})

    assert list(found) == ["type", "properties", "required", "additionalProperties"]


# --- the parameters of a tool and their order (requirements 11.4 and 11.5) ---------------

# One case per kind of tool, with the parameter paths it generates, in the order of
# requirement 11.4: the fields the pack declares at that level, then the writable standard
# fields it did not declare, then lines.
PARAM_PATHS: Final[Mapping[str, tuple[str, ...]]] = {
    # The header declares price_includes_tax by name, so the parameter stays where the pack
    # wrote it and biz_date and remark follow (requirement 1.5); the lines declare tax_rate,
    # so the standard qty and price follow it.
    "supply_order_create": (
        "vendor_id",
        "need_date",
        "price_includes_tax",
        "biz_date",
        "remark",
        "lines",
        "lines[].article_id",
        "lines[].measure_id",
        "lines[].tax_rate",
        "lines[].qty",
        "lines[].price",
    ),
    # Nothing of this header is declared by name: the three standard parameters follow the
    # declared ones in the order biz_date, price_includes_tax, remark, and the lines of the
    # pricing bill get qty, price and tax_rate in that order.
    "pay_slip_create": (
        "vendor_id",
        "pay_mode",
        "biz_date",
        "price_includes_tax",
        "remark",
        "lines",
        "lines[].item_text",
        "lines[].qty",
        "lines[].price",
        "lines[].tax_rate",
    ),
    # A bill without pricing has no price_includes_tax and no standard line parameters.
    "count_sheet_create": (
        "depot_id",
        "count_kind",
        "biz_date",
        "remark",
        "lines",
        "lines[].article_id",
        "lines[].measure_id",
        "lines[].diff_qty",
    ),
    # update takes the id first and then exactly what create takes.
    "count_sheet_update": (
        "id",
        "depot_id",
        "count_kind",
        "biz_date",
        "remark",
        "lines",
        "lines[].article_id",
        "lines[].measure_id",
        "lines[].diff_qty",
    ),
    # A push tool: the short name of the source (requirement 11.13) and the
    # required_line_inputs of the conversion, in the order it declares them.
    "goods_entry_create_from_order": (
        "order_id",
        "lines",
        "lines[].source_line_id",
        "lines[].entry_qty",
        "lines[].depot_id",
    ),
    "pay_slip_create_from_fee": (
        "fee_id",
        "lines",
        "lines[].source_line_id",
        "lines[].qty",
        "lines[].price",
    ),
    # search: the keyword, the filters in declared order with biz_date as a range, and the
    # two page parameters (requirements 11.4 and 11.5).
    "fee_note_search": (
        "keyword",
        "status",
        "vendor_id",
        "date_from",
        "date_to",
        "page",
        "page_size",
    ),
    # An entity whose search declares no filters still gets keyword and the page ones.
    "measure_search": ("keyword", "page", "page_size"),
    # A declarative query takes the parameters of the exposure, a primitive query those the
    # primitive defines (requirements 5.13, 9.7 and 9.12).
    "supply_price_query": ("vendor_id", "article_id"),
    "stock_level_query": ("material_id", "warehouse_id", "page", "page_size"),
}


@pytest.mark.req("11.4")
@pytest.mark.parametrize("tool", list(PARAM_PATHS))
def test_the_parameters_of_a_tool_follow_the_declaration_order(
    specs: Mapping[str, ToolSpec], tool: str
) -> None:
    expected = PARAM_PATHS[tool]
    spec = specs[tool]
    schema = as_json(spec.input_schema)

    # param_paths names every parameter that needs a description (requirement 3.2), in the
    # declaration order of the schema itself.
    assert spec.param_paths == expected
    top = [path for path in expected if "." not in path]
    assert names(schema) == top
    if "lines" in top:
        prefix = "lines[]."
        element = [path.removeprefix(prefix) for path in expected if path.startswith(prefix)]
        assert names(lines_items(schema)) == element


@pytest.mark.req("11.4")
def test_a_read_or_state_tool_takes_the_id_alone(specs: Mapping[str, ToolSpec]) -> None:
    found = {
        name: spec
        for name, spec in specs.items()
        if spec.action in ID_ONLY_ACTIONS and name != BUSINESS_CONTEXT_GET
    }
    # get, delete and the state operations of both packs of the fixture.
    assert len(found) > 20

    for name, spec in found.items():
        schema = as_json(spec.input_schema)

        assert spec.param_paths == ("id",), name
        assert properties(schema) == {"id": {"type": "string"}}, name
        assert required(schema) == ["id"], name


@pytest.mark.req("11.4")
def test_a_write_tool_takes_the_writable_fields_and_no_other_one(
    specs: Mapping[str, ToolSpec],
) -> None:
    domain = fixture_domain()

    for name, spec in specs.items():
        if spec.action not in {"create", "update"}:
            continue
        schema = as_json(spec.input_schema)
        bill = domain.bills.get(spec.target)
        fields = bill.header if bill is not None else domain.entities[spec.target].fields

        # Read-only and computed fields are no parameters of a write tool (req 1.3): the
        # id, the number, the status, the amounts, qty_base and the link field of a push.
        assert set(names(schema)) - {"id", "lines"} == {
            item for item, declared in fields.items() if declared.writable
        }, name
        if bill is not None:
            assert set(names(lines_items(schema))) == {
                item for item, declared in bill.lines.items() if declared.writable
            }, name


@pytest.mark.req("11.4")
def test_the_standard_parameters_are_the_writable_standard_fields() -> None:
    domain = fixture_domain()
    levels: list[tuple[FieldLevel, Mapping[str, FieldModel]]] = [
        ("record", entity.fields) for entity in domain.entities.values()
    ]
    levels += [("header", bill.header) for bill in domain.bills.values()]
    levels += [("line", bill.lines) for bill in domain.bills.values()]

    # STANDARD_ORDER holds the writable standard fields of each level and nothing else: a
    # standard field a write tool may write is one of them (requirement 1.3), and the
    # fixture, with its pricing bills and its entity with a status, covers all of them.
    found: dict[FieldLevel, set[str]] = {"record": set(), "header": set(), "line": set()}
    for level, fields in levels:
        writable = {
            item
            for item, declared in fields.items()
            if declared.origin == "standard" and declared.writable
        }
        assert writable <= set(STANDARD_ORDER[level]), level
        found[level] |= writable
    assert found == {level: set(order) for level, order in STANDARD_ORDER.items()}


@pytest.mark.req("11.5")
def test_a_date_filter_becomes_a_from_and_a_to_parameter() -> None:
    order = fixture_domain().bills["supply_order"]
    # The fixture filters by biz_date, which becomes date_from and date_to; every other
    # date field F becomes F_from and F_to, so the declared need_date is filtered here.
    filtered = replace(
        order,
        search=SearchModel(
            keyword_fields=(FieldRef("header", "number"),),
            filters=(
                FieldRef("header", "need_date"),
                FieldRef("header", "biz_date"),
                FieldRef("header", "status"),
            ),
        ),
    )
    schema = as_json(bill_input(filtered, "search").schema)

    assert names(schema) == [
        "keyword",
        "need_date_from",
        "need_date_to",
        "date_from",
        "date_to",
        "status",
        "page",
        "page_size",
    ]
    # Both ends are optional; only the end that is given limits the range (req 5.9).
    assert required(schema) == []
    date = {"type": "string", "format": "date"}
    assert properties(schema)["need_date_from"] == date
    assert properties(schema)["date_to"] == date


# --- the limits (requirement 11.6) -------------------------------------------------------


@pytest.mark.req("11.6")
def test_page_starts_at_one_and_a_page_holds_at_most_100_records(
    specs: Mapping[str, ToolSpec],
) -> None:
    assert (MIN_PAGE, MAX_PAGE_SIZE) == (1, 100)

    for tool in ("article_search", "supply_order_search", "stock_level_query"):
        found = properties(schema_of(specs, tool))

        assert found["page"] == {"type": "integer", "minimum": MIN_PAGE}, tool
        assert found["page_size"] == {
            "type": "integer",
            "minimum": MIN_PAGE,
            "maximum": MAX_PAGE_SIZE,
        }, tool


@pytest.mark.req("11.6")
def test_a_bill_is_written_with_1_to_200_lines(specs: Mapping[str, ToolSpec]) -> None:
    assert (MIN_LINES, MAX_LINES) == (1, 200)
    bills = fixture_domain().bills
    # Every tool that writes a bill: its create, its update and the pushes into it.
    written = [
        (name, spec)
        for name, spec in specs.items()
        if (spec.action in {"create", "update"} and spec.target in bills) or spec.kind == "push"
    ]
    assert len(written) > 10

    for name, spec in written:
        found = properties(as_json(spec.input_schema)).get("lines")
        assert found is not None, name
        assert found["type"] == "array", name
        assert (found["minItems"], found["maxItems"]) == (MIN_LINES, MAX_LINES), name
        assert found["items"]["type"] == "object", name


# --- the type of a parameter (requirements 11.6 and 11.11) -------------------------------

# The type table of meta-model.md section 3, one case per type and per constraint a field
# carries (requirement 11.11).
TYPE_CASES: Final[tuple[tuple[str, FieldModel, Json], ...]] = (
    ("string", field("string"), {"type": "string", "maxLength": 100}),
    ("string_max_length", field("string", max_length=20), {"type": "string", "maxLength": 20}),
    ("text", field("text"), {"type": "string", "maxLength": 500}),
    ("int", field("int"), {"type": "integer"}),
    ("bool", field("bool"), {"type": "boolean"}),
    ("date", field("date"), {"type": "string", "format": "date"}),
    ("period", field("period"), {"type": "string", "pattern": PERIOD_PATTERN}),
    ("ref", field("ref", ref="measure"), {"type": "string"}),
    ("qty", field("qty"), {"type": "number", "minimum": 0}),
    ("price", field("price"), {"type": "number", "minimum": 0}),
    ("money", field("money"), {"type": "number", "minimum": 0}),
    ("rate", field("rate"), {"type": "number", "minimum": 0}),
    (
        "rate_values",
        field("rate", values=("0", "0.06", "0.130")),
        # The allowed values become JSON numbers, an integral one an integer; the input
        # validator compares them by value, so 0.130 is 0.13 (requirement 12.4).
        {"type": "number", "enum": [0, 0.06, 0.13], "minimum": 0},
    ),
)


@pytest.mark.req("11.11")
@pytest.mark.parametrize(
    ("declared", "expected"),
    [pytest.param(case[1], case[2], id=case[0]) for case in TYPE_CASES],
)
def test_the_schema_of_each_field_type(declared: FieldModel, expected: Json) -> None:
    assert as_json(field_schema(declared)) == expected


@pytest.mark.req("11.11")
def test_the_allowed_values_of_a_declared_field_become_a_numeric_enum() -> None:
    tax_rate = fixture_domain().entities["article"].fields["tax_rate"]

    assert as_json(field_schema(tax_rate)) == {
        "type": "number",
        "enum": [0, 0.06, 0.09, 0.13],
        "minimum": 0,
    }


@pytest.mark.req("11.11")
@pytest.mark.parametrize(
    ("tool", "param", "codes"),
    [
        # A declared enum field, in the order the pack writes its values.
        ("count_sheet_create", "count_kind", ["gain", "loss"]),
        # The status of a bill: the states of its own preset state machine.
        ("fee_note_search", "status", ["draft", "approved"]),
        ("supply_order_search", "status", ["draft", "submitted", "approved", "closed"]),
        # The status of an entity whose has_status is true.
        ("vendor_search", "status", ["enabled", "disabled"]),
    ],
)
def test_an_enum_parameter_lists_the_codes_of_its_field(
    specs: Mapping[str, ToolSpec], tool: str, param: str, codes: list[str]
) -> None:
    assert properties(schema_of(specs, tool))[param] == {"type": "string", "enum": codes}


@pytest.mark.req("11.11")
def test_a_string_parameter_carries_the_max_length_of_its_field(
    specs: Mapping[str, ToolSpec],
) -> None:
    found = properties(schema_of(specs, "count_sheet_create"))
    element = lines_items(schema_of(specs, "fee_note_create"))

    # remark is the standard text field of every bill header, 500 code points by default;
    # a declared string field without maxLength gets the 100 of its type.
    assert found["remark"] == {"type": "string", "maxLength": 500}
    assert properties(element)["item_text"] == {"type": "string", "maxLength": 100}


# --- required (requirement 11.12) --------------------------------------------------------


@pytest.mark.req("11.12")
@pytest.mark.parametrize(
    ("tool", "top", "element"),
    [
        # The fields the pack declared required; lines is required by create itself.
        ("count_sheet_create", ["depot_id", "count_kind", "lines"], ["article_id", "diff_qty"]),
        # taker_id is required and defaults to the current user, so a call may omit it.
        ("issue_note_create", ["depot_id", "lines"], ["article_id", "issue_qty"]),
        # A pricing bill requires the qty and the price of every line; measure_id and
        # tax_rate have a default_from, and need_date is declared required.
        (
            "supply_order_create",
            ["vendor_id", "need_date", "lines"],
            ["article_id", "qty", "price"],
        ),
        # The lines declare a default for tax_rate, which makes it optional again.
        ("fee_note_create", ["vendor_id", "due_date", "lines"], ["item_text", "qty", "price"]),
        # The source and every required_line_input of a push (requirement 11.12).
        (
            "goods_entry_create_from_order",
            ["order_id", "lines"],
            ["source_line_id", "entry_qty", "depot_id"],
        ),
        # A search requires nothing: a condition that is not given does not filter (5.9).
        ("article_search", [], []),
    ],
)
def test_required_holds_the_declared_required_fields_without_a_default(
    specs: Mapping[str, ToolSpec], tool: str, top: list[str], element: list[str]
) -> None:
    schema = schema_of(specs, tool)

    assert required(schema) == top
    if "lines" in properties(schema):
        assert required(lines_items(schema)) == element
    # An empty required list is left out of the schema entirely.
    assert ("required" in schema) == bool(top)


@pytest.mark.req("11.12")
def test_an_update_requires_the_id_alone_and_leaves_the_lines_optional(
    specs: Mapping[str, ToolSpec],
) -> None:
    # The create tool of the same object, which a rename may have named otherwise.
    creates = {spec.target: spec for spec in specs.values() if spec.action == "create"}
    updates = {name: spec for name, spec in specs.items() if spec.action == "update"}
    assert len(updates) > 5

    for name, spec in updates.items():
        schema = as_json(spec.input_schema)
        create = as_json(creates[spec.target].input_schema)

        assert required(schema) == ["id"], name
        # The lines of an update replace the old ones as a whole and a new line is filled
        # in like a new bill's, so the element requires what create's element requires
        # (requirement 5.3); it is the array itself that an update leaves optional.
        assert required(lines_items(schema)) == required(lines_items(create)), name


@pytest.mark.req("11.12")
def test_the_lines_of_a_push_hold_the_required_line_inputs_and_all_of_them(
    specs: Mapping[str, ToolSpec],
) -> None:
    domain = fixture_domain()
    pushes = [spec for spec in specs.values() if spec.kind == "push"]
    assert len(pushes) == 2

    for spec in pushes:
        conversion = domain.conversions[spec.target]
        schema = as_json(spec.input_schema)
        element = lines_items(schema)

        assert names(element) == list(conversion.required_line_inputs), spec.name
        assert required(element) == list(conversion.required_line_inputs), spec.name
        # Only the source bill and the lines; every other field of a pushed line comes
        # from the mapping or from a default (requirement 11.4).
        assert required(schema) == names(schema), spec.name
