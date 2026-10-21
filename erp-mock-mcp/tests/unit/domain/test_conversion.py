"""erp_mock.domain.conversion: the draft a push generates and the quota it may take (req 8).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. The source bills are created, submitted and
approved the same way, so a push runs exactly as a tools/call runs it - one operation, one
transaction - and the draft the tests read is the draft the primitive wrote.

The fixture has one conversion per ``limit_by`` (tests/fixtures/packs/fx_trade/conversions):
``supply_order_to_entry`` pushes a pricing bill to a bill without pricing and is limited by
the base unit quantity of each source line, and ``fee_note_to_pay_slip`` pushes a simple
bill to a standard one and is limited by the amount of the source bill. Their packs name the
sources ``order`` and ``fee``, so the parameter that names the source bill is ``order_id``
and ``fee_id`` (requirement 11.13). What the tests pin:

- the draft: its lines answer to the call one by one, the header takes the
  ``header_mapping``, every line the ``line_mapping`` of its source line and the
  ``required_line_inputs`` of the caller, and the draft records ``source_bill_type``,
  ``source_bill_id``, ``source_line_id`` and the conversion that generated it
  (requirement 8.1);
- the defaults: a field the mapping or the inputs cover keeps what they gave it, an empty
  value included, and every other field takes the default of its own bill
  (requirement 8.2);
- the source bill is untouched, its state, its link field and its ``updated_seq`` included
  (requirement 8.12);
- R-GEN-004 with the field path of the source bill parameter and the state the bill is in,
  and a line of another bill reported before it (requirements 8.3, 8.4 and 12.10);
- R-GEN-005 for both quotas: an exact fit goes through, the lines that name one source line
  are checked together, a draft already counts against the quota, and the details are
  ordered by the line that names each source line first (requirements 8.5, 8.6 and 8.7).
"""

from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import LINE_SUFFIX
from erp_mock.domain.conversion import (
    EXCEEDS_AMOUNT_REASON,
    EXCEEDS_QTY_REASON,
    NOT_IN_SOURCE_REASON,
    QUOTA_RULE,
    SOURCE_BILL_TYPE,
    SOURCE_LINE_ID,
    SOURCE_STATUS_RULE,
    PushLine,
    occupied_qty,
    push_header,
    push_line,
    pushable_amount,
    pushable_qty,
    source_path,
    target_bill,
)
from erp_mock.domain.crud import (
    LINES_NOT_EDITABLE_REASON,
    NOT_FOUND_REASON,
    build_push_candidate,
)
from erp_mock.domain.numbering import CREATED_SEQ, UPDATED_SEQ
from erp_mock.domain.state_machine import INVALID_STATE_REASON, STATUS, StateAction
from erp_mock.domain.views import LINES, RecordView, RefSummary
from erp_mock.state.schema import BILL_ID_COLUMN, CONVERSION_COLUMN, ID_COLUMN
from erp_mock.state.store import Record, Value
from tests.helpers.engine_env import (
    ANCHOR_DATE,
    BOLT,
    BOX,
    BOX_FACTOR,
    MAIN_BRANCH,
    MAIN_DEPOT,
    MAIN_VENDOR,
    PCS,
    SPARE_DEPOT,
    WASHER,
    EngineEnv,
    as_record,
    engine_env,
    record_lines,
    summary_of,
)

# The two conversions of the fixture and the bills they run between.
ORDER_TO_ENTRY: Final = "supply_order_to_entry"
FEE_TO_SLIP: Final = "fee_note_to_pay_slip"
ORDER: Final = "supply_order"
ENTRY: Final = "goods_entry"
NOTE: Final = "fee_note"
SLIP: Final = "pay_slip"

# The field path of the parameter that names the source bill of each of them, built from
# the short name the pack declares (requirement 11.13).
ORDER_PARAM: Final = "order_id"
FEE_PARAM: Final = "fee_id"
ORDER_SHORT: Final = "order"
FEE_SHORT: Final = "fee"

# The fields of the two targets a test names.
ARTICLE: Final = "article_id"
MEASURE: Final = "measure_id"
DEPOT: Final = "depot_id"
ENTRY_QTY: Final = "entry_qty"
AMOUNT_WITH_TAX: Final = "amount_with_tax"

