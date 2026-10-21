"""erp_mock.domain.crud: the update, the delete and the uniqueness check (req 5.2 to 5.8).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. So an update and a delete are exercised
exactly as a tools/call runs them - one operation, one transaction - on records this
primitive wrote itself. What the tests pin:

- an update takes the caller's header fields and leaves every other field of the record as
  it is, the number of the bill included, and recomputes the computed fields on the merged
  bill: changing ``price_includes_tax`` alone moves the amounts of every line and of the
  header (requirements 5.2 and 6.1 to 6.4);
- ``lines`` replaces the lines as a whole, their numbers running from 1 again and their ids
  built from those numbers, the new lines taking the defaults of a create; a call without
  ``lines`` leaves the stored lines in place (requirements 5.3, 5.1 and 4.2);
- R-GEN-006 looks at the references an update writes and at no others, so a reference the
  record already held and the call leaves alone is not checked (requirement 5.7);
- an update of a bill a push generated cannot replace its lines: VALIDATION_FAILED whose
  one detail item names ``lines`` (requirement 5.4);
- a delete removes the bill with every one of its lines, so a get of its id afterwards ends
  in NOT_FOUND, and it returns the object the record had before it was removed
  (requirements 5.5 and 13.9); an entity record another record refers to is not removed and
  the call ends in CONFLICT with the reason ``referenced`` under the ``id`` parameter
  (design.md 4.2);
- a unique value another record already holds ends a create or an update in CONFLICT whose
  detail names the field path; the comparison is case sensitive, an empty value takes no
  part in it and a record is no duplicate of itself (requirement 5.8);
- an update returns the object a get of the record returns right after it, the sequence
  numbers included (requirements 5.16 and 4.5).

What is not here: the state the bill has to be in, which R-GEN-001 decides in the
``bill_status`` slot before any of this runs (requirements 7.3 and 12.1).
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import DomainModel, EntityModel
from erp_mock.domain.crud import (
    DISABLED_REASON,
    DISABLED_RULE,
    DUPLICATE_REASON,
    LINES_NOT_EDITABLE_REASON,
    NOT_FOUND_REASON,
    REFERENCED_REASON,
    Candidate,
    build_update_candidate,
    referring_record,
    unique_conflicts,
    unique_values,
)
from erp_mock.domain.engine import GetOp
from erp_mock.domain.numbering import CREATED_SEQ, UPDATED_SEQ
from erp_mock.domain.views import RecordView, ViewValue
from erp_mock.state.store import Value
from tests.helpers.engine_env import (
    BOLT,
    BOX,
    MAIN_BRANCH,
    MAIN_DEPOT,
    MAIN_VENDOR,
    PCS,
    SPARE_BRANCH,
    SPARE_DEPOT,
    SPARE_VENDOR,
    WASHER,
    EngineEnv,
    arguments,
    as_record,
    engine_env,
    record_lines,
    summary_of,
)

# The bills and the entity these tests work on: the pricing bill whose lines take their
# unit and tax rate from their material, the bill a push generates, and the organization
# entity, whose code is unique (tests/fixtures).
ORDER: Final = "supply_order"
ENTRY: Final = "goods_entry"
BRANCH: Final = "branch"
VENDOR: Final = "vendor"

# A date the caller may send as the business date or the required date.
NEXT_DAY: Final = date(2026, 10, 10)
LATER_DAY: Final = date(2026, 10, 20)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


def order_line(env: EngineEnv, **values: Value) -> dict[str, Value]:
    """One line of a supply order: 2000 bolts at 0.85, taxed at the bolt's own rate."""
    line: dict[str, Value] = {
        "article_id": env.id(BOLT),
        "qty": Decimal("2000"),
        "price": Decimal("0.85"),
    }
    line.update(values)
    return line


def washer_line(env: EngineEnv, **values: Value) -> dict[str, Value]:
    """One line of a supply order: 10 washers at 1.50, taxed at the washer's rate."""
    line: dict[str, Value] = {
        "article_id": env.id(WASHER),
        "qty": Decimal("10"),
        "price": Decimal("1.50"),
    }
    line.update(values)
    return line


def create_order(env: EngineEnv, **header: Value) -> dict[str, Value]:
    """The header of a supply order: the enabled partner and a required date."""
    values: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "need_date": NEXT_DAY}
    values.update(header)
    return values


