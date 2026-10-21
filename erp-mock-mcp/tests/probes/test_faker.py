"""Probe for the Faker behaviour the seed builder relies on (design V10).

Checked with Faker 40.43.0 on the uv-managed CPython 3.12.14. V10 holds; the tests below
are the evidence. If a test fails after a version change, switch to the fallback named at
the end.

Replacing the random source. ``Faker("zh_CN")`` is a proxy over one ``Generator``; every
provider added to it holds that generator and draws through ``self.generator.random``.
The proxy's ``random`` setter (single-locale mode only) stores the given ``random.Random``
on the generator, so ``name()`` (zh_CN format ``{{last_name}}{{first_name}}``, filled in by
``Generator.parse``), ``company()`` (``{{company_prefix}}{{company_suffix}}``) and
``company_prefix()`` draw only from it and advance it: Faker and the built-in generators
consume one sequence. ``fake.random = rng`` works at runtime, but Faker's proxy.pyi omits
the ``random`` property, so mypy reports attr-defined. The typed equivalent assigns
``random`` on a ``Generator()`` and passes that as ``Faker("zh_CN", generator=...)``; it
yields the same sequence.

No hidden shared state on that path. Generators that were not given a Random share the
module-level ``faker.generator.random``, which ``Faker.seed()`` reseeds. Once a Random is
assigned, neither that object, ``Faker.seed()``, other Faker instances nor the stdlib's
global random change the output, and two instances given equal seeds produce equal
sequences, also when their calls interleave. Two pitfalls remain. ``seed_instance(s)``
creates a per-generator Random only while the generator still uses the shared one; after
an assignment it reseeds the assigned Random in place, restarting the sequence the
built-in generators share. ``fake.unique`` keeps its seen values per Faker instance and
per method and arguments, and they survive a new assignment of ``random``.

PYTHONHASHSEED plays no part: the zh_CN data are lists plus an OrderedDict of weighted
surnames, and interpreters started with PYTHONHASHSEED 0 and 12345 (their str hashes
differ) print the sequence the test process computes.

Constraints for the generators (task 17.1):

* Create one ``Faker("zh_CN")`` per build and assign the build's Random before the first
  draw; do not call ``Faker.seed()``, ``seed_instance()`` or ``seed_locale()``. Use a single
  locale: with several, the ``random`` property raises NotImplementedError.
* Draw with ``name()``, ``company()`` and ``company_prefix()``, not through ``fake.unique``:
  its seen set ignores the blacklist and the per-entity scope and gives up after its own
  1000 retries. For duplicates alone, a discard loop over plain calls yields the values
  ``unique`` would; discarded draws advance the sequence deterministically either way.
* Some methods bypass the assigned Random: the passport methods, served by the en_US
  fallback provider, use the stdlib's global random (``passport_gender(seed)`` reseeds it);
  ``binary()``, ``zip()`` and ``tar()`` read ``os.urandom`` because an assignment does not
  mark the generator as seeded; date and time methods resolve "now" and "today" from the
  system clock.
* ``company()`` has 324 distinct values (81 distinct prefixes, two of them listed twice,
  times 4 suffixes): an entity can hold at most 324 generated company names before the
  next one exhausts the 1000-discard limit. Two prefixes already end in 公司
  (菊风公司, 惠派国际公司), giving names such as 菊风公司网络有限公司.
* ``name()`` joins one of 399 weighted surnames with one of 131 given names. The
  constructor's ``use_weighting`` (default True) selects the surname weighting; changing
  it changes the sequence.

Fallback: take only the word lists (``last_names`` and ``first_names`` of
``faker.providers.person.zh_CN.Provider``, ``company_prefixes`` and ``company_suffixes``
of ``faker.providers.company.zh_CN.Provider``) and choose from them with the shared Random
in the built-in generators.
"""

import json
import os
import random
import subprocess
import sys
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import faker.generator
import pytest
from faker import Faker, Generator
from faker.providers.company.zh_CN import Provider as CompanyProvider
from faker.providers.person.zh_CN import Provider as PersonProvider

REPO_ROOT = Path(__file__).resolve().parents[2]

SEED = 20240115
# Steps per sampled sequence; each step draws three Faker values and one built-in value.
STEPS = 200
UNIQUE_DRAWS = 50

PERSON_ZH_CN = "faker.providers.person.zh_CN"
COMPANY_ZH_CN = "faker.providers.company.zh_CN"

