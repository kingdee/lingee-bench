"""erp_mock.seed.resolve: the static pass over a dataset (req 20.1, 20.6, 20.16).

The fixture's own initial dataset (``tests/fixtures/instances/fx-demo/datasets/fx-basic.yaml``)
is the one dataset that has to resolve: the first tests walk the plan it gives and pin what
the build reads from it - the records in written order with their kinds, the resolved field
names and values, the conversion and the line numbers of a push, the state operations of a
status, and the generators of ``bulk``.

:data:`CASES` then holds one dataset per class of error requirement 20.6 lists, each built
by changing one thing in the valid records of :data:`GOOD`, and asserts the config path and
the reason :class:`~erp_mock.seed.resolve.SeedError` reports. Three classes are checked
elsewhere, because a validated document cannot carry them: a repeated alias is a repeated
YAML mapping key (:func:`test_a_repeated_alias_is_reported_by_the_yaml_reader`), a
``closed_before`` that is no period expression is rejected by the document model
(:func:`test_closed_before_is_a_period_expression`), and a generator a business pack binds
to a field that is not there is an error of that pack's file
(:func:`test_a_generator_bound_to_an_unknown_field_names_the_pack_file`). The spelling
``<kind value>_kind`` needs an object with a field named ``kind``, which the fixture packs
have none of, so those two tests build a small domain model of their own.
"""

from collections.abc import Mapping
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.date_expr import MonthDay, MonthPeriod, Today, WeekDay
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.instance import DatasetDef, PeriodsDef
from erp_mock.config.model import DomainModel, EntityModel, FieldModel
from erp_mock.config.pack import SeedGeneratorsFile
from erp_mock.loader.step4 import RawDoc, RawPack
from erp_mock.loader.yaml_io import YamlLoadError, parse_yaml
from erp_mock.seed.resolve import (
    AliasId,
    CodeId,
    DateAt,
    Drawn,
    PlanLine,
    PlanRecord,
    SeedError,
    SeedPlan,
    Written,
    dataset_file,
    resolve,
)
from tests.helpers.fixtures import BASE_PACK, DATASET, load_fixture

# The current user of the fixture: the first operator of its Shared_Data (requirement 20.16).
USER: Final = "opr_000001"

# Valid records to change one thing in; ``article:main`` needs ``measure`` PCS, which the
# fixture's Shared_Data provides, and ``vendor:main`` is the partner of every bill below.
VENDOR: Final[Mapping[str, object]] = {"kind": "vendor", "name": "夹具甲供货单位"}
ARTICLE: Final[Mapping[str, object]] = {
    "kind": "article",
    "name": "夹具甲存货",
    "grade": "plain",
    "base_measure": "PCS",
}
LINK: Final[Mapping[str, object]] = {
    "kind": "measure_link",
    "article": "article:main",
    "alt_measure": "BOX",
    "ratio": "12",
}
ORDER: Final[Mapping[str, object]] = {
    "kind": "supply_order",
    "vendor": "vendor:main",
    "biz_date": "month(-1).day(5)",
    "need_date": "month(0).day(5)",
    "status": "approved",
    "lines": [{"article": "article:main", "measure": "PCS", "qty": "10", "price": "1.00"}],
}
# A push: the header fields the conversion maps are not written (requirement 20.6) and the
# line names its source line by its number (requirement 20.16).
ENTRY: Final[Mapping[str, object]] = {
    "kind": "goods_entry",
    "source": "supply_order:main",
    "biz_date": "month(-1).day(8)",
    "lines": [{"source_line": 1, "entry_qty": "4", "depot": "D001"}],
}
FEE: Final[Mapping[str, object]] = {
    "kind": "fee_note",
    "vendor": "vendor:main",
    "biz_date": "month(-1).day(5)",
    "due_date": "month(0).day(5)",
    "lines": [{"item_text": "夹具甲运费", "qty": "1", "price": "100"}],
}