# The day after the anchor date, the required date of a supply order.
NEXT_DAY: Final = date(2026, 10, 10)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


# --- building the source bills ------------------------------------------------------------


def order_line(
    env: EngineEnv, article: str = BOLT, qty: str = "10", **values: Value
) -> dict[str, Value]:
    """One line of a supply order: ``qty`` of ``article`` at a price (requirement 6.1)."""
    line: dict[str, Value] = {
        ARTICLE: env.id(article),
        "qty": Decimal(qty),
        "price": Decimal("0.85"),
    }
    line.update(values)
    return line


def new_order(env: EngineEnv, lines: Sequence[Mapping[str, Value]] | None = None) -> str:
    """Create an approved supply order and return its id (requirements 7.1 and 7.2)."""
    order = draft_order(env, lines)
    approve(env, ORDER, order)
    return order


def draft_order(env: EngineEnv, lines: Sequence[Mapping[str, Value]] | None = None) -> str:
    """Create a supply order draft and return its id."""
    header: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "need_date": NEXT_DAY}
    rows = [order_line(env)] if lines is None else [dict(line) for line in lines]
    return record_id(env.create(ORDER, header, rows))


def note_line(env: EngineEnv, qty: str = "100", price: str = "1.5") -> dict[str, Value]:
    """One line of a fee note: the item, the quantity and the price (requirement 6.1)."""
    return {"item_text": "夹具运费", "qty": Decimal(qty), "price": Decimal(price)}


def new_note(
    env: EngineEnv,
    lines: Sequence[Mapping[str, Value]] | None = None,
    **header: Value,
) -> str:
    """Create an approved fee note and return its id; a simple bill approves from draft."""
    values: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "due_date": NEXT_DAY}
    values.update(header)
    rows = [note_line(env)] if lines is None else [dict(line) for line in lines]
    note = record_id(env.create(NOTE, values, rows))
    env.transition(NOTE, note, "approve")
    return note


def entry_line(
    env: EngineEnv, source_line_id: str, qty: str, depot: str = MAIN_DEPOT
) -> dict[str, Value]:
    """One line of a push to a goods entry: the ``required_line_inputs`` of the conversion."""
    return {
        SOURCE_LINE_ID: source_line_id,
        ENTRY_QTY: Decimal(qty),
        DEPOT: env.id(depot),
    }


def slip_line(source_line_id: str, qty: str, price: str) -> dict[str, Value]:
    """One line of a push to a pay slip: the ``required_line_inputs`` of the conversion."""
    return {SOURCE_LINE_ID: source_line_id, "qty": Decimal(qty), "price": Decimal(price)}


def approve(env: EngineEnv, bill: str, bill_id: str) -> None:
    """Submit and approve the standard bill ``bill_id`` (requirement 7.1)."""
    env.transition(bill, bill_id, "submit")
    env.transition(bill, bill_id, "approve")


# --- reading results ----------------------------------------------------------------------


def record_id(result: object) -> str:
    """The internal id of the record a write operation returned (requirement 5.16)."""
    assert hasattr(result, "view")
    return str(as_record(result.view).fields[ID_COLUMN])


def field(view: RecordView, name: str) -> Value:
    """One value of a result object; the lines and the summaries have their own readers."""
    value = view.fields[name]
    assert not isinstance(value, (RefSummary, tuple))
    return value


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


def refused(
    env: EngineEnv, conversion: str, source_id: str, lines: Sequence[Mapping[str, Value]]
) -> ErpError:
    """Push and return the error it ended in; the short names are those of the fixture."""
    with pytest.raises(ErpError) as raised:
        env.push(conversion, source_id, lines, source_name=_short_name(conversion))
    return raised.value


def pushed(
    env: EngineEnv, conversion: str, source_id: str, lines: Sequence[Mapping[str, Value]]
) -> RecordView:
    """Push and return the draft it generated (requirements 8.1 and 13.2)."""
    result = env.push(conversion, source_id, lines, source_name=_short_name(conversion))
    return as_record(result.view)


def _short_name(conversion: str) -> str:
    """The short name the pack of ``conversion`` gives its source (requirement 11.13)."""
    return ORDER_SHORT if conversion == ORDER_TO_ENTRY else FEE_SHORT


