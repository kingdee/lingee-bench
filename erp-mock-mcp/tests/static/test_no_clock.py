"""System clock, randomness and floats outside the code allowed to use them.

Only gateway and cli read the system clock (requirement 24.7) and draw system randomness
(requirement 24.6); the checks cover every other module, the package root included.

check_clock rules:

- ``clock-import``: importing time in any form, dynamic imports of a literal name
  included.
- ``clock-call``: referring to ``datetime.datetime.now``, ``utcnow`` or ``today`` or to
  ``datetime.date.today`` through any import alias, called or not.
- ``sql-clock``: SQL that reads the current time. String literals other than docstrings
  that contain ``'now'`` or ``"now"``, consist of ``now`` alone (a bound parameter),
  contain CURRENT_TIMESTAMP, CURRENT_DATE or CURRENT_TIME, or call a SQLite date
  function without a time value (``date()``, ``time()``, ``datetime()``,
  ``julianday()``, ``unixepoch()``, or ``strftime`` with only a format), all
  case-insensitive; SQLAlchemy ``func.now``, ``func.current_*``, ``func.localtimestamp``
  and the like, ``func.date()`` and the other SQLite functions without a time value, and
  imports of those functions from sqlalchemy.

check_randomness rules:

- ``random-function``: importing or referring to any name of the random module other
  than ``Random`` (``random.SystemRandom`` and star imports included), or to the module
  object itself.
- ``random-source``: importing uuid or secrets, referring to ``os.urandom`` or
  ``os.getrandom``, or creating ``random.Random()`` without a seed.

check_floats covers domain only (engine numbers are Decimal, requirement 4.7):

- ``float-annotation``: float in a parameter, return or variable annotation, a type alias
  (``type X = ...`` or ``X: TypeAlias = ...``), a TypeVar bound or constraint or a type
  parameter bound, also inside string annotations; ``Literal[...]`` values do not count.
- ``float-call``: calling ``float`` or one of its methods, such as ``float.fromhex``.
- ``float-literal``: float and complex literals.

Known limitations: receivers that do not resolve through imports are not seen, such as
``d.now()`` on a datetime instance ``d`` or ``rng.seed()``; a dynamic import is seen only
when the module name is a string literal; ``isinstance(x, float)`` is allowed.
"""

import ast
import re
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Final

import pytest

from tests.helpers.source_scan import (
    ERP_MOCK_SRC,
    NameResolver,
    SourceModule,
    Violation,
    as_triples,
    expected_violations,
    format_violations,
    iter_imports,
    iter_modules,
    iter_references,
    iter_string_literals,
    write_package,
)

# The only subpackages that read the system clock and draw system randomness.
EXEMPT: Final = frozenset({"gateway", "cli"})

# datetime functions that read the system clock.
CLOCK_FUNCTIONS: Final = frozenset(
    {
        "datetime.date.today",
        "datetime.datetime.now",
        "datetime.datetime.today",
        "datetime.datetime.utcnow",
    }
)

# SQL functions that return the current date or time whatever their arguments.
SQL_CLOCK_FUNCTIONS: Final = frozenset(
    {
        "current_date",
        "current_time",
        "current_timestamp",
        "localtime",
        "localtimestamp",
        "now",
        "sysdate",
    }
)

# SQLite date functions that use the current time when called without a time value;
# strftime takes a format before the time value.
SQLITE_DATE_FUNCTIONS: Final = frozenset({"date", "datetime", "julianday", "time", "unixepoch"})

