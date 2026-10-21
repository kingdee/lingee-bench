"""erp_mock.tools.output_schema: what every generated tool returns (requirements 11.7, 11.8).

Three halves, the first two about the schema and the third about the annotations:

- every outputSchema of the engine test fixture, loaded through step 6, is checked as a
  schema: it passes the meta-schema of JSON Schema 2020-12, uses no keyword outside the
  five structural ones, closes every object, requires every key but ``truncated`` and
  declares ``truncated`` last (requirement 11.7);
- the keys of each kind of result and their order are asserted against a table, one case
  per shape - a bill with its lines, an entity with its conversion records, a list, a
  declarative query, the stock balance query and the built-in tool - plus the shape of a
  nested reference, of a decimal value and of an entity that declares neither code nor
  name, which the fixture has no tool for;
- the annotations are read off every tool: ``openWorldHint`` false everywhere,
  ``readOnlyHint`` true exactly on the searches, gets, queries and the built-in tool, and
  ``destructiveHint`` false exactly on the write tools that only add (requirement 11.8).

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

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact
from erp_mock.tools.generator import ACTIONS, BUSINESS_CONTEXT_GET, PUSH_ACTION, ToolSpec
from erp_mock.tools.input_schema import KEYWORDS as INPUT_KEYWORDS
from erp_mock.tools.output_schema import (
    ADDITIVE_ACTIONS,
    KEYWORDS,
    READ_ONLY,
    READ_ONLY_ACTIONS,
    TRUNCATED,
    ToolAnnotationsSpec,
    annotations_of,
    object_output,
    schema_object,
)
from tests.helpers.fixtures import fixture_domain, load_fixture

# The keywords an outputSchema uses: structure only (requirement 11.7), written out here so
# that the module under test is compared with the specification rather than with itself.
STRUCTURE_KEYWORDS: Final = frozenset(
    {"type", "properties", "required", "items", "additionalProperties"}
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
    """The outputSchema of ``tool``, parsed."""
    return as_json(specs[tool].output_schema)


def properties(schema: Json) -> Json:
    """The ``properties`` of a schema object."""
    found = schema["properties"]
    assert isinstance(found, dict)
    return found


def names(schema: Json) -> list[str]:
    """The key names of a schema object, in declaration order."""
    return list(properties(schema))


def required(schema: Json) -> list[str]:
    """The required keys of a schema object; an empty list when it has none."""
    return list(schema.get("required", []))


def node_at(schema: Json, path: str) -> Json:
    """The schema at ``path``: ``""`` the top level, ``lines[]`` an element of an array."""
    found = schema
    for step in path.split(".") if path else []:
        found = properties(found)[step.removesuffix("[]")]
        if step.endswith("[]"):
            found = found["items"]
        assert isinstance(found, dict)
    return found


def nodes(schema: Json, path: str = "") -> Iterator[tuple[str, Json]]:
    """Every schema in ``schema``, the top level included, with the path it sits at."""
    yield path, schema
    for key, item in schema.get("properties", {}).items():
        if isinstance(item, dict):
            yield from nodes(item, f"{path}.{key}")
    items = schema.get("items")
    if isinstance(items, dict):
        yield from nodes(items, f"{path}[]")


def objects(schema: Json) -> Iterator[tuple[str, Json]]:
    """Every object of ``schema``: the nodes that declare properties."""
    return ((path, node) for path, node in nodes(schema) if "properties" in node)


# --- the shape of every schema (requirement 11.7) ----------------------------------------


@pytest.mark.req("11.7")
def test_the_keywords_say_structure_and_nothing_else() -> None:
    assert frozenset(KEYWORDS) == STRUCTURE_KEYWORDS
    assert len(set(KEYWORDS)) == len(KEYWORDS)
    # A result schema is written with keywords of the compatible subset of
    # tool-design.md section 4, but with none of its constraints: enum, pattern, the
    # bounds and the lengths say what a tool accepts, not what it returns.
    assert frozenset(KEYWORDS) < frozenset(INPUT_KEYWORDS)


@pytest.mark.req("11.7")
def test_every_output_schema_is_a_valid_json_schema(specs: Mapping[str, ToolSpec]) -> None:
    assert len(specs) > 50

    for name, spec in specs.items():
        schema = as_json(spec.output_schema)

        assert schema["type"] == "object", name
        Draft202012Validator.check_schema(schema)


@pytest.mark.req("11.7")
def test_every_schema_uses_only_those_keywords_and_closes_every_object(
    specs: Mapping[str, ToolSpec],
) -> None:
    for name, spec in specs.items():
        schema = as_json(spec.output_schema)
        for path, node in nodes(schema):
            where = f"{name}{path}"
            assert set(node) <= STRUCTURE_KEYWORDS, where
            if "properties" in node:
                assert node["additionalProperties"] is False, where
            if node.get("type") == "array":
                assert isinstance(node["items"], dict), where


@pytest.mark.req("11.7")
def test_every_object_requires_every_key_but_truncated(specs: Mapping[str, ToolSpec]) -> None:
    pack_tools = {name: spec for name, spec in specs.items() if spec.pack is not None}
    assert len(pack_tools) == len(specs) - 1

    for name, spec in pack_tools.items():
        for path, node in objects(as_json(spec.output_schema)):
            expected = [key for key in names(node) if key != TRUNCATED]
            assert required(node) == expected, f"{name}{path}"


@pytest.mark.req("11.7")
def test_truncated_is_the_last_key_of_a_result_and_the_only_optional_one(
    specs: Mapping[str, ToolSpec],
) -> None:
    for name, spec in specs.items():
        schema = as_json(spec.output_schema)

        assert names(schema)[-1] == TRUNCATED, name
        assert properties(schema)[TRUNCATED] == {"type": "boolean"}, name
        assert TRUNCATED not in required(schema), name
        # A truncated result says so once, at the top level (requirement 13.5).
        nested = [path for path, node in objects(schema) if path and TRUNCATED in names(node)]
        assert nested == [], name


@pytest.mark.req("11.7")
@pytest.mark.parametrize("keyword", ["enum", "pattern", "maxLength", "minItems", "$ref", "title"])
def test_schema_object_rejects_a_keyword_outside_the_five(keyword: str) -> None:
    with pytest.raises(ValueError, match=keyword.replace("$", r"\$")):
        schema_object(type="string", **{keyword: "x"})


@pytest.mark.req("11.7")
def test_schema_object_writes_the_keys_in_the_order_of_the_keyword_list() -> None:
    found = schema_object(additionalProperties=False, required=("a",), properties={}, type="object")

    assert list(found) == ["type", "properties", "required", "additionalProperties"]


# --- the keys of each result and their order (requirement 11.7) --------------------------

# One case per result shape: the tool, the path inside its schema and the keys that path
# declares, in order. The top-level case leaves out ``truncated``, which every result
# carries last and which the test appends.
RESULT_KEYS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    # The business context, in the order requirement 16.10 returns it.
    (BUSINESS_CONTEXT_GET, "", ("business_date", "current_period", "org", "user")),
    # An entity record: every field of the field view, the standard id and the generated
    # code first, refs without their _id, and the conversion records of the material
    # entity last (requirements 5.12 and 6.9).
    (
        "article_get",
        "",
        (
            "id",
            "code",
            "name",
            "spec",
            "grade",
            "base_measure",
            "buy_measure",
            "tax_rate",
            "unit_conversions",
        ),
    ),
    # Each conversion record holds the conversion unit and the factor, under the names the
    # pack gave the two fields of its conversion entity (requirement 6.9).
    ("article_get", "unit_conversions[]", ("alt_measure", "ratio")),
    # A bill: the standard header fields, the amounts of pricing, the declared fields and
    # the lines last. price_includes_tax is declared by name, so it stays where the engine
    # put it (requirement 1.5).
    (
        "supply_order_get",
        "",
        (
            "id",
            "number",
            "bill_type",
            "org",
            "biz_date",
            "status",
            "source_bill_type",
            "source_bill",
            "remark",
            "created_seq",
            "updated_seq",
            "price_includes_tax",
            "amount",
            "tax_amount",
            "amount_with_tax",
            "vendor",
            "need_date",
            "lines",
        ),
    ),
    # The lines of the same bill: the standard line fields, the link field of the push
    # whose source it is, then the declared ones.
    (
        "supply_order_get",
        "lines[]",
        (
            "id",
            "line_no",
            "source_line_id",
            "qty",
            "qty_base",
            "price",
            "tax_rate",
            "amount",
            "tax_amount",
            "amount_with_tax",
            "linked_qty",
            "article",
            "measure",
        ),
    ),
    # The source of a push by amount carries the link field on its header; its state
    # machine is simple, which changes the values of status and not the keys.
    (
        "fee_note_get",
        "",
        (
            "id",
            "number",
            "bill_type",
            "org",
            "biz_date",
            "status",
            "source_bill_type",
            "source_bill",
            "remark",
            "created_seq",
            "updated_seq",
            "price_includes_tax",
            "amount",
            "tax_amount",
            "amount_with_tax",
            "linked_amount",
            "vendor",
            "due_date",
            "lines",
        ),
    ),
    # A write and a state operation return what a get returns (requirements 5.16, 13.2).
    (
        "count_sheet_create",
        "",
        (
            "id",
            "number",
            "bill_type",
            "org",
            "biz_date",
            "status",
            "source_bill_type",
            "source_bill",
            "remark",
            "created_seq",
            "updated_seq",
            "depot",
            "count_kind",
            "lines",
        ),
    ),
    (
        "count_sheet_delete",
        "lines[]",
        ("id", "line_no", "source_line_id", "qty_base", "article", "measure", "diff_qty"),
    ),
    # A push returns the target bill it generated, lines included (requirement 13.2).
    (
        "goods_entry_create_from_order",
        "lines[]",
        ("id", "line_no", "source_line_id", "qty_base", "article", "measure", "depot", "entry_qty"),
    ),
    # A list: the records, then the page they come from (requirement 5.11).
    ("supply_order_search", "", ("items", "total", "page", "page_size", "has_more")),
    # The summary of a bill: id, number, status, biz_date, the partner reference, the
    # fields of the filters and, with pricing, amount_with_tax (requirement 5.10). status
    # and biz_date are filters here as well and are returned once.
    (
        "supply_order_search",
        "items[]",
        ("id", "number", "status", "biz_date", "vendor", "amount_with_tax"),
    ),
    # A bill without pricing returns no amount, and one without a partner no reference.
    ("count_sheet_search", "items[]", ("id", "number", "status", "biz_date")),
    # The summary of an entity: id, the code and name it declares, the status it keeps and
    # the fields its search reads - the keyword fields, then the filters.
    ("article_search", "items[]", ("id", "code", "name", "spec", "grade")),
    ("vendor_search", "items[]", ("id", "code", "name", "status", "short_name")),
    ("measure_search", "items[]", ("id", "code", "name")),
    # A declarative query over the lines of a bill: the fields of returns.latest, which
    # name both levels, then those of returns.history (requirement 5.13).
    ("supply_price_query", "", ("latest", "history")),
    ("supply_price_query", "latest", ("price", "price_includes_tax", "biz_date", "number")),
    ("supply_price_query", "history[]", ("price", "biz_date", "number")),
    # The stock balance query: a list of the material, the warehouse, the base unit of the
    # material and the quantity on hand in that unit (requirements 9.7 and 9.12).
    ("stock_level_query", "", ("items", "total", "page", "page_size", "has_more")),
    ("stock_level_query", "items[]", ("material", "warehouse", "base_measure", "qty_base")),
)


@pytest.mark.req("11.7")
@pytest.mark.parametrize(
    ("tool", "path", "expected"),
    [pytest.param(*case, id=f"{case[0]}{case[1] and '.' + case[1]}") for case in RESULT_KEYS],
)
def test_the_keys_of_a_result_and_their_order(
    specs: Mapping[str, ToolSpec], tool: str, path: str, expected: tuple[str, ...]
) -> None:
    node = node_at(schema_of(specs, tool), path)
    # Only the top level carries truncated, as the last key.
    keys = (*expected, TRUNCATED) if path == "" else expected

    assert names(node) == list(keys)


@pytest.mark.req("11.7")
def test_a_ref_field_is_a_nested_object(specs: Mapping[str, ToolSpec]) -> None:
    header = properties(schema_of(specs, "supply_order_get"))

    # Basic data: id, code and name of the referenced record; the code of an entity that
    # declares a code rule is generated, so it is never null.
    assert header["vendor"] == {
        "type": ["object", "null"],
        "properties": {
            "id": {"type": "string"},
            "code": {"type": "string"},
            "name": {"type": ["string", "null"]},
        },
        "required": ["id", "code", "name"],
        "additionalProperties": False,
    }
    # A bill: id and number. The target of source_bill_id is the bill its own
    # source_bill_type names, so the key is the same for every bill (requirement 13.3).
    assert header["source_bill"] == {
        "type": ["object", "null"],
        "properties": {"id": {"type": "string"}, "number": {"type": "string"}},
        "required": ["id", "number"],
        "additionalProperties": False,
    }


@pytest.mark.req("11.7")
def test_an_entity_that_declares_no_code_or_name_contributes_no_such_key() -> None:
    domain = fixture_domain()
    article = domain.entities["article"]
    measure = domain.entities["measure"]
    # The same unit entity without its two declared fields: a reference to it then returns
    # the id alone (requirement 13.3).
    bare = replace(measure, fields=FrozenMap({"id": measure.fields["id"]}))
    stripped = replace(domain, entities=FrozenMap({**domain.entities, "measure": bare}))

    found = properties(as_json(object_output(stripped, article, "get").schema))

    assert found["base_measure"] == {
        "type": ["object", "null"],
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    }
    # The keys of the material itself are the same; only the referenced object got smaller.
    assert names(as_json(object_output(domain, article, "get").schema)) == list(found)


@pytest.mark.req("11.7")
def test_a_decimal_value_is_a_string_and_an_int_an_integer(
    specs: Mapping[str, ToolSpec],
) -> None:
    line = node_at(schema_of(specs, "supply_order_get"), "lines[]")
    header = properties(schema_of(specs, "supply_order_get"))
    vendor = node_at(schema_of(specs, "vendor_detail_get"), "")

    # qty, price, money and rate values are decimal strings, the engine's own qty_base and
    # linked_qty among them (requirement 13.4).
    for key in ("qty", "qty_base", "price", "tax_rate", "amount", "linked_qty"):
        assert properties(line)[key] == {"type": ["string", "null"]}, key
    assert header["amount_with_tax"] == {"type": ["string", "null"]}
    # int, bool and date keep their JSON type; the operation sequence numbers are assigned
    # by the engine, so they are never null.
    assert properties(line)["line_no"] == {"type": "integer"}
    assert header["created_seq"] == {"type": "integer"}
    assert header["price_includes_tax"] == {"type": ["boolean", "null"]}
    assert header["biz_date"] == {"type": ["string", "null"]}
    assert properties(vendor)["pay_days"] == {"type": ["integer", "null"]}


@pytest.mark.req("11.7")
def test_the_builtin_result_is_the_business_context_and_is_the_same_for_every_instance(
    specs: Mapping[str, ToolSpec],
) -> None:
    # The framework writes this schema, so it cannot read the field view of the two
    # context entities: a context entity that declares no code or name returns no such key
    # (requirement 13.3), which is why the two nested objects require the id alone.
    reference = {
        "type": ["object", "null"],
        "properties": {
            "id": {"type": "string"},
            "code": {"type": ["string", "null"]},
            "name": {"type": ["string", "null"]},
        },
        "required": ["id"],
        "additionalProperties": False,
    }

    assert schema_of(specs, BUSINESS_CONTEXT_GET) == {
        "type": "object",
        "properties": {
            "business_date": {"type": "string"},
            "current_period": {"type": "string"},
            "org": reference,
            "user": reference,
            "truncated": {"type": "boolean"},
        },
        "required": ["business_date", "current_period", "org", "user"],
        "additionalProperties": False,
    }


# --- the annotations (requirement 11.8) --------------------------------------------------


def hints(spec: ToolSpec) -> Json:
    """The annotations of ``spec`` as tools/list carries them."""
    return as_json(spec.annotations.as_json())


@pytest.mark.req("11.8")
def test_the_world_of_every_tool_is_closed(specs: Mapping[str, ToolSpec]) -> None:
    for name, spec in specs.items():
        assert spec.annotations.open_world_hint is False, name
        assert hints(spec)["openWorldHint"] is False, name


@pytest.mark.req("11.8")
def test_read_only_is_true_exactly_on_the_searches_gets_queries_and_the_builtin(
    specs: Mapping[str, ToolSpec],
) -> None:
    assert READ_ONLY_ACTIONS == frozenset({"search", "get", "query"})

    for name, spec in specs.items():
        expected = spec.kind == "builtin" or spec.action in READ_ONLY_ACTIONS
        assert spec.annotations.read_only_hint is expected, name
        # A tool that changes nothing says nothing about how it changes it: the hint is
        # meaningful only where readOnlyHint is false.
        assert (spec.annotations.destructive_hint is None) is expected, name
        assert ("destructiveHint" in hints(spec)) is not expected, name


@pytest.mark.req("11.8")
def test_destructive_is_false_only_on_the_write_tools_that_add(
    specs: Mapping[str, ToolSpec],
) -> None:
    assert ADDITIVE_ACTIONS == frozenset({"create", PUSH_ACTION, "submit", "approve"})
    writes = {name: spec for name, spec in specs.items() if not spec.annotations.read_only_hint}
    assert len(writes) > 20

    for name, spec in writes.items():
        expected = spec.action not in ADDITIVE_ACTIONS
        assert spec.annotations.destructive_hint is expected, name
        assert hints(spec)["destructiveHint"] is expected, name


@pytest.mark.req("11.8")
@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("stock_move_draft_create", False),
        ("goods_entry_create_from_order", False),
        ("supply_order_submit", False),
        ("supply_order_approve", False),
        ("supply_order_update", True),
        ("supply_order_delete", True),
        ("supply_order_unsubmit", True),
        ("supply_order_unapprove", True),
        ("supply_order_close", True),
        ("supply_order_unclose", True),
    ],
)
def test_the_destructive_hint_of_each_write_action(
    specs: Mapping[str, ToolSpec], tool: str, expected: bool
) -> None:
    assert specs[tool].annotations == ToolAnnotationsSpec(destructive_hint=expected)


@pytest.mark.req("11.8")
def test_the_annotations_carry_the_mcp_names_in_a_fixed_order() -> None:
    assert dict(READ_ONLY.as_json()) == {"readOnlyHint": True, "openWorldHint": False}
    assert list(annotations_of("create").as_json()) == [
        "readOnlyHint",
        "destructiveHint",
        "openWorldHint",
    ]


@pytest.mark.req("11.8")
def test_an_action_outside_the_additive_ones_changes_something() -> None:
    # Every action of the word list is either read-only or a write tool, and only the four
    # additive ones are not destructive.
    for action in ACTIONS:
        found = annotations_of(action)
        if action in READ_ONLY_ACTIONS:
            assert found == READ_ONLY, action
        else:
            assert found.destructive_hint is (action not in ADDITIVE_ACTIONS), action
    # A query tool whose name ends with no action of the list is still a read of the
    # sandbox; step 6 reports the name (requirement 11.2).
    assert annotations_of("").destructive_hint is True