def stored_bill(env: EngineEnv, bill: str, bill_id: str) -> tuple[Record, list[Record]]:
    """The header and the lines of ``bill_id`` as storage holds them, internal columns too."""
    header = env.record(bill, bill_id)
    assert header is not None, f"{bill} {bill_id} is not in the sandbox"
    lines = [
        line for line in env.records(f"{bill}{LINE_SUFFIX}") if line.get(BILL_ID_COLUMN) == bill_id
    ]
    return header, lines


# --- the draft a push generates (requirements 8.1 and 8.2) -------------------------------


@pytest.mark.req("8.1", "13.2")
def test_a_push_maps_the_header_the_lines_and_where_they_came_from(env: EngineEnv) -> None:
    # The bolt is ordered by the box, which is BOX_FACTOR base units; the washer by the
    # piece (tests.helpers.engine_env).
    order = new_order(env, [order_line(env, BOLT, "10"), order_line(env, WASHER, "50")])

    draft = pushed(
        env,
        ORDER_TO_ENTRY,
        order,
        [entry_line(env, f"{order}-2", "20", SPARE_DEPOT), entry_line(env, f"{order}-1", "4")],
    )

    # The header: the mapping of the conversion, then where the draft came from.
    assert summary_of(draft, "vendor")[ID_COLUMN] == env.id(MAIN_VENDOR)
    assert field(draft, SOURCE_BILL_TYPE) == ORDER
    assert summary_of(draft, "source_bill")[ID_COLUMN] == order
    assert field(draft, STATUS) == "draft"
    # The lines answer to the call one by one and in the same order (requirement 8.1).
    lines = record_lines(draft)
    assert [field(line, SOURCE_LINE_ID) for line in lines] == [f"{order}-2", f"{order}-1"]
    assert [summary_of(line, "article")[ID_COLUMN] for line in lines] == [
        env.id(WASHER),
        env.id(BOLT),
    ]
    # The unit comes from the source line, not from the default of the target's own field,
    # and the base unit quantity follows it (requirements 8.1 and 6.6).
    assert [summary_of(line, "measure")[ID_COLUMN] for line in lines] == [
        env.id(PCS),
        env.id(BOX),
    ]
    assert [field(line, "qty_base") for line in lines] == [
        Decimal("20"),
        Decimal("4") * BOX_FACTOR,
    ]
    # The caller's inputs stand as sent.
    assert [summary_of(line, "depot")[ID_COLUMN] for line in lines] == [
        env.id(SPARE_DEPOT),
        env.id(MAIN_DEPOT),
    ]
    assert [field(line, ENTRY_QTY) for line in lines] == [Decimal("20"), Decimal("4")]


@pytest.mark.req("8.1", "4.5")
def test_a_pushed_draft_is_numbered_and_sequenced_like_a_created_bill(env: EngineEnv) -> None:
    order = new_order(env)

    draft = pushed(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "1")])

    assert field(draft, ID_COLUMN) == "ge_000001"
    assert field(draft, "number") == "GRN202610090001"
    # The push is one write operation, so both sequence numbers of the new bill are its own
    # operation sequence number (requirement 4.5).
    created = field(draft, CREATED_SEQ)
    assert created == field(draft, UPDATED_SEQ)
    assert created == env.operations + 4  # the order, its submit, its approve, the push
    # The conversion that generated the draft is kept in the internal column the quota is
    # counted per (design.md 4.2), which no result shows (requirement 5.12).
    header, _ = stored_bill(env, ENTRY, "ge_000001")
    assert header[CONVERSION_COLUMN] == ORDER_TO_ENTRY
    assert CONVERSION_COLUMN not in draft.fields


