"""erp_mock.tools.validate: the arguments of one call, checked and normalized (req 12.2 to 12.6).

The tools of the engine test fixture, loaded through step 6, are called with the arguments a
client could send: the upper bounds that outrank every other error (requirement 12.3), the
decimal strings a number parameter accepts and the ones it does not (requirements 12.5 and
12.13), the rounding to field precision (requirement 12.6), the order of the details
(requirement 12.12), and the page parameters a search rejects below 1 (requirement 5.15).

The fixture has no write tool for an int, a bool, a money or a period parameter, so one
section builds a bill of its own by adding such fields to the header of ``count_sheet`` and
generating the tool from it, the way tests/unit/tools/test_input_schema.py reads the type
table off :func:`~erp_mock.tools.input_schema.field_schema`.

Every assertion reads the outcome the way the pipeline will (design.md 5.3): ``values``,
``coerced`` and ``error`` of the returned :class:`~erp_mock.tools.validate.ValidatedArgs`,
with the details of an error as the ``(field, reason)`` pairs of
:func:`reasons`.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, Final

import pytest

from erp_mock.common.errors import ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.fields import FieldType
from erp_mock.config.model import BillModel, DomainModel, FieldModel
from erp_mock.domain.crud import line_path
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import MAX_LINES, MAX_PAGE_SIZE, bill_input
from erp_mock.tools.validate import (
    ENUM_REASON,
    FORMAT_REASON,
    MAX_ITEMS_REASON,
    MAX_LENGTH_REASON,
    MAXIMUM_REASON,
    MIN_ITEMS_REASON,
    MINIMUM_REASON,
    PATTERN_REASON,
    POSITIVE_REASON,
    REASONS,
    REQUIRED_REASON,
    TYPE_REASON,
    UNKNOWN_FIELD_REASON,
    ValidatedArgs,
    quantity_field,
    validate_args,
)
from tests.helpers.fixtures import fixture_domain, load_fixture

# A valid call of each tool the sections below vary one parameter of. The arguments are only
# checked against the schema here, so the ids need not exist in any sandbox.
ORDER: Final[Mapping[str, Any]] = {
    "vendor_id": "ven_000001",
    "need_date": "2026-10-20",
    "lines": [{"article_id": "art_000001", "qty": 20, "price": "0.85"}],
}
COUNT: Final[Mapping[str, Any]] = {
    "depot_id": "dep_000001",
    "count_kind": "gain",
    "lines": [{"article_id": "art_000001", "diff_qty": 3}],
}


@pytest.fixture(scope="module")
def specs() -> Mapping[str, ToolSpec]:
    """Every tool the fixture exposes, by name, as step 6 generated it."""
    tools = load_fixture(6).tools
    assert tools is not None
    return {spec.name: spec for spec in tools.tools}


@pytest.fixture(scope="module")
def domain() -> DomainModel:
    """The resolved configuration of the fixture, what the validator reads fields from."""
    return fixture_domain()


def check(
    domain: DomainModel,
    specs: Mapping[str, ToolSpec],
    tool: str,
    raw: Mapping[str, Any] | None,
) -> ValidatedArgs:
    """Validate ``raw`` against the tool ``tool`` of the fixture."""
    return validate_args(domain, specs[tool], raw)


def details(args: ValidatedArgs) -> list[dict[str, Any]]:
    """The detail items of a rejected call, in report order (requirement 12.12)."""
    error = args.error
    assert error is not None, "expected the arguments to be rejected"
    return [dict(item) for item in error.details]


def reasons(args: ValidatedArgs) -> list[tuple[str, str]]:
    """The ``(field, reason)`` of each detail item, in report order (requirement 12.12)."""
    return [(str(item["field"]), str(item["reason"])) for item in details(args)]


def code(args: ValidatedArgs) -> ErrorCode:
    """The error code of a rejected call."""
    error = args.error
    assert error is not None, "expected the arguments to be rejected"
    return error.code


def accepted(args: ValidatedArgs) -> FrozenMap[str, Any]:
    """The normalized arguments of a call that went through."""
    assert args.error is None, f"unexpected error: {args.error}"
    return FrozenMap(args.values)


def line(args: ValidatedArgs, index: int = 0) -> Mapping[str, Any]:
    """The arguments of one line of a call that went through."""
    lines = args.lines
    assert lines is not None
    return lines[index]


def with_line(base: Mapping[str, Any], **values: Any) -> dict[str, Any]:
    """``base`` with the values of its first line replaced by ``values``."""
    first = {**base["lines"][0], **values}
    return {**base, "lines": [first]}


def field_of(field_type: FieldType, **overrides: Any) -> FieldModel:
    """One declared, writable field of ``field_type`` (as in test_input_schema.py)."""
    return FieldModel(name="x", type=field_type, origin="declared", writable=True, **overrides)


# --- the upper bounds outrank every other error (requirement 12.3) -----------------------


@pytest.mark.req("12.3")
def test_a_page_size_over_100_is_the_only_error_reported(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    # page is below its minimum and status is no code of the enum, both VALIDATION_FAILED
    # on their own; the page size over its maximum is reported alone.
    args = check(
        domain, specs, "supply_order_search", {"page": 0, "page_size": 101, "status": "nope"}
    )

    assert code(args) == ErrorCode.LIMIT_EXCEEDED
    assert details(args) == [
        {"field": "page_size", "reason": MAXIMUM_REASON, "expected": MAX_PAGE_SIZE}
    ]


@pytest.mark.req("12.3")
def test_more_than_200_lines_is_the_only_error_reported(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    lines = [{"article_id": "art_000001", "qty": 1, "price": 1}] * (MAX_LINES + 1)
    args = check(domain, specs, "supply_order_create", {**ORDER, "lines": lines, "zz": 1})

    assert code(args) == ErrorCode.LIMIT_EXCEEDED
    assert reasons(args) == [("lines", MAX_ITEMS_REASON)]


@pytest.mark.req("12.3")
def test_exactly_the_bound_is_within_it(domain: DomainModel, specs: Mapping[str, ToolSpec]) -> None:
    lines = [{"article_id": "art_000001", "qty": 1, "price": 1}] * MAX_LINES
    page = check(domain, specs, "supply_order_search", {"page_size": MAX_PAGE_SIZE})
    full = check(domain, specs, "supply_order_create", {**ORDER, "lines": lines})

    assert accepted(page)["page_size"] == MAX_PAGE_SIZE
    assert len(accepted(full)["lines"]) == MAX_LINES


@pytest.mark.req("12.3")
def test_a_page_size_that_is_no_integer_is_a_type_error(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    # "101" is no integer, so the upper bound does not apply and the type does.
    args = check(domain, specs, "supply_order_search", {"page_size": "101"})

    assert code(args) == ErrorCode.VALIDATION_FAILED
    assert reasons(args) == [("page_size", TYPE_REASON)]


@pytest.mark.req("12.3")
def test_lines_that_are_no_array_are_a_type_error(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_create", {**ORDER, "lines": "many"})

    assert code(args) == ErrorCode.VALIDATION_FAILED
    assert reasons(args) == [("lines", TYPE_REASON)]


# --- numbers: what a number parameter accepts (requirements 12.4, 12.5 and 12.13) --------


@pytest.mark.req("12.5")
def test_a_decimal_string_is_accepted_and_its_path_recorded(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_create", with_line(ORDER, qty="20.5", price="0.85"))

    assert line(args)["qty"] == Decimal("20.5")
    assert line(args)["price"] == Decimal("0.85")
    # Both paths, in the order of the details (requirements 12.12 and 17.11); qty is
    # declared after tax_rate in the lines of this bill.
    assert args.coerced == ("lines[0].qty", "lines[0].price")


@pytest.mark.req("12.5")
def test_a_json_number_is_no_coerced_path(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_create", with_line(ORDER, qty=20, price=0.85))

    assert accepted(args)
    assert args.coerced == ()


@pytest.mark.req("12.4")
def test_a_json_number_keeps_the_value_it_was_written_as(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_create", with_line(ORDER, price=0.85))

    # 0.85 is no binary fraction: through repr() it is exactly Decimal("0.85") (req 12.4).
    assert line(args)["price"] == Decimal("0.85")


@pytest.mark.req("12.13")
@pytest.mark.parametrize(
    "written",
    [
        pytest.param("1e3", id="exponent"),
        pytest.param(" 5", id="leading_space"),
        pytest.param("1,000", id="thousands_separator"),
        pytest.param("+5", id="plus_sign"),
        pytest.param(".5", id="no_integer_part"),
        pytest.param("5.", id="no_fraction"),
        pytest.param("", id="empty"),
        pytest.param("20\n", id="trailing_newline"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(None, id="null"),
        pytest.param([20], id="array"),
        pytest.param({"qty": 20}, id="object"),
    ],
)
def test_a_boolean_or_another_string_posing_as_a_number_is_a_type_error(
    domain: DomainModel, specs: Mapping[str, ToolSpec], written: Any
) -> None:
    args = check(domain, specs, "supply_order_create", with_line(ORDER, qty=written))

    assert code(args) == ErrorCode.VALIDATION_FAILED
    assert details(args) == [{"field": "lines[0].qty", "reason": TYPE_REASON, "expected": "number"}]
    # Nothing was converted here, so this path is no coerced one; the price of the call,
    # which is a decimal string, still is (requirement 12.5).
    assert args.coerced == ("lines[0].price",)


@pytest.mark.req("12.4")
def test_a_number_that_is_no_finite_value_is_a_type_error(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_create", with_line(ORDER, qty=float("inf")))

    assert reasons(args) == [("lines[0].qty", TYPE_REASON)]


@pytest.mark.req("12.4")
def test_a_number_the_engine_cannot_compute_with_is_a_type_error(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    # More significant digits than the engine's arithmetic holds: rounding it would raise,
    # so the value is rejected as a type error rather than ending the call internally.
    args = check(domain, specs, "supply_order_create", with_line(ORDER, qty="9" * 85))

    assert reasons(args) == [("lines[0].qty", TYPE_REASON)]
    assert "lines[0].qty" not in args.coerced


@pytest.mark.req("12.4")
def test_a_number_below_its_minimum_is_rejected(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    # Every qty, price, money and rate parameter has minimum 0 (requirement 11.11).
    args = check(domain, specs, "count_sheet_create", with_line(COUNT, diff_qty="-1"))

    assert reasons(args) == [("lines[0].diff_qty", MINIMUM_REASON)]
    # The conversion happened, so the path is recorded whether or not the call failed.
    assert args.coerced == ("lines[0].diff_qty",)


# --- rounding and the quantity of a line (requirements 12.2 and 12.6) --------------------


@pytest.mark.req("12.6")
def test_a_value_with_more_decimals_than_its_field_is_rounded(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(
        domain,
        specs,
        "supply_order_create",
        with_line(ORDER, qty="20.00005", price=0.1234565),
    )

    # price keeps 6 decimals and qty 4, both ROUND_HALF_UP (requirement 4.7).
    assert line(args)["price"] == Decimal("0.123457")
    assert line(args)["qty"] == Decimal("20.0001")


@pytest.mark.req("12.2")
def test_a_line_quantity_that_is_not_positive_after_rounding_is_rejected(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    # The quantity of this bill is its declared diff_qty: below half a unit in the last
    # kept place it rounds to 0, which is no quantity (requirement 12.2).
    zero = check(domain, specs, "count_sheet_create", with_line(COUNT, diff_qty="0.00004"))
    kept = check(domain, specs, "count_sheet_create", with_line(COUNT, diff_qty="0.00005"))

    assert reasons(zero) == [("lines[0].diff_qty", POSITIVE_REASON)]
    assert line(kept)["diff_qty"] == Decimal("0.0001")


@pytest.mark.req("12.2")
def test_the_quantity_of_a_line_is_the_one_its_bill_measures(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    # A price that rounds to 0 is no error: only the quantity must be positive.
    args = check(domain, specs, "supply_order_create", with_line(ORDER, qty=1, price="0.0000001"))
    quantity = check(domain, specs, "supply_order_create", with_line(ORDER, qty=0, price=1))

    assert line(args)["price"] == Decimal("0")
    assert reasons(quantity) == [("lines[0].qty", POSITIVE_REASON)]


@pytest.mark.req("12.2")
def test_the_quantity_field_of_each_bill_of_the_fixture(domain: DomainModel) -> None:
    found = {name: quantity_field(bill) for name, bill in domain.bills.items()}

    assert found == {
        # A bill with pricing measures its standard qty, whether or not it declares
        # quantity semantics (requirement 6.6).
        "supply_order": "qty",
        "fee_note": "qty",
        "pay_slip": "qty",
        # A bill without pricing measures the quantity its line_quantity declares.
        "goods_entry": "entry_qty",
        "issue_note": "issue_qty",
        "count_sheet": "diff_qty",
        "stock_move": "move_qty",
    }


# --- the page parameters of a read (requirement 5.15) ------------------------------------


@pytest.mark.req("5.15")
@pytest.mark.parametrize("param", ["page", "page_size"])
@pytest.mark.parametrize("tool", ["supply_order_search", "stock_level_query"])
def test_a_page_below_one_is_rejected(
    domain: DomainModel, specs: Mapping[str, ToolSpec], tool: str, param: str
) -> None:
    args = check(domain, specs, tool, {param: 0})
    negative = check(domain, specs, tool, {param: -1})

    assert code(args) == ErrorCode.VALIDATION_FAILED
    assert reasons(args) == [(param, MINIMUM_REASON)]
    assert reasons(negative) == [(param, MINIMUM_REASON)]


@pytest.mark.req("5.15")
def test_a_page_arrives_as_an_integer(domain: DomainModel, specs: Mapping[str, ToolSpec]) -> None:
    args = check(domain, specs, "supply_order_search", {"page": 2, "page_size": 20.0})

    # A JSON number without a fraction is an integer; the engine pages with ints.
    assert accepted(args)["page"] == 2
    assert accepted(args)["page_size"] == 20
    assert reasons(check(domain, specs, "supply_order_search", {"page": 2.5})) == [
        ("page", TYPE_REASON)
    ]


# --- dates and strings (requirement 12.2) ------------------------------------------------


@pytest.mark.req("12.2")
@pytest.mark.parametrize(
    "written",
    [
        pytest.param("2026-02-30", id="february_30"),
        pytest.param("2026-13-01", id="month_13"),
        pytest.param("2026-2-3", id="one_digit_parts"),
        pytest.param("20261009", id="no_separators"),
        pytest.param("2026-10-09T00:00:00", id="with_time"),
        pytest.param("2026-W41-5", id="week_date"),
        pytest.param("0000-01-01", id="year_zero"),
    ],
)
def test_a_date_that_is_no_calendar_date_is_rejected(
    domain: DomainModel, specs: Mapping[str, ToolSpec], written: str
) -> None:
    args = check(domain, specs, "supply_order_create", {**ORDER, "need_date": written})

    assert code(args) == ErrorCode.VALIDATION_FAILED
    assert reasons(args) == [("need_date", FORMAT_REASON)]


@pytest.mark.req("12.2")
def test_a_date_arrives_as_a_date(domain: DomainModel, specs: Mapping[str, ToolSpec]) -> None:
    args = check(
        domain,
        specs,
        "supply_order_create",
        {**ORDER, "need_date": "2026-10-20", "biz_date": "2024-02-29"},
    )

    # The storage layer writes a date and nothing else (design.md data model 3); 2024 is a
    # leap year, so the 29th of February is a date of the calendar and 2026-02-29 is not.
    assert accepted(args)["need_date"] == date(2026, 10, 20)
    assert accepted(args)["biz_date"] == date(2024, 2, 29)
    assert reasons(
        check(domain, specs, "supply_order_create", {**ORDER, "biz_date": "2026-02-29"})
    ) == [("biz_date", FORMAT_REASON)]


@pytest.mark.req("12.2")
def test_a_string_longer_than_its_field_is_rejected(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    # remark is the standard text field of a bill header, 500 code points (req 11.11).
    long = check(domain, specs, "supply_order_create", {**ORDER, "remark": "长" * 501})
    kept = check(domain, specs, "supply_order_create", {**ORDER, "remark": "长" * 500})

    assert reasons(long) == [("remark", MAX_LENGTH_REASON)]
    assert accepted(kept)["remark"] == "长" * 500


@pytest.mark.req("12.2")
def test_an_enum_parameter_takes_the_codes_of_its_field(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    good = check(domain, specs, "count_sheet_create", {**COUNT, "count_kind": "loss"})
    label = check(domain, specs, "count_sheet_create", {**COUNT, "count_kind": "盘亏"})

    assert accepted(good)["count_kind"] == "loss"
    assert reasons(label) == [("count_kind", ENUM_REASON)]


@pytest.mark.req("12.2")
def test_a_boolean_parameter_takes_a_boolean_alone(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    good = check(domain, specs, "supply_order_create", {**ORDER, "price_includes_tax": True})
    text = check(domain, specs, "supply_order_create", {**ORDER, "price_includes_tax": "true"})

    assert accepted(good)["price_includes_tax"] is True
    assert reasons(text) == [("price_includes_tax", TYPE_REASON)]


@pytest.mark.req("12.2")
@pytest.mark.parametrize("written", [20, None, True, ["x"]])
def test_a_string_parameter_takes_a_string_alone(
    domain: DomainModel, specs: Mapping[str, ToolSpec], written: Any
) -> None:
    args = check(domain, specs, "supply_order_create", {**ORDER, "vendor_id": written})

    assert reasons(args) == [("vendor_id", TYPE_REASON)]


@pytest.mark.req("12.2")
def test_a_lone_surrogate_in_a_string_is_a_type_error(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    # A client writes it as "\ud800"; UTF-8 cannot encode it, so no result and no diff of
    # this call could be written (design.md 5.2).
    args = check(domain, specs, "supply_order_create", {**ORDER, "remark": "备注\ud800"})
    paired = check(domain, specs, "supply_order_create", {**ORDER, "remark": "备注\U0001f600"})

    assert reasons(args) == [("remark", TYPE_REASON)]
    # A supplementary character, which a client writes as a surrogate pair, goes through.
    assert accepted(paired)["remark"] == "备注\U0001f600"


# --- what is missing and what is not declared (requirements 12.2 and 12.12) --------------


@pytest.mark.req("12.2")
def test_a_missing_required_parameter_is_reported(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_create", {"lines": ORDER["lines"]})
    empty = check(domain, specs, "supply_order_create", None)

    assert reasons(args) == [("vendor_id", REQUIRED_REASON), ("need_date", REQUIRED_REASON)]
    # Every required parameter of the tool, in declaration order, lines included.
    assert reasons(empty) == [
        ("vendor_id", REQUIRED_REASON),
        ("need_date", REQUIRED_REASON),
        ("lines", REQUIRED_REASON),
    ]


@pytest.mark.req("12.2")
def test_an_empty_lines_array_and_an_element_that_is_no_object(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    empty = check(domain, specs, "supply_order_create", {**ORDER, "lines": []})
    scalar = check(domain, specs, "supply_order_create", {**ORDER, "lines": [1]})

    assert reasons(empty) == [("lines", MIN_ITEMS_REASON)]
    assert reasons(scalar) == [("lines[0]", TYPE_REASON)]


@pytest.mark.req("12.12")
def test_the_details_follow_the_declaration_order_of_the_schema(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(
        domain,
        specs,
        "supply_order_create",
        {
            # Written in an order of their own; the details follow the schema, which
            # declares vendor_id, need_date, price_includes_tax, biz_date, remark, lines.
            "remark": 1,
            "biz_date": "2026-02-30",
            "need_date": "nope",
            "vendor_id": 1,
            "lines": [{"article_id": 1, "qty": "x", "price": 1}],
        },
    )

    assert reasons(args) == [
        ("vendor_id", TYPE_REASON),
        ("need_date", FORMAT_REASON),
        ("biz_date", FORMAT_REASON),
        ("remark", TYPE_REASON),
        ("lines[0].article_id", TYPE_REASON),
        ("lines[0].qty", TYPE_REASON),
    ]


@pytest.mark.req("12.12")
def test_the_errors_of_the_lines_follow_the_index_and_the_element_order(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(
        domain,
        specs,
        "supply_order_create",
        {
            **ORDER,
            "lines": [
                {"article_id": "art_000001", "qty": 1, "price": 1},
                {"qty": "x", "article_id": 1, "price": 1},
                {"article_id": "art_000001", "qty": 1},
            ],
        },
    )

    # By index, and inside one element by the declaration order of the element: the lines
    # of this bill declare article_id, measure_id, tax_rate, qty, price.
    assert reasons(args) == [
        ("lines[1].article_id", TYPE_REASON),
        ("lines[1].qty", TYPE_REASON),
        ("lines[2].price", REQUIRED_REASON),
    ]
    # The path of a line parameter is the one the engine reports it under (req 12.2).
    assert line_path(1, "qty") == "lines[1].qty"


@pytest.mark.req("12.12")
def test_unknown_parameters_come_after_the_declared_ones_of_their_level(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(
        domain,
        specs,
        "supply_order_create",
        {
            "zz": 1,
            "aa": 2,
            "Ab": 3,
            "vendor_id": 1,
            "need_date": "2026-10-20",
            "lines": [{"article_id": "art_000001", "qty": 1, "price": 1, "zz": 1, "aa": 2}],
        },
    )

    # The declared parameters first, the unknown ones of each level after them by code
    # point: "Ab" before "aa" and "zz" (requirement 12.12).
    assert reasons(args) == [
        ("vendor_id", TYPE_REASON),
        ("lines[0].aa", UNKNOWN_FIELD_REASON),
        ("lines[0].zz", UNKNOWN_FIELD_REASON),
        ("Ab", UNKNOWN_FIELD_REASON),
        ("aa", UNKNOWN_FIELD_REASON),
        ("zz", UNKNOWN_FIELD_REASON),
    ]


@pytest.mark.req("12.2")
def test_the_builtin_tool_takes_no_parameters(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    empty = check(domain, specs, "business_context_get", None)
    given = check(domain, specs, "business_context_get", {"org_id": "br_000001"})

    assert dict(empty.values) == {}
    assert reasons(given) == [("org_id", UNKNOWN_FIELD_REASON)]


# --- the shape of the outcome (requirements 12.9, 13.6, 13.7 and 13.8) -------------------


@pytest.mark.req("13.6", "13.8")
def test_an_error_names_no_rule_and_writes_its_items_in_order(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_search", {"page": 0, "status": "nope"})
    error = args.error
    assert error is not None

    # A VALIDATION_FAILED of the schema stage is decided by no rule (requirement 13.8).
    assert error.rule_id is None
    # field, then reason, then the extra keys (design.md data model 6).
    assert [list(item) for item in error.details] == [
        ["field", "reason", "expected"],
        ["field", "reason", "expected"],
    ]
    # The search declares keyword, the filters, page and page_size, so status comes first
    # (requirement 12.12); the enum names the codes the schema allows.
    assert details(args) == [
        {
            "field": "status",
            "reason": ENUM_REASON,
            "expected": ("draft", "submitted", "approved", "closed"),
        },
        {"field": "page", "reason": MINIMUM_REASON, "expected": 1},
    ]


@pytest.mark.req("13.6")
def test_the_reasons_are_those_of_the_data_model() -> None:
    # The schema reasons of design.md data model 6, plus the quantity that is not positive
    # after rounding; nothing else is reported from input validation.
    assert REASONS == (
        "required",
        "unknown_field",
        "type",
        "enum",
        "minimum",
        "maximum",
        "min_items",
        "max_items",
        "max_length",
        "pattern",
        "format",
        "must_be_positive",
    )


@pytest.mark.req("13.7")
def test_the_message_is_chinese_and_names_the_first_field(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_create", with_line(ORDER, qty="1e3", price="x"))
    error = args.error
    assert error is not None

    assert error.message.startswith("参数 lines[0].qty ")
    assert "另有 1 处参数有误" in error.message
    assert any("\u4e00" <= char <= "\u9fff" for char in error.message)


@pytest.mark.req("12.9")
def test_the_same_arguments_give_the_same_error(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    raw: dict[str, Any] = {"zz": 1, "aa": 2, "page": 0, "status": "nope"}
    first = check(domain, specs, "supply_order_search", raw)
    # The order the client wrote the keys in changes nothing (requirements 12.9 and 12.12).
    second = check(domain, specs, "supply_order_search", dict(reversed(list(raw.items()))))

    assert details(first) == details(second)
    assert first.error is not None and second.error is not None
    assert first.error.message == second.error.message


@pytest.mark.req("12.1")
def test_the_arguments_come_out_as_the_engine_takes_them(
    domain: DomainModel, specs: Mapping[str, ToolSpec]
) -> None:
    args = check(domain, specs, "supply_order_create", ORDER)
    update = check(domain, specs, "supply_order_update", {"id": "so_000001", "remark": "改期"})

    # scalars are the values of the header and lines the array; an update that carries no
    # lines leaves them None, which is what the engine reads as "unchanged" (req 5.2).
    assert dict(args.scalars) == {
        "vendor_id": "ven_000001",
        "need_date": date(2026, 10, 20),
    }
    assert args.lines is not None
    assert dict(args.lines[0]) == {
        "article_id": "art_000001",
        "qty": Decimal("20"),
        "price": Decimal("0.85"),
    }
    assert update.lines is None
    assert dict(update.scalars) == {"id": "so_000001", "remark": "改期"}


# --- the types the fixture has no write tool for (requirement 12.2) ----------------------

# A bill of this test alone: the header of count_sheet with one field of each remaining
# type, so an int, a bool, a money and a period parameter are read off a generated schema.
EXTRA_FIELDS: Final[tuple[tuple[str, FieldModel], ...]] = (
    ("fee", field_of("money")),
    ("term", field_of("int")),
    ("urgent", field_of("bool")),
    ("period", field_of("period")),
    ("tag", field_of("string", max_length=4)),
    # Allowed values, which become a numeric enum of the schema (requirement 11.11).
    ("levy", field_of("rate", values=("0", "0.06", "0.130"))),
)


def sample_tool() -> tuple[DomainModel, ToolSpec]:
    """A domain model whose count_sheet carries :data:`EXTRA_FIELDS`, and its create tool."""
    domain = fixture_domain()
    bill: BillModel = domain.bills["count_sheet"]
    header = FrozenMap(
        {
            **bill.header,
            **{name: replace(declared, name=name) for name, declared in EXTRA_FIELDS},
        }
    )
    sample = replace(
        bill,
        header=header,
        header_declared_fields=(
            *bill.header_declared_fields,
            *(name for name, _ in EXTRA_FIELDS),
        ),
    )
    built = bill_input(sample, "create")
    spec = ToolSpec(
        name="count_sheet_create",
        generated_name="count_sheet_create",
        pack=sample.pack,
        kind="entity_op",
        action="create",
        target=sample.name,
        input_schema=built.schema,
        param_paths=built.param_paths,
    )
    return replace(domain, bills=FrozenMap({**domain.bills, sample.name: sample})), spec


@pytest.mark.req("12.2", "12.6")
def test_a_money_parameter_is_rounded_to_two_decimals() -> None:
    model, spec = sample_tool()

    args = validate_args(model, spec, {**COUNT, "fee": "1.005"})

    assert accepted(args)["fee"] == Decimal("1.01")


@pytest.mark.req("12.2")
def test_an_int_a_bool_and_a_period_parameter() -> None:
    model, spec = sample_tool()

    good = validate_args(
        model, spec, {**COUNT, "term": 30, "urgent": False, "period": "2026-10", "tag": "abcd"}
    )
    bad = validate_args(
        model, spec, {**COUNT, "term": 1.5, "urgent": 1, "period": "2026-13", "tag": "abcde"}
    )

    assert accepted(good)["term"] == 30
    assert accepted(good)["urgent"] is False
    assert accepted(good)["period"] == "2026-10"
    # A JSON number is no boolean, a month 13 is no period, and the limit counts code
    # points (requirements 12.2 and 12.13).
    assert reasons(bad) == [
        ("term", TYPE_REASON),
        ("urgent", TYPE_REASON),
        ("period", PATTERN_REASON),
        ("tag", MAX_LENGTH_REASON),
    ]


@pytest.mark.req("12.2")
def test_a_period_takes_no_trailing_newline() -> None:
    model, spec = sample_tool()

    args = validate_args(model, spec, {**COUNT, "period": "2026-10\n"})

    assert reasons(args) == [("period", PATTERN_REASON)]


@pytest.mark.req("12.4")
def test_an_allowed_value_is_compared_by_value() -> None:
    model, spec = sample_tool()

    # The field allows 0, 0.06 and 0.130, which the schema writes as the JSON numbers 0,
    # 0.06 and 0.13; a parameter equals an allowed value whenever the two are numerically
    # equal, so "0.130", 0.13 and 0.1300 all pass (requirement 12.4).
    for written in ("0.130", 0.13, "0.1300", 0, "0.06"):
        args = validate_args(model, spec, {**COUNT, "levy": written})
        assert args.error is None, written
        assert args.values["levy"] == Decimal(str(written))

    other = validate_args(model, spec, {**COUNT, "levy": "0.07"})

    assert details(other) == [{"field": "levy", "reason": ENUM_REASON, "expected": (0, 0.06, 0.13)}]
    assert other.coerced == ("levy",)
