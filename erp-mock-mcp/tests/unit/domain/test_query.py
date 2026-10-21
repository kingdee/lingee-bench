"""erp_mock.domain.query: which records a declarative query selects, and what it returns.

The tests run against a real sandbox: ``engine_env`` (tests.helpers.engine_env) creates the
tables of the engine test fixture and its basic data through the engine, and the orders the
queries read are created the same way. A state a bill reaches only through a state operation
is written directly, since the state_machine primitive arrives with a later task.

The fixture's own query is ``supply_price_query``: it reads the lines of the pricing bill,
takes the partner at the header level and the article at the line level, filters by
``header.status in ['approved', 'closed']``, sorts by ``header.biz_date desc,
header.number desc`` and returns the price of the line with fields of its header
(tests/fixtures/packs/fx_trade/tools/exposure.yaml). The tests of the filter scope build
queries of their own over the same configuration, the way loading step 5 builds them, so
every form of path requirement 10.10 allows is evaluated on a stored record.

What the tests pin:

- a parameter selects by the field it maps to, at the level its path names, and one that
  was not given does not filter (requirement 5.13);
- only a record the filter is true for is selected; false and unknown are not
  (requirements 5.13 and 10.6);
- ``header.``, ``line.``, a ref expansion, ``context.``, ``sum``, ``count`` and ``exists``
  are evaluated on the source record and its bill, and a path with no record to read -
  through an empty reference, or of a level the source does not have - is null
  (requirements 10.6 and 10.10);
- the source is a bill at either level or an entity at the record level, and a query that
  names no source the instance has is run by none of this (requirements 2.9 and 9.7);
- the records are ordered by ``sort`` and then by the id of the source record
  (requirements 5.13 and 5.14);
- ``latest`` is the first record and ``history`` the first ``limit`` of them, both with the
  fields their paths name, in the order the outputSchema of the tool declares them
  (requirement 5.13);
- no match leaves ``latest`` null and ``history`` empty, and a reference parameter that
  names no record ends the call in NOT_FOUND (requirements 5.13 and 5.6).
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
from erp_mock.config.model import (
    DomainModel,
    FieldLevel,
    FieldRef,
    QueryModel,
    QueryParamModel,
    SortRef,
)
from erp_mock.domain.engine import QueryOp
from erp_mock.domain.query import (
    query_filter,
    query_filters,
    resolve_source,
    result_fields,
    run_query,
    selected_records,
)
from erp_mock.domain.views import QueryView, RecordView
from erp_mock.state.schema import ID_COLUMN
from erp_mock.state.store import Record, Value
from erp_mock.tools.output_schema import query_output
from tests.helpers.engine_env import (
    BOLT,
    MAIN_VENDOR,
    SPARE_VENDOR,
    WASHER,
    EngineEnv,
    engine_env,
)

# The query tools of the fixture: the declared one over the lines of the pricing bill, and
# the one the inventory primitive provides (requirement 9.7).
PRICE_QUERY: Final = "supply_price_query"
BALANCE_QUERY: Final = "stock_level_query"

ORDER: Final = "supply_order"
ORDER_LINE: Final = "supply_order_line"

# The business dates of the orders the tests create, one order per day.
FIRST_DAY: Final = date(2026, 10, 9)
SECOND_DAY: Final = date(2026, 10, 10)
THIRD_DAY: Final = date(2026, 10, 11)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    with engine_env(tmp_path / "sandbox") as found:
        yield found


@pytest.fixture
def orders(env: EngineEnv) -> dict[str, str]:
    """Four orders: two the price query selects from, one draft and one of another partner.

    - ``approved``: the first day, the main partner, one line of each article;
    - ``closed``: the second day, the main partner, one line of the first article;
    - ``draft``: the third day, the main partner, one line of the first article, in a state
      the filter does not select;
    - ``other``: the second day, the second partner, one line of the first article.
    """
    return {
        "approved": create_order(
            env,
            vendor=MAIN_VENDOR,
            biz_date=FIRST_DAY,
            status="approved",
            lines=[order_line(env, BOLT, "10"), order_line(env, WASHER, "4")],
        ),
        "closed": create_order(
            env,
            vendor=MAIN_VENDOR,
            biz_date=SECOND_DAY,
            status="closed",
            lines=[order_line(env, BOLT, "12")],
        ),
        "draft": create_order(
            env,
            vendor=MAIN_VENDOR,
            biz_date=THIRD_DAY,
            lines=[order_line(env, BOLT, "99")],
        ),
        "other": create_order(
            env,
            vendor=SPARE_VENDOR,
            biz_date=SECOND_DAY,
            status="approved",
            lines=[order_line(env, BOLT, "50")],
        ),
    }


def create_order(
    env: EngineEnv,
    *,
    vendor: str,
    biz_date: date,
    lines: Sequence[Mapping[str, Value]],
    status: str | None = None,
) -> str:
    """Create one supply order and return its id, in ``status`` where one is given."""
    values: dict[str, Value] = {
        "vendor_id": env.id(vendor),
        "biz_date": biz_date,
        "need_date": biz_date,
    }
    view = env.create(ORDER, values, lines=list(lines)).view
    assert isinstance(view, RecordView)
    order_id = view.fields["id"]
    assert isinstance(order_id, str)
    if status is not None:
        with env.store.transaction() as uow:
            uow.update(ORDER, order_id, {"status": status})
    return order_id


def order_line(env: EngineEnv, article: str, price: str) -> dict[str, Value]:
    """One line of a supply order: the article, a quantity and a price."""
    return {"article_id": env.id(article), "qty": Decimal("2"), "price": Decimal(price)}


def run(env: EngineEnv, tool: str, args: Mapping[str, Value] = FrozenMap()) -> QueryView:
    """Run the query tool ``tool`` through the engine and return its result."""
    view = env.run(QueryOp(tool, FrozenMap(args))).view
    assert isinstance(view, QueryView)
    return view


def price_args(env: EngineEnv, article: str = BOLT, vendor: str = MAIN_VENDOR) -> dict[str, Value]:
    """The two parameters of the fixture's price query (requirement 5.13)."""
    return {"vendor_id": env.id(vendor), "article_id": env.id(article)}


