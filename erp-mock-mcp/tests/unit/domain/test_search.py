"""erp_mock.domain.search: what a search matches, in which order, and which page of it.

The tests run against a real sandbox: ``engine_env`` (tests.helpers.engine_env) creates the
tables of the engine test fixture and the basic data of :data:`BASIC_DATA` through the
engine, and the bills of the search tests are created the same way. A state a bill reaches
only through a state operation is written directly, since the state_machine primitive
arrives with a later task. What the tests pin:

- a keyword matches any keyword field of the object, ASCII letters without case and every
  other character as written, and the two wildcards of LIKE are escaped
  (requirement 5.9);
- a filter compares for equality, a date filter includes both of its ends, and a condition
  the caller left out does not filter (requirements 5.9 and 11.5);
- a search returns the summary fields and no lines, in the order the outputSchema of its
  tool declares them (requirement 5.10);
- ``total``, ``page``, ``page_size``, ``has_more`` and the slice of the page, a page past
  the last one included (requirement 5.11);
- the records are ordered by ``default_sort`` and then by id, each value compared within
  its own type (requirement 5.14);
- a reference parameter that names no record ends the call in NOT_FOUND (requirement 5.6).
"""

from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.model import BillModel, DomainModel, EntityModel
from erp_mock.domain.engine import SearchOp
from erp_mock.domain.search import (
    DEFAULT_PAGE,
    DEFAULT_PAGE_SIZE,
    SortKey,
    keyword_pattern,
    search_parameters,
    sorted_records,
    summary_fields,
)
from erp_mock.domain.views import PageView, RecordView
from erp_mock.state.store import Record, Value
from erp_mock.tools.output_schema import object_output
from tests.helpers.engine_env import (
    BOLT,
    MAIN_VENDOR,
    RETIRED_VENDOR,
    SPARE_VENDOR,
    EngineEnv,
    engine_env,
    summary_of,
)

# The objects the tests search: the partner entity with three keyword fields and a status
# filter, the material entity with an enum filter, the pricing bill with a reference and a
# date filter, and the organization entity, which declares no search at all.
VENDOR: Final = "vendor"
ARTICLE: Final = "article"
ORDER: Final = "supply_order"
BRANCH: Final = "branch"

# The three business dates the orders of the search tests carry, and the number each one's
# order gets: the first order of a (prefix, date) is 0001 (requirement 4.4).
FIRST_DAY: Final = date(2026, 10, 9)
SECOND_DAY: Final = date(2026, 10, 10)
THIRD_DAY: Final = date(2026, 10, 11)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    with engine_env(tmp_path / "sandbox") as found:
        yield found


@pytest.fixture
def orders(env: EngineEnv) -> tuple[str, ...]:
    """Three orders, one per business date, the middle one of the second vendor."""
    return (
        create_order(env, vendor=MAIN_VENDOR, biz_date=FIRST_DAY),
        create_order(env, vendor=SPARE_VENDOR, biz_date=SECOND_DAY, status="approved"),
        create_order(env, vendor=MAIN_VENDOR, biz_date=THIRD_DAY),
    )


def create_order(
    env: EngineEnv,
    *,
    vendor: str,
    biz_date: date,
    status: str | None = None,
    lines: Sequence[Mapping[str, Value]] = (),
) -> str:
    """Create one supply order and return its id, in ``status`` where one is given.

    The state is written directly: the state operations of a bill arrive with the
    state_machine primitive (requirement 7.1), and what a search filters by is the value
    the header holds.
    """
    values: dict[str, Value] = {
        "vendor_id": env.id(vendor),
        "biz_date": biz_date,
        "need_date": biz_date,
    }
    view = env.create(ORDER, values, lines=list(lines) or [order_line(env)]).view
    assert isinstance(view, RecordView)
    order_id = view.fields["id"]
    assert isinstance(order_id, str)
    if status is not None:
        with env.store.transaction() as uow:
            uow.update(ORDER, order_id, {"status": status})
    return order_id


def order_line(env: EngineEnv, article: str = BOLT, price: str = "10") -> dict[str, Value]:
    """One line of a supply order: the article, a quantity and a price (requirement 6.1)."""
    return {"article_id": env.id(article), "qty": Decimal("2"), "price": Decimal(price)}


