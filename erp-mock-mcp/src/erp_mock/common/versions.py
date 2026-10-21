"""Package versions and version ranges (requirements 2.3 and 2.4).

A version (:class:`SemVer`) is written ``<major>.<minor>.<patch>``: three non-negative
integers in ASCII decimal without leading zeros, joined by ``.``, with no prerelease or
build suffix, no prefix and no whitespace. ``1.0.0`` and ``0.12.3`` are versions;
``01.0.0``, ``1.0``, ``1.0.0.0``, ``1.0.0-rc1``, ``1.0.0+build``, ``v1.0.0`` and range texts
such as ``>=1.0.0`` are not. Versions order numerically by major, then minor, then patch
(``1.10.0`` is above ``1.9.0``).

A version range (:class:`VersionRange`), as written in requires_meta_model and depends_on,
is a list of comparators. A comparator is one of the operators ``>=``, ``>``, ``<=``, ``<``
and ``=`` immediately followed by a version, or a version alone, which means ``=``. A
version matches a range when it satisfies every comparator: ``>=0.1.0 <1.0.0`` matches
0.3.2 but not 1.0.0. Other operators, such as ``~``, ``^``, ``==`` and ``!=``, are rejected.

Spaces in a range text:

- comparators are separated by one or more spaces (U+0020), as design.md 1.5 specifies,
  so ``>=0.1.0  <1.0.0`` is the same range as ``>=0.1.0 <1.0.0``;
- no other whitespace character is allowed anywhere: a tab, a line break, a no-break
  space or an ideographic space does not separate comparators;
- the text neither starts nor ends with a space;
- an operator is not separated from its version: ``>= 1.0.0`` is rejected;
- the text has at least one comparator, so an empty or all-space text is rejected.

A text that is not a version or a range raises :class:`VersionError`, a ValueError that
carries the text and a one-line reason. The reason is Chinese, like the loader's
configuration errors that embed it (requirements 2.3 and 2.5). Segments of any length
convert exactly: parsing does not depend on ``sys.get_int_max_str_digits()`` or any other
process-wide setting.
"""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, Self, TypeGuard, assert_never, get_args

__all__ = ["Comparator", "Operator", "SemVer", "VersionError", "VersionRange"]

Operator = Literal[">=", ">", "<=", "<", "="]

_OPERATORS: Final[tuple[Operator, ...]] = get_args(Operator)

# Characters that start an operator in this or other range syntaxes (npm, Cargo, PEP 440).
# The leading run of them in a comparator is its operator text, so ``~1.0.0`` reports the
# unsupported operator ``~`` and ``==1.0.0`` the unsupported operator ``==``.
_OPERATOR_CHARS: Final = "<>=!~^"

# Used with fullmatch: "$" would also accept a trailing newline. [0-9] is ASCII only.
_VERSION: Final = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
# Three numeric segments followed by a prerelease ("-") or build ("+") suffix; used with
# match, which anchors at the start only.
_SUFFIXED: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+[-+]")
_DIGITS: Final = re.compile(r"[0-9]+")
_SEGMENT_NAMES: Final = ("主版本号", "次版本号", "修订号")


class VersionError(ValueError):
    """A text that is not a version (SemVer.parse) or a version range (VersionRange.parse).

    ``text`` is the rejected text as given. ``reason`` is a one-line description of the
    first problem found, in Chinese; for a range it names the offending comparator. The
    loader embeds the reason in its configuration error.
    """

    text: str
    reason: str

    def __init__(self, text: str, reason: str) -> None:
        # Both fields go to args, so the exception pickles and copies.
        super().__init__(text, reason)
        self.text = text
        self.reason = reason

    def __str__(self) -> str:
        return f"{_quoted(self.text)}：{self.reason}"


