"""Syntax-tree helpers for static checks over the erp_mock source tree.

The layering, clock and randomness checks (tests/static/test_layering.py and
tests/static/test_no_clock.py) and the business name scan walk the modules returned by
:func:`iter_modules` and report :class:`Violation` objects. Each check runs against the
real tree, :data:`ERP_MOCK_SRC`, and against small sample trees that a test writes with
:func:`write_package`; ``# expect: <rule>`` comments in a sample mark the lines where the
check must report a violation (:func:`expected_violations`).

The analysis is syntactic and ignores scopes and control flow. :class:`NameResolver`
follows import aliases only, so a receiver bound by assignment or passed in as an
argument does not resolve (``d.now()`` on a datetime instance ``d`` is not seen), and a
dynamic import is seen only when the module name is a string literal.
"""

import ast
import re
import textwrap
import tokenize
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NamedTuple

__all__ = [
    "ERP_MOCK_SRC",
    "ImportRef",
    "NameResolver",
    "SourceModule",
    "StringLiteral",
    "Violation",
    "as_triples",
    "expected_violations",
    "format_violations",
    "iter_imports",
    "iter_modules",
    "iter_references",
    "iter_string_literals",
    "resolve_from",
    "write_package",
]

# The engine package that the static checks scan.
ERP_MOCK_SRC: Final = Path(__file__).resolve().parents[2] / "src" / "erp_mock"

# Callables that import the module named by their first argument.
_IMPORT_FUNCTIONS: Final = frozenset(
    {"__import__", "builtins.__import__", "importlib.__import__", "importlib.import_module"}
)

# A comment that lists the rules a check must report on its line.
_EXPECT: Final = re.compile(r"#\s*expect:(?P<rules>.*)")


@dataclass(frozen=True, order=True)
class Violation:
    """A finding of a static check.

    ``path`` is the POSIX path of the file relative to the parent of the package root
    (``erp_mock/domain/rules.py``), ``line`` the 1-based line and ``rule`` the name of the
    broken rule. Violations sort by path, line, rule and message.
    """

    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


def format_violations(violations: Iterable[Violation]) -> str:
    """Return a report with one line per violation, sorted, for assertion messages."""
    lines = [str(violation) for violation in sorted(violations)]
    return "\n".join([f"{len(lines)} violation(s):", *lines])


def as_triples(violations: Iterable[Violation]) -> list[tuple[str, int, str]]:
    """Return the ``(path, line, rule)`` of each violation, in the given order."""
    return [(violation.path, violation.line, violation.rule) for violation in violations]


@dataclass(frozen=True)
class SourceModule:
    """A parsed module of the scanned package.

    ``rel_path`` is formed like :attr:`Violation.path` and ``name`` is the dotted module
    name; an ``__init__.py`` has ``is_package`` set and the name of its package.
    ``subpackage`` is the first name below the package root (``domain`` for
    ``erp_mock.domain.rules``, ``foo`` for a top-level module ``erp_mock/foo.py``) and
    None for the root module ``erp_mock/__init__.py``.
    """

    path: Path
    rel_path: str
    name: str
    is_package: bool
    subpackage: str | None
    tree: ast.Module


def iter_modules(root: Path) -> Iterator[SourceModule]:
    """Parse every ``.py`` file below the package directory ``root``, in path order.

    The package is named ``root.name``. Raises NotADirectoryError at once when ``root``
    is not a directory, so a wrong path cannot pass a check by scanning nothing.
    """
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a package directory: {root}")
    return (_parse_module(root, path) for path in _source_files(root))


def _source_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _rel_path(root: Path, path: Path) -> str:
    return "/".join((root.name, *path.relative_to(root).parts))


def _parse_module(root: Path, path: Path) -> SourceModule:
    names = [root.name, *path.relative_to(root).with_suffix("").parts]
    is_package = names[-1] == "__init__"
    if is_package:
        names.pop()
    return SourceModule(
        path=path,
        rel_path=_rel_path(root, path),
        name=".".join(names),
        is_package=is_package,
        subpackage=names[1] if len(names) > 1 else None,
        tree=ast.parse(path.read_bytes(), filename=str(path)),
    )


