"""Reading of configuration files: strict UTF-8, YAML 1.2 core tags (design.md 2.1).

Every YAML file of an instance config or a business pack is read with :func:`load_yaml`:
:func:`read_text` decodes it as UTF-8, and :class:`Yaml12Loader`, a ``yaml.CSafeLoader``
(libyaml), parses it with the YAML 1.2 core schema instead of PyYAML's YAML 1.1 tag
resolution. Plain scalars resolve as follows; every other plain scalar is a string:

- null: ``~``, ``null``, ``Null``, ``NULL`` and the empty scalar.
- bool: ``true``, ``True``, ``TRUE``, ``false``, ``False``, ``FALSE`` only. ``yes``, ``no``,
  ``on``, ``off``, ``y`` and ``n`` are strings; writeback, linkage and rule configs have a
  key named ``on``.
- int: decimal only, ``[-+]?(0|[1-9][0-9]*)``. The YAML 1.1 forms ``012`` (octal), ``0b1``,
  ``1_000`` and ``1:30`` (base 60) are strings, and so are ``0o17`` and ``0x1F``, which the
  core schema reads as octal and hexadecimal.
- float: the decimal notation of the core schema (``0.13``, ``.5``, ``5.``, ``1e3``,
  ``-1.5E-3``), and ``.inf``, ``-.inf``, ``+.inf``, ``.nan`` with their capitalised and
  upper-case spellings. The strict config models (design.md 2.2) accept no float, so such
  a value fails model validation at its config path; decimal values are written as strings.
- No timestamps: ``2026-10-09`` is a string, so a date is checked by the config models,
  which report its config path (requirement 16.3), not by the YAML layer.
- No merge keys: ``<<`` is an ordinary string key, as in YAML 1.2.

Explicit tags are limited to the core schema: ``!!str``, ``!!seq``, ``!!map``, and
``!!null``, ``!!bool``, ``!!int``, ``!!float`` on a scalar of the form listed above (an
int form is also a float). Any other tag (``!!timestamp``, ``!!binary``, ``!!set``,
``!!python/...``, local tags) is an error, so parsed data holds only None, bool, int,
float, str, list and dict. A key that repeats within a mapping is an error too; keys
compare as the Python values they construct to, so ``true`` and ``True`` are one key.

Every failure raises :class:`YamlLoadError` with the file, the 1-based line and column of
the problem, and the reason. The loader reports the line as the config path of the error
(requirement 2.2, :attr:`YamlLoadError.config_path`). :func:`list_files` returns the files
of a directory sorted by path, so the order in which files are read never depends on the
file system.
"""

import json
import math
import os
import re
import sys
from collections.abc import Hashable
from pathlib import Path
from typing import Any, Final, NoReturn

import yaml
from yaml.constructor import ConstructorError, SafeConstructor
from yaml.error import MarkedYAMLError
from yaml.nodes import MappingNode, Node, ScalarNode
from yaml.reader import ReaderError

__all__ = [
    "Yaml12Loader",
    "YamlLoadError",
    "YamlScalar",
    "YamlValue",
    "list_files",
    "load_yaml",
    "parse_yaml",
    "read_text",
]

type YamlScalar = str | int | float | bool | None
type YamlValue = YamlScalar | list[YamlValue] | dict[YamlScalar, YamlValue]

_TAG_PREFIX: Final = "tag:yaml.org,2002:"  # written "!!" in a document
_NULL_TAG: Final = _TAG_PREFIX + "null"
_BOOL_TAG: Final = _TAG_PREFIX + "bool"
_INT_TAG: Final = _TAG_PREFIX + "int"
_FLOAT_TAG: Final = _TAG_PREFIX + "float"

# Plain scalar forms of the core types. PyYAML's resolver applies them with re.match, so
# each ends in \Z ("$" would also match before a trailing newline). [0-9] is ASCII only.
_NULL: Final = re.compile(r"(?:~|null|Null|NULL|)\Z")
_BOOL: Final = re.compile(r"(?:true|True|TRUE|false|False|FALSE)\Z")
_INT: Final = re.compile(r"[-+]?(?:0|[1-9][0-9]*)\Z")
# The core schema's float forms minus the digit-only texts, which are ints or (with a
# leading zero) strings: a float has a decimal point or an exponent.
_FLOAT: Final = re.compile(
    r"""(?:
        [-+]? (?: \.[0-9]+ | [0-9]+\.[0-9]* ) (?: [eE][-+]?[0-9]+ )?
      | [-+]? [0-9]+ [eE][-+]?[0-9]+
      | [-+]? \.(?: inf|Inf|INF )
      | \.(?: nan|NaN|NAN )
    )\Z""",
    re.VERBOSE,
)

_BOM: Final = "\ufeff"
# Line breaks as libyaml counts them in its marks (YAML 1.1 also breaks lines at NEL, LS
# and PS), so positions computed here agree with the parser's.
_LINE_BREAK: Final = re.compile("\r\n|[\r\n\x85\u2028\u2029]")


