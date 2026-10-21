"""erp_mock.domain.inventory: the stock an approve moves and the stock on hand (req 9).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. The bills are created, submitted and
approved the same way, so a linkage runs exactly as a tools/call runs it - one operation,
one transaction - and the stock the tests read is the stock the primitive wrote.

The fixture has one bill per direction, each naming its warehouse at a different level, so
a primitive that worked only on the names of the example pack would fail these tests
(requirement 9.11): ``goods_entry`` is ``in`` with the warehouse on the line,
``issue_note`` is ``out`` with it on the header, ``stock_move`` is ``transfer`` between two
header fields, and ``count_sheet`` is ``adjust`` by the enum field ``count_kind`` of its
header. What the tests pin:

- one transaction per line for in, out and adjust and two per line for a transfer, each
  carrying the material of the line, the warehouse of the linkage's field, the base unit
  quantity with the sign of the direction, the bill type and the source line
  (requirements 9.1 and 9.2);
- the id of a transaction is ``<source line id>:<warehouse id>:<direction>``, so an
  unapprove followed by an approve of the unchanged bill writes the ids it removed
  (requirement 18.8);
- R-GEN-007 is decided on the net change of each (material, warehouse): the balance before
  the operation plus the sum of the moves of the whole bill, with the details ordered by
  material id and then warehouse id, and a violation rolls the whole operation back
  (requirements 9.4, 9.5 and 7.10);
- an unapprove removes exactly the transactions of its approve and takes their quantities
  off the balances; a (material, warehouse) without a transaction left loses its balance
  record and one with a balance of 0 that still has transactions keeps it
  (requirements 9.6 and 9.10);
- the stock balance query returns one item per pair that has a record, filtered by the two
  optional parameters, ordered by material and then warehouse and paged, with the keys the
  outputSchema of its tool declares (requirements 9.7 and 9.12).
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import DomainModel
from erp_mock.domain.engine import OpResult, QueryOp, View
from erp_mock.domain.inventory import (
    NEGATIVE_STOCK_REASON,
    NEGATIVE_STOCK_RULE,
    StockMove,
    balance_id,
    check_negative_stock,
    linkage_moves,
    linkages_of,
    material_entity,
    net_changes,
    run_balance_query,
    stock_balance,
    txn_id,
    warehouse_entity,
)
from erp_mock.domain.state_machine import APPROVED, STATUS, SUBMITTED, StateAction
from erp_mock.domain.views import PageView
from erp_mock.state.schema import (
    ID_COLUMN,
    MATERIAL_COLUMN,
    QTY_BASE_COLUMN,
    QTY_DELTA_COLUMN,
    SOURCE_BILL_TYPE_COLUMN,
    SOURCE_LINE_COLUMN,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    WAREHOUSE_COLUMN,
)
from erp_mock.state.store import Record, Value
from erp_mock.tools.output_schema import query_output
from tests.helpers.engine_env import (
    BOLT,
    BOX,
    BOX_FACTOR,
    MAIN_DEPOT,
    MAIN_VENDOR,
    SPARE_DEPOT,
    WASHER,
    EngineEnv,
    as_page,
    as_record,
    as_summary,
    engine_env,
)

# The four bills of the fixture that move stock, one per direction, and the one that moves
# none (tests/fixtures/packs/fx_trade).
ENTRY: Final = "goods_entry"
ISSUE: Final = "issue_note"
MOVE: Final = "stock_move"
COUNT: Final = "count_sheet"
ORDER: Final = "supply_order"
NOTE: Final = "fee_note"

# The linkage of each of them, by id (tests/fixtures/packs/fx_trade/linkages).
ENTRY_LINKAGE: Final = "goods_entry_inventory"
ISSUE_LINKAGE: Final = "issue_note_inventory"
MOVE_LINKAGE: Final = "stock_move_inventory"
COUNT_LINKAGE: Final = "count_sheet_inventory"

# The query tool the inventory primitive provides (requirement 9.7).
BALANCE_QUERY: Final = "stock_level_query"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


# --- building the bills of the four directions -------------------------------------------


def order_line(env: EngineEnv, article: str, qty: str, price: str) -> dict[str, Value]:
    """One line of a supply order, the pricing bill that declares no linkage (req 9.1)."""
    return {"article_id": env.id(article), "qty": Decimal(qty), "price": Decimal(price)}


def entry_line(
    env: EngineEnv, article: str, qty: str, depot: str, **values: Value
) -> dict[str, Value]:
    """One line of a goods entry: the warehouse is on the line (requirement 9.11)."""
    row: dict[str, Value] = {
        "article_id": env.id(article),
        "depot_id": env.id(depot),
        "entry_qty": Decimal(qty),
    }
    row.update(values)
    return row


def new_entry(env: EngineEnv, lines: Sequence[Mapping[str, Value]]) -> str:
    """Create a goods entry with ``lines`` and return its id."""
    header: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR)}
    return record_id(env.create(ENTRY, header, [dict(row) for row in lines]))


def new_issue(env: EngineEnv, depot: str, lines: Sequence[Mapping[str, Value]]) -> str:
    """Create an issue note out of ``depot``; its warehouse is on the header."""
    header: dict[str, Value] = {"depot_id": env.id(depot)}
    return record_id(env.create(ISSUE, header, [dict(row) for row in lines]))


def issue_line(env: EngineEnv, article: str, qty: str, **values: Value) -> dict[str, Value]:
    """One line of an issue note."""
    row: dict[str, Value] = {"article_id": env.id(article), "issue_qty": Decimal(qty)}
    row.update(values)
    return row


def new_move(env: EngineEnv, source: str, target: str, lines: Sequence[Mapping[str, Value]]) -> str:
    """Create a stock move from ``source`` to ``target``; both warehouses are on the header."""
    header: dict[str, Value] = {"from_depot_id": env.id(source), "to_depot_id": env.id(target)}
    return record_id(env.create(MOVE, header, [dict(row) for row in lines]))


def move_line(env: EngineEnv, article: str, qty: str, **values: Value) -> dict[str, Value]:
    """One line of a stock move."""
    row: dict[str, Value] = {"article_id": env.id(article), "move_qty": Decimal(qty)}
    row.update(values)
    return row


def new_count(env: EngineEnv, depot: str, kind: str, lines: Sequence[Mapping[str, Value]]) -> str:
    """Create a count sheet of ``depot`` whose ``count_kind`` decides the direction (9.2)."""
    header: dict[str, Value] = {"depot_id": env.id(depot), "count_kind": kind}
    return record_id(env.create(COUNT, header, [dict(row) for row in lines]))


def count_line(env: EngineEnv, article: str, qty: str, **values: Value) -> dict[str, Value]:
    """One line of a count sheet."""
    row: dict[str, Value] = {"article_id": env.id(article), "diff_qty": Decimal(qty)}
    row.update(values)
    return row


def record_id(result: OpResult) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    return str(as_record(result.view).fields[ID_COLUMN])


def approve(env: EngineEnv, bill: str, bill_id: str) -> None:
    """Submit and approve the standard bill ``bill_id``, which moves its stock (req 7.6)."""
    env.transition(bill, bill_id, "submit")
    env.transition(bill, bill_id, "approve")


def status_of(env: EngineEnv, bill: str, bill_id: str) -> Value:
    """The state the bill ``bill_id`` is in, as storage holds it (requirement 7.1)."""
    record = env.record(bill, bill_id)
    assert record is not None, f"{bill} {bill_id} is not in the sandbox"
    return record[STATUS]


# --- reading the two stock tables --------------------------------------------------------


def txns(env: EngineEnv) -> list[Record]:
    """Every stock transaction of the sandbox, by id (design.md data model 3)."""
    return env.records(STOCK_TXN_TABLE)


def txn_rows(env: EngineEnv) -> list[dict[str, Value]]:
    """Every stock transaction as a plain dict, by id."""
    return [dict(record) for record in txns(env)]


def txn_ids(env: EngineEnv) -> list[str]:
    """The ids of every stock transaction, in id order (requirement 18.8)."""
    return [str(record[ID_COLUMN]) for record in txns(env)]


def balances(env: EngineEnv) -> dict[str, Decimal]:
    """The quantity on hand of every (material, warehouse) that has a record (req 9.10)."""
    found: dict[str, Decimal] = {}
    for record in env.records(STOCK_BALANCE_TABLE):
        quantity = record[QTY_BASE_COLUMN]
        assert isinstance(quantity, Decimal)
        found[str(record[ID_COLUMN])] = quantity
    return found


def place(env: EngineEnv, article: str, depot: str) -> str:
    """The id of the balance record of one (material, warehouse) (design.md 3)."""
    return balance_id(env.id(article), env.id(depot))


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


def refused(env: EngineEnv, bill: str, bill_id: str, action: StateAction) -> ErpError:
    """Run ``action`` on ``bill_id`` and return the error it ended in."""
    with pytest.raises(ErpError) as raised:
        env.transition(bill, bill_id, action)
    return raised.value


# --- the four directions (requirements 9.1 and 9.2) --------------------------------------


@pytest.mark.req("9.1", "9.2", "18.8")
def test_an_in_linkage_writes_one_transaction_per_line(env: EngineEnv) -> None:
    # The bolt is received by the box, which is 12 base units (tests.helpers.engine_env).
    entry = new_entry(
        env,
        [
            entry_line(env, BOLT, "10", MAIN_DEPOT, measure_id=env.id(BOX)),
            entry_line(env, WASHER, "4", SPARE_DEPOT),
        ],
    )

    approve(env, ENTRY, entry)

    # One transaction per line, each at +qty_base of its own line and warehouse
    # (requirements 9.1 and 9.2); the id carries the source line, the warehouse and the
    # direction (requirement 18.8).
    assert txn_rows(env) == [
        {
            ID_COLUMN: txn_id(f"{entry}-1", env.id(MAIN_DEPOT), "in"),
            MATERIAL_COLUMN: env.id(BOLT),
            WAREHOUSE_COLUMN: env.id(MAIN_DEPOT),
            QTY_DELTA_COLUMN: Decimal("10") * BOX_FACTOR,
            SOURCE_BILL_TYPE_COLUMN: ENTRY,
            SOURCE_LINE_COLUMN: f"{entry}-1",
        },
        {
            ID_COLUMN: txn_id(f"{entry}-2", env.id(SPARE_DEPOT), "in"),
            MATERIAL_COLUMN: env.id(WASHER),
            WAREHOUSE_COLUMN: env.id(SPARE_DEPOT),
            QTY_DELTA_COLUMN: Decimal("4"),
            SOURCE_BILL_TYPE_COLUMN: ENTRY,
            SOURCE_LINE_COLUMN: f"{entry}-2",
        },
    ]
    assert balances(env) == {
        place(env, BOLT, MAIN_DEPOT): Decimal("120"),
        place(env, WASHER, SPARE_DEPOT): Decimal("4"),
    }


@pytest.mark.req("9.1", "9.2")
def test_an_out_linkage_takes_the_base_quantity_off_the_warehouse_of_the_header(
    env: EngineEnv,
) -> None:
    entry = new_entry(env, [entry_line(env, BOLT, "10", MAIN_DEPOT, measure_id=env.id(BOX))])
    approve(env, ENTRY, entry)
    issue = new_issue(env, MAIN_DEPOT, [issue_line(env, BOLT, "20")])

    approve(env, ISSUE, issue)

    out = txn_id(f"{issue}-1", env.id(MAIN_DEPOT), "out")
    stored = {str(record[ID_COLUMN]): record[QTY_DELTA_COLUMN] for record in txns(env)}
    assert stored[out] == Decimal("-20")
    assert balances(env) == {place(env, BOLT, MAIN_DEPOT): Decimal("100")}


@pytest.mark.req("9.1", "9.2", "18.8")
def test_a_transfer_linkage_writes_one_transaction_per_warehouse(env: EngineEnv) -> None:
    entry = new_entry(env, [entry_line(env, BOLT, "10", MAIN_DEPOT, measure_id=env.id(BOX))])
    approve(env, ENTRY, entry)
    move = new_move(env, MAIN_DEPOT, SPARE_DEPOT, [move_line(env, BOLT, "30")])

    approve(env, MOVE, move)

    # Two transactions for the one line: the warehouse the stock leaves at -qty_base and
    # the one it arrives in at +qty_base (requirements 9.1 and 9.2).
    moved = {
        str(record[ID_COLUMN]): record[QTY_DELTA_COLUMN]
        for record in txns(env)
        if record[SOURCE_BILL_TYPE_COLUMN] == MOVE
    }
    assert moved == {
        txn_id(f"{move}-1", env.id(MAIN_DEPOT), "out"): Decimal("-30"),
        txn_id(f"{move}-1", env.id(SPARE_DEPOT), "in"): Decimal("30"),
    }
    assert balances(env) == {
        place(env, BOLT, MAIN_DEPOT): Decimal("90"),
        place(env, BOLT, SPARE_DEPOT): Decimal("30"),
    }


@pytest.mark.req("9.2")
def test_an_adjust_linkage_takes_its_direction_from_the_enum_field_of_the_header(
    env: EngineEnv,
) -> None:
    gain = new_count(env, MAIN_DEPOT, "gain", [count_line(env, BOLT, "7")])
    approve(env, COUNT, gain)
    loss = new_count(env, MAIN_DEPOT, "loss", [count_line(env, BOLT, "5")])

    approve(env, COUNT, loss)

    # The values the linkage lists under increase take +qty_base and those under decrease
    # -qty_base, which the direction of the transaction id follows as well (18.8).
    stored = {str(record[ID_COLUMN]): record[QTY_DELTA_COLUMN] for record in txns(env)}
    assert stored == {
        txn_id(f"{gain}-1", env.id(MAIN_DEPOT), "in"): Decimal("7"),
        txn_id(f"{loss}-1", env.id(MAIN_DEPOT), "out"): Decimal("-5"),
    }
    assert balances(env) == {place(env, BOLT, MAIN_DEPOT): Decimal("2")}


@pytest.mark.req("9.1")
def test_a_bill_without_an_inventory_linkage_moves_no_stock(env: EngineEnv) -> None:
    order = record_id(
        env.create(
            ORDER,
            {"vendor_id": env.id(MAIN_VENDOR), "need_date": env.anchor_date},
            [order_line(env, BOLT, "100", "0.85")],
        )
    )

    approve(env, ORDER, order)

    assert linkages_of(env.domain, ORDER) == ()
    assert txns(env) == []
    assert balances(env) == {}


@pytest.mark.req("9.1")
def test_every_linkage_of_the_fixture_is_found_by_its_bill(env: EngineEnv) -> None:
    found = {
        bill: tuple(linkage.id for linkage in linkages_of(env.domain, bill))
        for bill in (ENTRY, ISSUE, MOVE, COUNT)
    }

    assert found == {
        ENTRY: (ENTRY_LINKAGE,),
        ISSUE: (ISSUE_LINKAGE,),
        MOVE: (MOVE_LINKAGE,),
        COUNT: (COUNT_LINKAGE,),
    }


# --- R-GEN-007 (requirements 9.4 and 9.5) ------------------------------------------------


@pytest.mark.req("9.4", "9.5")
def test_an_approve_that_leaves_the_stock_at_zero_goes_through(env: EngineEnv) -> None:
    entry = new_entry(env, [entry_line(env, BOLT, "100", MAIN_DEPOT)])
    approve(env, ENTRY, entry)
    # Two lines of 50 on the same (material, warehouse): the net change is exactly -100,
    # which is not below 0, so the rule holds (requirement 9.5).
    issue = new_issue(env, MAIN_DEPOT, [issue_line(env, BOLT, "50"), issue_line(env, BOLT, "50")])

    approve(env, ISSUE, issue)

    assert status_of(env, ISSUE, issue) == APPROVED
    # The pair keeps its balance record at 0, because it still has transactions (9.10).
    assert balances(env) == {place(env, BOLT, MAIN_DEPOT): Decimal("0")}


@pytest.mark.req("9.4", "9.5", "7.10")
def test_the_net_change_of_the_whole_bill_decides_and_a_violation_rolls_back(
    env: EngineEnv,
) -> None:
    entry = new_entry(env, [entry_line(env, BOLT, "100", MAIN_DEPOT)])
    approve(env, ENTRY, entry)
    before = (txn_rows(env), balances(env))
    # Each line on its own would leave 40 on hand; together they take 120 off 100, and the
    # sum is what decides (requirement 9.5).
    issue = new_issue(env, MAIN_DEPOT, [issue_line(env, BOLT, "60"), issue_line(env, BOLT, "60")])
    env.transition(ISSUE, issue, "submit")

    error = refused(env, ISSUE, issue, "approve")

    assert (error.code, error.rule_id) == (ErrorCode.BUSINESS_RULE_VIOLATION, NEGATIVE_STOCK_RULE)
    # One item per pair that would go below 0, with the quantity on hand before the
    # operation and no input field (requirement 9.4).
    assert details(error) == [
        {
            "reason": NEGATIVE_STOCK_REASON,
            MATERIAL_COLUMN: env.id(BOLT),
            WAREHOUSE_COLUMN: env.id(MAIN_DEPOT),
            QTY_BASE_COLUMN: "100",
        }
    ]
    # The whole operation rolled back: the state of the bill, the transactions and the
    # balance are the ones the call found (requirement 7.10).
    assert status_of(env, ISSUE, issue) == SUBMITTED
    assert (txn_rows(env), balances(env)) == before


@pytest.mark.req("9.4")
def test_the_details_are_ordered_by_material_then_warehouse(env: EngineEnv) -> None:
    # The washer is in stock, the bolt is not, and the issue note takes both out of the
    # same warehouse: only the pairs that go below 0 are reported, by material id.
    entry = new_entry(env, [entry_line(env, WASHER, "1", MAIN_DEPOT)])
    approve(env, ENTRY, entry)
    issue = new_issue(env, MAIN_DEPOT, [issue_line(env, WASHER, "4"), issue_line(env, BOLT, "2")])
    env.transition(ISSUE, issue, "submit")

    error = refused(env, ISSUE, issue, "approve")

    assert [(item[MATERIAL_COLUMN], item[QTY_BASE_COLUMN]) for item in details(error)] == [
        (env.id(BOLT), "0"),
        (env.id(WASHER), "1"),
    ]


@pytest.mark.req("9.5")
def test_an_intermediate_quantity_below_zero_is_no_violation(env: EngineEnv) -> None:
    # The moves of one operation on one (material, warehouse), the first of which would
    # take the stock below 0 on its own; only the net change decides (requirement 9.5).
    moves = (
        stock_move(env, BOLT, MAIN_DEPOT, "-30", "1"),
        stock_move(env, BOLT, MAIN_DEPOT, "50", "2"),
    )

    with env.store.transaction() as uow:
        changes = net_changes(uow, moves)

    assert [(change.before, change.delta, change.after) for change in changes] == [
        (Decimal("0"), Decimal("20"), Decimal("20"))
    ]
    # Nothing to report: the sum of the moves keeps the pair at or above 0.
    check_negative_stock(env.domain, changes)


@pytest.mark.req("9.4", "9.5")
def test_net_changes_are_ordered_by_material_then_warehouse(env: EngineEnv) -> None:
    moves = (
        stock_move(env, WASHER, MAIN_DEPOT, "-1", "1"),
        stock_move(env, BOLT, SPARE_DEPOT, "-2", "2"),
        stock_move(env, BOLT, MAIN_DEPOT, "-3", "3"),
    )

    with env.store.transaction() as uow:
        changes = net_changes(uow, moves)

    assert [change.place for change in changes] == [
        (env.id(BOLT), env.id(MAIN_DEPOT)),
        (env.id(BOLT), env.id(SPARE_DEPOT)),
        (env.id(WASHER), env.id(MAIN_DEPOT)),
    ]
    with pytest.raises(ErpError) as raised:
        check_negative_stock(env.domain, changes)
    assert [item[WAREHOUSE_COLUMN] for item in details(raised.value)] == [
        env.id(MAIN_DEPOT),
        env.id(SPARE_DEPOT),
        env.id(MAIN_DEPOT),
    ]


def stock_move(env: EngineEnv, article: str, depot: str, qty: str, line_no: str) -> StockMove:
    """One move of the primitive, built by hand to test the net change itself (req 9.5)."""
    return StockMove(
        id=txn_id(f"ge_999999-{line_no}", env.id(depot), "in"),
        material_id=env.id(article),
        warehouse_id=env.id(depot),
        qty_delta=Decimal(qty),
        source_bill_type=ENTRY,
        source_line_id=f"ge_999999-{line_no}",
        linkage=ENTRY_LINKAGE,
    )


# --- the reversal of an unapprove (requirements 9.6 and 9.10) ----------------------------


@pytest.mark.req("9.6", "9.10", "18.8")
def test_an_unapprove_removes_the_transactions_and_the_balance_record(env: EngineEnv) -> None:
    entry = new_entry(
        env,
        [
            entry_line(env, BOLT, "10", MAIN_DEPOT, measure_id=env.id(BOX)),
            entry_line(env, WASHER, "4", SPARE_DEPOT),
        ],
    )
    approve(env, ENTRY, entry)
    written = txn_rows(env)

    env.transition(ENTRY, entry, "unapprove")

    # Every transaction of the approve is gone and no pair is left with a balance record,
    # since none of them has a transaction any more (requirements 9.6 and 9.10).
    assert txns(env) == []
    assert balances(env) == {}
    # Approving the unchanged bill again writes exactly the transactions it removed, the
    # ids included (requirement 18.8).
    approve(env, ENTRY, entry)
    assert txn_rows(env) == written


@pytest.mark.req("9.6", "9.10")
def test_a_balance_record_stays_while_the_pair_has_a_transaction_left(env: EngineEnv) -> None:
    first = new_entry(env, [entry_line(env, BOLT, "30", MAIN_DEPOT)])
    approve(env, ENTRY, first)
    second = new_entry(env, [entry_line(env, BOLT, "12", MAIN_DEPOT)])
    approve(env, ENTRY, second)

    env.transition(ENTRY, second, "unapprove")

    # The quantity of the removed transaction is off the balance and the record stays, the
    # pair still having the transaction of the first entry (requirements 9.6 and 9.10).
    assert txn_ids(env) == [txn_id(f"{first}-1", env.id(MAIN_DEPOT), "in")]
    assert balances(env) == {place(env, BOLT, MAIN_DEPOT): Decimal("30")}

    env.transition(ENTRY, first, "unapprove")

    assert txns(env) == []
    assert balances(env) == {}


@pytest.mark.req("9.4", "9.6", "7.10")
def test_an_unapprove_that_would_make_the_stock_negative_is_refused(env: EngineEnv) -> None:
    entry = new_entry(env, [entry_line(env, BOLT, "40", MAIN_DEPOT)])
    approve(env, ENTRY, entry)
    issue = new_issue(env, MAIN_DEPOT, [issue_line(env, BOLT, "40")])
    approve(env, ISSUE, issue)
    before = (txn_rows(env), balances(env))

    error = refused(env, ENTRY, entry, "unapprove")

    assert (error.code, error.rule_id) == (ErrorCode.BUSINESS_RULE_VIOLATION, NEGATIVE_STOCK_RULE)
    assert details(error) == [
        {
            "reason": NEGATIVE_STOCK_REASON,
            MATERIAL_COLUMN: env.id(BOLT),
            WAREHOUSE_COLUMN: env.id(MAIN_DEPOT),
            QTY_BASE_COLUMN: "0",
        }
    ]
    # Nothing moved: the bill is still approved and the stock is as it was (7.10).
    assert status_of(env, ENTRY, entry) == APPROVED
    assert (txn_rows(env), balances(env)) == before


@pytest.mark.req("9.6")
def test_an_unapprove_removes_the_transactions_of_its_own_bill_alone(env: EngineEnv) -> None:
    first = new_entry(env, [entry_line(env, BOLT, "5", MAIN_DEPOT)])
    approve(env, ENTRY, first)
    issue = new_issue(env, MAIN_DEPOT, [issue_line(env, BOLT, "2")])
    approve(env, ISSUE, issue)

    env.transition(ISSUE, issue, "unapprove")

    assert txn_ids(env) == [txn_id(f"{first}-1", env.id(MAIN_DEPOT), "in")]
    assert balances(env) == {place(env, BOLT, MAIN_DEPOT): Decimal("5")}


@pytest.mark.req("9.5")
def test_the_quantity_on_hand_of_a_pair_without_a_record_is_zero(env: EngineEnv) -> None:
    with env.store.transaction() as uow:
        assert stock_balance(uow, env.id(BOLT), env.id(MAIN_DEPOT)) == Decimal("0")


# --- the stock balance query (requirements 9.7 and 9.12) ---------------------------------


@pytest.mark.req("9.7")
def test_the_balance_query_returns_the_two_references_the_base_unit_and_the_quantity(
    env: EngineEnv,
) -> None:
    entry = new_entry(env, [entry_line(env, BOLT, "10", MAIN_DEPOT, measure_id=env.id(BOX))])
    approve(env, ENTRY, entry)

    page = as_page(run_balance(env))

    assert len(page.items) == 1
    item = page.items[0]
    # The keys of the item are those the outputSchema of the tool declares, in its order
    # (requirement 13.1): the two references without their ``_id``, the base unit field of
    # the material entity and the quantity on hand.
    assert tuple(item.fields) == result_keys(env)
    assert dict(as_summary(item.fields["material"]).fields) == {
        "id": env.id(BOLT),
        "code": "A00001",
        "name": "夹具用六角螺栓",
    }
    assert as_summary(item.fields["warehouse"]).fields["id"] == env.id(MAIN_DEPOT)
    # The quantity is in the base unit of the material, not in the unit of the line (9.7).
    assert as_summary(item.fields["base_measure"]).fields["code"] == "PCS"
    assert item.fields[QTY_BASE_COLUMN] == Decimal("10") * BOX_FACTOR


def result_keys(env: EngineEnv) -> tuple[str, ...]:
    """The keys the outputSchema of the balance query declares for one item (req 13.1)."""
    schema = query_output(env.domain, env.domain.queries[BALANCE_QUERY]).schema
    properties = schema["properties"]
    assert isinstance(properties, FrozenMap)
    items = properties["items"]
    assert isinstance(items, FrozenMap)
    item = items["items"]
    assert isinstance(item, FrozenMap)
    keys = item["properties"]
    assert isinstance(keys, FrozenMap)
    return tuple(keys)


@pytest.mark.req("9.7", "9.10")
def test_the_balance_query_returns_a_pair_whose_quantity_is_zero(env: EngineEnv) -> None:
    entry = new_entry(env, [entry_line(env, BOLT, "6", MAIN_DEPOT)])
    approve(env, ENTRY, entry)
    issue = new_issue(env, MAIN_DEPOT, [issue_line(env, BOLT, "6")])
    approve(env, ISSUE, issue)

    page = as_page(run_balance(env))

    assert [item.fields[QTY_BASE_COLUMN] for item in page.items] == [Decimal("0")]


@pytest.mark.req("9.7")
def test_the_balance_query_filters_by_material_and_by_warehouse(env: EngineEnv) -> None:
    entry = new_entry(
        env,
        [
            entry_line(env, BOLT, "1", MAIN_DEPOT),
            entry_line(env, BOLT, "2", SPARE_DEPOT),
            entry_line(env, WASHER, "3", MAIN_DEPOT),
        ],
    )
    approve(env, ENTRY, entry)

    by_material = as_page(run_balance(env, {MATERIAL_COLUMN: env.id(BOLT)}))
    by_warehouse = as_page(run_balance(env, {WAREHOUSE_COLUMN: env.id(MAIN_DEPOT)}))
    by_both = as_page(
        run_balance(env, {MATERIAL_COLUMN: env.id(BOLT), WAREHOUSE_COLUMN: env.id(SPARE_DEPOT)})
    )

    assert quantities(by_material) == [Decimal("1"), Decimal("2")]
    assert quantities(by_warehouse) == [Decimal("1"), Decimal("3")]
    # Both parameters have to be satisfied at once (requirement 9.7).
    assert quantities(by_both) == [Decimal("2")]
    assert (by_both.total, by_both.page, by_both.page_size, by_both.has_more) == (1, 1, 20, False)


@pytest.mark.req("9.12")
def test_the_balance_query_orders_by_material_then_warehouse_and_pages(env: EngineEnv) -> None:
    entry = new_entry(
        env,
        [
            entry_line(env, WASHER, "4", MAIN_DEPOT),
            entry_line(env, BOLT, "2", SPARE_DEPOT),
            entry_line(env, BOLT, "1", MAIN_DEPOT),
        ],
    )
    approve(env, ENTRY, entry)

    first = as_page(run_balance(env, {"page_size": 2}))
    second = as_page(run_balance(env, {"page": 2, "page_size": 2}))
    third = as_page(run_balance(env, {"page": 3, "page_size": 2}))

    # By material id and then warehouse id, whatever order the lines were written in
    # (requirement 9.12).
    assert quantities(first) == [Decimal("1"), Decimal("2")]
    assert (first.total, first.page, first.page_size, first.has_more) == (3, 1, 2, True)
    assert quantities(second) == [Decimal("4")]
    assert second.has_more is False
    # A page past the last one is an empty page and no error (requirement 9.12).
    assert (third.items, third.total, third.has_more) == ((), 3, False)


@pytest.mark.req("9.12")
def test_the_balance_query_returns_an_empty_page_when_nothing_matches(env: EngineEnv) -> None:
    page = as_page(run_balance(env))

    assert (page.items, page.total, page.page, page.page_size, page.has_more) == (
        (),
        0,
        1,
        20,
        False,
    )


@pytest.mark.req("9.7")
def test_the_primitive_reads_the_material_and_the_warehouse_entity_from_the_configuration(
    env: EngineEnv,
) -> None:
    material = material_entity(env.domain)

    # The fixture calls them ``article`` and ``depot`` where the example pack calls them
    # ``material`` and ``warehouse`` (requirement 9.11, tests/fixtures).
    assert material is not None
    assert material.name == "article"
    assert warehouse_entity(env.domain) == "depot"
    # A transfer names no ``warehouse``, so its two warehouse fields answer instead.
    only_transfer = replace(
        env.domain, linkages=FrozenMap({MOVE_LINKAGE: env.domain.linkages[MOVE_LINKAGE]})
    )
    assert warehouse_entity(only_transfer) == "depot"
    # An instance with neither has neither; the result keeps the ids alone (13.3).
    assert (material_entity(DomainModel()), warehouse_entity(DomainModel())) == (None, None)


@pytest.mark.req("9.7")
def test_the_balance_query_refuses_what_is_no_parameter_of_it(env: EngineEnv) -> None:
    with pytest.raises(KeyError, match="depot_id"):
        run_balance(env, {"depot_id": env.id(MAIN_DEPOT)})


@pytest.mark.req("9.7")
def test_the_stock_balance_is_the_one_query_the_primitive_provides(env: EngineEnv) -> None:
    other = replace(env.domain.queries[BALANCE_QUERY], query="movements")

    with env.store.transaction() as uow, pytest.raises(RuntimeError, match="movements"):
        run_balance_query(uow, env.domain, other, FrozenMap())


@pytest.mark.req("9.3", "9.11")
def test_a_linkage_the_configuration_does_not_resolve_is_a_programming_error(
    env: EngineEnv,
) -> None:
    domain = env.domain
    entry = domain.bills[ENTRY]
    count = domain.bills[COUNT]
    header: Record = FrozenMap({"depot_id": env.id(MAIN_DEPOT), "count_kind": "gain"})
    line: Record = FrozenMap({"article_id": env.id(BOLT), QTY_BASE_COLUMN: Decimal("1")})
    empty: Record = FrozenMap()

    # A warehouse field step 5 could not resolve (requirement 9.11).
    without_warehouse = replace(domain.linkages[ENTRY_LINKAGE], warehouse=None)
    with pytest.raises(RuntimeError, match="warehouse_field"):
        linkage_moves(domain, without_warehouse, entry, empty, (line,))
    # An enum value the adjust lists under neither increase nor decrease (req 9.3).
    neither = replace(domain.linkages[COUNT_LINKAGE], increase=(), decrease=())
    with pytest.raises(RuntimeError, match="count_kind"):
        linkage_moves(domain, neither, count, header, (line,))
    # A line that names no material, which R-GEN-002 keeps a bill from leaving draft with.
    with pytest.raises(RuntimeError, match="article_id"):
        linkage_moves(domain, domain.linkages[COUNT_LINKAGE], count, header, (empty,))
    # A bill whose lines measure nothing has no material to move (requirement 9.11).
    with pytest.raises(RuntimeError, match="measure nothing"):
        linkage_moves(domain, domain.linkages[COUNT_LINKAGE], domain.bills[NOTE], header, (line,))


def run_balance(env: EngineEnv, args: Mapping[str, Value] = FrozenMap()) -> View:
    """Run the stock balance query with ``args`` and return its view (requirement 9.7)."""
    return env.run(QueryOp(BALANCE_QUERY, FrozenMap(args))).view


def quantities(page: PageView) -> list[object]:
    """The quantity on hand of each item of ``page``, in result order (requirement 9.12)."""
    return [item.fields[QTY_BASE_COLUMN] for item in page.items]