# The records every case starts from.
GOOD: Final[Mapping[str, Mapping[str, object]]] = {"vendor:main": VENDOR, "article:main": ARTICLE}


@cache
def _fixture() -> tuple[DomainModel, tuple[RawPack, ...], str | None, RawDoc[DatasetDef]]:
    """The fixture's configuration and its dataset definition, loaded once."""
    loader = load_fixture()
    if loader.instance is None or loader.domain is None:
        raise AssertionError("夹具没有通过加载步骤")
    document = next(doc for doc in loader.raw_datasets if doc.document.dataset == DATASET)
    return loader.domain, loader.raw_packs, loader.instance.current_user, document


def fixture_plan() -> SeedPlan:
    """The plan of the fixture's own initial dataset."""
    domain, packs, user, document = _fixture()
    return resolve(document.document, domain, packs, file=document.file, instance_user=user)


def definition(**changes: object) -> DatasetDef:
    """A dataset definition like the fixture's, with ``changes`` written into it."""
    written = {
        "dataset": DATASET,
        "version": "1.0.0",
        "random_seed": 7,
        "include_shared": [BASE_PACK],
        "current_user": USER,
        **changes,
    }
    return DatasetDef.model_validate(written)


def failure(**changes: object) -> SeedError:
    """The error resolving a definition with ``changes`` against the fixture raises."""
    domain, packs, user, _ = _fixture()
    with pytest.raises(SeedError) as caught:
        resolve(definition(**changes), domain, packs, instance_user=user)
    return caught.value


def changed(record: Mapping[str, object], **changes: object) -> dict[str, object]:
    """``record`` with ``changes`` written after its own keys, in that order."""
    return {**record, **changes}


def lines(record: Mapping[str, object], **changes: object) -> dict[str, object]:
    """``record`` with ``changes`` written into its first line."""
    written = record["lines"]
    assert isinstance(written, list)
    first = written[0]
    assert isinstance(first, dict)
    return {**record, "lines": [{**first, **changes}, *written[1:]]}


def without(record: Mapping[str, object], key: str) -> dict[str, object]:
    """``record`` without the line key ``key``."""
    written = record["lines"]
    assert isinstance(written, list)
    first = written[0]
    assert isinstance(first, dict)
    return {**record, "lines": [{k: v for k, v in first.items() if k != key}, *written[1:]]}


def fields_of(record: PlanRecord | PlanLine) -> dict[str, object]:
    """The resolved values of a record or of one line, by the field each one fills."""
    return {item.name: item.value for item in record.fields}


# --- the fixture's own dataset -----------------------------------------------------------


@pytest.mark.req("20.1", "20.6")
def test_the_fixture_dataset_resolves() -> None:
    plan = fixture_plan()
    _, _, _, document = _fixture()
    assert (plan.dataset, plan.version, plan.file) == (DATASET, "1.0.0", document.file)
    assert (plan.random_seed, plan.include_shared) == (20261009, (BASE_PACK,))
    # The dataset sets its own current user, and periods.closed_before is parsed.
    assert (plan.current_user, plan.closed_before) == (USER, MonthPeriod(-1))


@pytest.mark.req("20.17")
def test_the_records_keep_the_written_order_and_their_kinds() -> None:
    plan = fixture_plan()
    assert [(record.alias, record.kind) for record in plan.records][:6] == [
        ("vendor:main", "vendor"),
        ("vendor:spare", "vendor"),
        ("vendor:retired", "vendor"),
        ("article:bolt", "article"),
        ("measure_link:bolt_box", "measure_link"),
        ("article:washer", "article"),
    ]
    assert [record.alias for record in plan.records][-1] == "supply_order:open_draft"
    assert {record.kind for record in plan.records if record.bill} == {
        "supply_order",
        "goods_entry",
        "fee_note",
        "pay_slip",
        "issue_note",
        "stock_move",
        "count_sheet",
    }