def search(env: EngineEnv, target: str, args: Mapping[str, Value] = FrozenMap()) -> PageView:
    """Run the search of ``target`` with ``args`` and return the page it answers with."""
    view = env.run(SearchOp(target, FrozenMap(args))).view
    assert isinstance(view, PageView)
    return view


def ids(page: PageView) -> list[str]:
    """The internal ids of the records of ``page``, in the order they came back."""
    found: list[str] = []
    for item in page.items:
        record_id = item.fields["id"]
        assert isinstance(record_id, str)
        found.append(record_id)
    return found


def summary_keys(domain: DomainModel, item: EntityModel | BillModel) -> tuple[str, ...]:
    """The keys the ``items`` of the search tool of ``item`` declare (requirement 5.10).

    Read from the outputSchema the tool generator writes, which is the other place the
    summary rule of requirement 5.10 is implemented (design.md 5.1).
    """
    schema = object_output(domain, item, "search").schema
    properties = as_object(schema["properties"])
    items = as_object(as_object(properties["items"])["items"])
    return tuple(as_object(items["properties"]))


def as_object(value: JsonValue) -> Mapping[str, JsonValue]:
    """``value`` as the schema object it is."""
    assert isinstance(value, Mapping)
    return value


# --- the conditions of a search (requirement 5.9) ----------------------------------------


@pytest.mark.req("5.9")
def test_keyword_matches_any_keyword_field_ignoring_ascii_case(env: EngineEnv) -> None:
    # The keyword fields of the partner entity are code, name and short_name.
    assert len(ids(search(env, VENDOR, {"keyword": "v000"}))) == 3
    assert ids(search(env, VENDOR, {"keyword": "V0002"})) == [env.id(SPARE_VENDOR)]
    # A character that is no ASCII letter is compared as written.
    assert ids(search(env, VENDOR, {"keyword": "夹具主力"})) == [env.id(MAIN_VENDOR)]
    assert ids(search(env, VENDOR, {"keyword": "停用供货"})) == [env.id(RETIRED_VENDOR)]
    # An absent and an empty keyword do not filter (requirement 5.9).
    assert len(ids(search(env, VENDOR, {"keyword": ""}))) == 3
    assert len(ids(search(env, VENDOR))) == 3


@pytest.mark.req("5.9")
def test_keyword_takes_the_wildcards_of_like_as_written(env: EngineEnv) -> None:
    # Unescaped, '%V_000%' would match the code V0001; escaped, the underscore is a
    # character of its own and nothing matches.
    assert ids(search(env, VENDOR, {"keyword": "V_000"})) == []
    assert ids(search(env, VENDOR, {"keyword": "%"})) == []
    assert ids(search(env, VENDOR, {"keyword": "V0001"})) == [env.id(MAIN_VENDOR)]


@pytest.mark.req("5.9")
def test_keyword_pattern_escapes_the_wildcards_and_the_escape_itself() -> None:
    assert keyword_pattern("abc") == "%abc%"
    assert keyword_pattern("a_b%c") == r"%a\_b\%c%"
    assert keyword_pattern("a\\b") == "%a\\\\b%"


@pytest.mark.req("5.9")
def test_a_filter_compares_for_equality_and_an_absent_one_does_not_filter(
    env: EngineEnv, orders: tuple[str, ...]
) -> None:
    # An entity filter: the status of the partner entity (requirement 5.9).
    assert ids(search(env, VENDOR, {"status": "disabled"})) == [env.id(RETIRED_VENDOR)]
    assert len(ids(search(env, VENDOR, {"status": "enabled"}))) == 2
    # An enum filter of the material entity, and a reference filter of the bill.
    assert ids(search(env, ARTICLE, {"grade": "fine"})) == [env.id("article:washer")]
    assert ids(search(env, ORDER, {"vendor_id": env.id(SPARE_VENDOR)})) == [orders[1]]
    # Two conditions at once must both hold, and a condition left out does not filter.
    both = {"vendor_id": env.id(MAIN_VENDOR), "status": "draft"}
    assert sorted(ids(search(env, ORDER, both))) == sorted([orders[0], orders[2]])
    assert ids(search(env, ORDER, {"status": "approved"})) == [orders[1]]
    assert len(ids(search(env, ORDER))) == 3