def new_order(env: EngineEnv, *lines: dict[str, Value], **header: Value) -> RecordView:
    """One created supply order with ``lines``, as the create returned it."""
    return as_record(env.create(ORDER, create_order(env, **header), list(lines)).view)


def record_id(view: RecordView) -> str:
    """The internal id of the record ``view`` returns (requirement 4.1)."""
    value = view.fields["id"]
    assert isinstance(value, str), "every record carries an id"
    return value


def amounts(view: RecordView) -> tuple[ViewValue, ViewValue, ViewValue]:
    """The three money fields of a header or a line, in field order (requirement 6.4)."""
    return (view.fields["amount"], view.fields["tax_amount"], view.fields["amount_with_tax"])


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


# --- the merge of an update (requirement 5.2) --------------------------------------------


@pytest.mark.req("5.2", "4.4")
def test_the_caller_fields_are_written_and_every_other_field_stays(env: EngineEnv) -> None:
    created = new_order(env, order_line(env))

    updated = as_record(
        env.update(ORDER, record_id(created), {"need_date": LATER_DAY, "biz_date": NEXT_DAY}).view
    )

    assert (updated.fields["need_date"], updated.fields["biz_date"]) == (LATER_DAY, NEXT_DAY)
    # Everything the call did not name stays: the partner, the flag the pack defaults to
    # false, and the number, which does not follow the new business date (requirement 4.4).
    assert summary_of(updated, "vendor")["id"] == env.id(MAIN_VENDOR)
    assert updated.fields["price_includes_tax"] is False
    assert updated.fields["number"] == created.fields["number"]
    assert updated.fields["id"] == created.fields["id"]


@pytest.mark.req("5.2")
def test_a_value_the_caller_sent_empty_leaves_the_field_as_it_is(env: EngineEnv) -> None:
    created = new_order(env, order_line(env), remark="第一版")

    updated = as_record(env.update(ORDER, record_id(created), {"remark": None}).view)

    # An empty value counts as not sent, as it does for a create (requirement 5.1).
    assert updated.fields["remark"] == "第一版"


@pytest.mark.req("5.2")
def test_an_update_without_lines_leaves_the_lines_as_they_are(env: EngineEnv) -> None:
    created = new_order(env, order_line(env), washer_line(env))
    order_id = record_id(created)

    result = env.update(ORDER, order_id, {"need_date": LATER_DAY})

    lines = record_lines(as_record(result.view))
    assert [line.fields["id"] for line in lines] == [f"{order_id}-1", f"{order_id}-2"]
    assert lines == record_lines(created)
    # The header alone changed, so the lines are not part of what the call wrote
    # (requirement 17.4): they were stored again with the values they already held.
    assert [(write.entity, write.op, write.id) for write in result.writes] == [
        (ORDER, "update", order_id)
    ]


@pytest.mark.req("5.2", "6.1", "6.2")
def test_changing_price_includes_tax_alone_recomputes_every_amount(env: EngineEnv) -> None:
    created = new_order(env, order_line(env), washer_line(env))

    # The pack declares false for the order, so the price was net until now.
    assert amounts(created) == (Decimal("1715.00"), Decimal("222.35"), Decimal("1937.35"))
    assert [amounts(line) for line in record_lines(created)] == [
        (Decimal("1700.00"), Decimal("221.00"), Decimal("1921.00")),
        (Decimal("15.00"), Decimal("1.35"), Decimal("16.35")),
    ]

    updated = as_record(env.update(ORDER, record_id(created), {"price_includes_tax": True}).view)

    # The same product now reads as the total, so every line splits it the other way
    # (requirement 6.2) and the header follows its lines (requirement 6.4).
    assert [amounts(line) for line in record_lines(updated)] == [
        (Decimal("1504.42"), Decimal("195.58"), Decimal("1700.00")),
        (Decimal("13.76"), Decimal("1.24"), Decimal("15.00")),
    ]
    assert amounts(updated) == (Decimal("1518.18"), Decimal("196.82"), Decimal("1715.00"))