_SQL_CLOCK_TEXT: Final = re.compile(
    r"""
    (['"])now\1                                                 # the time value 'now'
    | \bcurrent_(?:timestamp|date|time)\b                       # current time keywords
    | \b(?:date|time|datetime|julianday|unixepoch)\s*\(\s*\)    # no time value
    | \bstrftime\s*\(\s*(?:'[^']*'|"[^"]*")\s*\)                # a format only
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Modules whose every name is a source of system randomness.
RANDOM_MODULES: Final = frozenset({"secrets", "uuid"})
OS_RANDOM: Final = frozenset({"os.getrandom", "os.urandom"})

FLOAT_TYPES: Final = frozenset({"builtins.float", "float"})
_LITERAL_FORMS: Final = frozenset({"Literal", "typing.Literal", "typing_extensions.Literal"})
_TYPE_ALIAS_FORMS: Final = frozenset(
    {"TypeAlias", "typing.TypeAlias", "typing_extensions.TypeAlias"}
)
_TYPEVAR_FORMS: Final = frozenset({"TypeVar", "typing.TypeVar", "typing_extensions.TypeVar"})


def check_clock(root: Path) -> list[Violation]:
    """Return the clock reads outside gateway and cli below ``root``, sorted."""
    return sorted(v for module in _checked_modules(root) for v in _clock_violations(module))


def check_randomness(root: Path) -> list[Violation]:
    """Return the system randomness outside gateway and cli below ``root``, sorted."""
    return sorted(v for module in _checked_modules(root) for v in _random_violations(module))


def check_floats(root: Path) -> list[Violation]:
    """Return the floats in the domain subpackage below ``root``, sorted."""
    return sorted(
        violation
        for module in iter_modules(root)
        if module.subpackage == "domain"
        for violation in _float_violations(module)
    )


def _checked_modules(root: Path) -> Iterator[SourceModule]:
    return (module for module in iter_modules(root) if module.subpackage not in EXEMPT)


def _prefixes(name: str) -> list[str]:
    """``a.b.c`` -> ``a``, ``a.b``, ``a.b.c``."""
    parts = name.split(".")
    return [".".join(parts[:end]) for end in range(1, len(parts) + 1)]


def _first_match(names: Iterable[str], matches: Callable[[str], bool]) -> str | None:
    """Return the least of ``names`` and their prefixes that ``matches``, if any."""
    return min((p for name in names for p in _prefixes(name) if matches(p)), default=None)


def _is_member(name: str, module: str) -> bool:
    """Whether ``name`` is the module ``module`` or a name inside it."""
    return name == module or name.startswith(f"{module}.")


# --- clock -------------------------------------------------------------------------


def _clock_violations(module: SourceModule) -> Iterator[Violation]:
    resolver = NameResolver(module)
    for ref in iter_imports(module):
        if _is_member(ref.target, "time"):
            message = f"imports {ref.target}; only gateway and cli read the system clock"
            yield Violation(module.rel_path, ref.line, "clock-import", message)
        elif _is_sql_clock_function(ref.target):
            message = f"imports {ref.target}, which reads the current time in SQL"
            yield Violation(module.rel_path, ref.line, "sql-clock", message)
    for reference in iter_references(module.tree):
        names = resolver.qualified_names(reference)
        clock = _first_match(names, CLOCK_FUNCTIONS.__contains__)
        if clock is not None:
            message = f"refers to {clock}; only gateway and cli read the system clock"
            yield Violation(module.rel_path, reference.lineno, "clock-call", message)
        sql = _first_match(names, _is_sql_clock_function)
        if sql is not None:
            message = f"refers to {sql}, which reads the current time in SQL"
            yield Violation(module.rel_path, reference.lineno, "sql-clock", message)
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Call):
            function = _sqlite_clock_call(resolver, node)
            if function is not None:
                message = f"calls {function} without a time value, which means now"
                yield Violation(module.rel_path, node.lineno, "sql-clock", message)
    for literal in iter_string_literals(module.tree):
        found = None if literal.bare else _sql_clock_text(literal.value)
        if found is not None:
            message = f"SQL text reads the current time: {found!r}"
            yield Violation(module.rel_path, literal.line, "sql-clock", message)


def _is_sql_clock_function(name: str) -> bool:
    """Whether ``name`` is ``func.now``, ``sqlalchemy.sql.functions.now`` or the like."""
    *path, last = name.split(".")
    return (
        last.casefold() in SQL_CLOCK_FUNCTIONS
        and bool(path)
        and (path[-1] == "func" or path[0] == "sqlalchemy")
    )


def _sqlite_clock_call(resolver: NameResolver, call: ast.Call) -> str | None:
    """Return ``func.<name>`` if ``call`` is a SQLite date function without a time value."""
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return None
    for name in sorted(resolver.qualified_names(call.func)):
        *path, last = name.split(".")
        if not path or path[-1] != "func":
            continue
        function = last.casefold()
        if (function in SQLITE_DATE_FUNCTIONS and not call.args) or (
            function == "strftime" and len(call.args) == 1
        ):
            return name
    return None


def _sql_clock_text(text: str) -> str | None:
    """Return the part of ``text`` that reads the current time in SQL, if any."""
    if text.strip().casefold() == "now":
        return text
    match = _SQL_CLOCK_TEXT.search(text)
    return None if match is None else match.group(0)


# --- randomness --------------------------------------------------------------------


def _random_violations(module: SourceModule) -> Iterator[Violation]:
    resolver = NameResolver(module)
    for ref in iter_imports(module):
        if ref.target.partition(".")[0] in RANDOM_MODULES:
            message = f"imports {ref.target}; data randomness comes only from random_seed"
            yield Violation(module.rel_path, ref.line, "random-source", message)
        elif ref.target in OS_RANDOM or ref.target == "os.*":
            message = f"imports {ref.target}; data randomness comes only from random_seed"
            yield Violation(module.rel_path, ref.line, "random-source", message)
        elif ref.target != "random" and _is_random_function(ref.target):
            # "import random" is allowed for random.Random; "from random import *" is not.
            message = f"imports {ref.target}; use a seeded random.Random instance"
            yield Violation(module.rel_path, ref.line, "random-function", message)
    for reference in iter_references(module.tree):
        # Only imported names count: an unbound "random" is a local variable.
        names = resolver.qualified_names(reference, unbound=False)
        source = _first_match(names, OS_RANDOM.__contains__)
        if source is not None:
            message = f"refers to {source}; data randomness comes only from random_seed"
            yield Violation(module.rel_path, reference.lineno, "random-source", message)
        function = min((name for name in names if _is_random_function(name)), default=None)
        if function is not None:
            message = f"refers to {function}; use a seeded random.Random instance"
            yield Violation(module.rel_path, reference.lineno, "random-function", message)
    for node in ast.walk(module.tree):
        if (
            isinstance(node, ast.Call)
            and "random.Random" in resolver.qualified_names(node.func, unbound=False)
            and _unseeded(node)
        ):
            message = "random.Random() without a seed is seeded from the operating system"
            yield Violation(module.rel_path, node.lineno, "random-source", message)


def _is_random_function(name: str) -> bool:
    """Whether ``name`` is the random module or a name in it other than ``Random``."""
    return _is_member(name, "random") and not _is_member(name, "random.Random")


def _unseeded(call: ast.Call) -> bool:
    """Whether the ``random.Random`` construction ``call`` passes no seed (or None)."""
    if any(keyword.arg is None for keyword in call.keywords):
        return False  # **kwargs may hold the seed
    if call.args:
        seed: ast.expr | None = call.args[0]
    else:
        seed = next((kw.value for kw in call.keywords if kw.arg == "x"), None)
    return seed is None or (isinstance(seed, ast.Constant) and seed.value is None)


# --- floats ------------------------------------------------------------------------


def _float_violations(module: SourceModule) -> Iterator[Violation]:
    resolver = NameResolver(module)
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float | complex):
            message = f"{type(node.value).__name__} literal {node.value!r}; use Decimal"
            yield Violation(module.rel_path, node.lineno, "float-literal", message)
        elif isinstance(node, ast.Call) and _first_match(
            resolver.qualified_names(node.func), FLOAT_TYPES.__contains__
        ):
            message = f"calls {ast.unparse(node.func)}; use Decimal"
            yield Violation(module.rel_path, node.lineno, "float-call", message)
    for annotation, what in _iter_type_expressions(resolver, module.tree):
        if _mentions_float(resolver, annotation):
            message = f"{what} uses float; use Decimal"
            yield Violation(module.rel_path, annotation.lineno, "float-annotation", message)


def _iter_type_expressions(resolver: NameResolver, tree: ast.AST) -> Iterator[tuple[ast.expr, str]]:
    """Yield each type expression of ``tree`` with a description of where it is."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            for arg in [
                *arguments.posonlyargs,
                *arguments.args,
                arguments.vararg,
                *arguments.kwonlyargs,
                arguments.kwarg,
            ]:
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation, f"annotation of parameter {arg.arg} of {node.name}()"
            if node.returns is not None:
                yield node.returns, f"return annotation of {node.name}()"
        elif isinstance(node, ast.AnnAssign):
            target = ast.unparse(node.target)
            yield node.annotation, f"annotation of {target}"
            if node.value is not None and _is_form(resolver, node.annotation, _TYPE_ALIAS_FORMS):
                yield node.value, f"type alias {target}"
        elif isinstance(node, ast.TypeAlias):
            yield node.value, f"type alias {ast.unparse(node.name)}"
        elif isinstance(node, ast.TypeVar) and node.bound is not None:
            yield node.bound, f"bound of type parameter {node.name}"
        elif isinstance(node, ast.Call) and _is_form(resolver, node.func, _TYPEVAR_FORMS):
            for constraint in node.args[1:]:
                yield constraint, "TypeVar constraint"
            for keyword in node.keywords:
                if keyword.arg == "bound":
                    yield keyword.value, "TypeVar bound"


