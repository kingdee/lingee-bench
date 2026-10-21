"""The seven built-in value generators of a dataset build, and the company name blacklist.

A business pack binds a generator to an entity field in ``seed/generators.yaml``
(``supplier.name: { gen: company_name }``); the Seed_Builder draws a value for every bound
field of a ``bulk`` record and for every field a dataset writes as ``{ gen: ... }``
(requirements 20.3 and 20.4). The parameters of each generator are the Pydantic models of
:mod:`erp_mock.config.pack` (:data:`~erp_mock.config.pack.GENERATORS` names all seven), so
a malformed parameter is already reported by loading step 4; :func:`param_problem` is the
cross-parameter check left over, which the dataset resolve pass reports as an invalid
generator parameter (requirement 20.6).

One sequence for the whole build. :class:`ValueGenerators` holds one
``random.Random(random_seed)`` and one ``Faker("zh_CN")`` whose ``random`` is that same
Random, so Faker's draws and the built-in draws come from a single pseudo random sequence
and nothing else (requirement 20.3, design V10, tests/probes/test_faker.py). V10 holds on
the locked Faker 40.43.0: assigning ``fake.random`` stores the Random on the one generator
behind the proxy, every provider draws through it, and the zh_CN word lists do not depend
on PYTHONHASHSEED, so the fallback (taking only the word lists) is not needed. Faker's
``proxy.pyi`` omits the ``random`` property, hence the one ``type: ignore`` below. The
probe also fixes how this module uses Faker: one instance per build, the Random assigned
before the first draw, no ``Faker.seed()``, ``seed_instance()`` or ``seed_locale()``, and
plain ``name()``, ``company()`` and ``company_prefix()`` calls rather than ``fake.unique``,
whose seen values ignore both the blacklist and the per-entity scope.

Discarding (requirement 20.7). A drawn string that contains any entry of
:data:`BLACKLIST`, the real company names of ``blacklist.txt`` next to this module, and a
drawn ``name`` that another record of the same entity already has, are discarded, and the
next value comes from the same sequence; a discarded draw therefore advances the sequence.
After :data:`DISCARD_LIMIT` discards in a row for one field the build fails with
:class:`DiscardLimitExceeded`, which the builder turns into its own error with the dataset
file and the config path of the record. The names a dataset writes out in full are not
drawn here, so the builder passes them to :meth:`ValueGenerators.add_name` to have them
count as taken.

What the generators draw:

- ``company_name``: ``<商号><行业><公司形式>``. Without ``industry`` it is Faker's
  ``company()``, 324 distinct values (81 distinct 商号 times 4 trade words, each ending in
  有限公司); with it, Faker's ``company_prefix()``, the given industry word and one of
  :data:`COMPANY_FORMS`. The shipped blacklist rules out part of both, so one entity holds
  a couple of hundred generated company names before the discard limit stops the build; an
  entity that needs more background records needs ``code_words`` instead.
- ``short_name``: :func:`short_name` of the name the same record already has, no draw.
- ``person_name``: Faker's ``name()``, one of 399 weighted surnames and one of 131 given
  names.
- ``choice``: one of the written ``values``, which may be strings, integers or booleans.
- ``int_range``: an integer in ``[min, max]``.
- ``decimal_range``: a Decimal in ``[min, max]``, a whole number of steps of the last
  decimal place the two bounds are written with (``"0.70"`` to ``"0.95"`` draws 0.70,
  0.71, ... 0.95). The resolve pass checks those places against the precision of the
  target field (requirement 4.7).
- ``code_words``: :data:`MIN_CODE_WORDS` to :data:`MAX_CODE_WORDS` of the written
  ``words``, each word at most once, joined in the order the pack writes them.
"""

import random
from collections.abc import Sequence
from decimal import Decimal
from importlib import resources
from typing import Final, assert_never

from faker import Faker

from erp_mock.common.decimals import WIDE, to_decimal
from erp_mock.config.pack import (
    ChoiceGenerator,
    CodeWordsGenerator,
    CompanyNameGenerator,
    DecimalRangeGenerator,
    GeneratorObject,
    IntRangeGenerator,
    PersonNameGenerator,
    ShortNameGenerator,
)

