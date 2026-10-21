"""erp_mock.seed.generators: the seven built-in generators and the blacklist (req 20.3, 20.7).

:data:`SAMPLES` holds one declaration per name in
:data:`~erp_mock.config.pack.GENERATORS` and :func:`draw_all` draws from all seven in that
order, as the Seed_Builder does for one record: the company name first, because
``short_name`` derives from the name the record already has. The tests then pin, in this
order:

- the sequence: the same ``random_seed`` gives the same values and a different one does
  not; ``int_range`` is the plain ``randint`` of a ``random.Random(random_seed)``; and a
  Faker draw advances that very Random, so Faker and the built-in generators share one
  sequence rather than keeping one each (requirement 20.3, design V10);
- what each generator draws: the shape and the bounds of its values, and the written order
  ``code_words`` keeps;
- the parameters :func:`~erp_mock.seed.generators.param_problem` rejects, which the
  resolve pass reports as an invalid generator parameter (requirement 20.6);
- the discarding (requirement 20.7): the kept values are the plain sequence with the
  blacklisted ones left out, a drawn ``name`` never repeats a name of the same entity -
  one the dataset wrote out in full included - and the build fails after exactly
  :data:`~erp_mock.seed.generators.DISCARD_LIMIT` discards in a row.

Every test draws from a fixed seed, so each of them has one outcome and no test depends on
a chance that a value shows up. The discard tests pass their own small blacklist and do not
depend on what ``blacklist.txt`` lists; two tests read the shipped file itself, one for its
format and one to keep it from ruling out so many company names that no entity can hold a
few hundred of them.
"""

import random
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

import pytest

from erp_mock.config.pack import (
    GENERATORS,
    ChoiceGenerator,
    CodeWordsGenerator,
    CompanyNameGenerator,
    DecimalRangeGenerator,
    GeneratorObject,
    IntRangeGenerator,
    PersonNameGenerator,
    ShortNameGenerator,
)
from erp_mock.seed.generators import (
    BLACKLIST,
    COMPANY_FORMS,
    DISCARD_LIMIT,
    MAX_CODE_WORDS,
    MIN_CODE_WORDS,
    SHORT_NAME_MAX,
    DiscardLimitExceeded,
    GeneratedValue,
    ValueGenerators,
    blacklist_hits,
    param_problem,
    short_name,
)

SEED: Final = 20261009

WORDS: Final = ("M8", "六角", "不锈钢", "螺栓", "垫片")
STATUSES: Final = ("enabled", "disabled")
PRICE_LOW: Final = "0.70"
PRICE_HIGH: Final = "0.95"
# Wide enough that two sequences of it never happen to agree.
WIDE_MAX: Final = 10**9

# One declaration per built-in generator, by the name that selects it.
SAMPLES: Final[Mapping[str, GeneratorObject]] = {
    "company_name": CompanyNameGenerator(gen="company_name"),
    "short_name": ShortNameGenerator(gen="short_name"),
    "person_name": PersonNameGenerator(gen="person_name"),
    "choice": ChoiceGenerator(gen="choice", values=STATUSES),
    "int_range": IntRangeGenerator(gen="int_range", min=0, max=WIDE_MAX),
    "decimal_range": DecimalRangeGenerator(gen="decimal_range", min=PRICE_LOW, max=PRICE_HIGH),
    "code_words": CodeWordsGenerator(gen="code_words", words=WORDS),
}

# The field each sample fills in; ``company_name`` fills in the name a record is checked on.
FIELDS: Final[Mapping[str, str]] = {
    "company_name": "name",
    "short_name": "short_name",
    "person_name": "contact",
    "choice": "status",
    "int_range": "payment_term_days",
    "decimal_range": "price",
    "code_words": "spec",
}

ENTITY: Final = "supplier"
# A field other than name, so that only the blacklist discards a value drawn for it.
OTHER_FIELD: Final = "code"

# The reasons one value is discarded, as erp_mock.seed.generators writes them.
BLACKLISTED: Final = "含真实企业名称黑名单词条 太极"
DUPLICATE: Final = "与同一实体已有记录的 name 相同"


def draw_all(gens: ValueGenerators, entity: str = ENTITY) -> list[GeneratedValue]:
    """One value from each generator, in :data:`GENERATORS` order, as one record."""
    name = gens.value(SAMPLES["company_name"], entity=entity, field=FIELDS["company_name"])
    values: list[GeneratedValue] = [name]
    values.extend(
        gens.value(SAMPLES[gen], entity=entity, field=FIELDS[gen], name=str(name))
        for gen in GENERATORS[1:]
    )
    return values