@pytest.mark.req("8.2")
def test_a_pushed_draft_takes_defaults_only_where_the_push_covers_nothing(env: EngineEnv) -> None:
    # price_includes_tax is mapped from the source, so the false of the note stands against
    # the engine's own default of true (requirements 8.2 and 6.3).
    note = new_note(env, [note_line(env, "100", "1.5")], price_includes_tax=False)

    draft = pushed(env, FEE_TO_SLIP, note, [slip_line(f"{note}-1", "100", "1.5")])

    assert field(draft, "price_includes_tax") is False
    # pay_mode is covered by neither the mapping nor the inputs, so it takes the constant
    # its own bill declares; the business date and the organization take the context
    # (requirements 8.2 and 5.1).
    assert field(draft, "pay_mode") == "transfer"
    assert field(draft, "biz_date") == ANCHOR_DATE
    assert summary_of(draft, "org")[ID_COLUMN] == env.id(MAIN_BRANCH)
    # The line takes its item and its tax rate from the source line and its quantity and
    # price from the caller; the amounts follow from them (requirements 8.1 and 6.1).
    line = record_lines(draft)[0]
    assert field(line, "item_text") == "夹具运费"
    assert field(line, "tax_rate") == Decimal("0.06")
    assert field(line, AMOUNT_WITH_TAX) == Decimal("159.00")


@pytest.mark.req("8.2")
def test_a_field_the_mapping_covers_takes_no_default_when_the_source_leaves_it_empty(
    env: EngineEnv,
) -> None:
    conversion = env.domain.conversions[ORDER_TO_ENTRY]
    order = new_order(env)
    header, lines = stored_bill(env, ORDER, order)
    # A source line without the mapped unit: the mapping covers the field of the target, so
    # the value it brings is empty rather than missing (requirement 8.2).
    line = PushLine(
        index=0,
        args=FrozenMap(entry_line(env, f"{order}-1", "4")),
        source_line_id=f"{order}-1",
        record=FrozenMap({**lines[0], MEASURE: None}),
    )
    values = push_line(conversion, line)
    assert values[MEASURE] is None

    entry = target_bill(env.domain, conversion)
    with env.store.transaction() as uow:
        covered = build_push_candidate(
            uow,
            env.domain,
            entry,
            FrozenMap(push_header(conversion, order, header)),
            [FrozenMap(values)],
            anchor_date=env.anchor_date,
        )
        uncovered = build_push_candidate(
            uow,
            env.domain,
            entry,
            FrozenMap(push_header(conversion, order, header)),
            [FrozenMap({name: value for name, value in values.items() if name != MEASURE})],
            anchor_date=env.anchor_date,
        )

    # The covered field stays empty; the same field takes its ``default_from`` where the
    # push does not cover it at all (requirements 8.2 and 5.1).
    assert covered.lines[0][MEASURE] is None
    assert uncovered.lines[0][MEASURE] == env.id(PCS)


@pytest.mark.req("8.1")
def test_a_line_input_the_conversion_does_not_declare_is_refused(env: EngineEnv) -> None:
    order = new_order(env)

    # The lines of a push tool offer a parameter for the ``required_line_inputs`` and for
    # nothing else (requirement 11.4), so a value outside them is a programming error
    # rather than a value that is quietly dropped.
    with pytest.raises(KeyError, match="remark"):
        env.push(
            ORDER_TO_ENTRY,
            order,
            [{**entry_line(env, f"{order}-1", "1"), "remark": "夹具备注"}],
            source_name=ORDER_SHORT,
        )


@pytest.mark.req("8.12")
def test_a_push_leaves_the_source_bill_as_it_was(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, BOLT, "10")])
    before = stored_bill(env, ORDER, order)

    pushed(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "4")])

    # Every field of the source, its state, its link field and its sequence numbers
    # included (requirement 8.12).
    assert stored_bill(env, ORDER, order) == before


@pytest.mark.req("8.1", "5.4")
def test_the_lines_of_a_pushed_draft_may_not_be_replaced(env: EngineEnv) -> None:
    order = new_order(env)
    draft = pushed(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "4")])
    entry = str(field(draft, ID_COLUMN))

    with pytest.raises(ErpError) as raised:
        env.update(ENTRY, entry, lines=[entry_line(env, f"{order}-1", "5")])

    # The draft carries the source bill it came from, which is what makes the update refuse
    # (requirements 8.1 and 5.4).
    assert raised.value.code == ErrorCode.VALIDATION_FAILED
    assert details(raised.value) == [{"field": LINES, "reason": LINES_NOT_EDITABLE_REASON}]


# --- the references and the ownership of the source lines (requirements 5.6 and 8.4) -----