__all__ = [
    "BLACKLIST",
    "BLACKLIST_FILE",
    "COMPANY_FORMS",
    "DISCARD_LIMIT",
    "LOCALE",
    "MAX_CODE_WORDS",
    "MIN_CODE_WORDS",
    "NAME_FIELD",
    "SHORT_NAME_MAX",
    "DiscardLimitExceeded",
    "GeneratedValue",
    "ValueGenerators",
    "blacklist_hits",
    "param_problem",
    "short_name",
]

# What a generator draws: a Decimal for decimal_range, an int for int_range, and for
# choice whatever the pack wrote (:data:`~erp_mock.config.fields.ScalarDefault`).
type GeneratedValue = str | int | bool | Decimal

# The only Faker locale the framework uses; with several, Faker's ``random`` property
# raises NotImplementedError (tests/probes/test_faker.py).
LOCALE: Final = "zh_CN"

# The file that holds the real company names, next to this module in the installed package.
BLACKLIST_FILE: Final = "blacklist.txt"

# The field whose generated value must differ from every record of the same entity
# (requirement 20.7); other fields may repeat a value.
NAME_FIELD: Final = "name"

# Discards in a row for one field that end the build (requirement 20.7).
DISCARD_LIMIT: Final = 1000

# The corporate forms ``company_name`` writes after an industry word.
COMPANY_FORMS: Final = ("有限公司", "有限责任公司", "股份有限公司")

# How many words ``code_words`` joins, both included.
MIN_CODE_WORDS: Final = 2
MAX_CODE_WORDS: Final = 3

# Code points a derived short name keeps at most.
SHORT_NAME_MAX: Final = 6

# Trailing parts :func:`short_name` drops, each group longest first: the corporate form,
# then one trade word (the words Faker's zh_CN company suffixes are built from).
_FORM_SUFFIXES: Final = ("股份有限公司", "有限责任公司", "有限公司", "公司")
_TRADE_SUFFIXES: Final = ("科技", "网络", "信息", "传媒")

# Why one drawn value was discarded (requirement 20.7).
_BLACKLISTED: Final = "含真实企业名称黑名单词条 {entry}"
_DUPLICATE_NAME: Final = "与同一实体已有记录的 name 相同"

# Why a generator cannot draw at all; the resolve pass reports these as an invalid
# generator parameter (requirement 20.6).
_NOT_ORDERED: Final = "min {min} 大于 max {max}"
_NOT_DECIMAL: Final = "{key} {value} 不是小数字符串"
_NO_RECORD_NAME: Final = "short_name 需要同一记录的 name"


def _read_blacklist() -> tuple[str, ...]:
    """The entries of :data:`BLACKLIST_FILE`, in the order the file writes them.

    One entry per line; a blank line and a line that starts with ``#`` is a comment.
    Surrounding whitespace is stripped, nothing else: an entry is matched as it is written.
    """
    text = resources.files("erp_mock.seed").joinpath(BLACKLIST_FILE).read_text("utf-8")
    lines = (line.strip() for line in text.splitlines())
    return tuple(line for line in lines if line and not line.startswith("#"))


# The real company names the framework maintains, as the file next to this module lists
# them (requirement 20.7). No generated string value may contain any of them.
BLACKLIST: Final[tuple[str, ...]] = _read_blacklist()


def blacklist_hits(text: str, entries: Sequence[str] = BLACKLIST) -> tuple[str, ...]:
    """The entries of ``entries`` that occur in ``text``, in the order of ``entries``.

    An entry occurs when it is a substring of the text, compared as both are written
    (requirement 20.7). The entry is returned as ``entries`` writes it, so an error names
    the entry of the list and not the text that matched it.
    """
    return tuple(entry for entry in entries if entry in text)


