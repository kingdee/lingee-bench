"""Import direction between the erp_mock subpackages (requirement 24.8).

Runtime imports of src/erp_mock go only gateway -> tools -> domain -> state. Every import
counts, wherever it appears: in functions, in ``if TYPE_CHECKING:`` blocks, relative
imports, and dynamic imports of a literal module name. Rules:

- ``layer-direction``: state does not import domain, tools or gateway; domain does not
  import tools or gateway; tools does not import gateway; gateway does not import domain
  (requirement 24.8). Following the architecture, domain imports only state, config and
  common, and state only config and common.
- ``leaf-package``: common imports no other erp_mock subpackage and config only common.
  The package root erp_mock/__init__.py imports no subpackage, so every layer may import
  names from the package root (``from erp_mock import FRAMEWORK_VERSION``).
- ``forbidden-library``: gateway, tools and domain do not import sqlite3 or sqlalchemy;
  domain does not import the MCP SDK (mcp), starlette or uvicorn (requirement 24.8).

loader, seed and cli have no rules of their own; tools and gateway import loader's
LoadedInstance.
"""

from collections.abc import Mapping, Set
from pathlib import Path
from typing import Final

import pytest

from tests.helpers.source_scan import (
    ERP_MOCK_SRC,
    ImportRef,
    SourceModule,
    Violation,
    as_triples,
    expected_violations,
    format_violations,
    iter_imports,
    iter_modules,
    write_package,
)

# The subpackages of erp_mock; a new one needs rules here.
SUBPACKAGES: Final = frozenset(
    {"common", "config", "loader", "state", "domain", "tools", "seed", "gateway", "cli"}
)

# Requirement 24.8: subpackages that a layer must not import.
FORBIDDEN_LAYERS: Final[Mapping[str, frozenset[str]]] = {
    "state": frozenset({"domain", "tools", "gateway"}),
    "domain": frozenset({"tools", "gateway"}),
    "tools": frozenset({"gateway"}),
    "gateway": frozenset({"domain"}),
}

# Architecture: the only subpackages that domain and state import.
LAYER_IMPORTS: Final[Mapping[str, frozenset[str]]] = {
    "domain": frozenset({"state", "config", "common"}),
    "state": frozenset({"config", "common"}),
}

# Leaf packages and the package root (None): the only subpackages that they import.
LEAF_IMPORTS: Final[Mapping[str | None, frozenset[str]]] = {
    None: frozenset(),
    "common": frozenset(),
    "config": frozenset({"common"}),
}

# Requirement 24.8: top-level modules of libraries that a layer must not import.
FORBIDDEN_LIBRARIES: Final[Mapping[str, frozenset[str]]] = {
    "gateway": frozenset({"sqlite3", "sqlalchemy"}),
    "tools": frozenset({"sqlite3", "sqlalchemy"}),
    "domain": frozenset({"sqlite3", "sqlalchemy", "mcp", "starlette", "uvicorn"}),
}


def check_layering(root: Path) -> list[Violation]:
    """Return the import direction violations of the package below ``root``, sorted."""
    modules = list(iter_modules(root))
    package = root.resolve().name
    subpackages = SUBPACKAGES | {m.subpackage for m in modules if m.subpackage is not None}
    violations = [
        violation
        for module in modules
        for ref in iter_imports(module)
        if (violation := _check_import(module, ref, package, subpackages)) is not None
    ]
    return sorted(violations)


def _check_import(
    module: SourceModule, ref: ImportRef, package: str, subpackages: Set[str]
) -> Violation | None:
    importer = module.subpackage
    top, _, rest = ref.target.partition(".")
    where = f"{module.name} imports {ref.target}"
    if top != package:
        if importer is not None and top in FORBIDDEN_LIBRARIES.get(importer, frozenset()):
            message = f"{where}: {importer} must not import {top} (requirement 24.8)"
            return Violation(module.rel_path, ref.line, "forbidden-library", message)
        return None
    imported = rest.partition(".")[0]
    if imported not in subpackages or imported == importer:
        # A name defined in the package root, or the importer's own subpackage.
        return None
    if importer is None or importer in LEAF_IMPORTS:
        leaf_allowed = LEAF_IMPORTS[importer]
        if imported in leaf_allowed:
            return None
        who = "the package root" if importer is None else importer
        only = ", ".join(sorted(leaf_allowed)) or "no other subpackage"
        message = f"{where}: {who} may import only {only}"
        return Violation(module.rel_path, ref.line, "leaf-package", message)
    if imported in FORBIDDEN_LAYERS.get(importer, frozenset()):
        message = f"{where}: {importer} must not import {imported} (requirement 24.8)"
        return Violation(module.rel_path, ref.line, "layer-direction", message)
    layer_allowed = LAYER_IMPORTS.get(importer)
    if layer_allowed is not None and imported not in layer_allowed:
        message = f"{where}: {importer} may import only {', '.join(sorted(layer_allowed))}"
        return Violation(module.rel_path, ref.line, "layer-direction", message)
    return None


@pytest.mark.req("24.8")
def test_source_tree_follows_import_direction() -> None:
    violations = check_layering(ERP_MOCK_SRC)
    assert violations == [], format_violations(violations)


