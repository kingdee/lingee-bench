"""erp_mock.domain.invariants: the four invariants of M0 (req 4.10, 8.11, 9.8, 20.9).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. The bills of the fixture are then created,
approved, pushed and approved again through the engine, so the sandbox the checks read is
one every primitive wrote into - stock transactions and balances, a writeback ledger and
the link fields of two sources, the amounts of three pricing bills, and the ids, line ids
and numbers of all of them. On such a sandbox :func:`check_invariants` returns an empty
list, and that is the starting point of every test here.

Each invariant is then reached by writing into the stored records behind the engine's back
(:func:`tamper`, :func:`remove`, :func:`add`), which is the only way to produce a state no
operation of the engine produces:

- ``INV-1``: a balance that is not the sum of its transactions, a balance record that is
  gone while its transactions stay, and a balance below 0 (requirement 9.8);
- ``INV-2``: a link field that is not the sum of what the ledger added to it, the amounts
  of a downstream bill that is no longer approved, the link field of a source *header* for
  a push limited by amount, and a source line occupied beyond its own ``qty_base``, which
  a line nothing points at never is (requirement 8.11);
- ``INV-3``: a header amount that is not the sum of its lines, and a line whose total is
  not its net amount plus its tax, which an empty amount is no case of
  (requirements 6.4 and 6.5);
- ``INV-6``: one id in two tables, one line id in two tables, one number on two bills, and
  an id and a number whose sequence is above the counter it would come from
  (requirement 4.10).

The last tests pin what :func:`check_invariants` reports and in which order: only the
invariants the caller asked for, in the order of
:data:`~erp_mock.domain.invariants.INVARIANTS` whatever order they were asked in, each
violation naming the invariant and the records it is about, and reading as one line that
names the conversion keeping it where there is one (requirement 20.9).
"""

from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.config.model import LINE_SUFFIX
from erp_mock.domain.invariants import (
    BALANCE_MISMATCH,
    DUPLICATE_ID,
    DUPLICATE_LINE_ID,
    DUPLICATE_NUMBER,
    HEADER_AMOUNT_MISMATCH,
    ID_ABOVE_COUNTER,
    INV_1,
    INV_2,
    INV_3,
    INV_6,
    INVARIANTS,
    LINE_AMOUNT_MISMATCH,
    LINK_FIELD_MISMATCH,
    NEGATIVE_BALANCE,
    NUMBER_ABOVE_COUNTER,
    OCCUPIED_ABOVE_QTY_BASE,
    InvariantId,
    InvariantViolation,
    check_invariants,
    id_sequence,
    number_parts,
)
from erp_mock.domain.numbering import format_id, format_number
from erp_mock.domain.state_machine import DRAFT, STATUS
from erp_mock.state.schema import (
    ID_COLUMN,
    MATERIAL_COLUMN,
    QTY_BASE_COLUMN,
    QTY_DELTA_COLUMN,
    STOCK_BALANCE_TABLE,
    STOCK_TXN_TABLE,
    WRITEBACK_LEDGER_TABLE,
)
from erp_mock.state.store import Value
from tests.helpers.engine_env import (
    BOLT,
    MAIN_DEPOT,
    MAIN_VENDOR,
    WASHER,
    EngineEnv,
    as_record,
    engine_env,
)

# The bills of the fixture a test names, the lines of three of them and the two conversions
# between them (tests/fixtures/packs/fx_trade).
ORDER: Final = "supply_order"
ORDER_LINE: Final = f"{ORDER}{LINE_SUFFIX}"
ENTRY: Final = "goods_entry"
ENTRY_LINE: Final = f"{ENTRY}{LINE_SUFFIX}"
NOTE: Final = "fee_note"
NOTE_LINE: Final = f"{NOTE}{LINE_SUFFIX}"
SLIP: Final = "pay_slip"
ORDER_TO_ENTRY: Final = "supply_order_to_entry"
FEE_TO_SLIP: Final = "fee_note_to_pay_slip"

# The short name the push tool of each conversion names its source bill under (req 11.13).
ORDER_SHORT: Final = "order"
FEE_SHORT: Final = "fee"

# The id prefix and the number prefix of a supply order (requirements 4.1 and 4.4).
ORDER_ID_PREFIX: Final = "so"
ORDER_NUMBER_PREFIX: Final = "SPO"