def records(seed: int, count: int) -> list[list[GeneratedValue]]:
    """``count`` records drawn one after another from one sequence seeded with ``seed``."""
    gens = ValueGenerators(seed)
    return [draw_all(gens) for _ in range(count)]


def draws(
    decl: GeneratorObject, count: int, blacklist: Sequence[str] = BLACKLIST
) -> list[GeneratedValue]:
    """``count`` values of ``decl`` for :data:`OTHER_FIELD`, from a new sequence."""
    gens = ValueGenerators(SEED, blacklist)
    return [gens.value(decl, entity=ENTITY, field=OTHER_FIELD) for _ in range(count)]


@pytest.mark.req("20.3")
def test_samples_cover_every_built_in_generator() -> None:
    assert tuple(SAMPLES) == GENERATORS
    assert set(FIELDS) == set(GENERATORS)


@pytest.mark.req("20.3")
def test_equal_seeds_give_equal_sequences() -> None:
    # Ten records, enough that the duplicate check discards and draws again on the way.
    expected = records(SEED, 10)
    assert records(SEED, 10) == expected
    assert records(SEED + 1, 10) != expected
    # The values of one record differ from one another, so the comparison is not vacuous.
    assert len({str(value) for value in expected[0]}) == len(GENERATORS)


@pytest.mark.req("20.3")
def test_int_range_is_the_randint_of_a_random_seeded_with_random_seed() -> None:
    plain = random.Random(SEED)
    expected = [plain.randint(0, WIDE_MAX) for _ in range(5)]
    assert draws(SAMPLES["int_range"], 5) == expected


@pytest.mark.req("20.3")
def test_one_sequence_drives_faker_and_the_built_in_generators() -> None:
    gens = ValueGenerators(SEED)
    before = gens.rng.getstate()
    # A Faker draw advances the Random the built-in generators draw from ...
    gens.value(SAMPLES["person_name"], entity="employee", field="contact")
    assert gens.rng.getstate() != before
    # ... and the next built-in draws go on from where Faker left the sequence.
    after_faker = [
        gens.value(SAMPLES["int_range"], entity=ENTITY, field=FIELDS["int_range"]) for _ in range(3)
    ]
    assert after_faker != draws(SAMPLES["int_range"], 3)


@pytest.mark.req("20.3")
def test_company_name_without_industry_is_a_faker_company_name() -> None:
    names = draws(SAMPLES["company_name"], 50)
    assert all(isinstance(name, str) and name.endswith("有限公司") for name in names)
    assert len(set(names)) > 40


@pytest.mark.req("20.3")
def test_company_name_with_industry_uses_the_industry_word_and_a_corporate_form() -> None:
    decl = CompanyNameGenerator(gen="company_name", industry="机械")
    names = [str(name) for name in draws(decl, 60)]
    assert all("机械" in name and name.endswith(COMPANY_FORMS) for name in names)
    # Both parts are drawn: every corporate form shows up and the trade names vary.
    assert {form for form in COMPANY_FORMS if any(n.endswith(form) for n in names)} == set(
        COMPANY_FORMS
    )
    assert len({name.partition("机械")[0] for name in names}) > 30


@pytest.mark.req("20.3")
def test_person_name_is_a_short_chinese_name() -> None:
    names = [str(name) for name in draws(SAMPLES["person_name"], 50)]
    assert all(2 <= len(name) <= 4 for name in names)
    assert len(set(names)) > 40


@pytest.mark.req("20.3")
def test_choice_draws_one_of_the_written_values() -> None:
    assert set(draws(SAMPLES["choice"], 50)) == set(STATUSES)
    # A written value of any scalar type comes back as it was written.
    mixed = draws(ChoiceGenerator(gen="choice", values=("raw", 5, True)), 50)
    assert set(mixed) == {"raw", 5, True}
    assert {type(value) for value in mixed} == {str, int, bool}


@pytest.mark.req("20.3")
def test_int_range_stays_within_its_bounds() -> None:
    assert set(draws(IntRangeGenerator(gen="int_range", min=7, max=9), 50)) == {7, 8, 9}
    assert draws(IntRangeGenerator(gen="int_range", min=-3, max=-3), 3) == [-3] * 3


@pytest.mark.req("20.3")
def test_decimal_range_draws_steps_of_the_written_place() -> None:
    values = draws(SAMPLES["decimal_range"], 500)
    low, high = Decimal(PRICE_LOW), Decimal(PRICE_HIGH)
    assert all(isinstance(value, Decimal) and low <= value <= high for value in values)
    # Two written decimals: every hundredth of the range, and nothing between them.
    assert set(values) == {low + Decimal(step) / 100 for step in range(26)}
    assert all(str(value) == f"{value:.2f}" for value in values)