@dataclass(frozen=True, order=True, slots=True)
class SemVer:
    """A version ``<major>.<minor>.<patch>``.

    Instances compare as the tuple ``(major, minor, patch)``, which is the numeric,
    segment by segment comparison of requirement 2.4. ``str()`` gives the version text, and
    ``SemVer.parse(str(v)) == v``.
    """

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        _check_segment("major", self.major)
        _check_segment("minor", self.minor)
        _check_segment("patch", self.patch)

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse ``text``, a version ``<major>.<minor>.<patch>`` (requirement 2.3).

        Raises VersionError for any other text, including a range such as ``>=1.0.0``,
        and TypeError when ``text`` is not a str.
        """
        if not isinstance(text, str):
            raise TypeError(f"a version must be a str, got {type(text).__name__}")
        segments = _match_version(text)
        if segments is None:
            raise VersionError(text, _version_problem(text))
        return cls(*segments)

    def __str__(self) -> str:
        return ".".join(_int_text(n) for n in (self.major, self.minor, self.patch))


# An operator and the version it compares against.
Comparator = tuple[Operator, SemVer]


@dataclass(frozen=True, slots=True)
class VersionRange:
    """A version range: the comparators that a matching version satisfies, in text order.

    A comparator written without an operator is stored with ``=``, so ``1.0.0`` and
    ``=1.0.0`` parse to equal ranges. ``str()`` gives the canonical text, every operator
    written and comparators separated by one space, and ``VersionRange.parse(str(r)) == r``.
    """

    comparators: tuple[Comparator, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.comparators, tuple):
            raise TypeError("comparators must be a tuple")
        if not self.comparators:
            raise ValueError("a version range has at least one comparator")
        for comparator in self.comparators:
            if not isinstance(comparator, tuple) or len(comparator) != 2:
                raise TypeError(f"a comparator is an (operator, SemVer) pair, got {comparator!r}")
            op, version = comparator
            if not _is_operator(op):
                raise ValueError(f"operator must be one of {', '.join(_OPERATORS)}, got {op!r}")
            if not isinstance(version, SemVer):
                raise TypeError(f"a comparator compares with a SemVer, got {version!r}")

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse ``text``, a version range (requirement 2.4, syntax in the module docstring).

        Raises VersionError when ``text`` is not a range, naming the first offending
        comparator, and TypeError when ``text`` is not a str.
        """
        if not isinstance(text, str):
            raise TypeError(f"a version range must be a str, got {type(text).__name__}")
        if not text.strip(" "):
            raise VersionError(text, "范围中没有比较式")
        if text.startswith(" ") or text.endswith(" "):
            raise VersionError(text, "首尾不能有空格")
        # A run of several spaces leaves empty strings between the comparators.
        tokens = [token for token in text.split(" ") if token]
        return cls(tuple(_parse_comparator(text, token) for token in tokens))

    def matches(self, version: SemVer) -> bool:
        """Return whether ``version`` satisfies every comparator of the range."""
        if not isinstance(version, SemVer):
            raise TypeError(f"expected a SemVer, got {type(version).__name__}")
        return all(_holds(op, version, bound) for op, bound in self.comparators)

    def __str__(self) -> str:
        return " ".join(f"{op}{bound}" for op, bound in self.comparators)


# --- helpers -----------------------------------------------------------------------------


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _check_segment(field: str, value: object) -> None:
    # bool is an int subclass, but True has no version text.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field} must not be negative, got {value}")


def _is_operator(text: str) -> TypeGuard[Operator]:
    return text in _OPERATORS


def _holds(op: Operator, version: SemVer, bound: SemVer) -> bool:
    """Return whether ``version`` ``op`` ``bound`` holds."""
    match op:
        case ">=":
            return version >= bound
        case ">":
            return version > bound
        case "<=":
            return version <= bound
        case "<":
            return version < bound
        case "=":
            return version == bound
        case _:
            assert_never(op)


def _match_version(text: str) -> tuple[int, int, int] | None:
    """Return the segments of the version ``text``, or None when it is not a version."""
    found = _VERSION.fullmatch(text)
    if found is None:
        return None
    major, minor, patch = found.groups()
    return _int_value(major), _int_value(minor), _int_value(patch)


def _version_problem(text: str) -> str:
    """Return why ``text``, which :func:`_match_version` rejects, is not a version."""
    if not text:
        return "版本为空"
    if text[0] in _OPERATOR_CHARS:
        return "范围写法不能用作具体版本"
    if any(char.isspace() for char in text):
        return "含空白字符"
    if _SUFFIXED.match(text):
        return "带预发布或构建后缀"
    segments = text.split(".")
    if len(segments) != len(_SEGMENT_NAMES):
        return f"应为 <主>.<次>.<修订> 三段，实际为 {len(segments)} 段"
    for name, segment in zip(_SEGMENT_NAMES, segments, strict=True):
        if not segment:
            return f"{name}为空"
        if _DIGITS.fullmatch(segment) is None:
            return f"{name} {_quoted(segment)} 不是非负整数"
        if len(segment) > 1 and segment[0] == "0":
            return f"{name} {_quoted(segment)} 有前导零"
    # Not reached: three segments of digits without leading zeros form a version.
    return "不是 <主>.<次>.<修订> 形式"


def _parse_comparator(text: str, token: str) -> Comparator:
    """Parse ``token``, one comparator of the range ``text``."""
    operator_text = token[: len(token) - len(token.lstrip(_OPERATOR_CHARS))]
    version_text = token[len(operator_text) :]
    op: Operator
    if not operator_text:
        op = "="
    elif _is_operator(operator_text):
        op = operator_text
    else:
        problem = f"不支持的运算符 {_quoted(operator_text)}，运算符只能是 >=、>、<=、<、= 之一"
        raise _comparator_error(text, token, problem)
    if not version_text:
        problem = "运算符之后缺少版本（运算符与版本之间不能有空格）"
        raise _comparator_error(text, token, problem)
    segments = _match_version(version_text)
    if segments is None:
        raise _comparator_error(text, token, _version_problem(version_text))
    return op, SemVer(*segments)


def _comparator_error(text: str, token: str, problem: str) -> VersionError:
    return VersionError(text, f"比较式 {_quoted(token)} 不合法：{problem}")


# --- integers of any length --------------------------------------------------------------
# int() and str() refuse decimal texts of more than sys.get_int_max_str_digits() digits
# (4300 by default), but a segment has no length limit. Decimal converts integers exactly
# in both directions without that limit, whatever the current decimal context.


def _int_value(digits: str) -> int:
    """Value of an ASCII decimal integer text such as ``12``."""
    return int(Decimal(digits))


def _int_text(n: int) -> str:
    """Decimal text of the non-negative ``n`` without leading zeros."""
    return str(Decimal(n))