# The link field of each conversion: the quantity of a source line, the amount of a source
# header (meta-model.md section 7).
LINKED_QTY: Final = "linked_qty"
LINKED_AMOUNT: Final = "linked_amount"

# The fields of the bills a test writes and reads.
VENDOR: Final = "vendor_id"
ARTICLE: Final = "article_id"
DEPOT: Final = "depot_id"
ENTRY_QTY: Final = "entry_qty"
NUMBER: Final = "number"
AMOUNT: Final = "amount"
TAX_AMOUNT: Final = "tax_amount"
AMOUNT_WITH_TAX: Final = "amount_with_tax"
SOURCE_LINE_ID: Final = "source_line_id"

# The day after the anchor date: the required date of a supply order and the due date of a
# fee note.
NEXT_DAY: Final = date(2026, 10, 10)

# What the one goods entry of the sandbox received of the first order line, in base units:
# the washer is bought in its base unit (requirement 6.6).
WASHERS: Final = Decimal("4")

# The amount the one pay slip of the sandbox wrote back to its fee note (requirement 8.8).
PAID: Final = Decimal("60.00")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


@pytest.fixture
def sandbox(env: EngineEnv) -> EngineEnv:
    """A sandbox every primitive of M0 has written into (see the module docstring).

    One approved supply order of 10 washers and 10 boxes of bolts, an approved goods entry
    pushed from it that received part of each line - so the order stays approved rather
    than being closed automatically (requirement 8.13) - and an approved fee note with an
    approved pay slip pushed from it. That leaves one stock transaction and one balance per
    received line, two ledger rows on the lines of the order and one on the header of the
    note.
    """
    order = new_order(env)
    entry = push_entry(
        env,
        order,
        [entry_line(env, f"{order}-1", "4"), entry_line(env, f"{order}-2", "5")],
    )
    approve(env, ENTRY, entry)
    note = new_note(env)
    approve(env, SLIP, push_slip(env, note, qty="40", price="1.5"))
    return env


# --- building the bills -------------------------------------------------------------------


def order_line(env: EngineEnv, article: str, qty: str = "10") -> dict[str, Value]:
    """One line of a supply order: ``qty`` of ``article`` at a price (requirement 6.1)."""
    return {ARTICLE: env.id(article), "qty": Decimal(qty), "price": Decimal("0.85")}


def new_order(env: EngineEnv) -> str:
    """Create an approved supply order of washers and bolts, and return its id (req 7.2).

    The washer is bought in its base unit and the bolt by the box, so the two lines measure
    10 and 120 base units and each has quantity left for a push (requirement 8.5).
    """
    header: dict[str, Value] = {VENDOR: env.id(MAIN_VENDOR), "need_date": NEXT_DAY}
    order = record_id(env.create(ORDER, header, [order_line(env, WASHER), order_line(env, BOLT)]))
    approve(env, ORDER, order)
    return order


def entry_line(env: EngineEnv, source_line_id: str, qty: str) -> dict[str, Value]:
    """One line of a push to a goods entry: the ``required_line_inputs`` of the conversion."""
    return {SOURCE_LINE_ID: source_line_id, ENTRY_QTY: Decimal(qty), DEPOT: env.id(MAIN_DEPOT)}


def push_entry(env: EngineEnv, order: str, lines: Sequence[Mapping[str, Value]]) -> str:
    """Push ``order`` to a goods entry draft and return its id (requirement 8.1)."""
    return record_id(env.push(ORDER_TO_ENTRY, order, lines, source_name=ORDER_SHORT))


def new_note(env: EngineEnv) -> str:
    """Create an approved fee note of 150.00 and return its id (requirements 6.2 and 7.2).

    A fee note has a simple state machine, so it is approved from draft, and its price
    includes tax, which no business pack of the fixture declares otherwise
    (requirement 6.3).
    """
    header: dict[str, Value] = {VENDOR: env.id(MAIN_VENDOR), "due_date": NEXT_DAY}
    line: dict[str, Value] = {
        "item_text": "夹具运费",
        "qty": Decimal("100"),
        "price": Decimal("1.5"),
    }
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


def record_id(result: object) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    assert hasattr(result, "view")
    return str(as_record(result.view).fields[ID_COLUMN])


# --- reading the sandbox -----------------------------------------------------------------


