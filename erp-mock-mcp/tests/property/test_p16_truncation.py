"""Property 16: result truncation.

**Validates: Requirements 13.5**

For any result object whose detail array takes its text over 16384 bytes, the text that
comes back is within the limit, the result carries ``truncated`` true as its last key, the
elements kept are a prefix of the original array that one more element would take over the
limit, and every other field keeps the value and the place it had; a result within the limit
comes back as it is, without ``truncated``.

The reference model is requirement 13.5 read literally (:func:`removed_from_the_end`): while
the text does not fit, one element comes off the end of the detail array and the text is
measured again with ``truncated`` added, until it fits or the array is empty.
:func:`~erp_mock.tools.results.truncate_result` reaches a prefix by binary search over the
number of elements kept instead, so what the property compares is the two: a scan that
removes one element at a time against a search that assumes the size of a prefix grows with
every element kept. Two results the model and the implementation both leave alone although
their text is too long, there being nothing to remove: one with no detail array, and one
whose array is already empty. A result whose other fields alone exceed the limit keeps the
empty array and still says it was truncated, so the test asks for a text within the limit
only while something is left to remove.

What is drawn, per example (:func:`shapes`):

- the fields of the result next to its detail array, up to four of them, each before or
  after the array, so the array is the first, the last or a middle key and truncation has to
  keep the order of the rest. Which value a field takes does not follow its name: what
  truncation does with a field does not depend on what the field means, so every one of them
  draws from the same pool of the values requirement 13.4 lets a result carry - a text, an
  integer, a boolean, null and a nested object;
- the detail array: one of the four keys a result holds one under, and up to a few hundred
  elements, each a line record whose remark is filled with a repeated character. The fillers
  come from a short cycle rather than one per element, so the elements differ in size and the
  size of a prefix does not grow by a fixed amount per element kept. Every element carries
  its own index, so a prefix is the only sub-array that compares equal to one;
- the size of the text, which a field of ASCII padding tunes to the byte: exactly the limit,
  one byte over it, a few bytes either side, far over it, and anywhere below. The padding is
  left out of one result in several, which leaves the size the one its elements make - how a
  result whose array alone goes over the limit, and one whose single element does, come up.

Text is drawn from pieces that are one, two, three, four and six bytes as the text of a
result writes them - ASCII, the two characters json escapes, control characters, the three
line terminators :mod:`erp_mock.common.jsonio` escapes itself, Chinese characters and a
character outside the BMP - so the number of characters of a result and the number of bytes
it is written as differ, and what the limit of requirement 13.5 counts is the bytes.

100 examples, the count every property but 3 and 17 runs (tests/conftest.py).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

import pytest
from hypothesis import example, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.jsonio import JsonValue, dumps_compact, encode_utf8
from erp_mock.tools.results import TEXT_LIMIT, truncate_result

# The key a truncated result carries, written out here rather than imported, so the module
# under test is compared with the specification and not with itself (requirement 13.5).
TRUNCATED: Final = "truncated"

# The keys a result holds its detail array under: the items of a list result and of the stock
# balance, the lines of a bill, the history of a declarative query and the conversion records
# of a material (requirements 5.11, 5.13, 6.9 and 13.5).
DETAIL_KEYS: Final[tuple[str, ...]] = ("items", "lines", "history", "unit_conversions")

# The keys a result carries next to its detail array: the four of a list result besides its
# items (requirement 5.11) and a few fields of a bill header, whose values a truncated result
# keeps as they were - the total and the header amounts included.
FIELD_KEYS: Final[tuple[str, ...]] = (
    "id",
    "number",
    "total",
    "page",
    "page_size",
    "has_more",
    "amount_with_tax",
    "biz_date",
    "vendor",
)

# The last field of a drawn result, which carries the ASCII padding that tunes the text to an
# exact size. One ASCII character is one byte of the text and json escapes none of them, so
# the size grows by exactly one byte per character of padding.
PAD_KEY: Final = "remark"

# The pieces a drawn text is built from, and how many bytes the text of a result writes each
# one as: 1 for an ASCII character, 2 for the escape of ``"`` and ``\``, 6 for the escape of a
# control character and of the three line terminators jsonio escapes, 3 for a Chinese
# character and 4 for one outside the BMP.
TEXT_PIECES: Final[tuple[str, ...]] = (
    "",
    "SPO202610090001",
    '"',
    "\\",
    "\n\t",
    "\x00\x1f",
    "\x85",
    "\u2028",
    "\u2029",
    "夹具主料仓",
    "🧲",
)

# The characters an element's remark is filled with: one, three and four UTF-8 bytes, none of
# which json escapes.
FILLERS: Final[tuple[str, ...]] = ("p", "夹", "🧲")
FILLER_BYTES: Final = max(len(encode_utf8(char)) for char in FILLERS)

# How large the text of a drawn result may get: enough that most of a long detail array has
# to go, small enough that removing the elements one at a time stays quick.
BUDGET: Final = 2 * TEXT_LIMIT


# --- the reference model: removing elements from the end one by one ----------------------


def size_of(value: JsonValue) -> int:
    """The size of the text of ``value`` in UTF-8 bytes, as it is written out (req 13.5)."""
    return len(encode_utf8(dumps_compact(value)))


def as_array(value: JsonValue) -> Sequence[JsonValue]:
    """``value`` as the array it is; the detail array of a drawn result is always one."""
    assert isinstance(value, list), f"not an array: {value!r}"
    return value


def with_prefix(structured: Mapping[str, JsonValue], key: str, count: int) -> dict[str, JsonValue]:
    """``structured`` with the first ``count`` elements of its detail array, and truncated.

    The keys keep the order they had and ``truncated`` comes last, which is the place every
    outputSchema declares it in (requirement 11.7).
    """
    items = as_array(structured[key])
    kept: dict[str, JsonValue] = {
        name: list(items[:count]) if name == key else value for name, value in structured.items()
    }
    kept[TRUNCATED] = True
    return kept


def removed_from_the_end(
    structured: Mapping[str, JsonValue], key: str | None
) -> dict[str, JsonValue]:
    """``structured`` truncated the way requirement 13.5 words it (the reference model).

    A result whose text fits as it is comes back untouched. One that does not loses one
    element off the end of its detail array - the one ``key`` names, None for a result that
    has none - and is measured again with ``truncated`` added, until the text fits or the
    array is empty. A result with nothing to remove comes back untouched as well, however
    long its text is.
    """
    if size_of(structured) <= TEXT_LIMIT or key is None:
        return dict(structured)
    count = len(as_array(structured[key]))
    if count == 0:
        return dict(structured)
    while count > 0:
        count -= 1
        if size_of(with_prefix(structured, key, count)) <= TEXT_LIMIT:
            break
    return with_prefix(structured, key, count)


def others(structured: Mapping[str, JsonValue], key: str | None) -> list[tuple[str, JsonValue]]:
    """The fields of a result next to its detail array, in order: what truncation keeps."""
    return [(name, value) for name, value in structured.items() if name not in (key, TRUNCATED)]


# --- the result object one example works on ----------------------------------------------


def line(index: int, filler: str, length: int) -> dict[str, JsonValue]:
    """One element of a detail array: the record of a line, ``length`` fillers of remark.

    Every element carries its index, in its id, its line number and its quantity, so no two
    elements of an array are equal and a prefix of it is the only sub-array that compares
    equal to one.
    """
    return {
        "id": f"r{index:06d}",
        "line_no": index + 1,
        "material": {"id": f"mat_{index % 7:06d}", "code": f"M{index % 7:04d}"},
        "qty": f"{index + 1}",
        PAD_KEY: filler * length,
    }


def lines(count: int, filler: str = "p", length: int = 0) -> tuple[JsonValue, ...]:
    """``count`` elements of a detail array, each with ``length`` fillers of remark."""
    return tuple(line(index, filler, length) for index in range(count))


# How many bytes one element takes with an empty remark, and how many elements an array can
# hold within :data:`BUDGET` once every remark is empty.
LINE_BYTES: Final = size_of(line(0, "p", 0))
MAX_ELEMENTS: Final = BUDGET // LINE_BYTES


@dataclass(frozen=True, slots=True)
class Shape:
    """What one example builds its result object from.

    ``head`` are the fields before the detail array and ``tail`` those after it, each one a
    key of the result and the value it carries; ``key`` names the detail array and is None
    for a result that has none, and ``elements`` are the elements of that array. ``pad`` is
    how many ASCII characters the last field of the result holds, which tunes the text to an
    exact size, and None for a result that carries no such field at all.
    """

    head: tuple[tuple[str, JsonValue], ...] = ()
    key: str | None = None
    elements: tuple[JsonValue, ...] = ()
    tail: tuple[tuple[str, JsonValue], ...] = ()
    pad: int | None = None


def build(shape: Shape) -> dict[str, JsonValue]:
    """The result object of ``shape``: its fields in order, the detail array among them."""
    found: dict[str, JsonValue] = dict(shape.head)
    if shape.key is not None:
        found[shape.key] = list(shape.elements)
    found.update(shape.tail)
    if shape.pad is not None:
        found[PAD_KEY] = "p" * shape.pad
    return found


def tuned(shape: Shape, size: int) -> Shape:
    """``shape`` with the padding that makes the text of its result exactly ``size`` bytes."""
    base = size_of(build(replace(shape, pad=0)))
    assert size >= base, f"{size} bytes is less than the {base} bytes of the result itself"
    return replace(shape, pad=size - base)


# --- what is drawn -----------------------------------------------------------------------


def texts(pieces: int = 3) -> st.SearchStrategy[str]:
    """A text of up to ``pieces`` of :data:`TEXT_PIECES`, joined."""
    return st.lists(st.sampled_from(TEXT_PIECES), max_size=pieces).map("".join)


def values() -> st.SearchStrategy[JsonValue]:
    """One value of a result field: what requirement 13.4 lets a result carry.

    A text, an integer, a boolean and null, which is what every scalar field of a result
    holds - a decimal and a date among them, both of which are texts - and the nested object
    of a reference (requirement 13.3). No value is an array: the detail array is the one
    array of a result object.
    """
    scalars: st.SearchStrategy[JsonValue] = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**6), max_value=10**6),
        texts(),
    )
    nested: st.SearchStrategy[JsonValue] = st.fixed_dictionaries(
        {"id": texts(1), "code": st.one_of(st.none(), texts(1))}
    )
    return st.one_of(scalars, nested)


def room_for(count: int) -> int:
    """How many fillers one remark may hold with ``count`` elements in the array.

    A filler takes at most :data:`FILLER_BYTES` bytes, so an array of ``count`` elements
    whose remarks are this long stays within :data:`BUDGET` however wide the characters
    drawn for them turn out to be.
    """
    if count == 0:
        return 0
    return max(0, BUDGET // count - LINE_BYTES) // FILLER_BYTES


def counts() -> st.SearchStrategy[int]:
    """How many elements a drawn detail array holds.

    Up to a few, up to a few dozen and up to as many as the budget holds: the last range is
    wide enough that the number of elements kept is far from both ends of it, which is where
    a binary search over that number is most likely to be off by one.
    """
    return st.one_of(
        st.integers(min_value=0, max_value=4),
        st.integers(min_value=0, max_value=40),
        st.integers(min_value=0, max_value=MAX_ELEMENTS),
        st.just(MAX_ELEMENTS),
    )


def lengths(room: int) -> st.SearchStrategy[int]:
    """How many fillers one element's remark holds, with ``room`` of them allowed.

    Hypothesis draws an integer near the low end of its range, which would make every
    element short and every array small; as many fillers as there is room for, and half of
    that, are therefore drawn as well, so an array whose elements alone take the result over
    the limit - a single long element among them - comes up often.
    """
    return st.one_of(st.integers(min_value=0, max_value=room), st.just(room), st.just(room // 2))


def targets() -> st.SearchStrategy[int]:
    """The size a drawn result's text is tuned to.

    The sizes straddle the limit: exactly at it, one byte over it, a few bytes either side,
    far over it, and anywhere below. The largest size is drawn on its own as well, where the
    padding alone is over the limit and no prefix of the array can fit.
    """
    return st.one_of(
        st.just(TEXT_LIMIT),
        st.just(TEXT_LIMIT + 1),
        st.integers(min_value=TEXT_LIMIT - 8, max_value=TEXT_LIMIT + 8),
        st.integers(min_value=TEXT_LIMIT + 1, max_value=BUDGET),
        st.just(BUDGET),
        st.integers(min_value=0, max_value=TEXT_LIMIT),
    )


# One result in five carries no detail array at all - a get, a write or a state operation
# returns a record whose every value is a scalar or a nested object - and one in four no
# padding field, which leaves the detail array as the last key of the result and the size of
# its text the one its elements make.
ARRAYS: Final = st.sampled_from((True, True, True, True, False))
PADDED: Final = st.sampled_from((True, True, True, False))


@st.composite
def shapes(draw: st.DrawFn) -> Shape:
    """One result object to truncate: its fields, its detail array and the size of its text."""
    names = draw(st.lists(st.sampled_from(FIELD_KEYS), unique=True, max_size=4))
    split = draw(st.integers(min_value=0, max_value=len(names)))
    key = draw(st.sampled_from(DETAIL_KEYS)) if draw(ARRAYS) else None
    count = 0 if key is None else draw(counts())
    room = room_for(count)
    fillers = draw(
        st.lists(st.tuples(st.sampled_from(FILLERS), lengths(room)), min_size=1, max_size=4)
    )
    shape = Shape(
        head=tuple((name, draw(values())) for name in names[:split]),
        key=key,
        elements=tuple(line(index, *fillers[index % len(fillers)]) for index in range(count)),
        tail=tuple((name, draw(values())) for name in names[split:]),
    )
    if not draw(PADDED):
        return shape
    # A result whose elements alone are larger than the size drawn carries no padding at all.
    return tuned(shape, max(draw(targets()), size_of(build(replace(shape, pad=0)))))


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 16: 结果截断
@pytest.mark.req("13.5")
@settings(max_examples=100)
@given(shape=shapes())
# A text of exactly 16384 bytes, which is within the limit, and one of 16385, which is not.
@example(shape=tuned(Shape(key="items", elements=lines(4)), TEXT_LIMIT))
@example(shape=tuned(Shape(key="items", elements=lines(4)), TEXT_LIMIT + 1))
# Nothing to remove: no detail array at all, and an array that is already empty.
@example(shape=tuned(Shape(head=(("id", "so_000001"),)), TEXT_LIMIT + 1))
@example(shape=tuned(Shape(key="lines", elements=()), TEXT_LIMIT + 1))
# One element that is over the limit on its own: the array goes empty and the result says it
# was truncated, although its text is still too long.
@example(shape=Shape(key="lines", elements=lines(1, length=TEXT_LIMIT)))
# Fields that are over the limit on their own, next to an array of elements that fit.
@example(shape=tuned(Shape(key="items", elements=lines(5), tail=(("total", 5),)), BUDGET))
# An array of elements whose characters are three and four UTF-8 bytes: what the limit counts
# is the bytes of the text and not its characters.
@example(shape=Shape(key="history", elements=lines(60, filler="夹", length=100)))
@example(shape=Shape(key="history", elements=lines(60, filler="🧲", length=100)))
# A field whose text json escapes: two bytes for a quote, six for a line terminator.
@example(
    shape=tuned(
        Shape(head=(("number", '"\\\n\x85\u2028\u2029"'),), key="items", elements=lines(8)),
        TEXT_LIMIT + 1,
    )
)
def test_a_result_is_cut_to_the_longest_prefix_of_its_detail_array(shape: Shape) -> None:
    built = build(shape)
    key = shape.key
    items = as_array(built[key]) if key is not None else ()
    before = dumps_compact(built)
    expected = removed_from_the_end(built, key)

    structured, text = truncate_result(built)

    kept = as_array(structured[key]) if key is not None else ()
    note(
        f"{len(items)} elements, {size_of(built)} bytes, {len(kept)} kept, "
        f"{size_of(structured)} bytes, truncated: {TRUNCATED in structured}"
    )
    # The prefix the binary search finds is the one removing elements one by one arrives at,
    # and the text is the text of exactly the object that comes back with it.
    assert list(structured.items()) == list(expected.items())
    assert text == dumps_compact(structured)
    # The result that went in is untouched, so what is compared with it below is the value it
    # carried before the call.
    assert dumps_compact(built) == before

    if TRUNCATED in structured:
        # Only a result that has a detail array is truncated at all.
        assert key is not None
        # The text is within the limit unless the array is empty, in which case there is
        # nothing left to remove; truncated is true and comes last.
        assert size_of(structured) <= TEXT_LIMIT or len(kept) == 0
        assert structured[TRUNCATED] is True
        assert list(structured)[-1] == TRUNCATED
        # What is kept is a prefix of the original array, it is shorter than the array, and
        # one more element of it would take the text over the limit.
        assert list(kept) == list(items[: len(kept)])
        assert len(kept) < len(items)
        assert size_of(with_prefix(built, key, len(kept) + 1)) > TEXT_LIMIT
        # Every other field keeps the value and the place it had (requirement 13.5).
        assert others(structured, key) == others(built, key)
    else:
        # An untruncated result is the one that went in, and it is untruncated because its
        # text fits or because there is nothing to remove: no array, or an empty one.
        assert list(structured.items()) == list(built.items())
        assert size_of(built) <= TEXT_LIMIT or len(items) == 0