def values_of(view: RecordView | None) -> dict[str, Value]:
    """The scalar values of one result object; every field of this query is a scalar."""
    assert view is not None
    found: dict[str, Value] = {}
    for key, value in view.fields.items():
        assert not isinstance(value, tuple), key
        found[key] = value if not hasattr(value, "fields") else None
    return found


def query_keys(domain: DomainModel, tool: str, key: str) -> tuple[str, ...]:
    """The keys the ``latest`` or the ``history`` of the query tool declares (req 5.13).

    Read from the outputSchema the tool generator writes, which is the other place the rule
    of requirement 5.13 is implemented (design.md 5.1).
    """
    schema = query_output(domain, domain.queries[tool]).schema
    found = as_object(as_object(schema["properties"])[key])
    if key == "history":
        found = as_object(found["items"])
    return tuple(as_object(found["properties"]))


def as_object(value: JsonValue) -> Mapping[str, JsonValue]:
    """``value`` as the schema object it is."""
    assert isinstance(value, Mapping)
    return value


def line_query(
    *,
    filter_text: str | None = None,
    sort: tuple[SortRef, ...] = (),
    latest: tuple[FieldRef, ...] = (),
    source: str = ORDER,
    level: FieldLevel = "line",
    params: Mapping[str, QueryParamModel] = FrozenMap(),
    history_limit: int | None = None,
) -> QueryModel:
    """One declarative query over the fixture's configuration, as step 5 resolves one."""
    return QueryModel(
        tool="fx_probe_query",
        pack="fx_trade",
        file="tools/exposure.yaml",
        source=source,
        source_level=level,
        params=FrozenMap(params),
        filter=filter_text,
        sort=sort,
        latest=latest,
        history_limit=history_limit,
    )


