"""erp_mock.domain.unit_conversion: the base unit quantity and the factors it reads (req 6).

The tests run against a real sandbox built by :mod:`tests.helpers.engine_env`: an empty
database over the tables of the engine test fixture with one standard set of basic data,
every record of it created through the engine. The fixture's names are its own - the
material entity is ``article``, its base unit field ``base_measure_id``, its conversion
records ``measure_link`` with the fields ``article_id``, ``alt_measure_id`` and ``ratio``,
and the lines of a bill name their material and unit as ``article_id`` and ``measure_id`` -
so a primitive that worked only on the names of the example pack would fail every one of
these tests (requirement 6.8). What they pin:

- ``qty_base = round(qty × factor, 4)`` with the factor 1 for a line in the base unit of
  its material and the declared factor for a line in an alternative unit; an empty
  material, unit or quantity leaves it empty, and the product is rounded ROUND_HALF_UP
  (requirement 6.6);
- a line whose unit is neither the base unit nor a unit the material has a conversion
  record for ends the call in VALIDATION_FAILED that no rule decided, its detail items
  naming the unit field of each such line by line order; the check runs before the business
  rules (requirements 6.7 and 12.10);
- a material carries its conversion records in its own object, by the internal id of the
  conversion unit, each one holding the unit and the factor alone; a material without one
  carries an empty array and a reference to a material carries none (requirement 6.9);
- the write of a conversion record needs a factor above 0 and a unit that is set, is not
  the base unit of its material and is not already taken, the last of these being a
  CONFLICT (requirement 6.11).
"""

from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.domain.engine import PIPELINE, GetOp
from erp_mock.domain.unit_conversion import (
    DUPLICATE_REASON,
    INVALID_UNIT_REASON,
    MISSING_REASON,
    NOT_POSITIVE_REASON,
    ONE,
    QTY_BASE,
    UNIT_CONVERSIONS,
    conversion_factor,
    qty_base,
    resolve_line_quantity,
)
from erp_mock.domain.views import RecordView, ViewValue
from erp_mock.state.store import Value
from tests.helpers.engine_env import (
    BOLT,
    BOLT_PER_BOX,
    BOX,
    BOX_FACTOR,
    MAIN_DEPOT,
    MAIN_VENDOR,
    PCS,
    RETIRED_VENDOR,
    WASHER,
    EngineEnv,
    as_lines,
    as_record,
    engine_env,
    record_lines,
    summary_of,
)

# The bills these tests write: the pricing bill whose lines measure the standard qty and
# take their unit from the purchase unit of the material, the bill without pricing whose
# lines measure a declared quantity field and take the base unit, and the pricing bill that
# declares no quantity semantics at all (tests/fixtures).
ORDER: Final = "supply_order"
ENTRY: Final = "goods_entry"
NOTE: Final = "fee_note"

# The entities: the material, the unit and the conversion records between them.
ARTICLE: Final = "article"
MEASURE: Final = "measure"
LINK: Final = "measure_link"

# The fields of the fixture, which the primitive reads from the configuration alone
# (requirement 6.8).
MATERIAL_FIELD: Final = "article_id"
UNIT_FIELD: Final = "measure_id"
LINK_UNIT_FIELD: Final = "alt_measure_id"
FACTOR_FIELD: Final = "ratio"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


def order_line(env: EngineEnv, **values: Value) -> dict[str, Value]:
    """One line of a supply order: 2000 bolts at 0.85, in the bolt's purchase unit."""
    line: dict[str, Value] = {
        MATERIAL_FIELD: env.id(BOLT),
        "qty": Decimal("2000"),
        "price": Decimal("0.85"),
    }
    line.update(values)
    return line


def create_order(env: EngineEnv, **header: Value) -> dict[str, Value]:
    """The header of a supply order: the enabled partner and a required date."""
    values: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR), "need_date": env.anchor_date}
    values.update(header)
    return values


def entry_line(env: EngineEnv, **values: Value) -> dict[str, Value]:
    """One line of a goods entry: bolts into the main depot, in the base unit."""
    line: dict[str, Value] = {
        MATERIAL_FIELD: env.id(BOLT),
        "depot_id": env.id(MAIN_DEPOT),
        "entry_qty": Decimal("2.5"),
    }
    line.update(values)
    return line


def create_entry(env: EngineEnv, **header: Value) -> dict[str, Value]:
    """The header of a goods entry: the enabled partner."""
    values: dict[str, Value] = {"vendor_id": env.id(MAIN_VENDOR)}
    values.update(header)
    return values