class YamlLoadError(Exception):
    """A configuration file could not be read, decoded as UTF-8 or parsed as YAML.

    ``file`` is the path as given by the caller. ``line`` and ``column`` are 1-based and
    locate the problem: for a YAML error the parser's problem mark, for a decoding error
    the first undecodable byte. Both are None when the file or directory could not be
    read at all. ``reason`` is a one-line description.
    """

    file: Path
    line: int | None
    column: int | None
    reason: str

    def __init__(self, file: Path, line: int | None, column: int | None, reason: str) -> None:
        # All fields go to args, so the exception pickles and copies.
        super().__init__(file, line, column, reason)
        self.file = file
        self.line = line
        self.column = column
        self.reason = reason

    @property
    def config_path(self) -> str | None:
        """The config path to report for this error (requirement 2.2): ``"line <n>"``.

        None when the error has no line, that is, when the file could not be read.
        """
        return None if self.line is None else f"line {self.line}"

    def __str__(self) -> str:
        where = f"{self.file}" if self.line is None else f"{self.file}:{self.line}:{self.column}"
        return f"{where}: {self.reason}"


def _position(prefix: str) -> tuple[int, int]:
    """Return the 1-based line and column of the character that follows ``prefix``."""
    line, line_start = 1, 0
    for match in _LINE_BREAK.finditer(prefix):
        line += 1
        line_start = match.end()
    return line, len(prefix) - line_start + 1


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _tag_text(tag: str) -> str:
    """Return ``tag`` as written in a document: ``!!int`` for the core schema's int tag."""
    return "!!" + tag.removeprefix(_TAG_PREFIX) if tag.startswith(_TAG_PREFIX) else tag