def selected(
    env: EngineEnv, query: QueryModel, args: Mapping[str, Value] = FrozenMap()
) -> list[str]:
    """The ids of the source records ``query`` selects, in the order it sorted them."""
    source = resolve_source(env.domain, query)
    assert source is not None
    with env.store.transaction() as uow:
        records: list[Record] = selected_records(
            uow,
            env.domain,
            query,
            source,
            FrozenMap(args),
            filter_tree=query_filter(query),
            anchor_date=env.anchor_date,
            current_user_id=env.current_user_id,
        )
    return [str(record[ID_COLUMN]) for record in records]


# --- the parameters and the filter (requirements 5.13 and 10.10) -------------------------


@pytest.mark.req("5.13")
def test_a_parameter_selects_by_the_field_it_maps_to_at_its_own_level(
    env: EngineEnv, orders: dict[str, str]
) -> None:
    view = run(env, PRICE_QUERY, price_args(env))

    # The partner is compared at the header level and the article at the line level, so of
    # the six lines only those of the main partner with the first article are left, and the
    # draft order is dropped by the filter (requirement 10.10).
    history = [values_of(item)["price"] for item in view.history]
    assert history == [Decimal("12"), Decimal("10")]
    # The other partner's line and the second article's line are not among them.
    assert values_of(view.latest)["number"] == "SPO202610100001"
    other = run(env, PRICE_QUERY, price_args(env, vendor=SPARE_VENDOR))
    assert [values_of(item)["price"] for item in other.history] == [Decimal("50")]
    washer = run(env, PRICE_QUERY, price_args(env, article=WASHER))
    assert [values_of(item)["price"] for item in washer.history] == [Decimal("4")]


@pytest.mark.req("5.13")
def test_a_parameter_that_was_not_given_does_not_filter(
    env: EngineEnv, orders: dict[str, str]
) -> None:
    query = line_query(
        params={
            "article_id": QueryParamModel(
                name="article_id", type="ref", maps_to=FieldRef("line", "article_id"), ref="article"
            )
        },
    )

    # Every line of every order, the draft one included, since this query has no filter.
    assert len(selected(env, query)) == 5
    assert len(selected(env, query, {"article_id": env.id(BOLT)})) == 4
    # A parameter given as null does not filter either.
    assert len(selected(env, query, {"article_id": None})) == 5


@pytest.mark.req("5.13", "10.6")
def test_only_a_record_the_filter_is_true_for_is_selected(
    env: EngineEnv, orders: dict[str, str]
) -> None:
    # The filter of the fixture's query, which the draft order does not satisfy.
    states = line_query(filter_text="header.status in ['approved', 'closed']")
    # A comparison with an empty value is unknown, which selects nothing (requirement 10.6).
    unknown = line_query(filter_text="header.remark == 'x'")

    assert len(selected(env, states)) == 4
    assert selected(env, unknown) == []
    # A filter the pack did not declare selects every record.
    assert len(selected(env, line_query())) == 5


@pytest.mark.req("10.10")
@pytest.mark.parametrize(
    ("filter_text", "expected"),
    [
        # line. is the source line, header. the bill it belongs to (requirement 10.10).
        ("line.price > 10", 3),
        ("header.price_includes_tax", 0),
        # A ref path is expanded to the record it points at, over both levels.
        ("line.article.grade == 'fine'", 1),
        ("header.vendor.short_name == '夹具主力'", 4),
        # The business context: the anchor date, its month and the current user; the
        # anchor date is the business date of the first order alone.
        ("header.biz_date == context.today", 2),
        ("header.biz_date <= context.today", 2),
        ("context.period == '2026-10'", 5),
        ("context.user.duty == 'buyer'", 5),
        ("context.user.duty == 'keeper'", 0),
        # sum and count read the bill the source line belongs to (requirement 10.13).
        ("count(lines) == 2", 2),
        ("sum(lines.price) == 14", 2),
        # exists and sum_of read a whole table; the unprefixed path is its record.
        ("exists(vendor, status == 'disabled')", 5),
        ("exists(vendor, code == 'V0009')", 0),
        ("sum_of(vendor.pay_days, status == 'enabled') == 75", 5),
    ],
)
def test_the_filter_is_evaluated_on_the_source_record_and_its_bill(
    env: EngineEnv, orders: dict[str, str], filter_text: str, expected: int
) -> None:
    assert len(selected(env, line_query(filter_text=filter_text))) == expected