def link(env: EngineEnv, material: str, unit: str, factor: Value) -> dict[str, Value]:
    """One conversion record: one ``unit`` of ``material`` is ``factor`` base units."""
    return {MATERIAL_FIELD: env.id(material), LINK_UNIT_FIELD: env.id(unit), FACTOR_FIELD: factor}


def quantities(view: RecordView) -> list[ViewValue]:
    """The ``qty_base`` of every line of the bill ``view`` returns, by line number."""
    return [line.fields[QTY_BASE] for line in record_lines(view)]


def stored_quantities(env: EngineEnv, bill: str) -> list[Value]:
    """The ``qty_base`` of every line of ``bill`` as storage holds it, by line id."""
    return [line[QTY_BASE] for line in env.records(f"{bill}_line")]


def conversions(view: RecordView) -> tuple[RecordView, ...]:
    """The conversion records the material ``view`` returns carries (requirement 6.9)."""
    return as_lines(view.fields[UNIT_CONVERSIONS])


def details(error: ErpError) -> list[dict[str, object]]:
    """The detail items of ``error`` as plain dicts, in report order (requirement 13.6)."""
    return [dict(item) for item in error.details]


def new_measure(env: EngineEnv, code: str, name: str) -> str:
    """Create one more unit and return its internal id."""
    return str(as_record(env.create(MEASURE, {"code": code, "name": name}).view).fields["id"])


def unmeasured_article(env: EngineEnv) -> str:
    """Create an article without a base unit, so a line of it gets no unit by default.

    ``buy_measure_id`` takes its default through ``base_measure_id``, so an article without
    one leaves both empty and a line that names it carries no unit (requirement 5.1).
    """
    view = as_record(env.create(ARTICLE, {"name": "夹具无单位件", "grade": "plain"}).view)
    return str(view.fields["id"])


# --- the base unit quantity of a line (requirement 6.6) ----------------------------------


@pytest.mark.req("6.6")
def test_a_line_in_the_base_unit_takes_a_factor_of_one(env: EngineEnv) -> None:
    view = as_record(env.create(ENTRY, create_entry(env), [entry_line(env)]).view)

    # The unit of the line defaults to the base unit of the bolt, which needs no
    # conversion record: 2.5 PCS are 2.5 base units.
    assert summary_of(record_lines(view)[0], "measure")["code"] == "PCS"
    assert quantities(view) == [Decimal("2.5")]


@pytest.mark.req("6.6")
def test_a_line_in_an_alternative_unit_takes_the_declared_factor(env: EngineEnv) -> None:
    view = as_record(env.create(ORDER, create_order(env), [order_line(env)]).view)

    # The unit of the line defaults to the purchase unit of the bolt, which is the box its
    # conversion record converts: 2000 boxes of 12 are 24000 base units.
    assert summary_of(record_lines(view)[0], "measure")["code"] == "BOX"
    assert quantities(view) == [Decimal("2000") * BOX_FACTOR]
    # The value reaches storage, where the stock of the sandbox is read from (req 9.2).
    assert stored_quantities(env, ORDER) == [Decimal("24000")]


@pytest.mark.req("6.6")
def test_the_unit_of_the_line_decides_the_factor_not_the_default(env: EngineEnv) -> None:
    view = as_record(
        env.create(
            ORDER,
            create_order(env),
            [order_line(env, measure_id=env.id(PCS)), order_line(env, measure_id=env.id(BOX))],
        ).view
    )

    # The same material and the same quantity in the two units of its conversion.
    assert quantities(view) == [Decimal("2000"), Decimal("24000")]


@pytest.mark.req("6.6")
def test_qty_base_rounds_the_product_half_up(env: EngineEnv) -> None:
    env.create(LINK, link(env, WASHER, BOX, Decimal("0.0001")))

    view = as_record(
        env.create(
            ENTRY,
            create_entry(env),
            [
                entry_line(env, article_id=env.id(WASHER), measure_id=env.id(BOX), entry_qty=qty)
                for qty in (Decimal("1.5"), Decimal("0.5"), Decimal("1.4"))
            ],
        ).view
    )

    # 0.00015 and 0.00005 move away from zero, 0.00014 does not (requirement 4.7).
    assert quantities(view) == [Decimal("0.0002"), Decimal("0.0001"), Decimal("0.0001")]