def short_name(name: str) -> str:
    """The short name derived from ``name``: its 商号 part, at most SHORT_NAME_MAX long.

    Drops one trailing corporate form (有限公司 and the like) and then one trailing trade
    word (科技, 网络, 信息, 传媒), each only when something is left over, and keeps the
    first :data:`SHORT_NAME_MAX` code points of the rest: 菊风机械有限公司 gives 菊风机械
    and 菊风公司网络有限公司 gives 菊风公司. Derived, not drawn, so it takes no value from
    the build's random sequence; any blacklist entry it contains is one ``name`` contains
    too, and a name that reaches this point was not discarded.
    """
    stem = name
    for group in (_FORM_SUFFIXES, _TRADE_SUFFIXES):
        for suffix in group:
            if stem.endswith(suffix) and len(stem) > len(suffix):
                stem = stem[: -len(suffix)]
                break
    return stem[:SHORT_NAME_MAX].strip()


def param_problem(decl: GeneratorObject) -> str | None:
    """Why ``decl`` cannot draw a value, or None when its parameters are usable.

    The parameter models check each parameter on its own; what is left are the relations
    between two of them. ``int_range`` and ``decimal_range`` need ``min`` at most ``max``,
    and the bounds of ``decimal_range``, non-empty text in the model, need to be decimal
    strings (:func:`~erp_mock.common.decimals.to_decimal`). The resolve pass reports the
    returned reason as an invalid generator parameter (requirement 20.6);
    :meth:`ValueGenerators.value` raises ValueError when it is handed a declaration that
    did not pass this check.
    """
    match decl:
        case IntRangeGenerator():
            if decl.min > decl.max:
                return _NOT_ORDERED.format(min=decl.min, max=decl.max)
        case DecimalRangeGenerator():
            for key, text in (("min", decl.min), ("max", decl.max)):
                try:
                    to_decimal(text)
                except ValueError:
                    return _NOT_DECIMAL.format(key=key, value=text)
            if to_decimal(decl.min) > to_decimal(decl.max):
                return _NOT_ORDERED.format(min=decl.min, max=decl.max)
        case _:
            return None
    return None


class DiscardLimitExceeded(Exception):
    """:data:`DISCARD_LIMIT` discards in a row for one field (requirement 20.7).

    Fields: the ``entity`` and ``field`` that was being filled in, the ``value`` discarded
    last and the ``cause`` that discarded it. ``str(...)`` is the one-line reason, which
    the builder reports together with the dataset file and the config path of the record.
    """

    __slots__ = ("entity", "field", "value", "cause")

    def __init__(self, entity: str, field: str, value: GeneratedValue, cause: str) -> None:
        super().__init__(entity, field, value, cause)
        self.entity = entity
        self.field = field
        self.value = value
        self.cause = cause

    def __str__(self) -> str:
        return (
            f"{self.entity}.{self.field} 连续 {DISCARD_LIMIT} 次生成的值都被丢弃，"
            f"最后一个是 {self.value}：{self.cause}"
        )