@pytest.mark.req("20.16")
def test_a_ref_resolves_by_alias_or_by_code() -> None:
    plan = fixture_plan()
    link = next(record for record in plan.records if record.alias == "measure_link:bolt_box")
    # ``article`` is written without the _id suffix: an alias of the dataset, and a code of
    # the Shared_Data the build looks up among the records it created.
    assert fields_of(link) == {
        "article_id": AliasId("article:bolt"),
        "alt_measure_id": CodeId("measure", "BOX"),
        "ratio": Written("12"),
    }
    assert [item.written for item in link.fields] == ["article", "alt_measure", "ratio"]


@pytest.mark.req("20.6")
def test_a_date_field_keeps_its_parsed_expression() -> None:
    plan = fixture_plan()
    order = next(record for record in plan.records if record.alias == "supply_order:open_draft")
    assert fields_of(order)["biz_date"] == DateAt(Today(0))
    assert fields_of(order)["need_date"] == DateAt(Today(14))
    waiting = next(
        record for record in plan.records if record.alias == "supply_order:waiting_approval"
    )
    assert fields_of(waiting)["biz_date"] == DateAt(WeekDay("this_week", "mon"))
    history = next(record for record in plan.records if record.alias == "fee_note:freight")
    assert fields_of(history)["biz_date"] == DateAt(MonthDay(-1, 15))


@pytest.mark.req("20.2")
def test_a_status_becomes_the_operations_that_reach_it() -> None:
    plan = fixture_plan()
    actions = {record.alias: (record.status, record.actions) for record in plan.records}
    # standard: submit, then approve, then the close that counts as manual.
    assert actions["supply_order:open_draft"] == (None, ())
    assert actions["supply_order:waiting_approval"] == ("submitted", ("submit",))
    assert actions["supply_order:partly_received"] == ("approved", ("submit", "approve"))
    assert actions["supply_order:closed_by_hand"] == ("closed", ("submit", "approve", "close"))
    # simple: approve alone, from draft (requirement 7.11).
    assert actions["fee_note:freight"] == ("approved", ("approve",))


@pytest.mark.req("20.6", "20.16")
def test_a_pushed_record_names_its_conversion_and_its_source_lines() -> None:
    plan = fixture_plan()
    entry = next(record for record in plan.records if record.alias == "goods_entry:partial")
    assert (entry.source, entry.conversion) == (
        "supply_order:partly_received",
        "supply_order_to_entry",
    )
    # The header fields the conversion maps are not written, only biz_date is.
    assert list(fields_of(entry)) == ["biz_date"]
    assert [(line.source_line, fields_of(line)) for line in entry.lines] == [
        (1, {"entry_qty": Written("400"), "depot_id": CodeId("depot", "D001")})
    ]
    pay = next(record for record in plan.records if record.alias == "pay_slip:freight_part")
    assert (pay.source, pay.conversion) == ("fee_note:freight", "fee_note_to_pay_slip")


@pytest.mark.req("20.4")
def test_bulk_draws_the_generators_the_pack_binds_in_their_written_order() -> None:
    plan = fixture_plan()
    assert [(item.kind, item.count, item.config_path) for item in plan.bulk] == [
        ("vendor", 12, "bulk[0]")
    ]
    item = plan.bulk[0]
    assert [field.written for field in item.fields] == [
        "vendor.name",
        "vendor.short_name",
        "vendor.contact",
        "vendor.pay_days",
        "vendor.default_tax_rate",
    ]
    assert [field.name for field in item.fields][:2] == ["name", "short_name"]
    assert all(isinstance(field.value, Drawn) for field in item.fields)


@pytest.mark.req("20.1")
def test_the_dataset_file_is_derived_when_the_caller_has_only_the_document() -> None:
    domain, packs, user, _ = _fixture()
    written = definition(entities=dict(GOOD))
    plan = resolve(written, domain, packs, instance_user=user)
    assert plan.file == dataset_file(written) == f"datasets/{DATASET}.yaml"