def write_package(base: Path, files: Mapping[str, str], package: str = "erp_mock") -> Path:
    """Write the package ``package`` below ``base`` and return its root directory.

    ``files`` maps paths relative to the package root (``"domain/rules.py"``) to source
    text. The text is dedented and loses one leading newline, so a triple-quoted sample
    whose code starts on the line after the opening quotes starts at line 1. Every
    directory of the tree without an ``__init__.py`` gets an empty one.
    """
    root = base / package
    root.mkdir(parents=True, exist_ok=True)
    for rel_path, text in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).removeprefix("\n"), encoding="utf-8")
    for directory in [root, *sorted(path for path in root.rglob("*") if path.is_dir())]:
        init = directory / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
    return root


def expected_violations(root: Path) -> list[tuple[str, int, str]]:
    """Return the ``(path, line, rule)`` triples marked in the tree below ``root``, sorted.

    A comment ``# expect: <rule>[, <rule>...]`` expects one violation of each listed rule
    on its line; a rule listed twice expects two. Paths are formed like
    :attr:`Violation.path`.
    """
    root = root.resolve()
    expected: list[tuple[str, int, str]] = []
    for path in _source_files(root):
        with path.open("rb") as source:
            for token in tokenize.tokenize(source.readline):
                if token.type != tokenize.COMMENT:
                    continue
                match = _EXPECT.fullmatch(token.string)
                if match is not None:
                    expected.extend(
                        (_rel_path(root, path), token.start[0], rule.strip())
                        for rule in match["rules"].split(",")
                    )
    return sorted(expected)


def resolve_from(module: SourceModule, node: ast.ImportFrom) -> str:
    """Return the absolute name of the module that ``node`` in ``module`` imports from.

    In ``erp_mock/domain/rules.py``, ``from ..tools import x`` imports from
    ``erp_mock.tools`` and ``from . import y`` from ``erp_mock.domain``. Raises ValueError
    when a relative import goes beyond the package root.
    """
    if node.level == 0:
        if node.module is None:
            raise ValueError(f"{module.rel_path}:{node.lineno}: import without a module name")
        return node.module
    package = module.name if module.is_package else module.name.rpartition(".")[0]
    return _resolve_relative(package, node.level, node.module)


def _resolve_relative(package: str, level: int, name: str | None) -> str:
    """Resolve the module ``name`` written after ``level`` dots against ``package``."""
    parts = package.split(".") if package else []
    keep = len(parts) - level + 1
    if keep < 1:
        raise ValueError(f"relative import with {level} dot(s) goes beyond {package!r}")
    base = ".".join(parts[:keep])
    return f"{base}.{name}" if name else base


class ImportRef(NamedTuple):
    """A module or module member imported at ``line``, by absolute name.

    ``import a.b`` and ``from a import b`` both give ``a.b``, ``from a import *`` gives
    ``a.*``, and ``importlib.import_module("a.b")`` and ``__import__("a.b")`` give ``a.b``.
    """

    target: str
    line: int


def iter_imports(module: SourceModule) -> Iterator[ImportRef]:
    """Yield every import of ``module``, wherever it appears.

    Covers import statements at any depth (in functions, ``if TYPE_CHECKING:`` blocks and
    ``try`` blocks), relative imports, and calls of ``importlib.import_module`` and
    ``__import__`` whose module name is a string literal; a relative name passed to
    ``import_module`` resolves when the package argument is a string literal too.
    """
    resolver = NameResolver(module)
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportRef(alias.name, alias.lineno)
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(module, node)
            for alias in node.names:
                yield ImportRef(f"{base}.{alias.name}", alias.lineno)
        elif isinstance(node, ast.Call):
            target = _dynamic_import(resolver, node)
            if target is not None:
                yield ImportRef(target, node.lineno)


