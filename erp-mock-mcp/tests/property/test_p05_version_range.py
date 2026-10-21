"""Property 5: version range matching.

**Validates: Requirements 2.4**

VersionRange.parse(text).matches(v) is compared with reference_matches, an independent
reference that compares (major, minor, patch) triples of integers segment by segment and
requires every comparator to hold (requirement 2.4). The parsed range also keeps the
comparators in text order, with an omitted operator read as =, and the canonical texts of
ranges and versions parse back to equal values. A range text with one malformed
comparator (a segment with a leading zero, a prerelease or build suffix, an unknown
operator) is rejected with VersionError, whatever the other comparators are.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from erp_mock.common.versions import Operator, SemVer, VersionError, VersionRange

# A version as the integers (major, minor, patch).
Triple = tuple[int, int, int]

OPERATORS: tuple[Operator, ...] = (">=", ">", "<=", "<", "=")

# --- reference ---------------------------------------------------------------------------


def compare(version: Triple, bound: Triple) -> int:
    """Return -1, 0 or 1 as ``version`` is below, equal to or above ``bound``.

    The major, minor and patch numbers compare numerically in that order: the first pair
    that differs decides.
    """
    for ours, theirs in zip(version, bound, strict=True):
        if ours != theirs:
            return -1 if ours < theirs else 1
    return 0


# Whether a result of compare(version, bound) satisfies the comparator <op><bound>.
SATISFIES: dict[Operator, Callable[[int], bool]] = {
    ">=": lambda order: order >= 0,
    ">": lambda order: order > 0,
    "<=": lambda order: order <= 0,
    "<": lambda order: order < 0,
    "=": lambda order: order == 0,
}


def reference_matches(comparators: tuple[tuple[Operator, Triple], ...], version: Triple) -> bool:
    """Return whether ``version`` satisfies every comparator."""
    return all(SATISFIES[op](compare(version, bound)) for op, bound in comparators)


def version_text(version: Triple) -> str:
    return ".".join(str(segment) for segment in version)


# --- generators --------------------------------------------------------------------------

# Small segments; both sides of a carry into one more digit (9 and 10, 99 and 100, ...);
# and segments far beyond 64-bit integers and the 28 digits of Decimal's default precision.
SEGMENTS = st.one_of(
    st.integers(min_value=0, max_value=12),
    st.integers(min_value=1, max_value=40).flatmap(
        lambda digits: st.sampled_from([10**digits - 1, 10**digits])
    ),
    st.integers(min_value=0, max_value=10**60),
)
TRIPLES = st.tuples(SEGMENTS, SEGMENTS, SEGMENTS)


def near(bound: Triple) -> st.SearchStrategy[Triple]:
    """Versions equal to ``bound`` or next to it.

    Above ``bound``: the next patch, the next minor (1.9.5, then 1.10.0) and the next major
    version. Below it: the previous patch, and the previous minor (1.10.0, then 1.9.x) or
    major version with any later segments.
    """
    major, minor, patch = bound
    options: list[st.SearchStrategy[Triple]] = [
        st.just(bound),
        st.just((major, minor, patch + 1)),
        st.just((major, minor + 1, 0)),
        st.just((major + 1, 0, 0)),
    ]
    if patch > 0:
        options.append(st.just((major, minor, patch - 1)))
    if minor > 0:
        options.append(st.tuples(st.just(major), st.just(minor - 1), SEGMENTS))
    if major > 0:
        options.append(st.tuples(st.just(major - 1), SEGMENTS, SEGMENTS))
    return st.one_of(options)


def written(op: Operator) -> st.SearchStrategy[str]:
    """``op`` as written in a comparator: = may also be omitted."""
    return st.sampled_from(["=", ""]) if op == "=" else st.just(op)


WRITTEN_OPERATORS = st.sampled_from(OPERATORS).flatmap(written)
COMPARATOR_TEXTS = st.tuples(WRITTEN_OPERATORS, TRIPLES.map(version_text)).map("".join)


def join_with_spaces(draw: st.DrawFn, tokens: list[str]) -> str:
    """Join comparator texts with runs of one to three spaces."""
    text = tokens[0]
    for token in tokens[1:]:
        text += " " * draw(st.integers(min_value=1, max_value=3)) + token
    return text


@dataclass(frozen=True)
class MatchCase:
    """A range text, the comparators it denotes in text order and a version to match."""

    text: str
    comparators: tuple[tuple[Operator, Triple], ...]
    version: Triple


@st.composite
def match_cases(draw: st.DrawFn) -> MatchCase:
    """Draw a range of one to four comparators and a version.

    Each later bound, and the version, is either drawn anew or drawn next to an earlier
    bound, so that the comparators of a range interact and versions fall on and around
    the bounds.
    """
    count = draw(st.integers(min_value=1, max_value=4))
    bounds = [draw(TRIPLES)]
    while len(bounds) < count:
        bounds.append(draw(TRIPLES | st.sampled_from(tuple(bounds)).flatmap(near)))
    comparators = tuple((draw(st.sampled_from(OPERATORS)), bound) for bound in bounds)
    tokens = [draw(written(op)) + version_text(bound) for op, bound in comparators]
    version = draw(TRIPLES | st.sampled_from(tuple(bounds)).flatmap(near))
    return MatchCase(join_with_spaces(draw, tokens), comparators, version)


# Operators other than >=, >, <=, <, =: the other single characters and runs of two or three
# of the characters that the operators of common range syntaxes use (~, ^, ==, !=, => and
# so on), and look-alikes outside ASCII.
UNKNOWN_OPERATORS = st.one_of(
    st.sampled_from(["~", "^", "!", "≥", "≤", "＞", "＜", "＝"]),
    st.text(alphabet="<>=!~^", min_size=2, max_size=3).filter(lambda text: text not in OPERATORS),
)
# Prerelease (-rc.1) and build (+build.5) suffixes.
SUFFIXES = st.from_regex(r"[-+][0-9A-Za-z.-]{1,8}", fullmatch=True)


@st.composite
def malformed_comparators(draw: st.DrawFn) -> str:
    """Draw a comparator text with one defect.

    The defect is a segment with a leading zero (01, 00), a prerelease or build suffix, or
    an unknown operator.
    """
    segments = [str(segment) for segment in draw(TRIPLES)]
    defect = draw(st.sampled_from(["leading zero", "suffix", "operator"]))
    if defect == "operator":
        return draw(UNKNOWN_OPERATORS) + ".".join(segments)
    if defect == "leading zero":
        index = draw(st.integers(min_value=0, max_value=2))
        segments[index] = "0" + segments[index]
    else:
        segments[-1] += draw(SUFFIXES)
    return draw(WRITTEN_OPERATORS) + ".".join(segments)


@st.composite
def malformed_ranges(draw: st.DrawFn) -> str:
    """Draw a range text of one to four comparators, exactly one of them malformed."""
    tokens = draw(st.lists(COMPARATOR_TEXTS, max_size=3))
    position = draw(st.integers(min_value=0, max_value=len(tokens)))
    tokens.insert(position, draw(malformed_comparators()))
    return join_with_spaces(draw, tokens)


# --- properties --------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 5: 版本范围匹配
@pytest.mark.req("2.4")
@settings(max_examples=100)
@given(case=match_cases())
@example(  # the example of requirement 2.4: 0.3.2 matches
    case=MatchCase(">=0.1.0 <1.0.0", ((">=", (0, 1, 0)), ("<", (1, 0, 0))), (0, 3, 2))
)
@example(  # and 1.0.0 does not
    case=MatchCase(">=0.1.0 <1.0.0", ((">=", (0, 1, 0)), ("<", (1, 0, 0))), (1, 0, 0))
)
@example(case=MatchCase("<1.10.0", (("<", (1, 10, 0)),), (1, 9, 10)))  # numbers, not texts
@example(  # = omitted, two spaces
    case=MatchCase(">1.9.99  1.10.0", ((">", (1, 9, 99)), ("=", (1, 10, 0))), (1, 10, 0))
)
def test_matches_agrees_with_the_reference(case: MatchCase) -> None:
    version_range = VersionRange.parse(case.text)
    expected = tuple((op, SemVer(*bound)) for op, bound in case.comparators)
    assert version_range.comparators == expected

    version = SemVer(*case.version)
    assert version_range.matches(version) is reference_matches(case.comparators, case.version)

    # Canonical texts: every operator written, comparators separated by one space.
    canonical = " ".join(op + version_text(bound) for op, bound in case.comparators)
    assert str(version_range) == canonical
    assert VersionRange.parse(str(version_range)) == version_range
    assert SemVer.parse(str(version)) == version


# Feature: erp-mock-mcp-m0, Property 5: 版本范围匹配
@pytest.mark.req("2.4")
@settings(max_examples=100)
@given(text=malformed_ranges())
@example(text=">=0.1.0 <1.00.0")  # leading zero
@example(text="1.0.0-rc1")  # prerelease suffix
@example(text=">=1.0.0 ~2.0.0")  # unknown operator
def test_malformed_ranges_are_rejected(text: str) -> None:
    with pytest.raises(VersionError) as info:
        VersionRange.parse(text)
    assert info.value.text == text