Draw = str | int


def seeded_faker(seed: int) -> tuple[Faker, random.Random]:
    """Return a zh_CN Faker drawing from a new ``Random(seed)``, and that Random."""
    rng = random.Random(seed)
    fake = Faker("zh_CN")
    fake.random = rng  # type: ignore[attr-defined]  # proxy.pyi omits the random property
    return fake, rng


def seeded_faker_typed(seed: int) -> tuple[Faker, random.Random]:
    """Like ``seeded_faker``, with the Random set on the generator before construction."""
    rng = random.Random(seed)
    generator = Generator()
    generator.random = rng
    return Faker("zh_CN", generator=generator), rng


def step(fake: Faker, rng: random.Random) -> list[Draw]:
    """Draw the way the seed builder interleaves Faker values with a built-in value."""
    return [fake.name(), fake.company(), fake.company_prefix(), rng.randrange(1_000_000)]


def sample(seed: int) -> list[Draw]:
    """Return ``STEPS`` steps drawn from a new Faker seeded with ``seed``.

    The subprocess probe imports and runs this function in a fresh interpreter.
    """
    fake, rng = seeded_faker(seed)
    values: list[Draw] = []
    for _ in range(STEPS):
        values.extend(step(fake, rng))
    return values


@pytest.fixture
def restore_shared_randoms(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Undo the test's changes to the process-wide random sources."""
    # Faker.seed() sets these class attributes; monkeypatch restores them on teardown.
    monkeypatch.setattr(Generator, "_is_seeded", Generator._is_seeded)
    monkeypatch.setattr(Generator, "_global_seed", Generator._global_seed)
    faker_state = faker.generator.random.getstate()
    stdlib_state = random.getstate()
    yield
    faker.generator.random.setstate(faker_state)
    random.setstate(stdlib_state)


@pytest.mark.req("20.3")
def test_assigned_random_is_the_only_source_of_name_and_company_draws() -> None:
    # Without an assignment, every generator uses the module-level Random.
    assert Faker("zh_CN").random is faker.generator.random
    shared_state = faker.generator.random.getstate()
    for make in (seeded_faker, seeded_faker_typed):
        fake, rng = make(SEED)
        assert fake.random is rng
        # One generator behind the proxy, shared by every provider.
        assert len(fake.factories) == 1
        generator: Any = fake.factories[0]
        providers = generator.get_providers()
        assert len(providers) > 1
        assert all(provider.generator is generator for provider in providers)
        # The methods and the tokens their formats expand to come from the zh_CN providers.
        for method, module in (
            ("name", PERSON_ZH_CN),
            ("last_name", PERSON_ZH_CN),
            ("first_name", PERSON_ZH_CN),
            ("company", COMPANY_ZH_CN),
            ("company_prefix", COMPANY_ZH_CN),
            ("company_suffix", COMPANY_ZH_CN),
        ):
            assert type(getattr(generator, method).__self__).__module__ == module
        assert PersonProvider.formats == ["{{last_name}}{{first_name}}"]
        assert CompanyProvider.formats == ["{{company_prefix}}{{company_suffix}}"]
        # Every call advances the assigned Random ...
        for _ in range(STEPS):
            for draw in (fake.name, fake.company, fake.company_prefix):
                before = rng.getstate()
                draw()
                assert rng.getstate() != before
    # ... and none touches the module-level one.
    assert faker.generator.random.getstate() == shared_state


@pytest.mark.req("20.3")
def test_equal_seeds_give_equal_name_and_company_sequences(restore_shared_randoms: None) -> None:
    expected = sample(SEED)
    names, companies = expected[0::4], expected[1::4]
    assert all(isinstance(name, str) and 2 <= len(name) <= 4 for name in names)
    assert all(isinstance(company, str) and company.endswith("有限公司") for company in companies)
    assert len(set(names)) > STEPS // 2
    assert sample(SEED + 1) != expected

    # Two live instances, one per way of assigning the Random, with interleaved calls;
    # between their draws other code drives every process-wide random source.
    first, first_rng = seeded_faker(SEED)
    second, second_rng = seeded_faker_typed(SEED)
    bystander = Faker("zh_CN")
    got_first: list[Draw] = []
    got_second: list[Draw] = []
    for i in range(STEPS):
        got_first.extend(step(first, first_rng))
        Faker.seed(i)
        random.seed(i)
        bystander.name()
        bystander.unique.company_prefix()
        bystander.unique.clear()
        got_second.extend(step(second, second_rng))
    assert got_first == expected
    assert got_second == expected


# Prints the str hash of this interpreter and sample(argv[1]) as ASCII JSON.
SUBPROCESS_SAMPLE = (
    "import json, sys\n"
    "from tests.probes.test_faker import sample\n"
    "json.dump({'hash': hash('erp-mock'), 'values': sample(int(sys.argv[1]))}, sys.stdout)\n"
)


@pytest.mark.req("20.3", "24.9")
def test_sequences_do_not_depend_on_pythonhashseed() -> None:
    runs: list[dict[str, Any]] = []
    for hash_seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(REPO_ROOT)}
        completed = subprocess.run(
            [sys.executable, "-c", SUBPROCESS_SAMPLE, str(SEED)],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        runs.append(json.loads(completed.stdout))
    # The two interpreters hash strings differently ...
    assert runs[0]["hash"] != runs[1]["hash"]
    # ... and draw the same sequence as this process.
    assert runs[0]["values"] == runs[1]["values"] == sample(SEED)


@pytest.mark.req("20.3")
def test_seed_instance_reseeds_the_assigned_random_in_place() -> None:
    fake, rng = seeded_faker(SEED)
    fake.name()
    fake.seed_instance(SEED + 1)
    # No new Random: the sequence shared with the built-in generators restarts.
    assert fake.random is rng
    assert rng.getstate() == random.Random(SEED + 1).getstate()


@pytest.mark.req("20.3")
def test_unique_proxy_keeps_seen_values_per_instance() -> None:
    plain, _ = seeded_faker(SEED)
    names = [plain.name() for _ in range(UNIQUE_DRAWS * 2)]
    reference, _ = seeded_faker(SEED)
    expected = [reference.unique.name() for _ in range(UNIQUE_DRAWS)]
    # unique retries plain calls on the same Random: it skips repeats of the plain sequence.
    assert expected == list(dict.fromkeys(names))[:UNIQUE_DRAWS]

    fake, _ = seeded_faker(SEED)
    first_build = [fake.unique.name() for _ in range(UNIQUE_DRAWS)]
    # Another instance's seen values do not leak in ...
    assert first_build == expected
    # ... but the instance's own survive a new Random, so a reused instance diverges.
    fake.random = random.Random(SEED)  # type: ignore[attr-defined]
    second_build = [fake.unique.name() for _ in range(UNIQUE_DRAWS)]
    assert set(second_build).isdisjoint(first_build)


@pytest.mark.req("20.3")
def test_passport_and_binary_bypass_the_assigned_random(restore_shared_randoms: None) -> None:
    fake, rng = seeded_faker(SEED)
    other, other_rng = seeded_faker(SEED)
    generator: Any = fake.factories[0]
    assert type(generator.passport_gender.__self__).__module__ == "faker.providers.passport.en_US"
    assigned_state = rng.getstate()

    # passport_gender() draws from the stdlib's global random.
    stdlib_state = random.getstate()
    fake.passport_gender()
    assert random.getstate() != stdlib_state
    assert rng.getstate() == assigned_state

    # binary() reads os.urandom: equal seeds give different bytes, and no Random moves.
    shared_state = faker.generator.random.getstate()
    stdlib_state = random.getstate()
    assert fake.binary(32) != other.binary(32)
    assert rng.getstate() == other_rng.getstate() == assigned_state
    assert faker.generator.random.getstate() == shared_state
    assert random.getstate() == stdlib_state


@pytest.mark.req("20.3")
def test_word_lists_behind_name_and_company() -> None:
    assert isinstance(PersonProvider.last_names, OrderedDict)
    assert len(PersonProvider.last_names) == 399
    assert len(PersonProvider.first_names) == len(set(PersonProvider.first_names)) == 131
    prefixes = CompanyProvider.company_prefixes
    suffixes = CompanyProvider.company_suffixes
    assert (len(prefixes), len(set(prefixes))) == (83, 81)
    assert suffixes == [kind + "有限公司" for kind in ("科技", "网络", "信息", "传媒")]

    fake, _ = seeded_faker(SEED)
    companies = {fake.company() for _ in range(5000)}
    assert companies == {prefix + suffix for prefix in prefixes for suffix in suffixes}
    assert len(companies) == 324
