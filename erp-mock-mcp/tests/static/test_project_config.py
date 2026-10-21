"""Static checks of the dependency and interpreter settings in pyproject.toml."""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

# "<name>==<version>" without extras, markers or ranges.
EXACT_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][0-9A-Za-z.]*")


def _pyproject() -> dict[str, Any]:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


@pytest.mark.req("25.8")
def test_requires_python_is_312() -> None:
    assert _pyproject()["project"]["requires-python"] == ">=3.12,<3.13"


@pytest.mark.req("25.8")
def test_all_declared_requirements_are_exact_pins() -> None:
    data = _pyproject()
    declared: list[str] = [
        *data["build-system"]["requires"],
        *data["project"]["dependencies"],
        *(req for group in data["dependency-groups"].values() for req in group),
    ]
    assert declared
    assert [req for req in declared if not EXACT_PIN.fullmatch(req)] == []