@pytest.mark.req("5.2")
def test_an_update_of_an_entity_record_writes_the_fields_it_names(env: EngineEnv) -> None:
    created = as_record(env.create(VENDOR, {"name": "夹具新供货单位", "pay_days": 60}).view)

    updated = as_record(env.update(VENDOR, record_id(created), {"name": "夹具改名供货单位"}).view)

    assert updated.fields["name"] == "夹具改名供货单位"
    # The generated code and the fields the call did not name stay as they were.
    assert updated.fields["code"] == created.fields["code"]
    assert updated.fields["pay_days"] == 60
    assert updated.fields["status"] == "enabled"


# --- lines replaced as a whole (requirement 5.3) -----------------------------------------


@pytest.mark.req("5.3", "4.2")
def test_lines_replace_the_stored_ones_and_are_numbered_from_one(env: EngineEnv) -> None:
    created = new_order(env, order_line(env), order_line(env), order_line(env))
    order_id = record_id(created)
    assert [line.fields["id"] for line in record_lines(created)] == [
        f"{order_id}-1",
        f"{order_id}-2",
        f"{order_id}-3",
    ]

    updated = as_record(env.update(ORDER, order_id, lines=[washer_line(env), order_line(env)]).view)

    lines = record_lines(updated)
    # Two lines answer to the array one by one, numbered 1 and 2, and the third line of the
    # bill is gone; a line id is the bill id and the line number (requirement 4.2).
    assert [line.fields["line_no"] for line in lines] == [1, 2]
    assert [line.fields["id"] for line in lines] == [f"{order_id}-1", f"{order_id}-2"]
    assert len(env.records(f"{ORDER}_line")) == 2
    # The new lines took the defaults of a create: the unit of the material and its rate
    # (requirement 5.1).
    assert [summary_of(line, "measure")["id"] for line in lines] == [env.id(PCS), env.id(BOX)]
    assert [line.fields["tax_rate"] for line in lines] == [Decimal("0.09"), Decimal("0.13")]


@pytest.mark.req("5.3")
def test_an_empty_lines_array_leaves_the_bill_without_lines(env: EngineEnv) -> None:
    created = new_order(env, order_line(env))

    updated = as_record(env.update(ORDER, record_id(created), lines=[]).view)

    assert record_lines(updated) == ()
    assert env.records(f"{ORDER}_line") == []
    # A bill without lines carries three zeros on its header (requirement 6.4).
    assert amounts(updated) == (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))


@pytest.mark.req("5.3", "5.7")
def test_the_rule_looks_at_the_references_of_the_new_lines(env: EngineEnv) -> None:
    created = new_order(env, order_line(env))

    with pytest.raises(ErpError) as raised:
        env.update(ORDER, record_id(created), lines=[order_line(env, article_id="art_000099")])

    # A reference of a replaced line is a reference the call writes (requirements 5.6).
    assert raised.value.code is ErrorCode.NOT_FOUND
    assert details(raised.value) == [{"field": "lines[0].article_id", "reason": NOT_FOUND_REASON}]
    # Nothing was written: the stored line is still there (requirement 12.7).
    assert len(env.records(f"{ORDER}_line")) == 1


@pytest.mark.req("5.7")
def test_an_update_checks_the_references_it_writes_and_no_others(env: EngineEnv) -> None:
    created = new_order(env, order_line(env))
    order_id = record_id(created)
    # The partner the order already references is retired after the order was written.
    env.update(VENDOR, env.id(MAIN_VENDOR), {"status": "disabled"})

    updated = as_record(env.update(ORDER, order_id, {"need_date": LATER_DAY}).view)

    # A reference the bill holds and this call does not write is not checked (req 5.7).
    assert updated.fields["need_date"] == LATER_DAY
    with pytest.raises(ErpError) as raised:
        env.update(ORDER, order_id, {"vendor_id": env.id(MAIN_VENDOR)})
    error = raised.value
    assert error.code is ErrorCode.BUSINESS_RULE_VIOLATION
    assert error.rule_id == DISABLED_RULE
    assert details(error) == [{"field": "vendor_id", "reason": DISABLED_REASON}]


# --- the lines of a pushed bill (requirement 5.4) ----------------------------------------


