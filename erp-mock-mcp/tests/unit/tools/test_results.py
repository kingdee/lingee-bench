"""erp_mock.tools.results: the structuredContent, the text and the error object (req 13).

The results are assembled from views the engine really produced: the tests run write and
read operations against a sandbox over the engine test fixture
(:mod:`tests.helpers.engine_env`) and hand the view to the result assembly together with
the tool step 6 generated for that operation, so what is checked here is the pair of
:mod:`erp_mock.domain.views` and :mod:`erp_mock.tools.output_schema` meeting
(requirement 13.1). Three parts:

- the shape of a successful result: it validates against the outputSchema of its tool,
  every object carries the properties of its schema in declaration order, the text is the
  compact JSON of exactly that object, references are nested objects while
  ``source_line_id`` stays a string, decimals are canonical strings, dates, ints, bools and
  enum codes are written as requirement 13.4 asks, and an empty value is null with its key
  kept (requirements 13.1 to 13.4);
- truncation: a result at the limit is returned as it is, one over it keeps the longest
  prefix of its detail array and says ``truncated``, the prefix is the one removing
  elements from the end arrives at, a bill of 200 lines comes back inside the limit and
  still validates, and a result with nothing to remove keeps every field
  (requirement 13.5);
- error results: the one ``error`` key with its four keys always present, no
  structuredContent, no truncation, and the preset message of INTERNAL_ERROR
  (requirements 13.6 to 13.10).
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from jsonschema import Draft202012Validator

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact
from erp_mock.domain.engine import BusinessContextOp, GetOp, QueryOp, SearchOp, View
from erp_mock.domain.views import RecordView, RefSummary
from erp_mock.state.store import Value
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET, ToolSpec
from erp_mock.tools.results import (
    CODE,
    DETAILS,
    ERROR_KEY,
    INTERNAL_MESSAGE,
    MESSAGE,
    RULE_ID,
    TEXT_LIMIT,
    error_payload,
    internal_error,
    result_kinds,
    result_object,
    result_payload,
    truncate_result,
)
from tests.helpers.engine_env import (
    BOLT,
    BOX,
    BOX_FACTOR,
    MAIN_DEPOT,
    MAIN_VENDOR,
    WASHER,
    EngineEnv,
    engine_env,
)
from tests.helpers.fixtures import load_fixture

# The key the engine adds to a truncated result (requirement 13.5), written out here so
# that the module under test is compared with the specification rather than with itself.
TRUNCATED: Final = "truncated"

# The detail array of a list result and the keys next to it (requirement 5.11).
ITEMS: Final = "items"
TOTAL: Final = "total"

# The objects and the tools the tests work on (tests/fixtures/packs/fx_trade). The count
# sheet adjusts the stock of a warehouse on approve, which gives the stock balance query
# something to return (requirement 9.2).
ORDER: Final = "supply_order"
ENTRY: Final = "goods_entry"
COUNT: Final = "count_sheet"
ORDER_TO_ENTRY: Final = "supply_order_to_entry"
PRICE_QUERY: Final = "supply_price_query"
BALANCE_QUERY: Final = "stock_level_query"

# The day after the anchor date, the required date of a supply order.
NEXT_DAY: Final = date(2026, 10, 10)


@pytest.fixture(scope="module")
def specs() -> Mapping[str, ToolSpec]:
    """Every tool the fixture exposes, by name, as step 6 generated it."""
    tools = load_fixture(6).tools
    assert tools is not None
    return {spec.name: spec for spec in tools.tools}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


# --- reading a result --------------------------------------------------------------------


def schema_of(spec: ToolSpec) -> Mapping[str, JsonValue]:
    """The outputSchema of ``spec`` as a client reads it: its canonical JSON, parsed."""
    parsed = json.loads(dumps_compact(spec.output_schema))
    assert isinstance(parsed, dict)
    return parsed


def result_of(
    env: EngineEnv, specs: Mapping[str, ToolSpec], tool: str, view: View
) -> Mapping[str, JsonValue]:
    """The structuredContent of ``tool`` for ``view``, validated against its schema.

    Every successful result passes the JSON Schema of its tool and carries the keys that
    schema declares, in declaration order, at every level (requirement 13.1); the text is
    the compact JSON of exactly that object and parses back to it.
    """
    spec = specs[tool]
    payload = result_payload(env.domain, spec, view)
    assert payload.structured is not None
    assert payload.error is None
    assert not payload.is_error
    schema = schema_of(spec)
    Draft202012Validator(schema).validate(payload.structured)
    check_key_order(payload.structured, schema, tool)
    assert payload.text == dumps_compact(payload.structured)
    assert json.loads(payload.text) == payload.structured
    return payload.structured


def check_key_order(
    value: Mapping[str, JsonValue], schema: Mapping[str, JsonValue], where: str
) -> None:
    """Every object inside ``value`` carries its declared keys in declaration order (13.1)."""
    properties = schema.get("properties")
    assert isinstance(properties, dict), where
    assert list(value) == [name for name in properties if name in value], where
    for name, found in value.items():
        declared = properties[name]
        assert isinstance(declared, dict)
        if isinstance(found, dict):
            check_key_order(found, declared, f"{where}.{name}")
        if isinstance(found, list):
            element = declared.get("items")
            assert isinstance(element, dict), f"{where}.{name}"
            for index, item in enumerate(found):
                assert isinstance(item, dict), f"{where}.{name}[{index}]"
                check_key_order(item, element, f"{where}.{name}[{index}]")


def order_line(env: EngineEnv, article: str = WASHER, qty: str = "10") -> dict[str, Value]:
    """One line of a supply order: ``qty`` of ``article`` at a price (requirement 6.1)."""
    return {"article_id": env.id(article), "qty": Decimal(qty), "price": Decimal("0.85")}


def new_order(env: EngineEnv, lines: Sequence[Mapping[str, Value]] | None = None) -> View:
    """Create a supply order and return the view of the create (requirement 5.16)."""
    header: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "need_date": NEXT_DAY}
    rows = [order_line(env)] if lines is None else [dict(line) for line in lines]
    return env.create(ORDER, header, rows).view


def record_id(view: View) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    assert isinstance(view, RecordView)
    return str(view.fields["id"])


def approved_order(env: EngineEnv, lines: Sequence[Mapping[str, Value]] | None = None) -> str:
    """Create, submit and approve a supply order and return its id (requirement 7.2)."""
    order = record_id(new_order(env, lines))
    env.transition(ORDER, order, "submit")
    env.transition(ORDER, order, "approve")
    return order


# --- the shape of a successful result (requirements 13.1 to 13.4) ------------------------


@pytest.mark.req("13.1", "13.2")
def test_a_bill_result_is_the_whole_bill_and_validates_against_its_schema(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    created = result_of(env, specs, f"{ORDER}_create", new_order(env))

    # The header fields in field view order, the lines last (requirement 5.12).
    assert list(created)[:3] == ["id", "number", "bill_type"]
    assert list(created)[-1] == "lines"
    assert created["id"] == "so_000001"
    lines = created["lines"]
    assert isinstance(lines, list)
    assert len(lines) == 1
    # A state operation returns the same object, with the state the call left behind
    # (requirement 13.2), and so does a get right after it.
    env.transition(ORDER, "so_000001", "submit")
    submitted = result_of(env, specs, f"{ORDER}_submit", env.run(GetOp(ORDER, "so_000001")).view)
    got = result_of(env, specs, f"{ORDER}_get", env.run(GetOp(ORDER, "so_000001")).view)
    assert submitted == got
    assert submitted["status"] == "submitted"


@pytest.mark.req("13.3")
def test_a_reference_is_a_nested_object_and_source_line_id_stays_a_string(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    order = approved_order(env)
    pushed = env.push(
        ORDER_TO_ENTRY,
        order,
        [
            {
                "source_line_id": f"{order}-1",
                "depot_id": env.id(MAIN_DEPOT),
                "entry_qty": Decimal("4"),
            }
        ],
        source_name="order",
    )

    entry = result_of(env, specs, f"{ENTRY}_create_from_order", pushed.view)

    # Basic data: id, code and name; a bill: id and number (requirement 13.3).
    assert entry["vendor"] == {"id": "ven_000001", "code": "V0001", "name": "夹具主力供货单位"}
    assert entry["source_bill"] == {"id": order, "number": "SPO202610090001"}
    assert entry["source_bill_type"] == ORDER
    # No key of the result ends in _id, and the line's source line is the id itself.
    assert [name for name in entry if name.endswith("_id")] == []
    lines = entry["lines"]
    assert isinstance(lines, list)
    assert isinstance(lines[0], dict)
    assert lines[0]["source_line_id"] == f"{order}-1"
    assert lines[0]["depot"] == {"id": "dep_000001", "code": "D001", "name": "夹具主料仓"}


@pytest.mark.req("13.4")
def test_decimal_values_are_canonical_strings_of_their_field_type(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    created = result_of(env, specs, f"{ORDER}_create", new_order(env))
    lines = created["lines"]
    assert isinstance(lines, list)
    line = lines[0]
    assert isinstance(line, dict)

    # money keeps exactly 2 decimals; qty, price and rate keep no trailing zero
    # (requirement 4.8). 10 pieces at 0.85 net, taxed at the washer's 9%.
    assert created["amount"] == "8.50"
    assert created["tax_amount"] == "0.77"
    assert created["amount_with_tax"] == "9.27"
    assert line["qty"] == "10"
    assert line["qty_base"] == "10"
    assert line["price"] == "0.85"
    assert line["tax_rate"] == "0.09"
    assert line["amount"] == "8.50"
    # Not a JSON number: every decimal is a string that reads back as the same value.
    assert Decimal(str(created["amount_with_tax"])) == Decimal("9.27")


@pytest.mark.req("13.4")
def test_a_date_an_int_a_bool_an_enum_and_an_empty_value(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    created = result_of(env, specs, f"{ORDER}_create", new_order(env))

    assert created["biz_date"] == "2026-10-09"
    assert created["need_date"] == NEXT_DAY.isoformat()
    assert created["created_seq"] == 15
    assert created["price_includes_tax"] is False
    assert created["status"] == "draft"
    # An empty value is null and its key stays (requirement 13.4).
    assert created["remark"] is None
    assert created["source_bill"] is None
    lines = created["lines"]
    assert isinstance(lines, list)
    assert isinstance(lines[0], dict)
    assert lines[0]["linked_qty"] is None


@pytest.mark.req("13.1", "13.4")
def test_a_list_a_query_and_the_stock_balance_result(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    approved_order(env)

    page = result_of(env, specs, f"{ORDER}_search", env.run(SearchOp(ORDER)).view)
    assert list(page) == [ITEMS, TOTAL, "page", "page_size", "has_more"]
    assert page[TOTAL] == 1
    assert page["page"] == 1
    assert page["has_more"] is False

    args = FrozenMap({"vendor_id": env.id(MAIN_VENDOR), "article_id": env.id(WASHER)})
    query = result_of(env, specs, PRICE_QUERY, env.run(QueryOp(PRICE_QUERY, args)).view)
    assert query["latest"] == {
        "price": "0.85",
        "price_includes_tax": False,
        "biz_date": "2026-10-09",
        "number": "SPO202610090001",
    }
    history = query["history"]
    assert isinstance(history, list)
    assert len(history) == 1
    # Nothing matched: latest is null and history an empty array (requirement 5.13).
    other = FrozenMap({"vendor_id": env.id(MAIN_VENDOR), "article_id": env.id(BOLT)})
    empty = result_of(env, specs, PRICE_QUERY, env.run(QueryOp(PRICE_QUERY, other)).view)
    assert empty == {"latest": None, "history": []}

    # The quantity on hand is a qty in the base unit of the material (requirement 9.7).
    count = record_id(
        env.create(
            COUNT,
            {"depot_id": env.id(MAIN_DEPOT), "count_kind": "gain"},
            [{"article_id": env.id(BOLT), "measure_id": env.id(BOX), "diff_qty": Decimal("2")}],
        ).view
    )
    env.transition(COUNT, count, "submit")
    env.transition(COUNT, count, "approve")
    balance = result_of(env, specs, BALANCE_QUERY, env.run(QueryOp(BALANCE_QUERY)).view)
    items = balance[ITEMS]
    assert isinstance(items, list)
    assert isinstance(items[0], dict)
    assert items[0]["qty_base"] == str(2 * BOX_FACTOR)
    assert items[0]["base_measure"] == {"id": "msr_000001", "code": "PCS", "name": "夹具基本件"}


@pytest.mark.req("13.1", "13.4")
def test_an_entity_record_under_a_tool_the_pack_renamed(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    # The schema and the kinds are those of what the tool works on, which the rename of
    # vendor_get to vendor_detail_get does not change (requirement 11.1).
    vendor = result_of(env, specs, "vendor_detail_get", env.run(GetOp("vendor", "ven_000001")).view)

    assert list(vendor) == [
        "id",
        "code",
        "status",
        "name",
        "short_name",
        "contact",
        "pay_days",
        "default_tax_rate",
    ]
    assert vendor["status"] == "enabled"
    assert vendor["pay_days"] == 45
    assert vendor["default_tax_rate"] == "0.13"


@pytest.mark.req("13.4")
def test_a_tool_whose_target_the_instance_does_not_have_formats_no_decimal(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    # Step 5 reports a target it cannot resolve (requirement 2.6); the assembly of such a
    # tool names no decimal kind rather than failing.
    for tool in (f"{ORDER}_get", f"{ENTRY}_create_from_order", PRICE_QUERY, BALANCE_QUERY):
        kinds = result_kinds(env.domain, replace(specs[tool], target="no_such_target"))
        assert kinds.kinds == {}, tool

    # A declarative query whose source step 5 could not resolve returns no field at all.
    query = env.domain.queries[PRICE_QUERY]
    without = FrozenMap({**env.domain.queries, PRICE_QUERY: replace(query, source=None)})
    found = result_kinds(replace(env.domain, queries=without), specs[PRICE_QUERY])
    assert found.children["latest"].kinds == {}


@pytest.mark.req("13.1", "13.3")
def test_the_business_context_and_the_conversion_records_of_a_material(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    context = result_of(env, specs, BUSINESS_CONTEXT_GET, env.run(BusinessContextOp()).view)
    assert list(context) == ["business_date", "current_period", "org", "user"]
    assert context["business_date"] == "2026-10-09"
    assert context["current_period"] == "2026-10"
    assert context["org"] == {"id": "br_000001", "code": "BR01", "name": "夹具核算本部"}

    # The conversion records of a material: the unit as a nested object, the factor as a
    # decimal string (requirement 6.9); a material without one returns an empty array.
    bolt = result_of(env, specs, "article_get", env.run(GetOp("article", env.id(BOLT))).view)
    records = bolt["unit_conversions"]
    assert records == [
        {
            "alt_measure": {"id": "msr_000002", "code": "BOX", "name": "夹具标准箱"},
            "ratio": str(BOX_FACTOR),
        }
    ]
    washer = result_of(env, specs, "article_get", env.run(GetOp("article", env.id(WASHER))).view)
    assert washer["unit_conversions"] == []


# --- the assembly follows the schema (requirement 13.1) ----------------------------------

# A result object of two keys, one of which may be null, and the optional truncated of
# every result: the shape of requirement 11.7, written by hand so the guard below does not
# depend on any tool of the fixture.
SMALL_SCHEMA: Final[Mapping[str, JsonValue]] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": ["string", "null"]},
        TRUNCATED: {"type": "boolean"},
    },
    "required": ["id", "name"],
    "additionalProperties": False,
}


# A result object with one reference: an entity may declare the ``name`` its summary
# carries as a decimal field, and the summary then returns the text of that type.
NESTED_SCHEMA: Final[Mapping[str, JsonValue]] = {
    "type": "object",
    "properties": {
        "vendor": {
            "type": ["object", "null"],
            "properties": {"id": {"type": "string"}, "name": {"type": ["string", "null"]}},
            "required": ["id", "name"],
            "additionalProperties": False,
        },
        TRUNCATED: {"type": "boolean"},
    },
    "required": ["vendor"],
    "additionalProperties": False,
}


@pytest.mark.req("13.3", "13.4")
def test_a_decimal_inside_a_reference_takes_the_kind_of_its_field(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    # The keys of a summary are fields of the referenced entity, so what format a value
    # takes follows that pack's declaration (requirements 13.3 and 13.4).
    domain = env.domain
    vendor = domain.entities["vendor"]
    fields = FrozenMap({**vendor.fields, "name": replace(vendor.fields["name"], type="money")})
    entities = FrozenMap({**domain.entities, "vendor": replace(vendor, fields=fields)})
    kinds = result_kinds(replace(domain, entities=entities), specs[f"{ORDER}_get"])

    assert kinds.children["vendor"].kinds == {"name": "money"}

    summary = RefSummary(FrozenMap({"id": "ven_000001", "name": Decimal("17")}))
    view = RecordView(FrozenMap({"vendor": summary}))
    assert result_object(NESTED_SCHEMA, view, kinds) == {
        "vendor": {"id": "ven_000001", "name": "17.00"}
    }
    # A key the field view names as no decimal keeps the canonical text of the value, which
    # is the text storage holds it in.
    assert result_object(NESTED_SCHEMA, view) == {"vendor": {"id": "ven_000001", "name": "17"}}


@pytest.mark.req("13.1")
def test_a_result_takes_the_keys_its_schema_declares_and_nothing_else() -> None:
    view = RecordView(FrozenMap({"id": "x", "name": None}))

    assert result_object(SMALL_SCHEMA, view) == {"id": "x", "name": None}

    # A key the schema does not declare and a required key the view leaves out are a
    # mismatch between the view and the schema, not an error of the call.
    with pytest.raises(RuntimeError, match="extra"):
        result_object(SMALL_SCHEMA, RecordView(FrozenMap({"id": "x", "name": None, "extra": 1})))
    with pytest.raises(RuntimeError, match="name"):
        result_object(SMALL_SCHEMA, RecordView(FrozenMap({"id": "x"})))
    # A declared key the schema does not require is left out where the view has none: the
    # code and the name of a context entity that declares neither (requirement 13.3).
    optional: Mapping[str, JsonValue] = {**SMALL_SCHEMA, "required": ["id"]}
    assert result_object(optional, RecordView(FrozenMap({"id": "x"}))) == {"id": "x"}


# --- truncation (requirement 13.5) -------------------------------------------------------


def padded(items: int, size: int, element: int = 40) -> dict[str, JsonValue]:
    """A list result of ``items`` elements whose text is exactly ``size`` bytes.

    Every element is the same length, so the size of the text grows by a fixed amount per
    element kept; the padding that makes up the difference sits in a field of its own, so
    truncation has to find the prefix rather than the padding.
    """
    found: dict[str, JsonValue] = {
        ITEMS: [{"id": f"r{index:06d}", "pad": "p" * element} for index in range(items)],
        TOTAL: items,
        "pad": "",
    }
    base = len(dumps_compact(found).encode())
    assert size >= base, f"{size} bytes is less than the {base} bytes of the fields themselves"
    found["pad"] = "p" * (size - base)
    return found


def with_prefix(structured: Mapping[str, JsonValue], count: int) -> dict[str, JsonValue]:
    """``structured`` truncated to the first ``count`` elements, as req 13.5 asks."""
    items = structured[ITEMS]
    assert isinstance(items, list)
    kept: dict[str, JsonValue] = {
        name: items[:count] if name == ITEMS else value for name, value in structured.items()
    }
    kept[TRUNCATED] = True
    return kept


def size_of(structured: Mapping[str, JsonValue]) -> int:
    """The size of the text of ``structured`` in UTF-8 bytes (requirement 13.5)."""
    return len(dumps_compact(structured).encode())


@pytest.mark.req("13.5")
def test_a_result_at_the_limit_is_returned_as_it_is() -> None:
    for size in (TEXT_LIMIT - 1, TEXT_LIMIT):
        built = padded(20, size)

        structured, text = truncate_result(built)

        assert structured == built
        assert TRUNCATED not in structured
        assert text == dumps_compact(built)
        assert len(text.encode()) == size


@pytest.mark.req("13.5")
def test_a_result_over_the_limit_keeps_the_longest_prefix_and_says_truncated() -> None:
    built = padded(100, TEXT_LIMIT + 1)
    items = built[ITEMS]
    assert isinstance(items, list)

    structured, text = truncate_result(built)

    kept = structured[ITEMS]
    assert isinstance(kept, list)
    # The elements kept are the first few of the original array, in their order, and one
    # more of them would not fit; every other field keeps the value it had.
    assert 0 < len(kept) < len(items)
    assert kept == items[: len(kept)]
    assert len(text.encode()) <= TEXT_LIMIT
    assert size_of(with_prefix(built, len(kept) + 1)) > TEXT_LIMIT
    assert structured[TOTAL] == 100
    assert structured["pad"] == built["pad"]
    # truncated is true and comes last, the place the outputSchema declares it in.
    assert structured[TRUNCATED] is True
    assert list(structured)[-1] == TRUNCATED
    assert text == dumps_compact(structured)


@pytest.mark.req("13.5")
def test_the_prefix_is_the_one_removing_elements_from_the_end_arrives_at() -> None:
    for size in (TEXT_LIMIT + 1, TEXT_LIMIT * 2, TEXT_LIMIT * 8):
        built = padded(200, size)
        items = built[ITEMS]
        assert isinstance(items, list)
        # Remove one element at a time from the end, as requirement 13.5 words it.
        count = len(items)
        while count > 0 and size_of(with_prefix(built, count)) > TEXT_LIMIT:
            count -= 1

        structured, _ = truncate_result(built)

        kept = structured[ITEMS]
        assert isinstance(kept, list)
        assert len(kept) == count


@pytest.mark.req("13.5")
def test_a_result_with_nothing_to_remove_is_left_alone() -> None:
    # An empty detail array: there is nothing to remove, so the result is not truncated.
    empty = padded(0, TEXT_LIMIT + 1)
    structured, text = truncate_result(empty)
    assert structured == empty
    assert TRUNCATED not in structured
    assert len(text.encode()) > TEXT_LIMIT

    # No detail array at all: a single record whose own fields are too long.
    record: Mapping[str, JsonValue] = {"id": "x", "remark": "p" * TEXT_LIMIT}
    structured, text = truncate_result(record)
    assert structured == record
    assert TRUNCATED not in structured

    # Fields that alone exceed the limit: every element goes and the result says so.
    heavy = padded(5, TEXT_LIMIT * 2, element=4)
    structured, _ = truncate_result(heavy)
    assert structured[ITEMS] == []
    assert structured[TRUNCATED] is True


@pytest.mark.req("13.1", "13.5")
def test_a_bill_of_two_hundred_lines_is_truncated_and_still_validates(
    env: EngineEnv, specs: Mapping[str, ToolSpec]
) -> None:
    spec = specs[f"{ORDER}_create"]
    view = new_order(env, [order_line(env) for _ in range(200)])

    payload = result_payload(env.domain, spec, view)

    structured = payload.structured
    assert structured is not None
    lines = structured["lines"]
    assert isinstance(lines, list)
    assert 0 < len(lines) < 200
    assert len(payload.text.encode()) <= TEXT_LIMIT
    assert structured[TRUNCATED] is True
    # The header amounts are those of all 200 lines, not of the lines that were kept
    # (requirement 13.5), and the truncated result still passes the outputSchema.
    assert structured["amount_with_tax"] == "1854.00"
    Draft202012Validator(schema_of(spec)).validate(structured)
    assert payload.text == dumps_compact(structured)


# --- error results (requirements 13.6 to 13.10) ------------------------------------------


@pytest.mark.req("13.6", "13.8")
def test_an_error_result_carries_the_one_error_key_with_its_four_keys() -> None:
    error = ErpError(
        ErrorCode.BUSINESS_RULE_VIOLATION,
        "入库数量超过可入库数量：lines[0].entry_qty",
        "R-GEN-005",
        [FrozenMap({"field": "lines[0].entry_qty", "reason": "exceeds_pushable_qty"})],
    )

    payload = error_payload(error)

    assert payload.is_error
    # No structuredContent at all, and the text is an object of the one error key.
    assert payload.structured is None
    assert payload.error is not None
    assert list(payload.error) == [CODE, MESSAGE, RULE_ID, DETAILS]
    assert payload.error[CODE] == "BUSINESS_RULE_VIOLATION"
    assert payload.error[RULE_ID] == "R-GEN-005"
    assert payload.error[DETAILS] == [
        {"field": "lines[0].entry_qty", "reason": "exceeds_pushable_qty"}
    ]
    assert payload.text == dumps_compact({ERROR_KEY: payload.error})
    assert json.loads(payload.text) == {ERROR_KEY: dict(payload.error)}


@pytest.mark.req("13.6", "13.8", "13.10")
def test_an_error_no_rule_decided_and_the_preset_internal_message() -> None:
    plain = error_payload(ErpError(ErrorCode.NOT_FOUND, "找不到 id 为 so_000009 的记录"))

    # The four keys are always there: no rule is null, no detail item an empty array.
    assert plain.error == {
        CODE: "NOT_FOUND",
        MESSAGE: "找不到 id 为 so_000009 的记录",
        RULE_ID: None,
        DETAILS: [],
    }

    internal = error_payload(internal_error())

    assert internal.error is not None
    assert internal.error[CODE] == "INTERNAL_ERROR"
    assert internal.error[MESSAGE] == INTERNAL_MESSAGE
    assert internal.error[RULE_ID] is None
    assert internal.error[DETAILS] == []
    # Nothing of the exception reaches the client (requirement 13.10).
    assert INTERNAL_MESSAGE == "服务内部错误，本次调用未产生任何写入"


@pytest.mark.req("13.5", "13.6")
def test_an_error_result_is_not_truncated() -> None:
    details = [
        FrozenMap({"field": f"lines[{index}].qty", "reason": "type"}) for index in range(1000)
    ]
    error = ErpError(ErrorCode.VALIDATION_FAILED, "数量不是数值：lines[0].qty", None, details)

    payload = error_payload(error)

    assert payload.error is not None
    assert len(payload.text.encode()) > TEXT_LIMIT
    assert payload.error[DETAILS] == [dict(item) for item in details]
    assert TRUNCATED not in payload.text