@pytest.mark.req("5.9", "11.5")
def test_a_date_filter_includes_both_of_its_ends(env: EngineEnv, orders: tuple[str, ...]) -> None:
    # biz_date becomes date_from and date_to (requirement 11.5).
    assert search_parameters(env.domain.bills[ORDER]) == (
        "keyword",
        "status",
        "vendor_id",
        "date_from",
        "date_to",
        "page",
        "page_size",
    )
    assert sorted(ids(search(env, ORDER, {"date_from": SECOND_DAY}))) == sorted(
        [orders[1], orders[2]]
    )
    assert sorted(ids(search(env, ORDER, {"date_to": SECOND_DAY}))) == sorted(
        [orders[0], orders[1]]
    )
    # Both ends on one day: the day itself is included at either end.
    one_day = {"date_from": SECOND_DAY, "date_to": SECOND_DAY}
    assert ids(search(env, ORDER, one_day)) == [orders[1]]
    assert ids(search(env, ORDER, {"date_from": THIRD_DAY, "date_to": SECOND_DAY})) == []


# --- the summary fields (requirement 5.10) -----------------------------------------------


@pytest.mark.req("5.10")
def test_a_search_returns_the_summary_fields_of_an_entity_and_no_other(env: EngineEnv) -> None:
    page = search(env, VENDOR, {"keyword": "V0001"})

    item = page.items[0]
    # id, code and name, the status the entity keeps, then the fields of its search
    # (requirement 5.10); the same keys the outputSchema of the tool declares.
    assert tuple(item.fields) == ("id", "code", "name", "status", "short_name")
    assert tuple(item.fields) == summary_keys(env.domain, env.domain.entities[VENDOR])
    assert item.fields["code"] == "V0001"
    assert item.fields["status"] == "enabled"
    # A field outside the summary is not returned, and a material's conversion records are
    # not either (requirement 6.9).
    assert "pay_days" not in item.fields
    article = search(env, ARTICLE, {"keyword": "A00001"}).items[0]
    assert tuple(article.fields) == ("id", "code", "name", "spec", "grade")
    assert tuple(article.fields) == summary_keys(env.domain, env.domain.entities[ARTICLE])


@pytest.mark.req("5.10")
def test_a_search_of_a_bill_returns_no_lines_and_the_partner_and_total(
    env: EngineEnv, orders: tuple[str, ...]
) -> None:
    page = search(env, ORDER, {"vendor_id": env.id(SPARE_VENDOR)})

    item = page.items[0]
    assert tuple(item.fields) == (
        "id",
        "number",
        "status",
        "biz_date",
        "vendor",
        "amount_with_tax",
    )
    assert tuple(item.fields) == summary_keys(env.domain, env.domain.bills[ORDER])
    # No line is returned (requirement 5.10); the reference comes back as a summary (13.3).
    assert "lines" not in item.fields
    assert summary_of(item, "vendor")["id"] == env.id(SPARE_VENDOR)
    assert item.fields["number"] == "SPO202610100001"
    # The bill prices net, so the total of 2 x 10 at 13% is 22.60 (requirement 6.1).
    assert item.fields["amount_with_tax"] == Decimal("22.60")


@pytest.mark.req("5.10")
def test_summary_fields_of_an_object_without_a_search(env: EngineEnv) -> None:
    fields = summary_fields(env.domain.entities[BRANCH])

    # An object that declares no search returns its id, code and name alone.
    assert tuple(field.name for field in fields) == ("id", "code", "name")
    assert tuple(field.name for field in fields) == summary_keys(
        env.domain, env.domain.entities[BRANCH]
    )


# --- the page (requirement 5.11) ---------------------------------------------------------


@pytest.mark.req("5.11")
def test_the_page_defaults_to_the_first_twenty_records(env: EngineEnv) -> None:
    page = search(env, VENDOR)

    assert (page.page, page.page_size) == (DEFAULT_PAGE, DEFAULT_PAGE_SIZE)
    assert (page.total, len(page.items), page.has_more) == (3, 3, False)


@pytest.mark.req("5.11")
def test_a_page_reports_the_total_and_whether_records_follow(env: EngineEnv) -> None:
    first = search(env, VENDOR, {"page": 1, "page_size": 2})
    second = search(env, VENDOR, {"page": 2, "page_size": 2})

    assert (first.total, first.page, first.page_size, first.has_more) == (3, 1, 2, True)
    assert len(first.items) == 2
    assert (second.total, second.has_more) == (3, False)
    assert len(second.items) == 1
    # The page is the slice of the sorted records, which the partner entity sorts by code.
    assert ids(first) + ids(second) == [
        env.id(MAIN_VENDOR),
        env.id(SPARE_VENDOR),
        env.id(RETIRED_VENDOR),
    ]


