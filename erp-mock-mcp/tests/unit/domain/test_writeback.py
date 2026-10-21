"""erp_mock.domain.writeback: what a pushed bill writes back to its source (req 8.8 to 8.14).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. The source bills are created, approved and
pushed the same way and the pushed bills are approved and unapproved through the engine, so
a writeback runs exactly as a tools/call runs it - one operation, one transaction - and what
the tests read is what the primitive wrote.

The fixture has one writeback per ``limit_by``
(tests/fixtures/packs/fx_trade/writebacks): ``entry_writeback_order`` adds the
``qty_base`` of every line of a goods entry to the ``linked_qty`` of the source line it was
pushed from and closes the source order once every line is linked, and
``pay_writeback_fee`` adds the ``amount_with_tax`` of a pay slip to the ``linked_amount``
of the fee note it came from, whose simple state machine has no closed state
(requirement 7.11). What the tests pin:

- the amounts: a ``source_line.<field>`` writeback is evaluated per line of the approved
  bill and adds the value to the line it was pushed from, a ``source.<field>`` one once to
  the source header; two lines that name one source line add up, and a bill no push
  generated writes nothing back (requirement 8.8);
- the ledger: one row per amount, carrying the bill, the writeback, the position, the
  record, the field and the amount, which an unapprove takes off again exactly and then
  removes, leaving the link field of a source nothing is linked to any more empty
  (requirement 8.9);
- the order of an approve and an unapprove: the state, the stock and the source bill in one
  call, and the same three taken back (requirements 7.6 and 7.7);
- the automatic close and unclose: a source bill every line of which is linked is closed as
  an automatic close and reopened when the unapprove of the downstream bill takes that
  away, while a partly linked one stays approved and a source bill the close operation
  closed stays closed (requirements 8.13 and 8.14);
- R-GEN-003: the unapprove of a bill a push generated a bill from is refused, a draft
  downstream bill included, and goes through once that bill is deleted (requirement 8.10).
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import LINE_SUFFIX
from erp_mock.domain.numbering import UPDATED_SEQ
from erp_mock.domain.state_machine import (
    APPROVED,
    AUTO_CLOSE,
    CLOSED,
    MANUAL_CLOSE,
    STATUS,
    StateAction,
)
from erp_mock.domain.writeback import (
    ALL_LINES_LINKED,
    HAS_DOWNSTREAM_REASON,
    PUSHED_RULE,
    all_lines_linked,
    downstream_bills,
    link_kind,
    source_link,
    writeback_values,
    writebacks_of,
)
from erp_mock.state.schema import (
    BILL_ID_COLUMN,
    CLOSE_MODE_COLUMN,
    ID_COLUMN,
    STOCK_TXN_TABLE,
    WRITEBACK_AMOUNT_COLUMN,
    WRITEBACK_COLUMN,
    WRITEBACK_FIELD_COLUMN,
    WRITEBACK_LEDGER_TABLE,
    WRITEBACK_ORD_COLUMN,
    WRITEBACK_SOURCE_ID_COLUMN,
    WRITEBACK_SOURCE_TABLE_COLUMN,
    WRITEBACK_TARGET_COLUMN,
)
from erp_mock.state.store import Record, Value
from tests.helpers.engine_env import (
    BOLT,
    BOLT_PER_BOX,
    BOX_FACTOR,
    MAIN_DEPOT,
    MAIN_VENDOR,
    SPARE_DEPOT,
    WASHER,
    EngineEnv,
    as_record,
    engine_env,
)

# The two conversions of the fixture, the bills they run between and the writeback of each
# (tests/fixtures/packs/fx_trade).
ORDER_TO_ENTRY: Final = "supply_order_to_entry"
FEE_TO_SLIP: Final = "fee_note_to_pay_slip"
ORDER: Final = "supply_order"
ORDER_LINE: Final = f"{ORDER}{LINE_SUFFIX}"
ENTRY: Final = "goods_entry"
NOTE: Final = "fee_note"
SLIP: Final = "pay_slip"
ENTRY_WRITEBACK: Final = "entry_writeback_order"
PAY_WRITEBACK: Final = "pay_writeback_fee"

# The short name the push tool of each conversion names its source bill under (req 11.13).
ORDER_SHORT: Final = "order"
FEE_SHORT: Final = "fee"

# The link field of each of them: the quantity on a source line, the amount on a source
# header (meta-model.md section 7).
LINKED_QTY: Final = "linked_qty"
LINKED_AMOUNT: Final = "linked_amount"

# The fields of the bills a test names.
ARTICLE: Final = "article_id"
DEPOT: Final = "depot_id"
ENTRY_QTY: Final = "entry_qty"
QTY_BASE: Final = "qty_base"
AMOUNT_WITH_TAX: Final = "amount_with_tax"
SOURCE_LINE_ID: Final = "source_line_id"

# The day after the anchor date, the required date of a supply order and the due date of a
# fee note.
NEXT_DAY: Final = date(2026, 10, 10)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


# --- building the bills -------------------------------------------------------------------


def order_line(env: EngineEnv, article: str = WASHER, qty: str = "10") -> dict[str, Value]:
    """One line of a supply order: ``qty`` of ``article`` at a price (requirement 6.1).

    The washer is bought in its base unit, so its ``qty_base`` is the quantity itself; the
    bolt is bought by the box, which is :data:`BOX_FACTOR` base units.
    """
    return {ARTICLE: env.id(article), "qty": Decimal(qty), "price": Decimal("0.85")}


def new_order(env: EngineEnv, lines: Sequence[Mapping[str, Value]] | None = None) -> str:
    """Create an approved supply order and return its id (requirements 7.1 and 7.2)."""
    header: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "need_date": NEXT_DAY}
    rows = [order_line(env)] if lines is None else [dict(line) for line in lines]
    order = record_id(env.create(ORDER, header, rows))
    approve(env, ORDER, order)
    return order


def entry_line(
    env: EngineEnv, source_line_id: str, qty: str, depot: str = MAIN_DEPOT
) -> dict[str, Value]:
    """One line of a push to a goods entry: the ``required_line_inputs`` of the conversion."""
    return {SOURCE_LINE_ID: source_line_id, ENTRY_QTY: Decimal(qty), DEPOT: env.id(depot)}


def push_entry(env: EngineEnv, order: str, lines: Sequence[Mapping[str, Value]]) -> str:
    """Push ``order`` to a goods entry draft and return its id (requirement 8.1)."""
    return record_id(env.push(ORDER_TO_ENTRY, order, lines, source_name=ORDER_SHORT))


def new_note(env: EngineEnv, qty: str = "100", price: str = "1.5") -> str:
    """Create an approved fee note and return its id; a simple bill approves from draft."""
    header: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "due_date": NEXT_DAY}
    line: dict[str, Value] = {"item_text": "夹具运费", "qty": Decimal(qty), "price": Decimal(price)}
    note = record_id(env.create(NOTE, header, [line]))
    env.transition(NOTE, note, "approve")
    return note


def push_slip(env: EngineEnv, note: str, qty: str, price: str) -> str:
    """Push ``note`` to a pay slip draft and return its id (requirement 8.1)."""
    line: dict[str, Value] = {
        SOURCE_LINE_ID: f"{note}-1",
        "qty": Decimal(qty),
        "price": Decimal(price),
    }
    return record_id(env.push(FEE_TO_SLIP, note, [line], source_name=FEE_SHORT))


def approve(env: EngineEnv, bill: str, bill_id: str) -> None:
    """Submit and approve the standard bill ``bill_id``, which writes back (req 7.6)."""
    env.transition(bill, bill_id, "submit")
    env.transition(bill, bill_id, "approve")


# --- reading the source bill and the ledger ----------------------------------------------


def record_id(result: object) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    assert hasattr(result, "view")
    return str(as_record(result.view).fields[ID_COLUMN])


def stored(env: EngineEnv, table: str, record_id: str) -> Record:
    """The record ``record_id`` of ``table`` as storage holds it; it has to be there."""
    record = env.record(table, record_id)
    assert record is not None, f"{table} {record_id} is not in the sandbox"
    return record


def linked_qty(env: EngineEnv, order: str) -> list[Value]:
    """The link field of every line of the supply order ``order``, by line number (8.8)."""
    lines = env.records(ORDER_LINE)
    return [line[LINKED_QTY] for line in lines if line[BILL_ID_COLUMN] == order]


def state_of(env: EngineEnv, bill: str, bill_id: str) -> tuple[Value, Value]:
    """The state of the bill ``bill_id`` and how it was closed (requirements 7.1 and 7.8)."""
    record = stored(env, bill, bill_id)
    return (record[STATUS], record[CLOSE_MODE_COLUMN])


def ledger(env: EngineEnv) -> list[dict[str, Value]]:
    """Every row of the writeback ledger, in ledger order (design.md data model 3)."""
    return [dict(row) for row in env.records(WRITEBACK_LEDGER_TABLE)]


def ledger_row(
    target_bill_id: str,
    writeback: str,
    ord_: int,
    source_table: str,
    source_id: str,
    field: str,
    amount: str,
) -> dict[str, Value]:
    """One row of the ledger as storage holds it (design.md data model 3)."""
    return {
        WRITEBACK_TARGET_COLUMN: target_bill_id,
        WRITEBACK_COLUMN: writeback,
        WRITEBACK_ORD_COLUMN: ord_,
        WRITEBACK_SOURCE_TABLE_COLUMN: source_table,
        WRITEBACK_SOURCE_ID_COLUMN: source_id,
        WRITEBACK_FIELD_COLUMN: field,
        WRITEBACK_AMOUNT_COLUMN: amount,
    }


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


def refused(env: EngineEnv, bill: str, bill_id: str, action: StateAction) -> ErpError:
    """Run ``action`` on ``bill_id`` and return the error it ended in."""
    with pytest.raises(ErpError) as raised:
        env.transition(bill, bill_id, action)
    return raised.value


# --- the amounts of a writeback (requirement 8.8) ----------------------------------------


@pytest.mark.req("8.8")
def test_approve_adds_the_value_of_each_line_to_the_source_line_it_came_from(
    env: EngineEnv,
) -> None:
    order = new_order(env, [order_line(env, WASHER, "10"), order_line(env, BOLT, "10")])
    entry = push_entry(
        env,
        order,
        [entry_line(env, f"{order}-1", "4"), entry_line(env, f"{order}-2", "5")],
    )

    approve(env, ENTRY, entry)

    # ``value: line.qty_base``, so the second line writes back the base unit quantity of
    # the five boxes it received and not the five (requirements 6.6 and 8.8).
    boxes = Decimal("5") * BOX_FACTOR
    assert boxes == Decimal("60")
    assert linked_qty(env, order) == [Decimal("4"), boxes]
    assert ledger(env) == [
        ledger_row(entry, ENTRY_WRITEBACK, 1, ORDER_LINE, f"{order}-1", LINKED_QTY, "4"),
        ledger_row(entry, ENTRY_WRITEBACK, 2, ORDER_LINE, f"{order}-2", LINKED_QTY, "60"),
    ]


@pytest.mark.req("8.8")
def test_two_lines_that_name_one_source_line_add_up(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    # One order line received into two warehouses, which the push accepts (requirement 8.7).
    entry = push_entry(
        env,
        order,
        [
            entry_line(env, f"{order}-1", "3", MAIN_DEPOT),
            entry_line(env, f"{order}-1", "4", SPARE_DEPOT),
        ],
    )

    approve(env, ENTRY, entry)

    assert linked_qty(env, order) == [Decimal("7")]
    # One row per line, numbered by the line of the entry the amount came from.
    assert [row[WRITEBACK_ORD_COLUMN] for row in ledger(env)] == [1, 2]
    assert [row[WRITEBACK_AMOUNT_COLUMN] for row in ledger(env)] == ["3", "4"]


@pytest.mark.req("8.8")
def test_approve_adds_the_value_of_the_header_to_the_source_bill(env: EngineEnv) -> None:
    note = new_note(env, qty="100", price="1.5")
    slip = push_slip(env, note, qty="40", price="1.5")
    amount = stored(env, SLIP, slip)[AMOUNT_WITH_TAX]

    approve(env, SLIP, slip)

    # ``target: source.linked_amount`` and ``value: header.amount_with_tax``: one amount
    # for the whole bill, on the header of the source (requirement 8.8).
    assert amount == Decimal("60.00")
    assert stored(env, NOTE, note)[LINKED_AMOUNT] == Decimal("60.00")
    assert ledger(env) == [ledger_row(slip, PAY_WRITEBACK, 1, NOTE, note, LINKED_AMOUNT, "60.00")]


@pytest.mark.req("8.8")
def test_a_bill_no_push_generated_writes_nothing_back(env: EngineEnv) -> None:
    line: dict[str, Value] = {
        "item_text": "夹具手工付款",
        "qty": Decimal("1"),
        "price": Decimal("10"),
        "tax_rate": Decimal("0.06"),
    }
    slip = record_id(env.create(SLIP, {"vendor_id": env.id(MAIN_VENDOR)}, [line]))

    approve(env, SLIP, slip)

    # The bill carries no conversion and no source bill, so there is nothing to write back
    # to and no ledger row (requirement 8.8).
    assert source_link(env.domain, stored(env, SLIP, slip)) is None
    assert ledger(env) == []
    env.transition(SLIP, slip, "unapprove")
    assert state_of(env, SLIP, slip) == ("draft", None)


# --- the order of an approve and an unapprove (requirements 7.6 and 7.7) -----------------


@pytest.mark.req("7.6", "4.5")
def test_approve_writes_the_state_the_stock_and_the_source_in_one_call(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    entry = push_entry(env, order, [entry_line(env, f"{order}-1", "4")])
    env.transition(ENTRY, entry, "submit")

    result = env.transition(ENTRY, entry, "approve")

    # The state first, then the linkages of the bill type, then the writebacks of the
    # conversion that generated it; all three in the transaction of this one call.
    assert state_of(env, ENTRY, entry) == (APPROVED, None)
    assert [row[ID_COLUMN] for row in env.records(STOCK_TXN_TABLE)] == [
        f"{entry}-1:{env.id(MAIN_DEPOT)}:in"
    ]
    assert linked_qty(env, order) == [Decimal("4")]
    # The source bill a writeback changed takes the operation sequence number of this
    # operation, and its writes name the writeback that made them (requirements 4.5, 17.4).
    assert stored(env, ORDER, order)[UPDATED_SEQ] == as_record(result.view).fields[UPDATED_SEQ]
    assert (f"{ORDER}_line", "update", f"{order}-1", ENTRY_WRITEBACK) in [
        (write.entity, write.op, write.id, write.cause) for write in result.writes
    ]


@pytest.mark.req("7.7", "8.9")
def test_unapprove_takes_the_state_the_stock_and_the_source_back(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    entry = push_entry(env, order, [entry_line(env, f"{order}-1", "4")])
    approve(env, ENTRY, entry)

    env.transition(ENTRY, entry, "unapprove")

    assert state_of(env, ENTRY, entry) == ("draft", None)
    assert env.records(STOCK_TXN_TABLE) == []
    # The ledger row is gone and the link field is empty again, as it was before the push:
    # a link field no ledger row contributes to any more holds nothing.
    assert ledger(env) == []
    assert linked_qty(env, order) == [None]


@pytest.mark.req("8.9")
def test_unapprove_takes_off_its_own_amounts_alone(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    first = push_entry(env, order, [entry_line(env, f"{order}-1", "4")])
    second = push_entry(env, order, [entry_line(env, f"{order}-1", "3")])
    approve(env, ENTRY, first)
    approve(env, ENTRY, second)
    assert linked_qty(env, order) == [Decimal("7")]

    env.transition(ENTRY, second, "unapprove")

    # Exactly the amounts of the second entry, read from the ledger rather than computed
    # again; the first entry keeps its own and its row stays (requirement 8.9).
    assert linked_qty(env, order) == [Decimal("4")]
    assert ledger(env) == [
        ledger_row(first, ENTRY_WRITEBACK, 1, ORDER_LINE, f"{order}-1", LINKED_QTY, "4")
    ]


@pytest.mark.req("8.9")
def test_unapprove_reverses_what_the_approve_wrote_whatever_the_value_would_be_now(
    env: EngineEnv,
) -> None:
    order = new_order(env, [order_line(env, BOLT, "10")])
    entry = push_entry(env, order, [entry_line(env, f"{order}-1", "4")])
    approve(env, ENTRY, entry)
    assert linked_qty(env, order) == [Decimal("4") * BOX_FACTOR]
    # The ledger keeps the amount that was added, so the reversal does not depend on what
    # the value expression would give now (design.md 4.2): the conversion record of the
    # bolt is changed, which is what every ``qty_base`` of a box is computed from.
    env.update("measure_link", env.id(BOLT_PER_BOX), {"ratio": Decimal("20")})

    env.transition(ENTRY, entry, "unapprove")

    assert linked_qty(env, order) == [None]
    assert ledger(env) == []


# --- the automatic close and unclose (requirements 8.13 and 8.14) ------------------------


@pytest.mark.req("8.13")
def test_approve_closes_the_source_bill_once_every_line_is_linked(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10"), order_line(env, WASHER, "6")])
    entry = push_entry(
        env,
        order,
        [entry_line(env, f"{order}-1", "10"), entry_line(env, f"{order}-2", "6")],
    )

    approve(env, ENTRY, entry)

    # ``all_lines_linked``: every source line holds a link field of at least its own
    # qty_base, so the source is closed and the close is recorded as an automatic one
    # (requirements 8.13 and 7.8).
    assert state_of(env, ORDER, order) == (CLOSED, AUTO_CLOSE)


@pytest.mark.req("8.13")
def test_a_partly_linked_source_bill_stays_approved(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10"), order_line(env, WASHER, "6")])
    # Everything of the first line and nothing of the second.
    entry = push_entry(env, order, [entry_line(env, f"{order}-1", "10")])

    approve(env, ENTRY, entry)

    assert state_of(env, ORDER, order) == (APPROVED, None)


@pytest.mark.req("8.13")
def test_a_writeback_without_the_condition_closes_nothing(env: EngineEnv) -> None:
    note = new_note(env, qty="100", price="1.5")
    slip = push_slip(env, note, qty="100", price="1.5")

    approve(env, SLIP, slip)

    # The whole fee note is linked, but its writeback declares no
    # ``auto_close_source_when``, and a simple bill has no closed state (requirement 7.11).
    assert stored(env, NOTE, note)[LINKED_AMOUNT] == stored(env, NOTE, note)[AMOUNT_WITH_TAX]
    assert state_of(env, NOTE, note) == (APPROVED, None)


@pytest.mark.req("8.14")
def test_unapprove_reopens_a_source_bill_the_engine_closed(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    first = push_entry(env, order, [entry_line(env, f"{order}-1", "4")])
    second = push_entry(env, order, [entry_line(env, f"{order}-1", "6")])
    approve(env, ENTRY, first)
    approve(env, ENTRY, second)
    assert state_of(env, ORDER, order) == (CLOSED, AUTO_CLOSE)

    env.transition(ENTRY, second, "unapprove")

    # The line is no longer linked in full, so the automatic close is taken back and the
    # close mode goes with it (requirements 8.14 and 7.8).
    assert linked_qty(env, order) == [Decimal("4")]
    assert state_of(env, ORDER, order) == (APPROVED, None)
    # Unapproving the other one leaves the source approved and linked to nothing.
    env.transition(ENTRY, first, "unapprove")
    assert (state_of(env, ORDER, order), linked_qty(env, order)) == ((APPROVED, None), [None])


@pytest.mark.req("8.14")
def test_a_source_bill_that_is_still_linked_in_full_stays_closed(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    first = push_entry(env, order, [entry_line(env, f"{order}-1", "10")])
    # A second entry of 0 is within what the source line has left and adds nothing; both
    # drafts are pushed while the order is still approved (requirement 8.3).
    second = push_entry(env, order, [entry_line(env, f"{order}-1", "0")])
    approve(env, ENTRY, first)
    assert state_of(env, ORDER, order) == (CLOSED, AUTO_CLOSE)
    approve(env, ENTRY, second)

    env.transition(ENTRY, second, "unapprove")

    # The first entry still links the whole line, so the source keeps its automatic close.
    assert state_of(env, ORDER, order) == (CLOSED, AUTO_CLOSE)
    assert linked_qty(env, order) == [Decimal("10")]


@pytest.mark.req("8.13", "8.14")
def test_a_manually_closed_source_bill_stays_closed(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    entry = push_entry(env, order, [entry_line(env, f"{order}-1", "10")])
    # The close operation closes the source while the entry is still a draft (req 7.1).
    env.transition(ORDER, order, "close")

    approve(env, ENTRY, entry)

    # The writeback runs, but a source bill that is not approved is not closed again and
    # the way it was closed stays manual (requirement 8.13).
    assert linked_qty(env, order) == [Decimal("10")]
    assert state_of(env, ORDER, order) == (CLOSED, MANUAL_CLOSE)

    env.transition(ENTRY, entry, "unapprove")

    # And a manually closed source bill is not reopened, however little is linked now
    # (requirement 8.14).
    assert state_of(env, ORDER, order) == (CLOSED, MANUAL_CLOSE)
    assert linked_qty(env, order) == [None]


@pytest.mark.req("8.13")
def test_the_condition_of_a_writeback_on_a_source_header_is_the_amount(env: EngineEnv) -> None:
    # No valid pack closes a fee note automatically - its state machine is simple
    # (requirement 7.11) - so the condition of a writeback that keeps the link field of a
    # source header is checked on its own: the amount of the source bill linked in full,
    # which is the amount a push limited by amount is counted against (requirement 8.6).
    note = new_note(env, qty="100", price="1.5")
    approve(env, SLIP, push_slip(env, note, qty="40", price="1.5"))
    partly = stored(env, NOTE, note)
    approve(env, SLIP, push_slip(env, note, qty="60", price="1.5"))
    fully = stored(env, NOTE, note)
    writeback = replace(
        env.domain.writebacks[PAY_WRITEBACK], auto_close_source_when=ALL_LINES_LINKED
    )
    fee = env.domain.bills[NOTE]

    with env.store.transaction() as uow:
        assert all_lines_linked(uow, writeback, fee, partly) is False
        assert all_lines_linked(uow, writeback, fee, fully) is True


# --- R-GEN-003 (requirement 8.10) --------------------------------------------------------


@pytest.mark.req("8.10")
def test_unapprove_is_refused_while_a_pushed_bill_exists(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    entry = push_entry(env, order, [entry_line(env, f"{order}-1", "4")])

    error = refused(env, ORDER, order, "unapprove")

    # A draft downstream bill is enough: it was built on the state and the link field of
    # this order (requirement 8.10).
    assert state_of(env, ENTRY, entry) == ("draft", None)
    assert (error.code, error.rule_id) == (ErrorCode.BUSINESS_RULE_VIOLATION, PUSHED_RULE)
    assert details(error) == [
        {"reason": HAS_DOWNSTREAM_REASON, "bill_type": ENTRY, "bill_id": entry}
    ]
    assert entry in error.message
    # The order is left as it was, its state included (requirement 12.7).
    assert state_of(env, ORDER, order) == (APPROVED, None)


@pytest.mark.req("8.10")
def test_unapprove_goes_through_once_the_pushed_bill_is_gone(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, WASHER, "10")])
    entry = push_entry(env, order, [entry_line(env, f"{order}-1", "4")])
    source = env.domain.bills[ORDER]
    with env.store.transaction() as uow:
        found = downstream_bills(uow, env.domain, source, order)
    assert [(item.conversion, item.bill.name, item.id) for item in found] == [
        (ORDER_TO_ENTRY, ENTRY, entry)
    ]

    # Every other state operation of the source bill is unaffected by a downstream bill:
    # R-GEN-003 is about the unapprove alone (requirement 8.10).
    env.transition(ORDER, order, "close")
    env.transition(ORDER, order, "unclose")
    assert state_of(env, ORDER, order) == (APPROVED, None)
    # Deleting the downstream bill ends the rule, and the order is unapproved again
    # (requirements 8.10 and 7.1).
    env.delete(ENTRY, entry)
    with env.store.transaction() as uow:
        assert downstream_bills(uow, env.domain, source, order) == ()
    env.transition(ORDER, order, "unapprove")
    assert state_of(env, ORDER, order) == ("draft", None)


# --- what the module reads from the configuration ----------------------------------------


@pytest.mark.req("8.8")
def test_the_primitive_reads_the_writebacks_and_their_values_from_the_configuration(
    env: EngineEnv,
) -> None:
    domain = env.domain
    assert [writeback.id for writeback in writebacks_of(domain, ORDER_TO_ENTRY)] == [
        ENTRY_WRITEBACK
    ]
    assert [writeback.id for writeback in writebacks_of(domain, FEE_TO_SLIP)] == [PAY_WRITEBACK]
    assert writebacks_of(domain, "no_such_conversion") == ()
    # Every value is parsed once per instance, which is what the engine holds (req 10.2).
    assert set(writeback_values(domain)) == {ENTRY_WRITEBACK, PAY_WRITEBACK}
    assert writeback_values(domain) == FrozenMap(env.engine.values)
    # The link field of a source line is a quantity and that of a source header an amount
    # (requirement 4.7).
    assert link_kind(domain, ORDER_LINE, LINKED_QTY) == "qty"
    assert link_kind(domain, NOTE, LINKED_AMOUNT) == "money"
    with pytest.raises(RuntimeError, match="no field"):
        link_kind(domain, NOTE, "no_such_field")
    with pytest.raises(RuntimeError, match="no amount"):
        link_kind(domain, NOTE, "remark")