def _mentions_float(resolver: NameResolver, expr: ast.expr) -> bool:
    """Whether the type expression ``expr`` names float, inside string annotations too."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        try:
            parsed = ast.parse(expr.value.strip(), mode="eval")
        except SyntaxError:
            return False
        return _mentions_float(resolver, parsed.body)
    if isinstance(expr, ast.Name | ast.Attribute):
        return _is_form(resolver, expr, FLOAT_TYPES)
    if isinstance(expr, ast.Subscript) and _is_form(resolver, expr.value, _LITERAL_FORMS):
        return False  # Literal[...] lists values, not types
    return any(
        _mentions_float(resolver, child)
        for child in ast.iter_child_nodes(expr)
        if isinstance(child, ast.expr)
    )


def _is_form(resolver: NameResolver, expr: ast.expr, forms: frozenset[str]) -> bool:
    """Whether ``expr`` refers to one of the qualified names ``forms``."""
    return not resolver.qualified_names(expr).isdisjoint(forms)


# --- tests -------------------------------------------------------------------------


@pytest.mark.req("24.7")
def test_source_tree_does_not_read_the_clock() -> None:
    violations = check_clock(ERP_MOCK_SRC)
    assert violations == [], format_violations(violations)


@pytest.mark.req("24.6")
def test_source_tree_draws_no_system_randomness() -> None:
    violations = check_randomness(ERP_MOCK_SRC)
    assert violations == [], format_violations(violations)


@pytest.mark.req("4.7")
def test_domain_has_no_floats() -> None:
    violations = check_floats(ERP_MOCK_SRC)
    assert violations == [], format_violations(violations)


def _assert_reports_marked_lines(check: Callable[[Path], list[Violation]], root: Path) -> None:
    expected = expected_violations(root)
    assert expected, "the sample must mark at least one violation"
    assert as_triples(check(root)) == expected


CLOCK_SAMPLE: Final = """
    import datetime
    import datetime as dt
    import importlib
    from datetime import date, timedelta
    from datetime import datetime as DateTime
    from typing import TYPE_CHECKING

    import time as clock  # expect: clock-import
    from time import monotonic  # expect: clock-import
    from time import *  # expect: clock-import

    if TYPE_CHECKING:
        import time  # expect: clock-import

    ANCHOR = date(2024, 1, 31) + timedelta(days=1)
    PARSED = DateTime.fromisoformat("2024-01-31T08:00:00")
    MIDNIGHT = datetime.time(0, 0)


    def stamps() -> list[object]:
        importlib.import_module("time")  # expect: clock-import
        __import__("time.monotonic")  # expect: clock-import
        return [
            datetime.datetime.now(),  # expect: clock-call
            dt.datetime.utcnow(),  # expect: clock-call
            DateTime.today(),  # expect: clock-call
            date.today(),  # expect: clock-call
            dt.date.today,  # expect: clock-call
            PARSED.date(),
        ]
    """


@pytest.mark.req("24.7")
def test_clock_imports_and_calls_are_reported(tmp_path: Path) -> None:
    root = write_package(tmp_path, {"tools/stamps.py": CLOCK_SAMPLE})
    _assert_reports_marked_lines(check_clock, root)


SQL_SAMPLE: Final = '''
    """Queries; a docstring may mention date('now') and CURRENT_TIMESTAMP."""

    from sqlalchemy import func, text
    from sqlalchemy.sql import functions
    from sqlalchemy.sql.functions import now  # expect: sql-clock

    DUE = text("SELECT date(:anchor, '+30 days')")
    YEAR = text("SELECT strftime('%Y', :anchor)")
    TABLE = "current_dates"
    TODAY = text("SELECT date('now')")  # expect: sql-clock
    LOCAL = text('SELECT datetime("NOW")')  # expect: sql-clock
    STAMP = "INSERT INTO t (at) VALUES (CURRENT_TIMESTAMP)"  # expect: sql-clock
    DAY = "select current_date"  # expect: sql-clock
    CLOCK = "SELECT CURRENT_TIME"  # expect: sql-clock
    BARE = "SELECT date(), time ( )"  # expect: sql-clock
    EPOCH = "SELECT unixepoch()"  # expect: sql-clock
    FORMAT = "SELECT strftime('%Y-%m-%d')"  # expect: sql-clock
    PARAMS = {"anchor": "now"}  # expect: sql-clock
    JOINED = f"SELECT julianday() FROM {TABLE}"  # expect: sql-clock


    def columns() -> list[object]:
        """Docstrings are not SQL: julianday() and 'now' may appear here."""
        return [
            func.now(),  # expect: sql-clock
            func.current_timestamp(),  # expect: sql-clock
            functions.localtimestamp(),  # expect: sql-clock
            now(),  # expect: sql-clock
            func.date(),  # expect: sql-clock
            func.strftime("%Y"),  # expect: sql-clock
            func.date(func.coalesce(None, "2024-01-31")),
            func.strftime("%Y", "2024-01-31"),
            func.count(),
        ]
    '''


@pytest.mark.req("24.7")
def test_sql_reading_the_clock_is_reported(tmp_path: Path) -> None:
    root = write_package(tmp_path, {"state/queries.py": SQL_SAMPLE})
    _assert_reports_marked_lines(check_clock, root)


RANDOM_SAMPLE: Final = """
    import os
    import random
    import random as rnd
    from random import Random
    from random import choice, shuffle as mix  # expect: random-function, random-function
    from random import *  # expect: random-function
    import uuid  # expect: random-source
    from secrets import token_hex  # expect: random-source
    from os import urandom  # expect: random-source

    RNG = random.Random(20240131)
    OTHER = Random(7)
    PATH = os.path.join("a", "b")


    def draw(rng: random.Random) -> list[object]:
        __import__("secrets")  # expect: random-source
        return [
            rng.random(),
            random.random(),  # expect: random-function
            rnd.randint(1, 6),  # expect: random-function
            random.SystemRandom(),  # expect: random-function
            choice([1, 2]),  # expect: random-function
            random.Random(),  # expect: random-source
            Random(None),  # expect: random-source
            os.urandom(8),  # expect: random-source
            urandom(8),  # expect: random-source
            random,  # expect: random-function
        ]
    """


@pytest.mark.req("24.6")
def test_system_randomness_is_reported(tmp_path: Path) -> None:
    root = write_package(tmp_path, {"seed/values.py": RANDOM_SAMPLE})
    _assert_reports_marked_lines(check_randomness, root)


@pytest.mark.req("24.6", "24.7")
@pytest.mark.parametrize(
    "rel_path",
    [
        "__init__.py",
        "common/clock.py",
        "config/clock.py",
        "domain/clock.py",
        "loader/clock.py",
        "seed/clock.py",
        "state/clock.py",
        "tools/clock.py",
    ],
)
def test_checks_cover_every_package_but_gateway_and_cli(tmp_path: Path, rel_path: str) -> None:
    source = """
        import time  # expect: clock-import
        import uuid  # expect: random-source
        """
    root = write_package(tmp_path, {rel_path: source})
    assert as_triples(sorted(check_clock(root) + check_randomness(root))) == (
        expected_violations(root)
    )


@pytest.mark.req("24.6", "24.7")
def test_gateway_and_cli_may_use_the_clock_and_system_randomness(tmp_path: Path) -> None:
    source = "\n".join([CLOCK_SAMPLE, SQL_SAMPLE, RANDOM_SAMPLE])
    root = write_package(tmp_path, {"gateway/clock.py": source, "cli/main.py": source})
    assert check_clock(root) == []
    assert check_randomness(root) == []


FLOAT_SAMPLE: Final = """
    import builtins
    from decimal import Decimal
    from typing import Literal, TypeAlias, TypeVar

    Ratio: TypeAlias = float  # expect: float-annotation
    type Amount = Decimal | float  # expect: float-annotation
    Number = TypeVar("Number", bound=float)  # expect: float-annotation
    Scalar = TypeVar("Scalar", int, "float")  # expect: float-annotation
    Kind = Literal["float", "money"]
    rate: float = 1  # expect: float-annotation
    half = 0.5  # expect: float-literal
    big = 1e3  # expect: float-literal
    turn = 2j  # expect: float-literal
    exact = Decimal("0.5")
    count: int = 3


    def scale(x: float, *rest: "list[float]") -> builtins.float:  # expect: float-annotation, float-annotation, float-annotation
        return float(x) + builtins.float("1") + float.fromhex("0x1p-2")  # expect: float-call, float-call, float-call


    def first[T: float](values: list[T]) -> T:  # expect: float-annotation
        return values[0]


    def accepts(value: Decimal, kind: Literal["float", "money"]) -> bool:
        return not isinstance(value, float) and kind in ("float", float.__name__)
    """


@pytest.mark.req("4.7")
def test_floats_in_domain_are_reported(tmp_path: Path) -> None:
    root = write_package(tmp_path, {"domain/money.py": FLOAT_SAMPLE})
    _assert_reports_marked_lines(check_floats, root)


@pytest.mark.req("4.7")
def test_float_check_covers_only_domain(tmp_path: Path) -> None:
    root = write_package(
        tmp_path, {"common/money.py": FLOAT_SAMPLE, "tools/money.py": FLOAT_SAMPLE}
    )
    assert check_floats(root) == []