def rows(env: EngineEnv, table: str) -> list[dict[str, Value]]:
    """Every record of ``table`` as plain dicts, by id."""
    return [dict(record) for record in env.records(table)]


def stored(env: EngineEnv, table: str, record: str) -> Mapping[str, Value]:
    """The record ``record`` of ``table`` as storage holds it; it has to be there."""
    found = env.record(table, record)
    assert found is not None, f"{table} {record} is not in the sandbox"
    return found


def only_id(env: EngineEnv, table: str) -> str:
    """The id of the one record ``table`` holds, which the sandbox fixture wrote."""
    found = rows(env, table)
    assert len(found) == 1, f"{table} holds {len(found)} records"
    return str(found[0][ID_COLUMN])


def balance_of(env: EngineEnv, article: str) -> str:
    """The id of the balance record of ``article`` in the main depot (design.md 3)."""
    return f"{env.id(article)}:{env.id(MAIN_DEPOT)}"


def txn_of(env: EngineEnv, article: str) -> str:
    """The id of the one stock transaction of ``article`` (requirement 18.8)."""
    found = [row for row in rows(env, STOCK_TXN_TABLE) if row[MATERIAL_COLUMN] == env.id(article)]
    assert len(found) == 1, f"{article} has {len(found)} stock transactions"
    return str(found[0][ID_COLUMN])


# --- writing behind the engine's back and reading the violations -------------------------


def tamper(env: EngineEnv, table: str, record: str, values: Mapping[str, Value]) -> None:
    """Write ``values`` into a stored record behind the engine's back.

    A state no operation of the engine produces - a balance that is not the sum of its
    transactions, a link field another number was written into - is how a test reaches an
    invariant that does not hold.
    """
    with env.store.transaction() as uow:
        uow.update(table, record, values)


def remove(env: EngineEnv, table: str, record: str) -> None:
    """Delete a stored record behind the engine's back."""
    with env.store.transaction() as uow:
        uow.delete(table, record)


def add(env: EngineEnv, table: str, values: Mapping[str, Value]) -> None:
    """Insert a record behind the engine's back; the columns it omits stay empty."""
    with env.store.transaction() as uow:
        uow.insert(table, values)


def violations(
    env: EngineEnv, which: Sequence[InvariantId] = INVARIANTS
) -> list[InvariantViolation]:
    """The violations of ``which`` the sandbox holds, in report order (requirement 20.9)."""
    with env.store.transaction() as uow:
        return check_invariants(uow, env.domain, which)


def reported(env: EngineEnv, which: Sequence[InvariantId] = INVARIANTS) -> list[tuple[str, ...]]:
    """Each violation as (invariant, reason, every record it names) (requirement 20.9)."""
    return [
        (violation.invariant, violation.reason, *(ref.text for ref in violation.records))
        for violation in violations(env, which)
    ]


def reasons(env: EngineEnv, which: Sequence[InvariantId] = INVARIANTS) -> list[str]:
    """What did not hold, one word per violation, in report order."""
    return [violation.reason for violation in violations(env, which)]


def numbers_of(env: EngineEnv, which: Sequence[InvariantId] = INVARIANTS) -> list[str]:
    """The invariant of each violation, in report order (requirement 20.9)."""
    return [violation.invariant for violation in violations(env, which)]


def amounts(violation: InvariantViolation) -> tuple[str | None, str | None]:
    """The two values of a violation: what the invariant asks for and what is there."""
    return (violation.expected, violation.found)


# --- a sandbox the engine built (requirements 4.10, 8.11, 9.8, 20.9) ---------------------


@pytest.mark.req("4.10", "8.11", "9.8", "20.9")
def test_a_sandbox_the_engine_built_breaks_no_invariant(sandbox: EngineEnv) -> None:
    # Every primitive wrote into it: one transaction and one balance per received line, and
    # three ledger rows - two on the lines of the order, one on the header of the note.
    assert len(rows(sandbox, STOCK_TXN_TABLE)) == 2
    assert len(rows(sandbox, STOCK_BALANCE_TABLE)) == 2
    assert len(rows(sandbox, WRITEBACK_LEDGER_TABLE)) == 3

    assert violations(sandbox) == []
    # And each invariant on its own, so that no check is vacuous for this sandbox.
    for invariant in INVARIANTS:
        assert violations(sandbox, (invariant,)) == [], invariant