@pytest.mark.req("20.5")
def test_the_current_user_falls_back_to_the_instance_configuration() -> None:
    domain, packs, user, _ = _fixture()
    plan = resolve(definition(current_user=None), domain, packs, instance_user=user)
    assert plan.current_user == user == USER


# --- one dataset per class of error ------------------------------------------------------

# (changes to write into the definition, the config path reported, part of the reason).
CASES: Final = [
    pytest.param(
        {"entities": {"ghost:one": {"kind": "ghost", "name": "x"}}},
        "entities.ghost:one.kind",
        "kind ghost 不是实例配置中的实体或单据",
        id="kind-unknown",
    ),
    pytest.param(
        {"entities": {"article:one": dict(VENDOR)}},
        "entities.article:one",
        "别名前缀 article 与 kind vendor 不同",
        id="alias-prefix",
    ),
    pytest.param(
        {"entities": {"measure_link:one": dict(LINK), "article:main": dict(ARTICLE)}},
        "entities.measure_link:one.article",
        "别名 article:main 在引用处之后才声明",
        id="alias-used-too-early",
    ),
    pytest.param(
        {"entities": {**GOOD, "measure_link:one": changed(LINK, article="article:ghost")}},
        "entities.measure_link:one.article",
        "别名 article:ghost 不存在",
        id="alias-unknown",
    ),
    pytest.param(
        {"entities": {**GOOD, "measure_link:one": changed(LINK, article="vendor:main")}},
        "entities.measure_link:one.article",
        "别名 vendor:main 的 kind 是 vendor，与引用字段 article_id 引用的 article 不符",
        id="alias-kind-mismatch",
    ),
    pytest.param(
        {"entities": {"vendor:one": changed(VENDOR, nickname="甲")}},
        "entities.vendor:one.nickname",
        "vendor 没有字段 nickname",
        id="field-unknown",
    ),
    pytest.param(
        {"entities": {"article:one": changed(ARTICLE, base_measure_id="PCS")}},
        "entities.article:one.base_measure_id",
        "引用字段 base_measure_id 去掉 _id 后缀书写，即 base_measure",
        id="ref-written-with-id-suffix",
    ),
    pytest.param(
        {"entities": {"vendor:one": changed(VENDOR, code="V9999")}},
        "entities.vendor:one.code",
        "字段 code 是只读字段，不能写入",
        id="read-only-field",
    ),
    pytest.param(
        {"entities": {**GOOD, "supply_order:one": changed(ORDER, biz_date="2026-10-09")}},
        "entities.supply_order:one.biz_date",
        "2026-10-09 不是日期类表达式",
        id="date-is-not-an-expression",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "supply_order:one": changed(ORDER, biz_date={"gen": "choice", "values": ["today"]}),
            }
        },
        "entities.supply_order:one.biz_date",
        "日期字段 biz_date 不能由生成器取值",
        id="date-from-a-generator",
    ),
    pytest.param(
        {
            "entities": {
                "article:one": changed(ARTICLE, base_measure={"gen": "choice", "values": ["PCS"]})
            }
        },
        "entities.article:one.base_measure",
        "引用字段 base_measure_id 不能由生成器取值",
        id="ref-from-a-generator",
    ),
    pytest.param(
        {"entities": {"article:one": changed(ARTICLE, base_measure=5)}},
        "entities.article:one.base_measure",
        "引用字段 base_measure_id 的值必须写成别名或编码",
        id="ref-is-no-text",
    ),
    pytest.param(
        {
            "entities": {
                "vendor:one": changed(VENDOR, pay_days={"gen": "int_range", "min": 9, "max": 7})
            }
        },
        "entities.vendor:one.pay_days",
        "生成器 int_range 的参数不合法：min 9 大于 max 7",
        id="generator-parameters",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "measure_link:one": changed(
                    LINK, ratio={"gen": "decimal_range", "min": "0.00001", "max": "0.00002"}
                ),
            }
        },
        "entities.measure_link:one.ratio",
        "生成器 decimal_range 的边界有 5 位小数，超过 ratio（qty）的 4 位",
        id="generator-precision",
    ),
    pytest.param(
        {
            "entities": {
                "vendor:one": changed(
                    VENDOR, pay_days={"gen": "decimal_range", "min": "1", "max": "9"}
                )
            }
        },
        "entities.vendor:one.pay_days",
        "生成器 decimal_range 要求小数字段，pay_days 是 int 字段",
        id="generator-field-type",
    ),
    pytest.param(
        {
            "entities": {
                "vendor:one": {
                    "kind": "vendor",
                    "short_name": {"gen": "short_name"},
                    "name": "夹具甲供货单位",
                }
            }
        },
        "entities.vendor:one.short_name",
        "生成器 short_name 由同一记录的 name 派生，要求 name 写在它之前",
        id="short-name-without-a-name",
    ),
    pytest.param(
        {"entities": {**GOOD, "fee_note:one": changed(FEE, status="submitted")}},
        "entities.fee_note:one.status",
        "status submitted 不属于 fee_note 的 simple 状态机（draft、approved）",
        id="status-outside-the-state-machine",
    ),
    pytest.param(
        {"entities": {**GOOD, "supply_order:one": changed(ORDER, status=5)}},
        "entities.supply_order:one.status",
        "status 必须是状态名",
        id="status-is-no-text",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "supply_order:main": dict(ORDER),
                "goods_entry:one": changed(ENTRY, vendor="vendor:main"),
            }
        },
        "entities.goods_entry:one.vendor",
        "表头字段 vendor_id 由下推 supply_order_to_entry 的 header_mapping 映射，不能写入",
        id="header-field-the-conversion-maps",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "supply_order:main": dict(ORDER),
                "goods_entry:one": lines(ENTRY, measure="PCS"),
            }
        },
        "entities.goods_entry:one.lines[0].measure",
        "下推 supply_order_to_entry 的分录只能写 source_line 与 depot_id、entry_qty，没有 measure_id",
        id="line-field-the-push-does-not-take",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "supply_order:main": dict(ORDER),
                "goods_entry:one": without(ENTRY, "source_line"),
            }
        },
        "entities.goods_entry:one.lines[0]",
        "下推生成的分录要写 source_line，即来源单据的行号",
        id="source-line-missing",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "supply_order:main": dict(ORDER),
                "goods_entry:one": lines(ENTRY, source_line=2),
            }
        },
        "entities.goods_entry:one.lines[0].source_line",
        "source_line 2 超出来源单据 supply_order:main 的分录数 1",
        id="source-line-out-of-range",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "supply_order:main": dict(ORDER),
                "goods_entry:one": lines(ENTRY, source_line=0),
            }
        },
        "entities.goods_entry:one.lines[0].source_line",
        "source_line 必须是正整数行号",
        id="source-line-is-no-line-number",
    ),
    pytest.param(
        {"entities": {**GOOD, "supply_order:one": lines(ORDER, source_line=1)}},
        "entities.supply_order:one.lines[0].source_line",
        "source_line 只能写在下推生成的单据的分录上",
        id="source-line-without-a-push",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "pay_slip:one": {
                    "kind": "pay_slip",
                    "source": "article:main",
                    "lines": [{"source_line": 1, "qty": "1", "price": "1"}],
                },
            }
        },
        "entities.pay_slip:one.source",
        "别名 article:main 的 kind article 不是单据，不能作为下推的来源",
        id="source-is-an-entity",
    ),
    pytest.param(
        {"entities": {"vendor:one": changed(VENDOR, source="vendor:main")}},
        "entities.vendor:one.source",
        "kind vendor 不是单据，不能声明 source",
        id="source-on-an-entity-record",
    ),
    pytest.param(
        {"entities": {**GOOD, "supply_order:one": changed(ORDER, source=5)}},
        "entities.supply_order:one.source",
        "source 的值必须是来源单据的别名",
        id="source-is-no-alias",
    ),
    pytest.param(
        {
            "entities": {
                **GOOD,
                "supply_order:main": dict(ORDER),
                "pay_slip:one": {
                    "kind": "pay_slip",
                    "source": "supply_order:main",
                    "lines": [{"source_line": 1, "qty": "1", "price": "1"}],
                },
            }
        },
        "entities.pay_slip:one.source",
        "supply_order 到 pay_slip 之间没有 conversion",
        id="no-conversion-between-source-and-target",
    ),
    pytest.param(
        {"entities": {"vendor:one": changed(VENDOR, lines=[{"name": "甲"}])}},
        "entities.vendor:one.lines",
        "实体 vendor 没有分录",
        id="lines-on-an-entity",
    ),
    pytest.param(
        {"entities": {"vendor:one": changed(VENDOR, extras=[{"name": "甲"}])}},
        "entities.vendor:one.extras",
        "分录写在 lines 下，不是 extras",
        id="a-list-under-another-key",
    ),
    pytest.param(
        {"bulk": [{"kind": "supply_order", "count": 2}]},
        "bulk[0].kind",
        "bulk 的 kind supply_order 是单据，bulk 只批量生成实体的记录",
        id="bulk-of-a-bill",
    ),
    pytest.param(
        {"bulk": [{"kind": "depot", "count": 2}]},
        "bulk[0].kind",
        "bulk 的 kind depot 的 shared 为 true",
        id="bulk-of-shared-data",
    ),
    pytest.param(
        {"bulk": [{"kind": "ghost", "count": 2}]},
        "bulk[0].kind",
        "bulk 的 kind ghost 不是实例配置中的实体",
        id="bulk-of-an-unknown-kind",
    ),
    pytest.param(
        {"current_user": "ven_000001"},
        "current_user",
        "current_user ven_000001 不是用户上下文实体 operator 的内部 ID（以 opr_ 开头）",
        id="current-user-is-no-user-id",
    ),
]