class Yaml12Loader(yaml.CSafeLoader):
    """``yaml.CSafeLoader`` with the tag resolution and constructors of the module docstring.

    One instance parses one stream: ``Yaml12Loader(data).get_single_data()``, then
    ``dispose()``. :func:`parse_yaml` does this and converts errors to YamlLoadError.
    """

    # Replaces the inherited YAML 1.1 table; filled by the add_implicit_resolver calls
    # after the class body.
    yaml_implicit_resolvers = {}

    def _scalar_text(self, node: ScalarNode, *forms: re.Pattern[str]) -> str:
        """Return the text of ``node``, which must match one of ``forms``.

        An implicitly resolved scalar always matches; this rejects explicit tags such as
        ``!!int 0x1F`` or ``!!bool yes``.
        """
        text = self.construct_scalar(node)
        if not any(form.match(text) for form in forms):
            raise ConstructorError(
                None, None, f"{_tag_text(node.tag)} 不接受 {_quoted(text)}", node.start_mark
            )
        return text

    def construct_undefined(self, node: Node) -> NoReturn:
        raise ConstructorError(None, None, f"不支持的标签 {_tag_text(node.tag)}", node.start_mark)

    def construct_yaml12_null(self, node: ScalarNode) -> None:
        self._scalar_text(node, _NULL)

    def construct_yaml12_bool(self, node: ScalarNode) -> bool:
        return self._scalar_text(node, _BOOL).lower() == "true"

    def construct_yaml12_int(self, node: ScalarNode) -> int:
        text = self._scalar_text(node, _INT)
        try:
            return int(text)
        except ValueError as error:  # more digits than Python converts
            limit = sys.get_int_max_str_digits()
            reason = f"整数的位数超过上限 {limit}"
            raise ConstructorError(None, None, reason, node.start_mark) from error

    def construct_yaml12_float(self, node: ScalarNode) -> float:
        text = self._scalar_text(node, _FLOAT, _INT)
        body = text.lstrip("+-").lower()
        if body == ".inf":
            return -math.inf if text.startswith("-") else math.inf
        if body == ".nan":
            return math.nan
        return float(text)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        """Construct a mapping as BaseConstructor does, but reject a repeated key.

        Unlike SafeConstructor.construct_mapping, ``<<`` keys are not merged.
        """
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None, None, f"expected a mapping node, but found {node.id}", node.start_mark
            )
        mapping: dict[Hashable, Any] = {}
        first_lines: dict[Hashable, int] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found unhashable key",
                    key_node.start_mark,
                )
            if key in mapping:
                raise ConstructorError(
                    None,
                    None,
                    f"重复的键 {_key_text(key_node)}（第 {first_lines[key]} 行已出现）",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
            first_lines[key] = key_node.start_mark.line + 1
        return mapping

    # Only the core schema's tags. The None entry makes every other tag an error instead
    # of letting it fall back to a plain scalar, sequence or mapping.
    yaml_constructors = {
        _NULL_TAG: construct_yaml12_null,
        _BOOL_TAG: construct_yaml12_bool,
        _INT_TAG: construct_yaml12_int,
        _FLOAT_TAG: construct_yaml12_float,
        _TAG_PREFIX + "str": SafeConstructor.construct_yaml_str,
        _TAG_PREFIX + "seq": SafeConstructor.construct_yaml_seq,
        _TAG_PREFIX + "map": SafeConstructor.construct_yaml_map,
        None: construct_undefined,
    }


# The first characters a plain scalar of each type can start with; "" is the empty scalar.
Yaml12Loader.add_implicit_resolver(_NULL_TAG, _NULL, ["~", "n", "N", ""])
Yaml12Loader.add_implicit_resolver(_BOOL_TAG, _BOOL, list("tTfF"))
Yaml12Loader.add_implicit_resolver(_INT_TAG, _INT, list("-+0123456789"))
Yaml12Loader.add_implicit_resolver(_FLOAT_TAG, _FLOAT, list("-+.0123456789"))


def _key_text(node: Node) -> str:
    return _quoted(node.value) if isinstance(node, ScalarNode) else f"<{node.tag}>"


def _marked_error(file: Path, error: MarkedYAMLError) -> YamlLoadError:
    """Convert a scanner, parser, composer or constructor error, located at its problem."""
    mark = error.problem_mark or error.context_mark
    parts = []
    if error.context:
        context = error.context
        start = error.context_mark
        if start is not None and mark is not None and start.line != mark.line:
            context += f" (line {start.line + 1})"
        parts.append(context)
    if error.problem:
        parts.append(error.problem)
    reason = "YAML 无法解析：" + ", ".join(parts)
    if mark is None:
        return YamlLoadError(file, None, None, reason)
    return YamlLoadError(file, mark.line + 1, mark.column + 1, reason)


def parse_yaml(text: str, file: Path) -> YamlValue:
    """Parse ``text``, a single YAML document, with :class:`Yaml12Loader`.

    ``file`` only names the source in errors. A leading byte order mark is ignored; an
    empty document gives None. Raises YamlLoadError for any syntax error, a second
    document, an undefined alias, a character YAML does not allow (such as a control
    character), a tag outside the core schema, or a repeated mapping key.
    """
    text = text.removeprefix(_BOM)
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as error:  # a lone surrogate
        line, column = _position(text[: error.start])
        reason = f"YAML 无法解析：字符 U+{ord(text[error.start]):04X} 无法按 UTF-8 编码"
        raise YamlLoadError(file, line, column, reason) from error
    loader = Yaml12Loader(data)
    try:
        return loader.get_single_data()
    except MarkedYAMLError as error:
        raise _marked_error(file, error) from error
    except ReaderError as error:
        # libyaml reports the byte offset into data of the offending character.
        line, column = _position(data[: error.position].decode("utf-8", "replace"))
        reason = f"YAML 无法解析：unacceptable character #x{error.character:04x}: {error.reason}"
        raise YamlLoadError(file, line, column, reason) from error
    finally:
        loader.dispose()


def read_text(path: Path) -> str:
    """Return the content of ``path`` decoded as strict UTF-8.

    A leading byte order mark is dropped; line breaks are kept as written. Raises
    YamlLoadError located at the first byte that is not valid UTF-8, or without a line
    when the file cannot be read (missing, a directory, no permission).
    """
    try:
        data = path.read_bytes()
    except OSError as error:
        raise YamlLoadError(path, None, None, f"无法读取文件：{error.strerror}") from error
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        line, column = _position(data[: error.start].decode("utf-8").removeprefix(_BOM))
        raise YamlLoadError(path, line, column, f"不是有效的 UTF-8 文本：{error.reason}") from error
    return text.removeprefix(_BOM)


def load_yaml(path: Path) -> YamlValue:
    """Read ``path`` with :func:`read_text` and parse it with :func:`parse_yaml`."""
    return parse_yaml(read_text(path), path)


def list_files(
    directory: Path, suffix: str = ".yaml", *, recursive: bool = True
) -> tuple[Path, ...]:
    """Return the files in ``directory`` whose names end with ``suffix``, sorted by path.

    Paths are ``directory / <relative path>``, sorted by the code points of the relative
    path written with ``/`` (so ``a-b.yaml``, ``a.yaml``, ``a/x.yaml``), the same on every
    platform and file system. With ``recursive`` subdirectories are searched too. Names starting with ``.`` are skipped, files and directories alike.
    Only regular files (or symbolic links to them) are returned, and symbolic links to
    directories are not followed. A missing ``directory`` gives an empty tuple; raises
    YamlLoadError without a line when ``directory`` is not a directory or cannot be listed.
    """
    if not directory.exists():
        return ()
    found: list[str] = []
    pending = [""]
    while pending:
        relative_dir = pending.pop()
        current = directory / relative_dir
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if recursive:
                            pending.append(relative)
                    elif entry.name.endswith(suffix) and entry.is_file():
                        found.append(relative)
        except OSError as error:
            raise YamlLoadError(current, None, None, f"无法读取目录：{error.strerror}") from error
    return tuple(directory / relative for relative in sorted(found))