@pytest.mark.req("20.9")
def test_an_empty_sandbox_breaks_no_invariant(tmp_path: Path) -> None:
    # No record, no transaction and every counter at 0 (requirement 4.6).
    with engine_env(tmp_path / "empty", records=()) as empty:
        assert violations(empty) == []


# --- INV-1: the stock on hand of each (material, warehouse) (requirement 9.8) ------------


@pytest.mark.req("9.8")
def test_a_balance_that_is_not_the_sum_of_its_transactions_is_found(sandbox: EngineEnv) -> None:
    washers = balance_of(sandbox, WASHER)
    tamper(sandbox, STOCK_BALANCE_TABLE, washers, {QTY_BASE_COLUMN: WASHERS + Decimal("1")})

    found = violations(sandbox, (INV_1,))

    assert reported(sandbox, (INV_1,)) == [
        (INV_1, BALANCE_MISMATCH, f"{STOCK_BALANCE_TABLE} {washers}")
    ]
    # The sum of the transactions of the pair against the quantity the record holds.
    assert amounts(found[0]) == ("4", "5")
    assert found[0].field == QTY_BASE_COLUMN


@pytest.mark.req("9.8")
def test_a_balance_record_that_is_gone_is_found(sandbox: EngineEnv) -> None:
    # The transactions of the pair stay, so its quantity on hand reads as 0 while they
    # still add up to 4; which pairs have a record is requirement 9.10 and not INV-1.
    washers = balance_of(sandbox, WASHER)
    remove(sandbox, STOCK_BALANCE_TABLE, washers)

    found = violations(sandbox, (INV_1,))

    assert reported(sandbox, (INV_1,)) == [
        (INV_1, BALANCE_MISMATCH, f"{STOCK_BALANCE_TABLE} {washers}")
    ]
    assert amounts(found[0]) == ("4", "0")


@pytest.mark.req("9.8")
def test_a_negative_balance_is_found(sandbox: EngineEnv) -> None:
    # The transaction and the balance of the pair are turned negative together, so the sum
    # still matches and the only thing left is the balance being below 0.
    washers = balance_of(sandbox, WASHER)
    tamper(sandbox, STOCK_TXN_TABLE, txn_of(sandbox, WASHER), {QTY_DELTA_COLUMN: -WASHERS})
    tamper(sandbox, STOCK_BALANCE_TABLE, washers, {QTY_BASE_COLUMN: -WASHERS})

    found = violations(sandbox, (INV_1,))

    assert reported(sandbox, (INV_1,)) == [
        (INV_1, NEGATIVE_BALANCE, f"{STOCK_BALANCE_TABLE} {washers}")
    ]
    assert amounts(found[0]) == (None, "-4")


# --- INV-2: the link field and the occupancy of a source (requirement 8.11) --------------


@pytest.mark.req("8.11")
def test_a_link_field_that_is_not_the_sum_of_the_writebacks_is_found(sandbox: EngineEnv) -> None:
    order = only_id(sandbox, ORDER)
    tamper(sandbox, ORDER_LINE, f"{order}-1", {LINKED_QTY: WASHERS + Decimal("3")})

    found = violations(sandbox, (INV_2,))

    assert reported(sandbox, (INV_2,)) == [(INV_2, LINK_FIELD_MISMATCH, f"{ORDER_LINE} {order}-1")]
    # What the ledger added to the source line against what the line now holds.
    assert amounts(found[0]) == ("4", "7")
    assert (found[0].field, found[0].cause) == (LINKED_QTY, ORDER_TO_ENTRY)


@pytest.mark.req("8.11")
def test_the_amounts_of_a_bill_that_is_no_longer_approved_do_not_count(
    sandbox: EngineEnv,
) -> None:
    order = only_id(sandbox, ORDER)
    # Only an unapprove takes the amounts of an approve off a link field, and it takes them
    # off in full (requirement 8.9); a bill back in draft whose amounts are still counted in
    # its source is a state no operation produces.
    tamper(sandbox, ENTRY, only_id(sandbox, ENTRY), {STATUS: DRAFT})

    found = violations(sandbox, (INV_2,))

    # One violation per source line the entry wrote back to, by line id.
    assert reported(sandbox, (INV_2,)) == [
        (INV_2, LINK_FIELD_MISMATCH, f"{ORDER_LINE} {order}-1"),
        (INV_2, LINK_FIELD_MISMATCH, f"{ORDER_LINE} {order}-2"),
    ]
    assert [amounts(violation) for violation in found] == [("0", "4"), ("0", "60")]