@pytest.mark.req("5.11")
def test_a_page_past_the_last_one_and_a_search_without_a_match_are_empty(env: EngineEnv) -> None:
    past = search(env, VENDOR, {"page": 9, "page_size": 2})
    nothing = search(env, VENDOR, {"keyword": "没有这个名称"})

    assert (past.total, past.items, past.has_more) == (3, (), False)
    assert (nothing.total, nothing.items, nothing.has_more) == (0, (), False)


@pytest.mark.req("5.15")
def test_a_page_below_one_and_an_unknown_parameter_are_programming_errors(env: EngineEnv) -> None:
    # The tools layer reports both as VALIDATION_FAILED before the engine sees them
    # (requirements 5.15 and 12.2).
    with pytest.raises(ValueError, match="page"):
        search(env, VENDOR, {"page": 0})
    with pytest.raises(KeyError, match="no_such_filter"):
        search(env, VENDOR, {"no_such_filter": "x"})


# --- the order of the records (requirement 5.14) -----------------------------------------


@pytest.mark.req("5.14")
def test_default_sort_orders_by_the_declared_keys_and_directions(
    env: EngineEnv, orders: tuple[str, ...]
) -> None:
    page = search(env, ORDER)

    # The bill sorts by biz_date desc, number desc: the latest business date first.
    assert ids(page) == [orders[2], orders[1], orders[0]]
    assert [item.fields["biz_date"] for item in page.items] == [THIRD_DAY, SECOND_DAY, FIRST_DAY]
    # The partner entity sorts by code ascending, and the codes were handed out in
    # creation order (requirement 4.3).
    assert ids(search(env, VENDOR)) == [
        env.id(MAIN_VENDOR),
        env.id(SPARE_VENDOR),
        env.id(RETIRED_VENDOR),
    ]


@pytest.mark.req("5.14")
def test_an_object_without_a_default_sort_is_ordered_by_id(env: EngineEnv) -> None:
    page = search(env, BRANCH)

    assert ids(page) == ["br_000001", "br_000002"]


@pytest.mark.req("5.14")
def test_values_are_compared_within_their_type_and_the_id_is_the_last_key() -> None:
    records: tuple[Record, ...] = (
        FrozenMap({"id": "a_2", "qty": Decimal("9"), "day": date(2026, 1, 2)}),
        FrozenMap({"id": "a_10", "qty": Decimal("10"), "day": date(2026, 1, 10)}),
        FrozenMap({"id": "a_1", "qty": Decimal("9.0"), "day": date(2026, 1, 2)}),
        FrozenMap({"id": "a_3", "qty": None, "day": None}),
    )
    by_qty = (SortKey(value=lambda record: record.get("qty")),)
    by_day_desc = (SortKey(value=lambda record: record.get("day"), descending=True),)

    # Numbers compare by magnitude, not as text, and an empty value comes last; records
    # with equal keys keep the order of their ids (requirement 5.14).
    assert [record["id"] for record in sorted_records(records, by_qty)] == [
        "a_1",
        "a_2",
        "a_10",
        "a_3",
    ]
    # A descending key reverses everything, the ranking of the empty value included.
    assert [record["id"] for record in sorted_records(records, by_day_desc)] == [
        "a_3",
        "a_10",
        "a_1",
        "a_2",
    ]
    # Without a key the records come back by id alone (requirement 5.13).
    assert [record["id"] for record in sorted_records(records)] == ["a_1", "a_10", "a_2", "a_3"]


# --- the reference parameters of a search (requirement 5.6) ------------------------------


@pytest.mark.req("5.6")
def test_a_reference_filter_that_names_no_record_is_not_found(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        search(env, ORDER, {"vendor_id": "ven_000009"})

    error = raised.value
    assert error.code is ErrorCode.NOT_FOUND
    assert error.rule_id is None
    assert [dict(item) for item in error.details] == [{"field": "vendor_id", "reason": "not_found"}]


@pytest.mark.req("5.6")
def test_a_search_makes_no_writes(env: EngineEnv) -> None:
    result = env.run(SearchOp(VENDOR, FrozenMap({"keyword": "V0001"})), call_seq=3)

    assert result.writes == ()
    assert env.counters().get(("op", "op")) is None