def _dynamic_import(resolver: "NameResolver", call: ast.Call) -> str | None:
    """Return the module that ``call`` imports, if it is a resolvable dynamic import."""
    functions = resolver.qualified_names(call.func) & _IMPORT_FUNCTIONS
    name = _str_argument(call, 0, "name")
    if not functions or name is None:
        return None
    if not name.startswith("."):
        return name
    package = _str_argument(call, 1, "package")
    if "importlib.import_module" not in functions or package is None:
        return None
    level = len(name) - len(name.lstrip("."))
    return _resolve_relative(package, level, name[level:] or None)


def _str_argument(call: ast.Call, position: int, keyword: str) -> str | None:
    """Return the string literal passed to ``call`` at ``position`` or as ``keyword``."""
    if position < len(call.args):
        node: ast.expr | None = call.args[position]
    else:
        node = next((kw.value for kw in call.keywords if kw.arg == keyword), None)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class NameResolver:
    """Resolves names and attribute chains of a module to qualified names via its imports.

    Bindings come from every import statement of the module, wherever it appears, and a
    name bound by several imports resolves to each of their targets. With
    ``import datetime as dt``, ``dt.date.today`` resolves to ``datetime.date.today``;
    with ``from datetime import datetime``, ``datetime.now`` resolves to
    ``datetime.datetime.now``. Star imports bind nothing.
    """

    def __init__(self, module: SourceModule) -> None:
        bindings: dict[str, set[str]] = {}
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname is None:
                        # "import a.b" binds "a" to the top-level package.
                        top = alias.name.partition(".")[0]
                        bindings.setdefault(top, set()).add(top)
                    else:
                        bindings.setdefault(alias.asname, set()).add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = resolve_from(module, node)
                for alias in node.names:
                    if alias.name != "*":
                        local = alias.asname or alias.name
                        bindings.setdefault(local, set()).add(f"{base}.{alias.name}")
        self._bindings = {name: frozenset(targets) for name, targets in bindings.items()}

    def qualified_names(self, expr: ast.expr, *, unbound: bool = True) -> frozenset[str]:
        """Return the qualified names that ``expr`` may refer to.

        Names and attribute chains rooted at a name resolve; other expressions resolve to
        nothing. A name that no import binds stands for itself (a builtin such as
        ``float``, or ``func`` that was not imported) unless ``unbound`` is false, in
        which case it resolves to nothing.
        """
        if isinstance(expr, ast.Name):
            bound = self._bindings.get(expr.id)
            if bound is not None:
                return bound
            return frozenset({expr.id}) if unbound else frozenset()
        if isinstance(expr, ast.Attribute):
            return frozenset(
                f"{name}.{expr.attr}" for name in self.qualified_names(expr.value, unbound=unbound)
            )
        return frozenset()


def iter_references(tree: ast.AST) -> Iterator[ast.Name | ast.Attribute]:
    """Yield every name and attribute chain of ``tree`` that is not part of a longer chain.

    For ``a.b.c`` only the node of the whole chain is yielded, so a check that tests a
    chain and its prefixes reports each reference once. In ``f(a).b`` the names ``f`` and
    ``a`` and the attribute ``f(a).b`` are yielded; the attribute resolves to nothing.
    """
    inner = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name | ast.Attribute) and id(node) not in inner:
            yield node


class StringLiteral(NamedTuple):
    """A string constant starting at ``line``.

    ``bare`` marks a constant that forms a statement by itself, such as a docstring.
    """

    value: str
    line: int
    bare: bool


def iter_string_literals(tree: ast.AST) -> Iterator[StringLiteral]:
    """Yield every string constant of ``tree``, including the constant parts of f-strings.

    Implicitly concatenated literals form one constant, at the line of its first part.
    """
    bare = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield StringLiteral(node.value, node.lineno, id(node) in bare)