@pytest.mark.req("8.11")
def test_the_link_field_of_a_source_header_is_checked_as_well(sandbox: EngineEnv) -> None:
    # The conversion of the fee note is limited by amount, so its link field is a money
    # field of the source header rather than a quantity of a source line (requirement 8.6).
    note = only_id(sandbox, NOTE)
    tamper(sandbox, NOTE, note, {LINKED_AMOUNT: PAID + Decimal("0.01")})

    found = violations(sandbox, (INV_2,))

    assert reported(sandbox, (INV_2,)) == [(INV_2, LINK_FIELD_MISMATCH, f"{NOTE} {note}")]
    assert amounts(found[0]) == ("60.00", "60.01")
    assert (found[0].field, found[0].cause) == (LINKED_AMOUNT, FEE_TO_SLIP)


@pytest.mark.req("8.11")
def test_a_source_line_occupied_above_its_qty_base_is_found(sandbox: EngineEnv) -> None:
    order = only_id(sandbox, ORDER)
    # The goods entry received 4 of the first order line, which the quota allowed because
    # the line measured 10; a line that measures 1 is occupied beyond itself.
    tamper(sandbox, ORDER_LINE, f"{order}-1", {QTY_BASE_COLUMN: Decimal("1")})

    found = violations(sandbox, (INV_2,))

    assert reported(sandbox, (INV_2,)) == [
        (INV_2, OCCUPIED_ABOVE_QTY_BASE, f"{ORDER_LINE} {order}-1")
    ]
    # The quantity of the source line against the quantity its downstream lines take.
    assert amounts(found[0]) == ("1", "4")
    assert (found[0].field, found[0].cause) == (QTY_BASE_COLUMN, ORDER_TO_ENTRY)


@pytest.mark.req("8.11")
def test_a_source_line_nothing_points_at_occupies_nothing(sandbox: EngineEnv) -> None:
    # A second approved order, which was never pushed: no line of a goods entry names its
    # lines, so each of them is occupied by nothing and stays within its own quantity even
    # where that quantity is 0 (requirement 8.11).
    other = new_order(sandbox)
    tamper(sandbox, ORDER_LINE, f"{other}-1", {QTY_BASE_COLUMN: Decimal("0")})

    assert violations(sandbox, (INV_2,)) == []


# --- INV-3: the amounts of a bill and of its lines (requirements 6.4 and 6.5) ------------


@pytest.mark.req("20.9", "6.4")
def test_a_header_amount_that_is_not_the_sum_of_its_lines_is_found(sandbox: EngineEnv) -> None:
    note = only_id(sandbox, NOTE)
    tamper(sandbox, NOTE, note, {TAX_AMOUNT: Decimal("0.01")})

    found = violations(sandbox, (INV_3,))

    assert reported(sandbox, (INV_3,)) == [(INV_3, HEADER_AMOUNT_MISMATCH, f"{NOTE} {note}")]
    # The one line of the note carries the tax included in a price of 150.00 at 6%.
    assert amounts(found[0]) == ("8.49", "0.01")
    assert found[0].field == TAX_AMOUNT


@pytest.mark.req("20.9", "6.5")
def test_a_line_whose_total_is_not_its_amount_plus_its_tax_is_found(sandbox: EngineEnv) -> None:
    note = only_id(sandbox, NOTE)
    # The total of the line and the total of the header are moved by the same amount, so
    # the header is still the sum of its lines and the line alone is left
    # (requirements 6.4 and 6.5).
    tamper(sandbox, NOTE, note, {AMOUNT_WITH_TAX: Decimal("151.00")})
    tamper(sandbox, NOTE_LINE, f"{note}-1", {AMOUNT_WITH_TAX: Decimal("151.00")})

    found = violations(sandbox, (INV_3,))

    assert reported(sandbox, (INV_3,)) == [(INV_3, LINE_AMOUNT_MISMATCH, f"{NOTE_LINE} {note}-1")]
    assert amounts(found[0]) == ("150.00", "151.00")
    assert found[0].field == AMOUNT_WITH_TAX


