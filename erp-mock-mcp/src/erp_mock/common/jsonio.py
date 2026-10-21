"""Canonical JSON text and the UTF-8 bytes written to files.

Every JSON output of the service goes through this module: tool result text and trace
lines use :func:`dumps_compact`; diff, snapshot metadata and alias files use
:func:`dumps_pretty`; :func:`encode_utf8` turns either into the bytes written to a file.

Producers build each output object as a dict in a fixed key order. Nothing here sorts
keys, so the text lists keys in insertion order (requirement 13.1), and equal input,
including key order, gives identical text (requirements 24.1 and 24.2). Both helpers
write non-ASCII characters as they are (only U+0085, U+2028 and U+2029 are escaped, see
below) and reject NaN and infinities, which JSON cannot represent.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Final

__all__ = ["JsonValue", "dumps_compact", "dumps_pretty", "encode_utf8"]

# A JSON value. Arrays may be lists or tuples and objects any Mapping with str keys, so
# read-only containers (tuple, FrozenMap) serialize like list and dict. Covariant
# Sequence and Mapping let e.g. a list[str] or a dict[str, str] pass as a JsonValue.
type JsonValue = None | bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue]

# json escapes every control character below U+0020, "\n" and "\r" among them, but with
# ensure_ascii=False it leaves these three line terminators raw. str.splitlines() and
# JavaScript treat them as line breaks, so they are escaped as well: a compact text is
# then one line however it is split (requirement 17.8). The escapes read back as the
# same characters.
_LINE_BREAK_ESCAPES: Final = (
    ("\x85", "\\u0085"),
    ("\u2028", "\\u2028"),
    ("\u2029", "\\u2029"),
)


def _default(value: object) -> object:
    """Serialize a Mapping that is not a dict, such as FrozenMap, as an object.

    json calls this for objects it cannot serialize itself; it handles dict, list,
    tuple, str, int, float, bool and None. Key order is the mapping's iteration order.
    Anything else, such as Decimal, date, set or bytes, raises TypeError.
    """
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# Encoders hold no per-call state, so module-level instances are safe to share.
_COMPACT: Final = json.JSONEncoder(
    ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=_default
)
_PRETTY: Final = json.JSONEncoder(
    ensure_ascii=False, allow_nan=False, indent=2, separators=(",", ": "), default=_default
)


def _escape_line_breaks(text: str) -> str:
    # The three characters can occur only inside string literals, where each one is a
    # complete token, so replacing it with its escape keeps the JSON value unchanged.
    for char, escape in _LINE_BREAK_ESCAPES:
        text = text.replace(char, escape)
    return text


def dumps_compact(obj: JsonValue) -> str:
    """Return ``obj`` as JSON on a single line, without any whitespace between tokens.

    Used for tool result text and trace lines. Keys keep their insertion order;
    characters inside strings are written as they are, except that json escapes ``"``,
    ``\\`` and control characters (``\\n``, ``\\r``, ``\\t`` and the like) and this module
    escapes U+0085, U+2028 and U+2029, so the text contains no line break.

    Raises ValueError for NaN or an infinity and TypeError for a value that is not JSON
    (such as Decimal or date).
    """
    return _escape_line_breaks(_COMPACT.encode(obj))


def dumps_pretty(obj: JsonValue) -> str:
    """Return ``obj`` as JSON indented by 2 spaces, ending with a newline.

    Used for every diff (Admin API, stdio diff.json, export zip; requirement 24.2) and
    for metadata and alias files. Separators are ``","`` at line ends and ``": "`` after
    keys, so no line has trailing whitespace; empty arrays and objects are ``[]`` and
    ``{}``. Keys, strings and errors are handled as in :func:`dumps_compact`.
    """
    return _escape_line_breaks(_PRETTY.encode(obj)) + "\n"


def encode_utf8(text: str) -> bytes:
    """Encode ``text`` as UTF-8, writing each lone surrogate as a ``\\udXXX`` escape.

    Surrogate code points (U+D800 to U+DFFF) are the only ones UTF-8 cannot encode. A
    Python string gets them from JSON escapes such as ``"\\ud800"`` in client arguments,
    which trace events record verbatim. In text from :func:`dumps_compact` or
    :func:`dumps_pretty` they can occur only inside string literals, where the escape is a
    JSON escape, so ``json.loads`` on the bytes restores the original code point and the
    bytes are valid UTF-8 (requirement 17.8). Other characters, BMP or not, are plain
    UTF-8.

    A high surrogate directly followed by a low surrogate is not lone: JSON reads the two
    escapes back as one supplementary character, and has no other way to spell them.
    Strings decoded from JSON never contain such a pair, because the decoder combines it.
    """
    return text.encode("utf-8", "backslashreplace")