def pushed_entry(env: EngineEnv, source_id: str) -> str:
    """One goods entry marked as generated by a push from the bill ``source_id``.

    The push primitive arrives with its own task (design.md 4.2), so the two header fields
    it writes are set through a unit of work here.
    """
    entry = as_record(
        env.create(
            ENTRY,
            {"vendor_id": env.id(MAIN_VENDOR)},
            [{"article_id": env.id(BOLT), "depot_id": env.id(MAIN_DEPOT), "entry_qty": Decimal(2)}],
        ).view
    )
    entry_id = record_id(entry)
    with env.store.transaction() as uow:
        uow.update(ENTRY, entry_id, {"source_bill_type": ORDER, "source_bill_id": source_id})
    return entry_id


@pytest.mark.req("5.4")
def test_an_update_of_a_pushed_bill_cannot_replace_its_lines(env: EngineEnv) -> None:
    order_id = record_id(new_order(env, order_line(env)))
    entry_id = pushed_entry(env, order_id)

    with pytest.raises(ErpError) as raised:
        env.update(ENTRY, entry_id, lines=[{"entry_qty": Decimal(5)}])

    error = raised.value
    assert error.code is ErrorCode.VALIDATION_FAILED
    # No rule decides it (requirement 13.8), and the one detail item names the parameter.
    assert error.rule_id is None
    assert details(error) == [{"field": "lines", "reason": LINES_NOT_EDITABLE_REASON}]
    assert "lines" in error.message
    # The lines of the bill are as the push left them (requirement 12.7).
    entry = as_record(env.run(GetOp(ENTRY, entry_id)).view)
    assert [line.fields["entry_qty"] for line in record_lines(entry)] == [Decimal(2)]


@pytest.mark.req("5.4", "5.2")
def test_an_update_of_a_pushed_bill_without_lines_goes_through(env: EngineEnv) -> None:
    order_id = record_id(new_order(env, order_line(env)))
    entry_id = pushed_entry(env, order_id)

    updated = as_record(env.update(ENTRY, entry_id, {"vendor_id": env.id(SPARE_VENDOR)}).view)

    # Requirement 5.4 is about the lines alone: the header of a pushed draft is writable.
    assert summary_of(updated, "vendor")["id"] == env.id(SPARE_VENDOR)
    assert summary_of(updated, "source_bill") == {"id": order_id, "number": "SPO202610090001"}
    assert [line.fields["entry_qty"] for line in record_lines(updated)] == [Decimal(2)]


# --- deleting (requirement 5.5) ----------------------------------------------------------


@pytest.mark.req("5.5", "13.9")
def test_a_delete_removes_the_bill_with_its_lines_and_returns_its_object(
    env: EngineEnv,
) -> None:
    created = new_order(env, order_line(env), washer_line(env))
    order_id = record_id(created)
    before = as_record(env.run(GetOp(ORDER, order_id)).view)

    result = env.delete(ORDER, order_id)

    # What a get right before the delete returned, its lines included (requirement 13.9).
    removed = as_record(result.view)
    assert removed == before
    assert len(record_lines(removed)) == 2
    # The header and both lines are what the call changed (requirement 17.4).
    assert [(write.entity, write.op, write.id) for write in result.writes] == [
        (ORDER, "delete", order_id),
        (f"{ORDER}_line", "delete", f"{order_id}-1"),
        (f"{ORDER}_line", "delete", f"{order_id}-2"),
    ]
    # The header and every line are gone from the sandbox (requirement 5.5).
    assert env.records(ORDER) == []
    assert env.records(f"{ORDER}_line") == []
    with pytest.raises(ErpError) as raised:
        env.run(GetOp(ORDER, order_id))
    assert raised.value.code is ErrorCode.NOT_FOUND
    assert details(raised.value) == [{"field": "id", "reason": NOT_FOUND_REASON}]