class ValueGenerators:
    """The built-in generators of one build, drawing from one pseudo random sequence.

    One instance per build, created with the dataset's ``random_seed`` before the first
    draw. :meth:`value` draws for one field of one record, discarding what requirement
    20.7 discards, and :meth:`add_name` registers a name the dataset wrote out in full so
    that later draws do not repeat it. ``blacklist`` is the shipped :data:`BLACKLIST`
    unless a caller passes its own list.
    """

    __slots__ = ("_rng", "_fake", "_blacklist", "_names")

    def __init__(self, random_seed: int, blacklist: Sequence[str] = BLACKLIST) -> None:
        self._rng = random.Random(random_seed)
        self._fake = Faker(LOCALE)
        # The one sequence behind Faker and the built-in generators (design V10).
        self._fake.random = self._rng  # type: ignore[attr-defined]
        self._blacklist = tuple(blacklist)
        self._names: dict[str, set[str]] = {}

    @property
    def rng(self) -> random.Random:
        """The build's random sequence, the one Faker draws from as well."""
        return self._rng

    def add_name(self, entity: str, name: str) -> None:
        """Count ``name`` as taken in ``entity``, so no drawn name repeats it."""
        self._names.setdefault(entity, set()).add(name)

    def taken_names(self, entity: str) -> frozenset[str]:
        """The names of ``entity`` that are taken, drawn or registered."""
        return frozenset(self._names.get(entity, ()))

    def value(
        self, decl: GeneratorObject, *, entity: str, field: str, name: str | None = None
    ) -> GeneratedValue:
        """Draw the value of ``entity``.``field``, discarding by requirement 20.7.

        ``name`` is the name the record already has, which ``short_name`` derives from.
        A drawn :data:`NAME_FIELD` value is registered as taken, as if :meth:`add_name`
        had been called with it.

        Raises :class:`DiscardLimitExceeded` after :data:`DISCARD_LIMIT` discards in a
        row, and ValueError when ``decl`` has a :func:`param_problem` or when
        ``short_name`` is asked for without a ``name``.
        """
        problem = param_problem(decl)
        if problem is not None:
            raise ValueError(f"{entity}.{field}: {problem}")
        taken = self._names.setdefault(entity, set())
        # DISCARD_LIMIT is positive, so the loop sets both before the raise below.
        cause = ""
        drawn: GeneratedValue = ""
        for _ in range(DISCARD_LIMIT):
            drawn = self._draw(decl, name)
            found = self._discard_cause(drawn, field, taken)
            if found is None:
                if field == NAME_FIELD and isinstance(drawn, str):
                    taken.add(drawn)
                return drawn
            cause = found
        raise DiscardLimitExceeded(entity, field, drawn, cause)

    def _discard_cause(self, drawn: GeneratedValue, field: str, taken: set[str]) -> str | None:
        """Why ``drawn`` is discarded, or None when it is kept (requirement 20.7)."""
        if not isinstance(drawn, str):
            return None
        hits = blacklist_hits(drawn, self._blacklist)
        if hits:
            return _BLACKLISTED.format(entry=hits[0])
        if field == NAME_FIELD and drawn in taken:
            return _DUPLICATE_NAME
        return None

    def _draw(self, decl: GeneratorObject, name: str | None) -> GeneratedValue:
        """One value from the build's sequence, before the discard checks."""
        match decl:
            case CompanyNameGenerator():
                return self._company_name(decl.industry)
            case ShortNameGenerator():
                if name is None:
                    raise ValueError(_NO_RECORD_NAME)
                return short_name(name)
            case PersonNameGenerator():
                return self._fake.name()
            case ChoiceGenerator():
                return self._rng.choice(decl.values)
            case IntRangeGenerator():
                return self._rng.randint(decl.min, decl.max)
            case DecimalRangeGenerator():
                return self._decimal_range(decl.min, decl.max)
            case CodeWordsGenerator():
                return self._code_words(decl.words)
            case _:
                assert_never(decl)

    def _company_name(self, industry: str | None) -> str:
        """Faker's ``company()``, or the 商号 with ``industry`` and a corporate form."""
        if industry is None:
            return self._fake.company()
        return self._fake.company_prefix() + industry + self._rng.choice(COMPANY_FORMS)

    def _decimal_range(self, low: str, high: str) -> Decimal:
        """A Decimal in ``[low, high]``, a whole number of steps of their last place."""
        places = max(_written_places(low), _written_places(high))
        units = self._rng.randint(
            int(to_decimal(low).scaleb(places, context=WIDE)),
            int(to_decimal(high).scaleb(places, context=WIDE)),
        )
        # Exact both ways: scaleb only moves the decimal point, and WIDE keeps 80 digits.
        return Decimal(units).scaleb(-places, context=WIDE)

    def _code_words(self, words: Sequence[str]) -> str:
        """``MIN_CODE_WORDS`` to ``MAX_CODE_WORDS`` of ``words``, in the written order."""
        count = min(len(words), self._rng.randint(MIN_CODE_WORDS, MAX_CODE_WORDS))
        picked = sorted(self._rng.sample(range(len(words)), count))
        return "".join(words[index] for index in picked)


def _written_places(text: str) -> int:
    """Number of written decimals of a decimal string."""
    return len(text.partition(".")[2])