@pytest.mark.req("10.10")
def test_a_query_over_bill_headers_and_over_entity_records(
    env: EngineEnv, orders: dict[str, str]
) -> None:
    headers = line_query(level="header", filter_text="header.status == 'approved'")
    records = line_query(source="vendor", level="record", filter_text="header.status == 'enabled'")

    # A header source reads one record per bill, and header. is that record.
    assert selected(env, headers) == [orders["approved"], orders["other"]]
    # An entity source reads its records, and header. is the record itself.
    assert selected(env, records) == [env.id(MAIN_VENDOR), env.id(SPARE_VENDOR)]


# --- the order of the records (requirements 5.13 and 5.14) -------------------------------


@pytest.mark.req("10.6")
def test_without_a_current_user_a_context_user_path_is_null(tmp_path: Path) -> None:
    with engine_env(tmp_path / "sandbox", current_user_id=None) as env:
        create_order(
            env,
            vendor=MAIN_VENDOR,
            biz_date=FIRST_DAY,
            status="approved",
            lines=[order_line(env, BOLT, "10")],
        )

        # No current user leaves every context.user path null, so the comparison is
        # unknown and selects nothing (requirement 10.6).
        assert selected(env, line_query(filter_text="context.user.duty == 'buyer'")) == []
        assert len(selected(env, line_query(filter_text="context.today == header.biz_date"))) == 1


@pytest.mark.req("10.6")
def test_a_path_through_an_empty_reference_is_null(env: EngineEnv) -> None:
    # One draft order whose line names no article: a required field is checked at submit,
    # not at create (R-GEN-002, requirement 7.5).
    create_order(
        env,
        vendor=MAIN_VENDOR,
        biz_date=FIRST_DAY,
        lines=[{"qty": Decimal("2"), "price": Decimal("10")}],
    )

    # The ref has no record to read, so a path through it is null and the comparison is
    # unknown either way round, which selects nothing (requirement 10.6).
    assert selected(env, line_query(filter_text="line.article.grade == 'fine'")) == []
    assert selected(env, line_query(filter_text="line.article.grade != 'fine'")) == []
    # Without a filter the line is selected like any other.
    assert len(selected(env, line_query())) == 1


@pytest.mark.req("5.13")
def test_a_path_with_no_record_to_read_is_null(env: EngineEnv, orders: dict[str, str]) -> None:
    # A query over bill headers has no line to read, so a line. path of its returns is
    # null; only its filter is kept from naming one, by loading step 5 (requirement 10.10).
    query = line_query(level="header", latest=(FieldRef("line", "price"),))
    source = resolve_source(env.domain, query)
    assert source is not None

    with env.store.transaction() as uow:
        view = run_query(
            uow,
            env.domain,
            query,
            FrozenMap(),
            anchor_date=env.anchor_date,
            current_user_id=env.current_user_id,
        )

    assert values_of(view.latest) == {"price": None}