@pytest.mark.req("20.6", "20.16")
@pytest.mark.parametrize(("changes", "config_path", "reason"), CASES)
def test_one_error_per_class(changes: Mapping[str, object], config_path: str, reason: str) -> None:
    error = failure(**changes)
    assert error.config_path == config_path
    assert reason in error.reason


@pytest.mark.req("20.6")
def test_the_error_names_the_dataset_file_the_config_path_and_the_reason() -> None:
    error = failure(entities={"vendor:one": changed(VENDOR, nickname="甲")})
    assert error.file == "datasets/fx-basic.yaml"
    assert str(error) == f"{error.file}: {error.config_path}: {error.reason}"
    # A plain exception with read-only-looking fields, which args carry (copies, pickles).
    assert error.args == (error.file, error.config_path, error.reason)


@pytest.mark.req("20.6")
def test_more_than_one_conversion_between_the_same_bills_is_an_error() -> None:
    domain, packs, user, _ = _fixture()
    twin = replace(domain.conversions["supply_order_to_entry"], id="supply_order_to_entry_2")
    conversions = FrozenMap({**domain.conversions, twin.id: twin})
    written = definition(
        entities={
            **GOOD,
            "supply_order:main": dict(ORDER),
            "goods_entry:one": dict(ENTRY),
        }
    )
    with pytest.raises(SeedError) as caught:
        resolve(written, replace(domain, conversions=conversions), packs, instance_user=user)
    assert caught.value.config_path == "entities.goods_entry:one.source"
    assert caught.value.reason == (
        "supply_order 到 goods_entry 之间有 2 个 conversion："
        "supply_order_to_entry、supply_order_to_entry_2"
    )