@pytest.mark.req("5.6")
def test_a_source_bill_or_source_line_that_is_not_there_is_not_found(env: EngineEnv) -> None:
    order = new_order(env)

    error = refused(
        env,
        ORDER_TO_ENTRY,
        order,
        [entry_line(env, f"{order}-1", "1"), entry_line(env, f"{order}-9", "1")],
    )

    assert error.code == ErrorCode.NOT_FOUND
    assert details(error) == [{"field": f"{LINES}[1].{SOURCE_LINE_ID}", "reason": NOT_FOUND_REASON}]


@pytest.mark.req("5.6")
def test_the_source_bill_and_the_line_inputs_are_reported_together(env: EngineEnv) -> None:
    order = new_order(env)

    error = refused(
        env,
        ORDER_TO_ENTRY,
        "so_999999",
        [{SOURCE_LINE_ID: f"{order}-1", ENTRY_QTY: Decimal("1"), DEPOT: "dep_999999"}],
    )

    # The source bill first, under the path of its own parameter, then the references of
    # each line (requirement 5.6).
    assert error.code == ErrorCode.NOT_FOUND
    assert details(error) == [
        {"field": ORDER_PARAM, "reason": NOT_FOUND_REASON},
        {"field": f"{LINES}[0].{DEPOT}", "reason": NOT_FOUND_REASON},
    ]


@pytest.mark.req("8.4", "12.10")
def test_a_source_line_of_another_bill_is_reported_before_the_state_of_the_source(
    env: EngineEnv,
) -> None:
    approved = new_order(env)
    # The bill the push names is still a draft, so R-GEN-004 would refuse it as well; the
    # ownership of the source lines is checked first (requirements 8.4 and 12.10).
    other = draft_order(env)

    error = refused(
        env,
        ORDER_TO_ENTRY,
        other,
        [entry_line(env, f"{other}-1", "1"), entry_line(env, f"{approved}-1", "1")],
    )

    assert (error.code, error.rule_id) == (ErrorCode.VALIDATION_FAILED, None)
    assert details(error) == [
        {"field": f"{LINES}[1].{SOURCE_LINE_ID}", "reason": NOT_IN_SOURCE_REASON}
    ]


@pytest.mark.req("8.4")
def test_every_line_of_another_bill_is_reported_in_line_order(env: EngineEnv) -> None:
    order = new_order(env)
    other = new_order(env)

    error = refused(
        env,
        ORDER_TO_ENTRY,
        order,
        [
            entry_line(env, f"{other}-1", "1"),
            entry_line(env, f"{order}-1", "1"),
            entry_line(env, f"{other}-1", "1"),
        ],
    )

    assert details(error) == [
        {"field": f"{LINES}[0].{SOURCE_LINE_ID}", "reason": NOT_IN_SOURCE_REASON},
        {"field": f"{LINES}[2].{SOURCE_LINE_ID}", "reason": NOT_IN_SOURCE_REASON},
    ]


# --- R-GEN-004: the state of the source bill (requirement 8.3) ---------------------------


@pytest.mark.req("8.3")
@pytest.mark.parametrize(
    ("actions", "status"),
    [
        pytest.param((), "draft", id="draft"),
        pytest.param(("submit",), "submitted", id="submitted"),
        pytest.param(("submit", "approve", "close"), "closed", id="closed"),
    ],
)
def test_a_source_bill_in_another_state_breaks_r_gen_004(
    env: EngineEnv, actions: tuple[StateAction, ...], status: str
) -> None:
    order = draft_order(env)
    for action in actions:
        env.transition(ORDER, order, action)

    error = refused(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "1")])

    assert (error.code, error.rule_id) == (ErrorCode.INVALID_STATE, SOURCE_STATUS_RULE)
    # The detail item names the parameter the source bill came from and carries its state
    # (requirement 8.3, design.md data model 6).
    assert details(error) == [
        {"field": ORDER_PARAM, "reason": INVALID_STATE_REASON, "status": status}
    ]
    # Nothing was written: the draft of the target was never stored (requirement 12.7).
    assert env.records(ENTRY) == []