@pytest.mark.req("6.6")
def test_qty_base_is_empty_when_the_quantity_is_empty(env: EngineEnv) -> None:
    view = as_record(
        env.create(ORDER, create_order(env), [order_line(env, qty=None), order_line(env)]).view
    )

    # qty is the quantity a pricing bill measures, so a line without one has no qty_base
    # and the write is not rejected over it (requirement 6.10).
    assert quantities(view) == [None, Decimal("24000")]
    assert stored_quantities(env, ORDER) == [None, Decimal("24000")]


@pytest.mark.req("6.6")
def test_qty_base_is_empty_when_the_unit_is_empty(env: EngineEnv) -> None:
    article = unmeasured_article(env)

    view = as_record(
        env.create(ORDER, create_order(env), [order_line(env, article_id=article)]).view
    )

    line = record_lines(view)[0]
    assert line.fields["measure"] is None
    assert line.fields[QTY_BASE] is None


@pytest.mark.req("6.6")
def test_a_bill_without_quantity_semantics_keeps_qty_base_empty(env: EngineEnv) -> None:
    view = as_record(
        env.create(
            NOTE,
            {"vendor_id": env.id(MAIN_VENDOR), "due_date": env.anchor_date},
            [{"item_text": "运费", "qty": Decimal("3"), "price": Decimal("12.5")}],
        ).view
    )

    # A pricing bill of money alone has the column and no material to read a factor
    # through, so nothing fills it (requirement 6.6), while the amounts are computed as
    # ever: the price of this bill includes tax (requirements 6.2 and 6.3).
    assert quantities(view) == [None]
    assert record_lines(view)[0].fields["amount_with_tax"] == Decimal("37.50")


@pytest.mark.req("6.6")
def test_an_update_recomputes_the_base_unit_quantity(env: EngineEnv) -> None:
    created = as_record(env.create(ORDER, create_order(env), [order_line(env)]).view)
    order_id = str(created.fields["id"])

    view = as_record(
        env.update(ORDER, order_id, lines=[order_line(env, qty=Decimal("1.0625"))]).view
    )

    # 1.0625 boxes of 12 are 12.75 base units, and the new value reaches storage.
    assert quantities(view) == [Decimal("12.75")]
    assert stored_quantities(env, ORDER) == [Decimal("12.75")]


@pytest.mark.req("6.6")
def test_the_factor_is_read_at_every_write(env: EngineEnv) -> None:
    created = as_record(env.create(ORDER, create_order(env), [order_line(env)]).view)
    order_id = str(created.fields["id"])

    env.update(LINK, env.id(BOLT_PER_BOX), {FACTOR_FIELD: Decimal("20")})
    view = as_record(env.update(ORDER, order_id, {"remark": "改箱规"}).view)

    # An update of the header alone recomputes the lines, so the new factor applies
    # (requirements 5.2 and 6.6).
    assert quantities(view) == [Decimal("40000")]


@pytest.mark.req("6.6")
def test_qty_base_is_the_rounded_product_of_the_quantity_and_the_factor() -> None:
    # The computation itself: the product is exact and only the result is rounded, to the
    # four decimals of a quantity (requirement 4.7).
    assert qty_base(Decimal("20"), Decimal("100")) == Decimal("2000.0000")
    assert qty_base(Decimal("0.00005"), ONE) == Decimal("0.0001")
    assert qty_base(Decimal("1.00005"), Decimal("1.00005")) == Decimal("1.0001")
    assert qty_base(Decimal("0"), Decimal("12")) == Decimal("0.0000")


# --- a unit without a conversion record (requirements 6.7 and 12.10) ---------------------