@pytest.mark.req("20.3")
def test_decimal_range_of_whole_numbers_draws_integers() -> None:
    values = draws(DecimalRangeGenerator(gen="decimal_range", min="-2", max="3"), 100)
    assert set(values) == {Decimal(step) for step in range(-2, 4)}


@pytest.mark.req("20.3")
def test_code_words_joins_the_words_in_the_written_order() -> None:
    values = [str(value) for value in draws(SAMPLES["code_words"], 100)]
    counts = set()
    for value in values:
        picked = [word for word in WORDS if word in value]
        assert "".join(picked) == value
        counts.add(len(picked))
    assert counts == {MIN_CODE_WORDS, MAX_CODE_WORDS}
    assert len(set(values)) > 10


@pytest.mark.req("20.3")
def test_code_words_with_fewer_words_than_the_minimum() -> None:
    assert draws(CodeWordsGenerator(gen="code_words", words=("垫片",)), 3) == ["垫片"] * 3


@pytest.mark.req("20.3")
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("菊风机械有限公司", "菊风机械"),
        ("菊风公司网络有限公司", "菊风公司"),
        ("海创信息有限公司", "海创"),
        ("巨奥机械股份有限公司", "巨奥机械"),
        ("良诺机械有限责任公司", "良诺机械"),
        ("雨林木风计算机科技有限公司", "雨林木风计算"),
        ("公司", "公司"),
        ("科技", "科技"),
    ],
)
def test_short_name_keeps_the_trade_name(name: str, expected: str) -> None:
    assert short_name(name) == expected
    assert len(short_name(name)) <= SHORT_NAME_MAX


@pytest.mark.req("20.3")
def test_short_name_derives_from_the_name_of_the_same_record() -> None:
    gens = ValueGenerators(SEED)
    before = gens.rng.getstate()
    drawn = gens.value(SAMPLES["short_name"], entity=ENTITY, field="short_name", name="海创科技")
    assert drawn == "海创"
    # Derived, not drawn: the sequence stands still.
    assert gens.rng.getstate() == before


@pytest.mark.req("20.3")
def test_short_name_without_a_name_is_an_error() -> None:
    gens = ValueGenerators(SEED)
    with pytest.raises(ValueError, match="short_name"):
        gens.value(SAMPLES["short_name"], entity=ENTITY, field="short_name")


@pytest.mark.req("20.6")
@pytest.mark.parametrize(
    ("decl", "expected"),
    [
        pytest.param(
            IntRangeGenerator(gen="int_range", min=9, max=7), "min 9 大于 max 7", id="int"
        ),
        pytest.param(
            DecimalRangeGenerator(gen="decimal_range", min="0.95", max="0.70"),
            "min 0.95 大于 max 0.70",
            id="decimal",
        ),
        pytest.param(
            DecimalRangeGenerator(gen="decimal_range", min="1e3", max="2"),
            "min 1e3 不是小数字符串",
            id="not-a-decimal",
        ),
    ],
)
def test_param_problem_reports_bounds_that_cannot_draw(
    decl: GeneratorObject, expected: str
) -> None:
    assert param_problem(decl) == expected
    gens = ValueGenerators(SEED)
    with pytest.raises(ValueError, match=f"{ENTITY}[.]{OTHER_FIELD}: "):
        gens.value(decl, entity=ENTITY, field=OTHER_FIELD)


@pytest.mark.req("20.6")
def test_usable_parameters_have_no_problem() -> None:
    assert [gen for gen, decl in SAMPLES.items() if param_problem(decl) is not None] == []
    equal = DecimalRangeGenerator(gen="decimal_range", min="2.0", max="2")
    assert param_problem(equal) is None
    assert draws(equal, 3) == [Decimal("2.0")] * 3


@pytest.mark.req("20.7")
def test_blacklist_file_ships_with_the_framework() -> None:
    assert len(BLACKLIST) > 50
    assert all(entry and entry == entry.strip() for entry in BLACKLIST)
    assert [entry for entry in BLACKLIST if entry.startswith("#")] == []
    assert len(set(BLACKLIST)) == len(BLACKLIST)
    assert "华为" in BLACKLIST


@pytest.mark.req("20.7")
def test_blacklist_hits_names_the_entries_in_list_order() -> None:
    entries = ("太极", "彩虹", "方正科技")
    assert blacklist_hits("彩虹太极信息有限公司", entries) == ("太极", "彩虹")
    assert blacklist_hits("海创信息有限公司", entries) == ()
    # Compared as both are written: no folding of case or whitespace.
    assert blacklist_hits("IBM 中国", ("IBM",)) == ("IBM",)
    assert blacklist_hits("ibm 中国", ("IBM",)) == ()