@pytest.mark.req("8.3", "11.13")
def test_the_state_error_names_the_source_bill_itself_without_a_short_name(
    env: EngineEnv,
) -> None:
    order = draft_order(env)

    with pytest.raises(ErpError) as raised:
        env.push(ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "1")])

    # A caller that knows no short name falls back to the name of the source bill, which is
    # what requirement 11.13 does for a pack that declares none.
    assert details(raised.value) == [
        {"field": f"{ORDER}_id", "reason": INVALID_STATE_REASON, "status": "draft"}
    ]
    assert source_path(env.domain.conversions[ORDER_TO_ENTRY]) == f"{ORDER}_id"


@pytest.mark.req("8.3")
def test_a_source_bill_in_the_required_state_goes_through(env: EngineEnv) -> None:
    order = new_order(env)

    draft = pushed(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "1")])

    assert field(draft, STATUS) == "draft"


# --- R-GEN-005 by quantity (requirements 8.5 and 8.7) ------------------------------------


@pytest.mark.req("8.5")
def test_a_push_that_uses_the_quota_exactly_goes_through(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, BOLT, "10")])

    draft = pushed(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "10")])

    # The order line is 10 boxes, which is 10 × BOX_FACTOR base units; the push takes all
    # of them, and an exact fit is allowed (requirement 8.5).
    assert field(record_lines(draft)[0], "qty_base") == Decimal("10") * BOX_FACTOR


@pytest.mark.req("8.5")
def test_a_push_beyond_the_quota_of_a_source_line_breaks_r_gen_005(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, BOLT, "10")])

    error = refused(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "11")])

    assert (error.code, error.rule_id) == (ErrorCode.BUSINESS_RULE_VIOLATION, QUOTA_RULE)
    # The item carries the quantity field path of the line, the source line and what was
    # left, in the base unit (requirements 8.5 and 13.4).
    assert details(error) == [
        {
            "field": f"{LINES}[0].{ENTRY_QTY}",
            "reason": EXCEEDS_QTY_REASON,
            SOURCE_LINE_ID: f"{order}-1",
            "pushable_qty": "120",
        }
    ]
    assert env.records(ENTRY) == []


@pytest.mark.req("8.7")
def test_the_lines_that_name_one_source_line_are_checked_together(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, BOLT, "10")])

    # Two warehouses, five boxes each: together they are the whole quota, so the push goes
    # through (requirements 8.7 and 8.5).
    draft = pushed(
        env,
        ORDER_TO_ENTRY,
        order,
        [
            entry_line(env, f"{order}-1", "5", MAIN_DEPOT),
            entry_line(env, f"{order}-1", "5", SPARE_DEPOT),
        ],
    )
    assert [field(line, "qty_base") for line in record_lines(draft)] == [
        Decimal("5") * BOX_FACTOR,
        Decimal("5") * BOX_FACTOR,
    ]

    # The same two lines on an untouched order line, one box more between them: the sum is
    # one box too many, and the one source line yields one detail item, which names the
    # line that came first (requirements 8.7 and 8.5).
    beyond = new_order(env, [order_line(env, BOLT, "10")])
    error = refused(
        env,
        ORDER_TO_ENTRY,
        beyond,
        [
            entry_line(env, f"{beyond}-1", "5", MAIN_DEPOT),
            entry_line(env, f"{beyond}-1", "6", SPARE_DEPOT),
        ],
    )
    assert details(error) == [
        {
            "field": f"{LINES}[0].{ENTRY_QTY}",
            "reason": EXCEEDS_QTY_REASON,
            SOURCE_LINE_ID: f"{beyond}-1",
            "pushable_qty": "120",
        }
    ]