@pytest.mark.req("5.13", "5.14")
def test_the_records_are_sorted_by_the_declared_keys_then_by_the_source_id(
    env: EngineEnv, orders: dict[str, str]
) -> None:
    by_day = line_query(
        sort=(
            SortRef(FieldRef("header", "biz_date"), descending=True),
            SortRef(FieldRef("header", "number"), descending=True),
        )
    )

    # The business dates put the later orders first; the two orders of the second day are
    # ordered by their numbers, the second one first, and the two lines of the first order
    # share every sort key, so their ids decide (requirements 5.13 and 5.14).
    assert selected(env, by_day) == [
        f"{orders['draft']}-1",
        f"{orders['other']}-1",
        f"{orders['closed']}-1",
        f"{orders['approved']}-1",
        f"{orders['approved']}-2",
    ]
    # Without a sort key the ids alone decide (requirement 5.13).
    assert selected(env, line_query()) == sorted(selected(env, line_query()))


@pytest.mark.req("5.13")
def test_history_returns_at_most_the_declared_limit_and_latest_is_the_first(
    env: EngineEnv,
) -> None:
    order = create_order(
        env,
        vendor=MAIN_VENDOR,
        biz_date=FIRST_DAY,
        status="approved",
        lines=[order_line(env, BOLT, str(price)) for price in range(1, 8)],
    )

    view = run(env, PRICE_QUERY, price_args(env))

    # Seven lines of one order match; they share every sort key, so their ids decide and
    # the history keeps the first five of them, the first one included (requirement 5.13).
    assert values_of(view.latest)["price"] == Decimal("1")
    assert [values_of(item)["price"] for item in view.history] == [Decimal(n) for n in range(1, 6)]
    assert len(view.history) == env.domain.queries[PRICE_QUERY].history_limit == 5
    assert order.startswith("so_")


# --- what a query returns (requirement 5.13) ---------------------------------------------


@pytest.mark.req("5.13")
def test_latest_and_history_carry_the_fields_their_paths_name(
    env: EngineEnv, orders: dict[str, str]
) -> None:
    view = run(env, PRICE_QUERY, price_args(env))

    # latest: the price of the line, then three fields of its header; history: the price
    # and two of them. The keys are those of the outputSchema of the tool.
    assert view.latest is not None
    assert tuple(view.latest.fields) == ("price", "price_includes_tax", "biz_date", "number")
    assert tuple(view.latest.fields) == query_keys(env.domain, PRICE_QUERY, "latest")
    assert tuple(view.history[0].fields) == ("price", "biz_date", "number")
    assert tuple(view.history[0].fields) == query_keys(env.domain, PRICE_QUERY, "history")
    assert values_of(view.latest) == {
        "price": Decimal("12"),
        "price_includes_tax": False,
        "biz_date": SECOND_DAY,
        "number": "SPO202610100001",
    }


@pytest.mark.req("5.13")
def test_a_field_of_both_levels_with_one_name_is_returned_once(env: EngineEnv) -> None:
    source = resolve_source(env.domain, env.domain.queries[PRICE_QUERY])
    assert source is not None

    fields = result_fields(
        source,
        (
            FieldRef("line", "qty_base"),
            FieldRef("header", "biz_date"),
            FieldRef("line", "qty_base"),
            FieldRef("header", "no_such_field"),
        ),
    )

    # The repeated path is returned once, at the place it first appeared, and a path that
    # names no field of its level is left out.
    assert [(found.level, found.field.name) for found in fields] == [
        ("line", "qty_base"),
        ("header", "biz_date"),
    ]


@pytest.mark.req("5.13")
def test_a_query_over_an_entity_returns_the_fields_of_its_records(env: EngineEnv) -> None:
    source = resolve_source(env.domain, line_query(source="vendor", level="record"))
    assert source is not None

    fields = result_fields(source, (FieldRef("record", "code"), FieldRef("line", "qty")))

    # An entity has the one level its records are: a ``record.`` path names its own fields,
    # and a path of a level it does not have - an entity has no lines - is left out.
    assert (source.bill, source.table) == (None, "vendor")
    assert [(found.level, found.field.name) for found in fields] == [("record", "code")]