@pytest.mark.req("20.9", "6.5")
def test_a_line_whose_amounts_are_not_all_there_is_no_line_violation(sandbox: EngineEnv) -> None:
    note = only_id(sandbox, NOTE)
    tamper(sandbox, NOTE_LINE, f"{note}-1", {AMOUNT_WITH_TAX: None})

    # An empty total is no sum to compare with a net amount and a tax, so the line itself
    # is left alone; the header, whose own total counts that line as 0, is the one place
    # the empty value shows (requirements 6.4 and 6.5).
    assert reported(sandbox, (INV_3,)) == [(INV_3, HEADER_AMOUNT_MISMATCH, f"{NOTE} {note}")]
    assert amounts(violations(sandbox, (INV_3,))[0]) == ("0.00", "150.00")


# --- INV-6: the ids, the line ids and the numbers (requirement 4.10) ---------------------


@pytest.mark.req("4.10")
def test_one_id_in_two_tables_is_found(sandbox: EngineEnv) -> None:
    order = only_id(sandbox, ORDER)
    # A goods entry that carries the id of the supply order: the id is the primary key of
    # each table, so only a record written past the engine collides across two of them.
    add(sandbox, ENTRY, {ID_COLUMN: order})

    found = violations(sandbox, (INV_6,))

    assert reported(sandbox, (INV_6,)) == [
        (INV_6, DUPLICATE_ID, f"{ENTRY} {order}", f"{ORDER} {order}")
    ]
    assert amounts(found[0]) == (None, order)
    assert found[0].field == ID_COLUMN


@pytest.mark.req("4.10")
def test_one_line_id_in_two_tables_is_found(sandbox: EngineEnv) -> None:
    line = f"{only_id(sandbox, ORDER)}-1"
    add(sandbox, ENTRY_LINE, {ID_COLUMN: line})

    assert reported(sandbox, (INV_6,)) == [
        (INV_6, DUPLICATE_LINE_ID, f"{ENTRY_LINE} {line}", f"{ORDER_LINE} {line}")
    ]


@pytest.mark.req("4.10")
def test_one_number_on_two_bills_is_found(sandbox: EngineEnv) -> None:
    order = only_id(sandbox, ORDER)
    entry = only_id(sandbox, ENTRY)
    number = str(stored(sandbox, ORDER, order)[NUMBER])
    # The number of the order on the goods entry as well; it belongs to another prefix, so
    # it is a duplicate and no sequence above a counter of the entry.
    tamper(sandbox, ENTRY, entry, {NUMBER: number})

    found = violations(sandbox, (INV_6,))

    assert reported(sandbox, (INV_6,)) == [
        (INV_6, DUPLICATE_NUMBER, f"{ENTRY} {entry}", f"{ORDER} {order}")
    ]
    assert amounts(found[0]) == (None, number)
    assert found[0].field == NUMBER


@pytest.mark.req("4.10")
def test_an_id_whose_sequence_is_above_its_counter_is_found(sandbox: EngineEnv) -> None:
    # The id counter of the supply order stands at 1, so an id of 999 is one a later
    # allocation would hand out a second time (requirements 4.1 and 4.12).
    assert sandbox.counters()[("id", ORDER)] == 1
    high = format_id(ORDER_ID_PREFIX, 999)
    add(sandbox, ORDER, {ID_COLUMN: high})

    found = violations(sandbox, (INV_6,))

    assert reported(sandbox, (INV_6,)) == [(INV_6, ID_ABOVE_COUNTER, f"{ORDER} {high}")]
    assert amounts(found[0]) == ("1", "999")
    assert id_sequence(ORDER_ID_PREFIX, high) == 999


@pytest.mark.req("4.10")
def test_a_number_whose_sequence_is_above_its_counter_is_found(sandbox: EngineEnv) -> None:
    order = only_id(sandbox, ORDER)
    high = format_number(ORDER_NUMBER_PREFIX, sandbox.anchor_date, 9999)
    tamper(sandbox, ORDER, order, {NUMBER: high})

    found = violations(sandbox, (INV_6,))

    # The counter of (prefix, business date) stands at 1: one order was numbered that day.
    assert reported(sandbox, (INV_6,)) == [(INV_6, NUMBER_ABOVE_COUNTER, f"{ORDER} {order}")]
    assert amounts(found[0]) == ("1", "9999")
    assert number_parts(ORDER_NUMBER_PREFIX, high) == (sandbox.anchor_date, 9999)