@pytest.mark.req("8.5")
def test_a_draft_the_conversion_generated_already_takes_from_the_quota(env: EngineEnv) -> None:
    order = new_order(env, [order_line(env, BOLT, "10")])
    pushed(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "4")])

    error = refused(env, ORDER_TO_ENTRY, order, [entry_line(env, f"{order}-1", "7")])

    # The first draft takes 4 × BOX_FACTOR of the 10 × BOX_FACTOR, so 6 boxes are left and
    # 7 are too many; the occupied quantity counts a draft (requirement 8.5).
    assert details(error) == [
        {
            "field": f"{LINES}[0].{ENTRY_QTY}",
            "reason": EXCEEDS_QTY_REASON,
            SOURCE_LINE_ID: f"{order}-1",
            "pushable_qty": "72",
        }
    ]
    conversion = env.domain.conversions[ORDER_TO_ENTRY]
    entry = target_bill(env.domain, conversion)
    line = env.record(f"{ORDER}{LINE_SUFFIX}", f"{order}-1")
    assert line is not None
    with env.store.transaction() as uow:
        assert occupied_qty(uow, conversion, entry, f"{order}-1") == Decimal("4") * BOX_FACTOR
        assert pushable_qty(uow, conversion, entry, line) == Decimal("6") * BOX_FACTOR


@pytest.mark.req("8.5")
def test_every_source_line_over_its_quota_is_reported_by_first_occurrence(
    env: EngineEnv,
) -> None:
    order = new_order(env, [order_line(env, BOLT, "10"), order_line(env, WASHER, "50")])

    error = refused(
        env,
        ORDER_TO_ENTRY,
        order,
        [
            entry_line(env, f"{order}-2", "40"),
            entry_line(env, f"{order}-1", "11"),
            entry_line(env, f"{order}-2", "20"),
        ],
    )

    # The washer line is over its quota through its first and third line together, and the
    # bolt line through its second; the items follow the first line of each source line
    # (requirements 8.5 and 8.7).
    assert details(error) == [
        {
            "field": f"{LINES}[0].{ENTRY_QTY}",
            "reason": EXCEEDS_QTY_REASON,
            SOURCE_LINE_ID: f"{order}-2",
            "pushable_qty": "50",
        },
        {
            "field": f"{LINES}[1].{ENTRY_QTY}",
            "reason": EXCEEDS_QTY_REASON,
            SOURCE_LINE_ID: f"{order}-1",
            "pushable_qty": "120",
        },
    ]


# --- R-GEN-005 by amount (requirement 8.6) -----------------------------------------------


@pytest.mark.req("8.6")
def test_a_push_of_the_whole_amount_of_the_source_bill_goes_through(env: EngineEnv) -> None:
    note = new_note(env, [note_line(env, "100", "1.5")])

    draft = pushed(env, FEE_TO_SLIP, note, [slip_line(f"{note}-1", "100", "1.5")])

    # The note is 150.00 including tax and so is the draft: an exact fit is allowed
    # (requirement 8.6).
    assert field(draft, AMOUNT_WITH_TAX) == Decimal("150.00")


@pytest.mark.req("8.6")
def test_a_push_beyond_the_amount_of_the_source_bill_breaks_r_gen_005(env: EngineEnv) -> None:
    note = new_note(env, [note_line(env, "100", "1.5")])

    error = refused(env, FEE_TO_SLIP, note, [slip_line(f"{note}-1", "101", "1.5")])

    assert (error.code, error.rule_id) == (ErrorCode.BUSINESS_RULE_VIOLATION, QUOTA_RULE)
    # One item, which names no input field and carries the amount that was left
    # (requirements 8.6 and 13.4).
    assert details(error) == [{"reason": EXCEEDS_AMOUNT_REASON, "pushable_amount": "150.00"}]
    assert env.records(SLIP) == []


@pytest.mark.req("8.6")
def test_a_draft_of_the_conversion_already_takes_from_the_amount(env: EngineEnv) -> None:
    note = new_note(env, [note_line(env, "100", "1.5")])
    pushed(env, FEE_TO_SLIP, note, [slip_line(f"{note}-1", "40", "1.5")])

    error = refused(env, FEE_TO_SLIP, note, [slip_line(f"{note}-1", "61", "1.5")])

    # 60.00 of the 150.00 is taken by the first draft, so 90.00 is left (requirement 8.6).
    assert details(error) == [{"reason": EXCEEDS_AMOUNT_REASON, "pushable_amount": "90.00"}]
    conversion = env.domain.conversions[FEE_TO_SLIP]
    header = env.record(NOTE, note)
    assert header is not None
    with env.store.transaction() as uow:
        assert pushable_amount(uow, conversion, target_bill(env.domain, conversion), header) == (
            Decimal("90.00")
        )