@pytest.mark.req("5.5")
def test_a_delete_of_a_record_that_is_not_there_ends_in_not_found(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.delete(ORDER, "so_000099")

    assert raised.value.code is ErrorCode.NOT_FOUND
    assert details(raised.value) == [{"field": "id", "reason": NOT_FOUND_REASON}]


@pytest.mark.req("5.5")
def test_an_entity_record_nothing_refers_to_is_removed(env: EngineEnv) -> None:
    vendor_id = env.id(SPARE_VENDOR)

    removed = as_record(env.delete(VENDOR, vendor_id).view)

    assert removed.fields["name"] == "夹具备用供货单位"
    assert env.record(VENDOR, vendor_id) is None


@pytest.mark.req("5.5")
def test_an_entity_record_another_record_refers_to_is_not_removed(env: EngineEnv) -> None:
    order_id = record_id(new_order(env, order_line(env)))
    vendor_id = env.id(MAIN_VENDOR)

    with pytest.raises(ErpError) as raised:
        env.delete(VENDOR, vendor_id)

    error = raised.value
    # The design keeps the references of a sandbox pointing at records that are there
    # (design.md 4.2); CONFLICT is decided by no rule (requirement 13.8).
    assert error.code is ErrorCode.CONFLICT
    assert error.rule_id is None
    assert details(error) == [{"field": "id", "reason": REFERENCED_REASON}]
    assert "id" in error.message
    assert env.record(VENDOR, vendor_id) is not None
    # The record that holds the reference is named by its table and its field.
    with env.store.transaction() as uow:
        found = referring_record(uow, env.domain, env.domain.entities[VENDOR], vendor_id)
    assert found is not None
    assert (found.table, found.id, found.field) == (ORDER, order_id, "vendor_id")


@pytest.mark.req("5.5")
def test_a_reference_of_a_line_keeps_an_entity_record_as_well(env: EngineEnv) -> None:
    order_id = record_id(new_order(env, washer_line(env)))
    washer_id = env.id(WASHER)

    with pytest.raises(ErpError) as raised:
        env.delete("article", washer_id)

    # Every ref field of every level is looked at, the lines of a bill included.
    assert raised.value.code is ErrorCode.CONFLICT
    with env.store.transaction() as uow:
        found = referring_record(uow, env.domain, env.domain.entities["article"], washer_id)
    assert found is not None
    assert (found.table, found.id, found.field) == (
        f"{ORDER}_line",
        f"{order_id}-1",
        "article_id",
    )


@pytest.mark.req("5.5")
def test_a_reference_a_record_holds_to_itself_goes_with_it(env: EngineEnv) -> None:
    """No entity of the fixture refers to its own kind, so the model is bent here."""
    looped, domain = _self_referring(env, "depot", "branch_id")
    main, spare = env.id(MAIN_DEPOT), env.id(SPARE_DEPOT)

    with env.store.transaction() as uow:
        uow.update("depot", main, {"branch_id": main})
        uow.update("depot", spare, {"branch_id": spare})
        own = referring_record(uow, domain, looped, main)
        uow.update("depot", spare, {"branch_id": main})
        other = referring_record(uow, domain, looped, main)

    # The record's own reference is no reason to keep it; another record's is.
    assert own is None
    assert other is not None
    assert (other.table, other.id, other.field) == ("depot", spare, "branch_id")


def _self_referring(env: EngineEnv, entity: str, field: str) -> tuple[EntityModel, DomainModel]:
    """``entity`` with its ref field ``field`` pointing at its own kind, and its domain."""
    declared = env.domain.entities[entity]
    fields = FrozenMap({**declared.fields, field: replace(declared.fields[field], ref=entity)})
    looped = replace(declared, fields=fields)
    return looped, replace(env.domain, entities=FrozenMap({**env.domain.entities, entity: looped}))


# --- uniqueness (requirement 5.8) --------------------------------------------------------


@pytest.mark.req("5.8")
def test_a_create_whose_unique_value_another_record_holds_ends_in_conflict(
    env: EngineEnv,
) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(BRANCH, {"code": "BR01", "name": "夹具重码本部"})

    error = raised.value
    assert error.code is ErrorCode.CONFLICT
    assert error.rule_id is None
    assert details(error) == [{"field": "code", "reason": DUPLICATE_REASON}]
    # The message names the field path of the detail item (requirement 13.7).
    assert "code" in error.message
    # Nothing was written and no counter moved (requirements 12.7 and 4.12).
    assert len(env.records(BRANCH)) == 2
    assert env.counters()[("id", BRANCH)] == 2


@pytest.mark.req("5.8")
def test_an_update_to_a_unique_value_another_record_holds_ends_in_conflict(
    env: EngineEnv,
) -> None:
    spare = env.id(SPARE_BRANCH)

    with pytest.raises(ErpError) as raised:
        env.update(BRANCH, spare, {"code": "BR01"})

    assert raised.value.code is ErrorCode.CONFLICT
    assert details(raised.value) == [{"field": "code", "reason": DUPLICATE_REASON}]
    stored = env.record(BRANCH, spare)
    assert stored is not None
    assert stored["code"] == "BR02"


@pytest.mark.req("5.8")
def test_a_record_is_no_duplicate_of_itself(env: EngineEnv) -> None:
    main = env.id(MAIN_BRANCH)

    updated = as_record(env.update(BRANCH, main, {"code": "BR01", "name": "夹具改名本部"}).view)

    # The record this write replaces is not among the others (requirement 5.8).
    assert (updated.fields["code"], updated.fields["name"]) == ("BR01", "夹具改名本部")


@pytest.mark.req("5.8")
def test_the_comparison_of_a_unique_value_is_case_sensitive(env: EngineEnv) -> None:
    created = as_record(env.create(BRANCH, {"code": "br01", "name": "夹具小写本部"}).view)

    # "br01" is not "BR01": the values are compared exactly as they are written.
    assert created.fields["code"] == "br01"
    assert sorted(str(record["code"]) for record in env.records(BRANCH)) == [
        "BR01",
        "BR02",
        "br01",
    ]


@pytest.mark.req("5.8")
def test_an_empty_unique_value_takes_no_part_in_the_comparison(env: EngineEnv) -> None:
    first = as_record(env.create(BRANCH, {"name": "夹具无码本部"}).view)
    second = as_record(env.create(BRANCH, {"name": "夹具另一无码本部"}).view)

    # Both records are stored with an empty code; what is required of a field is checked
    # by the schema of the tool and, for a bill, by R-GEN-002 at submit (requirement 7.5).
    assert (first.fields["code"], second.fields["code"]) == (None, None)


@pytest.mark.req("5.8")
def test_two_lines_of_one_call_that_carry_the_same_unique_value_conflict(
    env: EngineEnv,
) -> None:
    """The line level of requirement 5.8, which the fixture declares no unique field for."""
    bill = env.domain.bills[ORDER]
    unique_lines = FrozenMap(
        {**bill.lines, "article_id": replace(bill.lines["article_id"], unique=True)}
    )
    order = replace(bill, lines=unique_lines)
    created = new_order(env, order_line(env), washer_line(env))

    with env.store.transaction() as uow:
        stored = uow.get(ORDER, record_id(created))
        assert stored is not None
        candidate = build_update_candidate(
            uow,
            env.domain,
            order,
            stored,
            arguments({}),
            [arguments(order_line(env)), arguments(order_line(env))],
            anchor_date=env.anchor_date,
        )
        same = unique_conflicts(uow, candidate)
        kept = unique_conflicts(uow, _without_lines(candidate))

    # Both lines write the same article, so the second one is reported; the stored lines of
    # the bill are replaced by this write, so neither of them is another record.
    assert [value.path for value in unique_values(candidate)] == [
        "lines[0].article_id",
        "lines[1].article_id",
    ]
    assert [value.path for value in same] == ["lines[1].article_id"]
    # A call that does not write the lines compares none of their values (requirement 5.8).
    assert kept == ()


def _without_lines(candidate: Candidate) -> Candidate:
    """``candidate`` as an update that leaves the lines of the bill in place (req 5.2)."""
    return replace(candidate, writes_lines=False)


# --- what an update returns (requirement 5.16) -------------------------------------------


@pytest.mark.req("5.16", "4.5")
def test_an_update_returns_what_a_get_returns(env: EngineEnv) -> None:
    created = new_order(env, order_line(env))
    order_id = record_id(created)

    updated = as_record(env.update(ORDER, order_id, {"price_includes_tax": True}).view)

    assert updated == as_record(env.run(GetOp(ORDER, order_id)).view)
    # The create was the first write operation of the test, the update the second: the
    # update stamps updated_seq and leaves created_seq as it was (requirement 4.5).
    created_seq = env.operations + 1
    assert (updated.fields[CREATED_SEQ], updated.fields[UPDATED_SEQ]) == (
        created_seq,
        created_seq + 1,
    )