# --- what a validated document cannot carry ----------------------------------------------


@pytest.mark.req("20.6")
def test_a_repeated_alias_is_reported_by_the_yaml_reader() -> None:
    text = "\n".join(
        [
            "dataset: fx-basic",
            "entities:",
            "  vendor:main: { kind: vendor, name: 甲 }",
            "  vendor:main: { kind: vendor, name: 乙 }",
        ]
    )
    with pytest.raises(YamlLoadError) as caught:
        parse_yaml(text, Path("datasets/fx-basic.yaml"))
    assert caught.value.config_path == "line 4"
    assert "vendor:main" in caught.value.reason


@pytest.mark.req("20.1", "20.6")
def test_closed_before_is_a_period_expression() -> None:
    # The document model rejects the text, so loading step 4 reports it (requirement 2.13).
    with pytest.raises(ValueError, match="month"):
        definition(periods={"closed_before": "today"})
    # The pass checks it again, on a document that was not validated.
    domain, packs, user, _ = _fixture()
    written = DatasetDef.model_construct(
        dataset=DATASET,
        version="1.0.0",
        random_seed=7,
        periods=PeriodsDef.model_construct(closed_before="today"),
    )
    with pytest.raises(SeedError) as caught:
        resolve(written, domain, packs, instance_user=user)
    assert caught.value.config_path == "periods.closed_before"
    assert "today 不是期间类表达式" in caught.value.reason