@pytest.mark.req("5.13")
def test_a_query_without_a_match_returns_no_latest_and_no_history(
    env: EngineEnv, orders: dict[str, str]
) -> None:
    view = run(env, PRICE_QUERY, price_args(env, article=BOLT, vendor=SPARE_VENDOR))
    empty = run(
        env,
        PRICE_QUERY,
        {"vendor_id": env.id(SPARE_VENDOR), "article_id": env.id(WASHER)},
    )

    assert view.latest is not None
    assert (empty.latest, empty.history) == (None, ())


@pytest.mark.req("5.13")
def test_a_query_that_declares_no_history_returns_an_empty_one(
    env: EngineEnv, orders: dict[str, str]
) -> None:
    query = line_query(latest=(FieldRef("line", "price"),))
    source = resolve_source(env.domain, query)
    assert source is not None

    with env.store.transaction() as uow:
        view = run_query(
            uow,
            env.domain,
            query,
            FrozenMap(),
            filter_tree=None,
            anchor_date=env.anchor_date,
            current_user_id=env.current_user_id,
        )

    assert view.latest is not None
    assert view.history == ()


# --- the filter trees and the primitive query --------------------------------------------


@pytest.mark.req("10.2")
def test_every_declared_filter_is_parsed_once_per_instance(env: EngineEnv) -> None:
    filters = query_filters(env.domain)

    # The declared query has a filter; the primitive query declares none.
    assert tuple(filters) == (PRICE_QUERY,)
    assert env.engine.filters == filters
    # The tree the engine holds is the tree of the declared text.
    assert filters[PRICE_QUERY] == query_filter(env.domain.queries[PRICE_QUERY])


@pytest.mark.req("9.7")
def test_a_query_a_primitive_provides_is_not_run_as_a_declarative_one(env: EngineEnv) -> None:
    # The ``view`` slot sends it to the inventory primitive instead
    # (tests/unit/domain/test_inventory.py); this module performs the declared queries.
    with env.store.transaction() as uow, pytest.raises(NotImplementedError, match="balance"):
        run_query(
            uow,
            env.domain,
            env.domain.queries[BALANCE_QUERY],
            FrozenMap(),
            anchor_date=env.anchor_date,
        )


@pytest.mark.req("9.7")
def test_a_query_with_no_source_the_instance_declares_has_none(env: EngineEnv) -> None:
    # The three shapes that resolve to no source: a primitive query, which names none, and
    # a declared source loading step 5 could not resolve - a name the instance does not
    # have, and an entity read at a level its records do not have (requirement 2.9).
    assert resolve_source(env.domain, env.domain.queries[BALANCE_QUERY]) is None
    assert resolve_source(env.domain, line_query(source="fx_nothing")) is None
    assert resolve_source(env.domain, line_query(source="vendor", level="line")) is None

    # Such a query is never exposed as a tool, so running one is a programming error.
    with env.store.transaction() as uow, pytest.raises(RuntimeError, match="no source"):
        run_query(
            uow,
            env.domain,
            line_query(source="fx_nothing"),
            FrozenMap(),
            anchor_date=env.anchor_date,
        )


# --- the reference parameters of a query (requirement 5.6) -------------------------------


@pytest.mark.req("5.6")
def test_a_reference_parameter_that_names_no_record_is_not_found(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        run(env, PRICE_QUERY, {"vendor_id": "ven_000009", "article_id": "art_000009"})

    error = raised.value
    assert error.code is ErrorCode.NOT_FOUND
    assert error.rule_id is None
    # One item per parameter that names no record, in parameter order (requirement 5.6).
    assert [dict(item) for item in error.details] == [
        {"field": "vendor_id", "reason": "not_found"},
        {"field": "article_id", "reason": "not_found"},
    ]


@pytest.mark.req("5.6")
def test_a_query_makes_no_writes(env: EngineEnv, orders: dict[str, str]) -> None:
    result = env.run(QueryOp(PRICE_QUERY, FrozenMap(price_args(env))), call_seq=4)

    assert result.writes == ()