@pytest.mark.req("4.10")
def test_an_id_no_allocation_can_hand_out_is_no_violation(sandbox: EngineEnv) -> None:
    # Neither text is one of the two forms for any sequence - the padding of the id is not
    # the one it is written with, the date of the number is no date - so neither can be
    # handed out and neither is compared with a counter (requirements 4.1 and 4.4).
    assert id_sequence(ORDER_ID_PREFIX, "so_0000999") is None
    assert number_parts(ORDER_NUMBER_PREFIX, "SPO202602309999") is None
    # Nor is a text whose tail is no run of digits: there is no sequence to compare.
    assert id_sequence(ORDER_ID_PREFIX, "so_00099x") is None
    assert number_parts(ORDER_NUMBER_PREFIX, "SPO2026101O0001") is None
    add(sandbox, ORDER, {ID_COLUMN: "so_0000999"})

    assert violations(sandbox, (INV_6,)) == []


# --- what check_invariants reports (requirement 20.9) ------------------------------------


@pytest.mark.req("20.9")
def test_only_the_invariants_the_caller_asks_for_are_checked(sandbox: EngineEnv) -> None:
    order = only_id(sandbox, ORDER)
    tamper(sandbox, STOCK_BALANCE_TABLE, balance_of(sandbox, WASHER), {QTY_BASE_COLUMN: None})
    tamper(sandbox, ORDER_LINE, f"{order}-1", {LINKED_QTY: None})

    # An empty field counts as 0 on either side, so both tampered records are found; the
    # two invariants nothing was written into report nothing.
    assert reasons(sandbox, (INV_1,)) == [BALANCE_MISMATCH]
    assert reasons(sandbox, (INV_2,)) == [LINK_FIELD_MISMATCH]
    assert reasons(sandbox, (INV_3,)) == []
    assert reasons(sandbox, (INV_6,)) == []
    assert reasons(sandbox, ()) == []


@pytest.mark.req("20.9")
def test_the_violations_come_in_invariant_order(sandbox: EngineEnv) -> None:
    order = only_id(sandbox, ORDER)
    tamper(sandbox, STOCK_BALANCE_TABLE, balance_of(sandbox, WASHER), {QTY_BASE_COLUMN: None})
    tamper(sandbox, ORDER_LINE, f"{order}-1", {LINKED_QTY: None})
    tamper(sandbox, NOTE, only_id(sandbox, NOTE), {AMOUNT: Decimal("0.01")})
    add(sandbox, ENTRY, {ID_COLUMN: order})

    asked = (INV_6, INV_3, INV_2, INV_1)

    # The order of INVARIANTS and not the order the caller asked in, so what a report shows
    # does not depend on the caller (requirement 20.9).
    assert numbers_of(sandbox, asked) == [INV_1, INV_2, INV_3, INV_6]
    assert reported(sandbox, asked) == reported(sandbox)


@pytest.mark.req("20.9")
def test_a_violation_reads_as_one_line(sandbox: EngineEnv) -> None:
    washers = balance_of(sandbox, WASHER)
    remove(sandbox, STOCK_BALANCE_TABLE, washers)

    found = violations(sandbox, (INV_1,))

    assert found[0].text == (
        f"INV-1 不成立（{BALANCE_MISMATCH}）：{STOCK_BALANCE_TABLE} {washers}"
        f" 的 {QTY_BASE_COLUMN}，应为 4，实际为 0"
    )


@pytest.mark.req("20.9")
def test_a_violation_of_a_link_field_names_the_conversion_that_keeps_it(
    sandbox: EngineEnv,
) -> None:
    order = only_id(sandbox, ORDER)
    tamper(sandbox, ORDER_LINE, f"{order}-1", {LINKED_QTY: WASHERS + Decimal("3")})

    found = violations(sandbox, (INV_2,))

    # A violation the engine keeps through a conversion says so between the place and the
    # two values (requirement 20.9).
    assert found[0].text == (
        f"INV-2 不成立（{LINK_FIELD_MISMATCH}）：{ORDER_LINE} {order}-1 的 {LINKED_QTY}"
        f"，由 {ORDER_TO_ENTRY} 维护，应为 4，实际为 7"
    )