@pytest.mark.req("20.6")
def test_a_generator_bound_to_an_unknown_field_names_the_pack_file() -> None:
    domain, packs, user, _ = _fixture()
    document = SeedGeneratorsFile.model_validate(
        {"generators": {"vendor.nickname": {"gen": "person_name"}}}
    )
    broken = tuple(
        replace(pack, generators=RawDoc("seed/generators.yaml", document))
        if pack.name == BASE_PACK
        else pack
        for pack in packs
    )
    with pytest.raises(SeedError) as caught:
        resolve(
            definition(bulk=[{"kind": "vendor", "count": 2}]), domain, broken, instance_user=user
        )
    assert caught.value.file == f"{BASE_PACK}/seed/generators.yaml"
    assert caught.value.config_path == "generators.vendor.nickname"
    assert caught.value.reason == "vendor 没有字段 nickname"


# --- a field named kind ------------------------------------------------------------------

KIND_ENTITY: Final = "material"


def kind_domain() -> DomainModel:
    """A domain model with one entity that has a field named ``kind`` (req 20.16)."""
    fields = FrozenMap(
        (field.name, field)
        for field in (
            FieldModel(name="id", type="string", origin="standard", writable=False),
            FieldModel(name="name", type="string", origin="declared", writable=True),
            FieldModel(name="kind", type="string", origin="declared", writable=True),
        )
    )
    entity = EntityModel(
        name=KIND_ENTITY,
        pack="fx_base",
        file=f"entities/{KIND_ENTITY}.yaml",
        id_prefix="mat",
        fields=fields,
    )
    return DomainModel(entities=FrozenMap({KIND_ENTITY: entity}))


@pytest.mark.req("20.16")
def test_a_field_named_kind_is_written_with_the_kind_value_before_it() -> None:
    plan = resolve(
        definition(
            current_user=None,
            entities={f"{KIND_ENTITY}:one": {"kind": KIND_ENTITY, "material_kind": "raw"}},
        ),
        kind_domain(),
    )
    (field,) = plan.records[0].fields
    assert (field.written, field.name, field.value) == ("material_kind", "kind", Written("raw"))


@pytest.mark.req("20.16")
def test_another_prefix_before_kind_is_an_error_that_gives_the_spelling() -> None:
    written = definition(
        current_user=None,
        entities={f"{KIND_ENTITY}:one": {"kind": KIND_ENTITY, "goods_kind": "raw"}},
    )
    with pytest.raises(SeedError) as caught:
        resolve(written, kind_domain())
    assert caught.value.config_path == f"entities.{KIND_ENTITY}:one.goods_kind"
    assert caught.value.reason == "material 没有字段 goods_kind；名为 kind 的字段写作 material_kind"
