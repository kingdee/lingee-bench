"""erp_mock.domain.crud: the create of the crud primitive (requirements 5.1, 5.6, 5.7, 5.16).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. So a create is exercised exactly as a
tools/call runs it - one operation, one transaction - and the records a test points its
bills at were themselves written by this primitive. What the tests pin:

- the three rounds of defaults and their order: the caller's value stands, then the
  declared constant and the business context, then ``default_from`` through a reference of
  the same level; a bill's business date and organization fall back to the anchor date and
  the default organization, and an entity with a status is created enabled
  (requirement 5.1);
- a reference that is not there ends the call in NOT_FOUND, every missing reference of the
  call reported at once with the field path it came from, an id of another type included
  (requirement 5.6);
- R-GEN-006: a call whose references all point at enabled basic data goes through, one
  that writes a reference to a disabled record ends in BUSINESS_RULE_VIOLATION with that
  rule id and the field path, and the set the rule looks at holds the references the
  defaults brought as well (requirement 5.7);
- a create returns the object a get of the record returns right after it, the sequence
  numbers and the computed amounts included (requirement 5.16);
- a create under a traced call records the records it wrote as the effective call of that
  call, and one without a sequence number records none (requirement 18.10).
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.config.model import LINE_SUFFIX
from erp_mock.domain.crud import (
    DISABLED_REASON,
    DISABLED_RULE,
    NOT_FOUND_REASON,
    Reference,
    build_create_candidate,
    disabled_references,
    written_references,
)
from erp_mock.domain.engine import GetOp
from erp_mock.domain.numbering import CREATED_SEQ, UPDATED_SEQ
from erp_mock.state.schema import EFFECTIVE_WRITES_TABLE, ID_COLUMN
from erp_mock.state.store import Value
from tests.helpers.engine_env import (
    ANCHOR_DATE,
    BOLT,
    BOX,
    BUYER,
    MAIN_BRANCH,
    MAIN_DEPOT,
    MAIN_VENDOR,
    PCS,
    RETIRED_VENDOR,
    SPARE_VENDOR,
    WASHER,
    EngineEnv,
    arguments,
    as_record,
    engine_env,
    record_lines,
    summary_of,
)

# The three bills these tests create: a pricing bill whose lines take their unit and tax
# rate from their material, a bill without pricing, and the one whose header takes the
# current user as its default (tests/fixtures).
ORDER: Final = "supply_order"
ORDER_LINE: Final = f"{ORDER}{LINE_SUFFIX}"
ENTRY: Final = "goods_entry"
ISSUE: Final = "issue_note"
SLIP: Final = "pay_slip"

# The day after the anchor date, which a caller may send as the business date.
NEXT_DAY: Final = date(2026, 10, 10)

# The trace sequence number of the one traced call among these creates (requirement 18.10).
CALL_SEQ: Final = 3


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


def order_line(env: EngineEnv, **values: Value) -> dict[str, Value]:
    """One line of a supply order: the bolt, with whatever ``values`` override."""
    line: dict[str, Value] = {
        "article_id": env.id(BOLT),
        "qty": Decimal("2000"),
        "price": Decimal("0.85"),
    }
    line.update(values)
    return line


def create_order(env: EngineEnv, **header: Value) -> dict[str, Value]:
    """The header of a supply order: the enabled partner and a required date."""
    values: dict[str, Value] = {
        "vendor_id": env.id(MAIN_VENDOR),
        "need_date": NEXT_DAY,
    }
    values.update(header)
    return values


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


def effective_writes(env: EngineEnv) -> list[tuple[int, str, str]]:
    """What the sandbox recorded as written, as (call, table, id) (requirement 18.10)."""
    return [
        (int(str(row["seq"])), str(row["entity"]), str(row[ID_COLUMN]))
        for row in env.records(EFFECTIVE_WRITES_TABLE)
    ]


# --- the three rounds of defaults (requirement 5.1) --------------------------------------


@pytest.mark.req("5.1")
def test_the_caller_value_stands_against_every_default(env: EngineEnv) -> None:
    result = env.create(
        ORDER,
        create_order(env, biz_date=NEXT_DAY, price_includes_tax=True),
        [order_line(env, measure_id=env.id(PCS), tax_rate=Decimal("0.06"))],
    )

    view = as_record(result.view)
    # The business date and price_includes_tax have defaults of their own - the anchor date
    # and the pack's false - and the line's unit and tax rate come from the material.
    assert view.fields["biz_date"] == NEXT_DAY
    assert view.fields["price_includes_tax"] is True
    line = record_lines(view)[0]
    assert summary_of(line, "measure")["code"] == "PCS"
    assert line.fields["tax_rate"] == Decimal("0.06")


@pytest.mark.req("5.1")
def test_a_declared_constant_fills_what_the_caller_left_empty(env: EngineEnv) -> None:
    vendor = as_record(env.create("vendor", {"name": "夹具新供货单位"}).view)
    slip = as_record(
        env.create(SLIP, {"vendor_id": env.id(MAIN_VENDOR)}, [{"item_text": "运费"}]).view
    )

    # The int and the decimal constants the partner entity declares (tests/fixtures).
    assert vendor.fields["pay_days"] == 30
    assert vendor.fields["default_tax_rate"] == Decimal("0.13")
    # The enum constant of the payment slip header, and the false the order declares for
    # the standard field price_includes_tax, which the engine defaults to true.
    assert slip.fields["pay_mode"] == "transfer"
    order = as_record(env.create(ORDER, create_order(env), [order_line(env)]).view)
    assert order.fields["price_includes_tax"] is False


@pytest.mark.req("5.1")
def test_a_bill_takes_the_anchor_date_and_the_default_organization(env: EngineEnv) -> None:
    view = as_record(env.create(ORDER, create_order(env), [order_line(env)]).view)

    # biz_date and org_id carry the engine's own today and org defaults, and the order
    # declares none of its own (requirement 5.1).
    assert view.fields["biz_date"] == ANCHOR_DATE
    assert summary_of(view, "org") == {
        "id": env.id(MAIN_BRANCH),
        "code": "BR01",
        "name": "夹具核算本部",
    }


@pytest.mark.req("5.1")
def test_a_user_default_takes_the_current_user_of_the_call(env: EngineEnv) -> None:
    view = as_record(
        env.create(
            ISSUE,
            {"depot_id": env.id(MAIN_DEPOT)},
            [{"article_id": env.id(BOLT), "issue_qty": Decimal("5")}],
        ).view
    )

    # taker_id declares default_context user, so it is the current user of the context.
    assert summary_of(view, "taker")["id"] == env.id(BUYER)


@pytest.mark.req("5.1")
def test_default_from_takes_the_field_of_the_referenced_record(env: EngineEnv) -> None:
    view = as_record(
        env.create(
            ORDER, create_order(env), [order_line(env), order_line(env, article_id=env.id(WASHER))]
        ).view
    )

    bolt, washer = record_lines(view)
    # The order's lines read the unit from the material's purchase unit and the tax rate
    # from its own tax rate; the bolt buys in boxes, the washer in its base unit.
    assert summary_of(bolt, "measure")["id"] == env.id(BOX)
    assert bolt.fields["tax_rate"] == Decimal("0.13")
    assert summary_of(washer, "measure")["id"] == env.id(PCS)
    assert washer.fields["tax_rate"] == Decimal("0.09")


@pytest.mark.req("5.1")
def test_a_default_from_that_reads_through_an_empty_reference_leaves_the_field_empty(
    env: EngineEnv,
) -> None:
    # The entry's unit reads through its material, which is required but not filled here;
    # a required field left empty is checked at submit, not now (R-GEN-002, req 7.5).
    view = as_record(
        env.create(
            ENTRY, {"vendor_id": env.id(MAIN_VENDOR)}, [{"depot_id": env.id(MAIN_DEPOT)}]
        ).view
    )

    line = record_lines(view)[0]
    assert line.fields["article"] is None
    assert line.fields["measure"] is None


@pytest.mark.req("5.1")
def test_an_entity_with_a_status_is_created_enabled(env: EngineEnv) -> None:
    enabled = as_record(env.create("vendor", {"name": "夹具启用供货单位"}).view)
    disabled = as_record(
        env.create("vendor", {"name": "夹具另一停用供货单位", "status": "disabled"}).view
    )

    assert enabled.fields["status"] == "enabled"
    # The caller's value stands here as everywhere (round 1).
    assert disabled.fields["status"] == "disabled"


# --- references that are not there (requirement 5.6) -------------------------------------


@pytest.mark.req("5.6")
def test_every_reference_that_is_not_there_is_reported_in_one_not_found(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(
            ORDER,
            create_order(env, vendor_id="ven_000099"),
            [order_line(env), order_line(env, article_id="art_000099")],
        )

    error = raised.value
    assert error.code is ErrorCode.NOT_FOUND
    # NOT_FOUND is decided by no rule (requirement 13.8).
    assert error.rule_id is None
    # One item per missing reference, the header before the lines, each with its path.
    assert details(error) == [
        {"field": "vendor_id", "reason": NOT_FOUND_REASON},
        {"field": "lines[1].article_id", "reason": NOT_FOUND_REASON},
    ]
    # The message carries the path of a detail item (requirement 13.7).
    assert "vendor_id" in error.message
    assert "ven_000099" in error.message
    # Nothing was written and no counter moved (requirements 12.7 and 4.12).
    assert env.records(ORDER) == []
    assert ("id", ORDER) not in env.counters()


@pytest.mark.req("5.6")
def test_an_id_of_another_type_counts_as_a_reference_that_is_not_there(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        # A real record id, but of the unit entity rather than of the material.
        env.create(ORDER, create_order(env), [order_line(env, article_id=env.id(PCS))])

    assert raised.value.code is ErrorCode.NOT_FOUND
    assert details(raised.value) == [{"field": "lines[0].article_id", "reason": NOT_FOUND_REASON}]


@pytest.mark.req("5.6")
def test_a_reference_the_caller_left_empty_is_not_checked(env: EngineEnv) -> None:
    # No reference to check, so the create goes through and the field stays empty; the
    # required fields are checked at submit (R-GEN-002, requirement 7.5).
    view = as_record(env.create(ENTRY, {}, [{}]).view)

    assert view.fields["vendor"] is None
    assert record_lines(view)[0].fields["depot"] is None


# --- R-GEN-006 (requirement 5.7) ---------------------------------------------------------


@pytest.mark.req("5.7")
def test_a_create_whose_references_are_all_enabled_goes_through(env: EngineEnv) -> None:
    result = env.create(ORDER, create_order(env, vendor_id=env.id(SPARE_VENDOR)), [order_line(env)])

    view = as_record(result.view)
    assert summary_of(view, "vendor")["id"] == env.id(SPARE_VENDOR)
    # The sandbox holds a disabled partner, which this call does not reference, so the
    # rule has nothing to object to (requirement 5.7).
    retired = env.record("vendor", env.id(RETIRED_VENDOR))
    assert retired is not None
    assert retired["status"] == "disabled"


@pytest.mark.req("5.7")
def test_a_reference_to_disabled_basic_data_violates_r_gen_006(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(ORDER, create_order(env, vendor_id=env.id(RETIRED_VENDOR)), [order_line(env)])

    error = raised.value
    assert error.code is ErrorCode.BUSINESS_RULE_VIOLATION
    assert error.rule_id == DISABLED_RULE
    assert details(error) == [{"field": "vendor_id", "reason": DISABLED_REASON}]
    # The message names the field path of the detail item (requirement 13.7).
    assert "vendor_id" in error.message


@pytest.mark.req("5.7")
def test_a_create_that_breaks_r_gen_006_leaves_the_sandbox_as_it_was(env: EngineEnv) -> None:
    before = env.counters()

    with pytest.raises(ErpError):
        env.create(ORDER, create_order(env, vendor_id=env.id(RETIRED_VENDOR)), [order_line(env)])

    # The rule runs in the transaction of the operation, so the id, the number and the
    # operation sequence counters roll back with it (requirement 4.12).
    assert env.records(ORDER) == []
    assert env.counters() == before
    # The next successful create gets the first id and number of its type.
    view = as_record(env.create(ORDER, create_order(env), [order_line(env)]).view)
    assert view.fields["id"] == "so_000001"
    assert view.fields["number"] == "SPO202610090001"


@pytest.mark.req("5.7")
def test_a_reference_to_an_entity_without_a_status_is_never_disabled(env: EngineEnv) -> None:
    # The unit, the material and the warehouse have no status at all, so no reference to
    # one of them can break the rule (requirement 5.7).
    result = env.create(
        ISSUE,
        {"depot_id": env.id(MAIN_DEPOT)},
        [{"article_id": env.id(WASHER), "measure_id": env.id(PCS), "issue_qty": Decimal("1")}],
    )

    assert as_record(result.view).fields["status"] == "draft"


@pytest.mark.req("5.7")
def test_the_rule_looks_at_the_references_the_defaults_brought_as_well(env: EngineEnv) -> None:
    """The set of requirement 5.7: every reference the call writes, defaults included."""
    with env.store.transaction() as uow:
        candidate = build_create_candidate(
            uow,
            env.domain,
            env.domain.bills[ORDER],
            arguments(create_order(env)),
            [arguments(order_line(env))],
            anchor_date=env.anchor_date,
            current_user_id=env.current_user_id,
        )
        written = written_references(candidate)
        disabled = disabled_references(uow, env.domain, written)

    # org_id came from the business context and the line's unit from the material, and
    # both are in the set the rule is given, next to the caller's own references.
    assert [(ref.path, ref.id) for ref in written] == [
        ("org_id", env.id(MAIN_BRANCH)),
        ("vendor_id", env.id(MAIN_VENDOR)),
        ("lines[0].article_id", env.id(BOLT)),
        ("lines[0].measure_id", env.id(BOX)),
    ]
    assert disabled == ()


@pytest.mark.req("5.7")
def test_a_disabled_reference_is_reported_under_its_own_path(env: EngineEnv) -> None:
    references = (
        Reference(path="vendor_id", target="vendor", id=env.id(MAIN_VENDOR)),
        Reference(path="lines[0].vendor_id", target="vendor", id=env.id(RETIRED_VENDOR)),
        Reference(path="lines[1].article_id", target="article", id=env.id(BOLT)),
    )

    with env.store.transaction() as uow:
        disabled = disabled_references(uow, env.domain, references)

    # Every disabled reference of the call, in the order the paths were collected; an
    # enabled one and one to an entity without a status are not among them.
    assert [ref.path for ref in disabled] == ["lines[0].vendor_id"]


@pytest.mark.req("5.6", "5.7")
def test_a_reference_that_is_not_there_is_reported_before_r_gen_006(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(
            ORDER,
            create_order(env, vendor_id=env.id(RETIRED_VENDOR)),
            [order_line(env, article_id="art_000099")],
        )

    # Existence is step 1 of the pipeline and the business rules are step 7
    # (requirement 12.1), so the call reports the missing reference.
    assert raised.value.code is ErrorCode.NOT_FOUND
    assert details(raised.value) == [{"field": "lines[0].article_id", "reason": NOT_FOUND_REASON}]


# --- what a create returns (requirement 5.16) --------------------------------------------


@pytest.mark.req("5.16")
def test_a_create_of_a_bill_returns_what_a_get_returns(env: EngineEnv) -> None:
    created = as_record(
        env.create(
            ORDER, create_order(env), [order_line(env), order_line(env, qty=Decimal("10"))]
        ).view
    )

    fetched = as_record(env.run(GetOp(ORDER, str(created.fields["id"]))).view)
    assert created == fetched
    # So the object carries the sequence numbers of this very operation and the amounts
    # the pricing primitive computed (requirements 4.5 and 6.4).
    seq = env.operations + 1
    assert (created.fields[CREATED_SEQ], created.fields[UPDATED_SEQ]) == (seq, seq)
    assert created.fields["amount"] == Decimal("1708.50")
    assert [line.fields["amount"] for line in record_lines(created)] == [
        Decimal("1700.00"),
        Decimal("8.50"),
    ]


@pytest.mark.req("5.16")
def test_a_create_of_an_entity_record_returns_what_a_get_returns(env: EngineEnv) -> None:
    created = as_record(
        env.create(
            "article", {"name": "夹具用销钉", "grade": "fine", "base_measure_id": env.id(PCS)}
        ).view
    )

    fetched = as_record(env.run(GetOp("article", str(created.fields["id"]))).view)
    assert created == fetched
    # The generated code and the default that was read through a reference are both in it.
    assert created.fields["code"] == "A00003"
    assert summary_of(created, "buy_measure")["id"] == env.id(PCS)


@pytest.mark.req("18.10")
def test_a_create_under_a_traced_call_records_what_it_wrote(env: EngineEnv) -> None:
    traced = env.create(ORDER, create_order(env), [order_line(env)], call_seq=CALL_SEQ)

    # The sandbox records the bill and its line under the sequence number of the call, so
    # a diff can tell which call wrote them (requirement 18.10).
    order = str(as_record(traced.view).fields["id"])
    assert effective_writes(env) == [(CALL_SEQ, ORDER, order), (CALL_SEQ, ORDER_LINE, f"{order}-1")]
    # A create the setup of a sandbox runs carries no sequence number and records none,
    # although it writes just the same.
    untraced = env.create(ORDER, create_order(env), [order_line(env)])
    assert untraced.writes != ()
    assert effective_writes(env) == [(CALL_SEQ, ORDER, order), (CALL_SEQ, ORDER_LINE, f"{order}-1")]