@pytest.mark.req("24.8")
def test_rules_know_every_subpackage() -> None:
    found = {m.subpackage for m in iter_modules(ERP_MOCK_SRC) if m.subpackage is not None}
    assert found == SUBPACKAGES, "a new erp_mock subpackage needs layering rules here"


# One violating module per constrained importer; "# expect:" marks each reported line.
VIOLATING_MODULES: Final = {
    "state": (
        "state/store.py",
        """
        import sqlite3

        import sqlalchemy

        from erp_mock import FRAMEWORK_VERSION
        from erp_mock.common import decimals
        from erp_mock.config import model

        from . import schema
        from erp_mock import domain  # expect: layer-direction
        from ..tools import pipeline  # expect: layer-direction
        from erp_mock.loader import instance  # expect: layer-direction


        def build() -> None:
            import erp_mock.gateway.serve  # expect: layer-direction
            from erp_mock.seed import builder  # expect: layer-direction
        """,
    ),
    "domain": (
        "domain/engine.py",
        """
        import importlib
        from typing import TYPE_CHECKING

        from erp_mock import FRAMEWORK_VERSION
        from erp_mock.common.decimals import round_half_up
        from erp_mock.config import model
        from erp_mock.state import store

        from . import primitives
        from .rules import evaluate
        import sqlalchemy.orm  # expect: forbidden-library
        from mcp.server import Server  # expect: forbidden-library
        import starlette.routing, uvicorn  # expect: forbidden-library, forbidden-library
        from erp_mock.loader import instance  # expect: layer-direction
        from erp_mock.seed import builder  # expect: layer-direction

        if TYPE_CHECKING:
            from erp_mock.tools import pipeline  # expect: layer-direction

        try:
            import sqlite3  # expect: forbidden-library
        except ImportError:
            pass


        def serve() -> object:
            return importlib.import_module("erp_mock.gateway")  # expect: layer-direction
        """,
    ),
    "tools": (
        "tools/pipeline.py",
        """
        from erp_mock.common import errors
        from erp_mock.config import model
        from erp_mock.domain import engine
        from erp_mock.loader import LoadedInstance
        from erp_mock.seed import builder
        from erp_mock.state import store
        from erp_mock.gateway import trace  # expect: layer-direction
        from sqlalchemy import func  # expect: forbidden-library


        def run() -> None:
            import sqlite3  # expect: forbidden-library
        """,
    ),
    "gateway": (
        "gateway/serve.py",
        """
        import importlib

        import uvicorn
        from mcp.server import Server
        from starlette.applications import Starlette

        from erp_mock.loader import LoadedInstance
        from erp_mock.state import snapshot
        from erp_mock.tools import pipeline
        from erp_mock.domain import engine  # expect: layer-direction
        import sqlalchemy  # expect: forbidden-library


        def connect() -> object:
            importlib.import_module(".domain.rules", "erp_mock")  # expect: layer-direction
            return __import__("sqlite3")  # expect: forbidden-library
        """,
    ),
    "common": (
        "common/jsonio.py",
        """
        import json

        from erp_mock import META_MODEL_VERSION

        from . import decimals
        from .decimals import to_decimal
        from erp_mock.config import model  # expect: leaf-package
        from .. import state  # expect: leaf-package


        def load() -> None:
            import erp_mock.cli.main  # expect: leaf-package
        """,
    ),
    "config": (
        "config/model.py",
        """
        from pydantic import BaseModel

        from erp_mock import FRAMEWORK_VERSION
        from erp_mock.common.frozen import FrozenMap
        from erp_mock.loader import yaml_io  # expect: leaf-package
        from erp_mock.domain import primitives  # expect: leaf-package
        """,
    ),
    "package root": (
        "__init__.py",
        """
        from typing import Final

        import erp_mock
        from erp_mock.common import decimals  # expect: leaf-package
        from . import tools  # expect: leaf-package

        FRAMEWORK_VERSION: Final = "0.1.0"
        """,
    ),
}


@pytest.mark.req("24.8")
@pytest.mark.parametrize(
    ("rel_path", "source"),
    [pytest.param(*sample, id=importer) for importer, sample in VIOLATING_MODULES.items()],
)
def test_violating_imports_are_reported(tmp_path: Path, rel_path: str, source: str) -> None:
    root = write_package(tmp_path, {rel_path: source})
    expected = expected_violations(root)
    assert expected, "the sample must mark at least one violation"
    assert as_triples(check_layering(root)) == expected


@pytest.mark.req("24.8")
def test_unconstrained_packages_may_import_any_layer(tmp_path: Path) -> None:
    imports_everything = """
        import sqlite3

        import sqlalchemy
        import uvicorn

        from erp_mock.common import decimals
        from erp_mock.config import model
        from erp_mock.domain import engine
        from erp_mock.gateway import serve
        from erp_mock.loader import instance
        from erp_mock.seed import builder
        from erp_mock.state import store
        from erp_mock.tools import pipeline
        """
    root = write_package(
        tmp_path,
        {
            "cli/main.py": imports_everything,
            "loader/instance.py": imports_everything,
            "seed/builder.py": imports_everything,
        },
    )
    assert check_layering(root) == []


@pytest.mark.req("24.8")
def test_missing_root_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        check_layering(tmp_path / "erp_mock")