@pytest.mark.req("6.7")
def test_a_unit_without_a_conversion_record_fails_validation(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(
            ORDER,
            create_order(env),
            [order_line(env, article_id=env.id(WASHER), measure_id=env.id(BOX))],
        )

    error = raised.value
    assert error.code is ErrorCode.VALIDATION_FAILED
    # No rule decides it (requirement 13.8).
    assert error.rule_id is None
    assert details(error) == [
        {"field": f"lines[0].{UNIT_FIELD}", "reason": MISSING_REASON},
    ]
    # The message names the field path of a detail item (requirement 13.7).
    assert f"lines[0].{UNIT_FIELD}" in error.message
    # Nothing of the rejected call is left behind (requirement 12.7).
    assert env.records(ORDER) == []


@pytest.mark.req("6.7")
def test_every_line_without_a_conversion_is_reported_by_line_order(env: EngineEnv) -> None:
    washer = env.id(WASHER)

    with pytest.raises(ErpError) as raised:
        env.create(
            ORDER,
            create_order(env),
            [
                order_line(env),
                order_line(env, article_id=washer, measure_id=env.id(BOX)),
                order_line(env, article_id=washer, measure_id=env.id(PCS)),
                order_line(env, article_id=washer, measure_id=env.id(BOX)),
            ],
        )

    # The second and the fourth line, the index counting from 0; the third is in the base
    # unit of the washer and needs no conversion record.
    assert details(raised.value) == [
        {"field": f"lines[1].{UNIT_FIELD}", "reason": MISSING_REASON},
        {"field": f"lines[3].{UNIT_FIELD}", "reason": MISSING_REASON},
    ]


@pytest.mark.req("6.7")
def test_a_line_without_a_material_or_a_unit_is_not_reported(env: EngineEnv) -> None:
    article = unmeasured_article(env)

    view = as_record(
        env.create(
            ORDER,
            create_order(env),
            [order_line(env, article_id=article), order_line(env, measure_id=None)],
        ).view
    )

    # Both lines name a unit or a material that is empty, which is no missing conversion:
    # the second one takes the purchase unit of the bolt by default (requirement 5.1).
    assert quantities(view) == [None, Decimal("24000")]


@pytest.mark.req("6.7")
def test_the_unit_conversion_check_runs_before_the_business_rules(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(
            ORDER,
            create_order(env, vendor_id=env.id(RETIRED_VENDOR)),
            [order_line(env, article_id=env.id(WASHER), measure_id=env.id(BOX))],
        )

    # The call breaks R-GEN-006 as well, which the rules slot checks after this one
    # (requirement 12.10).
    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert raised.value.rule_id is None


@pytest.mark.req("6.7")
def test_the_slot_of_the_check_is_after_the_bill_status_and_before_the_rules() -> None:
    slots = [step.name for step in PIPELINE]

    # The order of the pipeline, which is the order the errors of one call are reported in
    # (requirement 12.10, design.md 4.1).
    assert slots.index("bill_status") < slots.index("unit_conversion") < slots.index("rules")


@pytest.mark.req("6.7")
def test_an_update_to_a_unit_without_a_conversion_is_rejected(env: EngineEnv) -> None:
    created = as_record(env.create(ORDER, create_order(env), [order_line(env)]).view)
    order_id = str(created.fields["id"])

    with pytest.raises(ErpError) as raised:
        env.update(
            ORDER,
            order_id,
            lines=[order_line(env, article_id=env.id(WASHER), measure_id=env.id(BOX))],
        )

    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    # The bill keeps the lines it had (requirement 12.7).
    view = as_record(env.run(GetOp(ORDER, order_id)).view)
    assert summary_of(record_lines(view)[0], "article")["code"] == "A00001"
    assert quantities(view) == [Decimal("24000")]


# --- the entities and fields of the configuration (requirement 6.8) ----------------------


@pytest.mark.req("6.8")
def test_the_primitive_reads_the_fields_the_pack_declared(env: EngineEnv) -> None:
    domain = env.domain

    order = resolve_line_quantity(domain, domain.bills[ORDER])
    entry = resolve_line_quantity(domain, domain.bills[ENTRY])

    assert order is not None and entry is not None
    # The material, the unit and the quantity of the lines: the standard qty for the
    # pricing bill and the declared field for the bill without pricing.
    assert (order.material_field, order.unit_field, order.qty_field) == (
        MATERIAL_FIELD,
        UNIT_FIELD,
        "qty",
    )
    assert entry.qty_field == "entry_qty"
    # The measured entity, its base unit field and the three fields of a conversion record,
    # all of them names of the fixture rather than of the example pack.
    assert order.material.name == ARTICLE
    assert order.conversion.base_unit_field == "base_measure_id"
    assert order.conversion.unit_entity == MEASURE
    assert (order.conversion.entity, order.conversion.unit_field) == (LINK, LINK_UNIT_FIELD)
    assert order.conversion.factor_field == FACTOR_FIELD


@pytest.mark.req("6.8")
def test_a_bill_without_quantity_semantics_resolves_to_nothing(env: EngineEnv) -> None:
    domain = env.domain

    assert resolve_line_quantity(domain, domain.bills[NOTE]) is None


@pytest.mark.req("6.8")
def test_the_factor_comes_from_the_base_unit_field_and_the_conversion_records(
    env: EngineEnv,
) -> None:
    domain = env.domain
    quantity = resolve_line_quantity(domain, domain.bills[ORDER])
    assert quantity is not None

    with env.store.transaction() as uow:
        base = conversion_factor(uow, quantity, env.id(BOLT), env.id(PCS))
        box = conversion_factor(uow, quantity, env.id(BOLT), env.id(BOX))
        missing = conversion_factor(uow, quantity, env.id(WASHER), env.id(BOX))

    assert base == ONE
    assert box == BOX_FACTOR
    assert missing is None


# --- the conversion records of a material (requirement 6.9) ------------------------------


@pytest.mark.req("6.9")
def test_get_of_a_material_carries_its_conversion_records(env: EngineEnv) -> None:
    view = as_record(env.run(GetOp(ARTICLE, env.id(BOLT))).view)

    # The array comes last, after every field of the record (requirement 13.1).
    assert list(view.fields)[-1] == UNIT_CONVERSIONS
    records = conversions(view)
    assert len(records) == 1
    # Each item holds the conversion unit as a nested object and the factor, and nothing
    # else - not its own id and not the material it belongs to.
    assert tuple(records[0].fields) == ("alt_measure", FACTOR_FIELD)
    assert summary_of(records[0], "alt_measure") == {
        "id": env.id(BOX),
        "code": "BOX",
        "name": "夹具标准箱",
    }
    assert records[0].fields[FACTOR_FIELD] == BOX_FACTOR


@pytest.mark.req("6.9")
def test_a_material_without_conversion_records_carries_an_empty_array(env: EngineEnv) -> None:
    view = as_record(env.run(GetOp(ARTICLE, env.id(WASHER))).view)

    # No item for the base unit is added (requirement 6.9).
    assert conversions(view) == ()


@pytest.mark.req("6.9")
def test_the_conversion_records_are_ordered_by_the_unit_id(env: EngineEnv) -> None:
    dozen = new_measure(env, "DOZ", "夹具打")
    pallet = new_measure(env, "PAL", "夹具托盘")
    washer = env.id(WASHER)
    # Written in the reverse order of the unit ids, so neither the order of the records
    # themselves nor their own ids give the order of the result.
    for unit in (pallet, dozen, env.id(BOX)):
        env.create(
            LINK,
            {MATERIAL_FIELD: washer, LINK_UNIT_FIELD: unit, FACTOR_FIELD: Decimal("6")},
        )

    view = as_record(env.run(GetOp(ARTICLE, washer)).view)

    # By the internal id of the conversion unit, which here is neither the order the
    # records were written in nor the order of their own ids (requirement 6.9).
    units = [summary_of(record, "alt_measure")["id"] for record in conversions(view)]
    assert units == sorted([env.id(BOX), dozen, pallet])


@pytest.mark.req("6.9")
def test_create_and_update_of_a_material_carry_the_records_too(env: EngineEnv) -> None:
    created = as_record(
        env.create(
            ARTICLE,
            {"name": "夹具新件", "grade": "fine", "base_measure_id": env.id(PCS)},
        ).view
    )
    article_id = str(created.fields["id"])
    env.create(
        LINK,
        {MATERIAL_FIELD: article_id, LINK_UNIT_FIELD: env.id(BOX), FACTOR_FIELD: Decimal("24")},
    )

    updated = as_record(env.update(ARTICLE, article_id, {"spec": "M10"}).view)

    # A create returns the array of a material that has no record yet, an update the
    # records written since (requirements 5.16 and 6.9).
    assert conversions(created) == ()
    assert [record.fields[FACTOR_FIELD] for record in conversions(updated)] == [Decimal("24")]


@pytest.mark.req("6.9")
def test_a_reference_to_a_material_carries_no_conversion_records(env: EngineEnv) -> None:
    view = as_record(env.create(ORDER, create_order(env), [order_line(env)]).view)

    # The nested object of a line's material holds the keys of a summary and no more
    # (requirement 13.3).
    article = summary_of(record_lines(view)[0], "article")
    assert tuple(article) == ("id", "code", "name")
    # A conversion record itself declares no unit conversion, so its own object has none.
    record = as_record(env.run(GetOp(LINK, env.id(BOLT_PER_BOX))).view)
    assert UNIT_CONVERSIONS not in record.fields


# --- the write of a conversion record (requirement 6.11) ---------------------------------


@pytest.mark.req("6.11")
@pytest.mark.parametrize("factor", [None, Decimal("0"), Decimal("-1"), Decimal("-0.5")])
def test_a_factor_that_is_not_above_zero_is_rejected(env: EngineEnv, factor: Value) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(LINK, link(env, WASHER, BOX, factor))

    error = raised.value
    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.rule_id is None
    assert details(error) == [{"field": FACTOR_FIELD, "reason": NOT_POSITIVE_REASON}]
    assert FACTOR_FIELD in error.message
    assert _links(env) == [env.id(BOLT_PER_BOX)]


@pytest.mark.req("6.11")
def test_a_conversion_record_without_a_unit_is_rejected(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(LINK, {MATERIAL_FIELD: env.id(WASHER), FACTOR_FIELD: Decimal("6")})

    error = raised.value
    assert error.code is ErrorCode.VALIDATION_FAILED
    assert details(error) == [{"field": LINK_UNIT_FIELD, "reason": INVALID_UNIT_REASON}]


@pytest.mark.req("6.11")
def test_the_base_unit_needs_no_conversion_record(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(LINK, link(env, WASHER, PCS, Decimal("1")))

    error = raised.value
    assert error.code is ErrorCode.VALIDATION_FAILED
    assert details(error) == [{"field": LINK_UNIT_FIELD, "reason": INVALID_UNIT_REASON}]
    assert LINK_UNIT_FIELD in error.message


@pytest.mark.req("6.11")
def test_a_second_record_for_the_same_unit_is_a_conflict(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.create(LINK, link(env, BOLT, BOX, Decimal("24")))

    error = raised.value
    assert error.code is ErrorCode.CONFLICT
    assert error.rule_id is None
    assert details(error) == [{"field": LINK_UNIT_FIELD, "reason": DUPLICATE_REASON}]
    # The factor of the record that is there is untouched.
    assert _links(env) == [env.id(BOLT_PER_BOX)]
    assert conversion_of(env, BOLT)[FACTOR_FIELD] == BOX_FACTOR


@pytest.mark.req("6.11")
def test_another_material_may_convert_the_same_unit(env: EngineEnv) -> None:
    env.create(LINK, link(env, WASHER, BOX, Decimal("100")))

    view = as_record(
        env.create(
            ORDER,
            create_order(env),
            [order_line(env, article_id=env.id(WASHER), measure_id=env.id(BOX))],
        ).view
    )

    # The unit is taken per material, so the new record converts the washer alone and the
    # line that needed it goes through (requirement 6.6).
    assert quantities(view) == [Decimal("200000")]


@pytest.mark.req("6.11")
def test_an_update_of_a_conversion_record_is_checked(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.update(LINK, env.id(BOLT_PER_BOX), {FACTOR_FIELD: Decimal("0")})

    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert details(raised.value) == [{"field": FACTOR_FIELD, "reason": NOT_POSITIVE_REASON}]
    assert conversion_of(env, BOLT)[FACTOR_FIELD] == BOX_FACTOR


@pytest.mark.req("6.11")
def test_a_record_is_no_duplicate_of_itself(env: EngineEnv) -> None:
    view = as_record(
        env.update(
            LINK,
            env.id(BOLT_PER_BOX),
            {LINK_UNIT_FIELD: env.id(BOX), FACTOR_FIELD: Decimal("30")},
        ).view
    )

    assert view.fields[FACTOR_FIELD] == Decimal("30")


@pytest.mark.req("6.11")
def test_an_update_to_the_base_unit_is_rejected(env: EngineEnv) -> None:
    with pytest.raises(ErpError) as raised:
        env.update(LINK, env.id(BOLT_PER_BOX), {LINK_UNIT_FIELD: env.id(PCS)})

    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert details(raised.value) == [{"field": LINK_UNIT_FIELD, "reason": INVALID_UNIT_REASON}]


def conversion_of(env: EngineEnv, material: str) -> Mapping[str, Value]:
    """The one conversion record of the material ``material`` as storage holds it."""
    records = [record for record in env.records(LINK) if record[MATERIAL_FIELD] == env.id(material)]
    assert len(records) == 1
    return records[0]


def _links(env: EngineEnv) -> Sequence[Value]:
    """The ids of the conversion records the sandbox holds, by id."""
    return [record["id"] for record in env.records(LINK)]