@pytest.mark.req("20.7")
def test_blacklisted_values_are_dropped_from_the_same_sequence() -> None:
    blocked = ("彩虹", "太极", "七喜")
    plain = draws(SAMPLES["company_name"], 200, blacklist=())
    kept = [value for value in plain if not blacklist_hits(str(value), blocked)]
    assert len(kept) < len(plain), "the sample must contain blacklisted values"
    # What a build keeps is the plain sequence without the discarded values: a discarded
    # draw advances the sequence and the next value comes from it.
    assert draws(SAMPLES["company_name"], len(kept), blacklist=blocked) == kept


@pytest.mark.req("20.7")
def test_a_discarded_draw_does_not_fail_the_build() -> None:
    decl = ChoiceGenerator(gen="choice", values=("太极科技", "海创科技"))
    assert draws(decl, 20, blacklist=("太极",)) == ["海创科技"] * 20


@pytest.mark.req("20.7")
def test_no_generated_company_name_contains_a_blacklisted_name() -> None:
    gens = ValueGenerators(SEED)
    decl = SAMPLES["company_name"]
    count = 200
    names = {
        str(gens.value(decl, entity=ENTITY, field=FIELDS["company_name"])) for _ in range(count)
    }
    assert [name for name in names if blacklist_hits(name)] == []
    # Every draw was kept and differs from the ones before it: the shipped blacklist
    # leaves one entity enough company names for a few hundred background records.
    assert len(names) == count


@pytest.mark.req("20.7")
def test_a_drawn_name_never_repeats_a_name_of_the_same_entity() -> None:
    gens = ValueGenerators(SEED)
    decl = SAMPLES["choice"]
    first = gens.value(decl, entity=ENTITY, field="name")
    second = gens.value(decl, entity=ENTITY, field="name")
    assert {first, second} == set(STATUSES)
    assert gens.taken_names(ENTITY) == frozenset(STATUSES)
    # Another entity has its own names, and a field other than name may repeat a value.
    assert gens.value(decl, entity="customer", field="name") in STATUSES
    assert gens.value(decl, entity=ENTITY, field="status") in STATUSES


@pytest.mark.req("20.7")
def test_names_the_dataset_writes_out_count_as_taken() -> None:
    gens = ValueGenerators(SEED)
    gens.add_name(ENTITY, "enabled")
    assert gens.value(SAMPLES["choice"], entity=ENTITY, field="name") == "disabled"


@pytest.mark.req("20.7")
@pytest.mark.parametrize(
    ("decl", "blacklist", "cause"),
    [
        pytest.param(
            ChoiceGenerator(gen="choice", values=("太极科技", "太极网络")),
            ("太极",),
            BLACKLISTED,
            id="blacklisted",
        ),
        pytest.param(
            ChoiceGenerator(gen="choice", values=("海创",)), (), DUPLICATE, id="duplicate"
        ),
    ],
)
def test_discarding_every_value_fails_after_the_limit(
    decl: GeneratorObject, blacklist: Sequence[str], cause: str
) -> None:
    gens = ValueGenerators(SEED, blacklist)
    if not blacklist:
        # Take the only value the generator can draw, so every later draw repeats it.
        assert gens.value(decl, entity=ENTITY, field="name") == "海创"
    with pytest.raises(DiscardLimitExceeded) as info:
        gens.value(decl, entity=ENTITY, field="name")
    error = info.value
    assert (error.entity, error.field, error.cause) == (ENTITY, "name", cause)
    assert str(error.value).startswith(("太极", "海创"))
    assert str(error) == (
        f"{ENTITY}.name 连续 {DISCARD_LIMIT} 次生成的值都被丢弃，最后一个是 {error.value}：{cause}"
    )


@pytest.mark.req("20.7")
def test_the_limit_is_reached_after_exactly_that_many_draws() -> None:
    decl = ChoiceGenerator(gen="choice", values=("太极科技", "太极网络"))
    failing = ValueGenerators(SEED, ("太极",))
    with pytest.raises(DiscardLimitExceeded):
        failing.value(decl, entity=ENTITY, field=OTHER_FIELD)
    reference = ValueGenerators(SEED, ())
    for _ in range(DISCARD_LIMIT):
        reference.value(decl, entity=ENTITY, field=OTHER_FIELD)
    assert failing.rng.getstate() == reference.rng.getstate()
