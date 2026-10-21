"""Export the JSON Schema of every root configuration model to schemas/ (requirement 1.1).

Run it from the repository root::

    uv run python scripts/gen_schemas.py

Each kind of configuration document has one root model in :mod:`erp_mock.config.pack` or
:mod:`erp_mock.config.instance`; :data:`ROOT_MODELS` maps the name of the kind to its
model, and this script writes ``schemas/<kind>.schema.json`` for each of them. The models
are the only source of truth: a schema is ``model_json_schema(mode="validation")``, the
draft 2020-12 dialect pydantic generates for, with ``$schema`` and ``$id`` put in front of
it. Nothing else is edited in, so a schema cannot say something the model does not. The
checks a JSON Schema cannot express (a calendar date, the decimals of a value, the name of
a ref field, an IANA time zone) stay in the models, which is why loading validates with
the models and not with these files; ``tests/contract/test_schemas.py`` lists the cases
where the model is the stricter of the two. The schemas serve business pack authors, whose
editor uses them for completion and for a first validation of a document.

The output is byte for byte reproducible: pydantic sorts the keys of every object of a
schema except the entries of ``properties`` and of a ``default``, so ``properties`` keeps
the declaration order of the model's fields, and
:func:`~erp_mock.common.jsonio.dumps_pretty` writes insertion order, two-space indentation
and a final newline. ``tests/contract/test_schemas.py`` regenerates every schema and
compares it with the file in the repository, so schemas/ cannot drift from the models.

Writing goes through :func:`write_schemas`, which reports each file on stderr. A
``*.schema.json`` in the directory that no root model produces is reported as stale and
makes the script exit with code 1; it is left in place, so removing it is a decision of
whoever reads the report. stdout is left empty.
"""

import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue

from erp_mock.common.jsonio import dumps_pretty, encode_utf8
from erp_mock.config import instance, pack

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SCHEMAS_DIR: Final = REPO_ROOT / "schemas"

# Suffix of an exported file: ``entity.schema.json``, the name editors match on.
SUFFIX: Final = ".schema.json"

# The dialect of the exported schemas, taken from the generator that writes them, so the
# two cannot disagree: JSON Schema draft 2020-12.
DIALECT: Final = GenerateJsonSchema.schema_dialect

# The root model of each kind of configuration document, in the order of the two tables
# in the module docstrings of erp_mock.config.pack and erp_mock.config.instance: the files
# of a business pack, then the files of an instance configuration. An entity and a bill are
# both written in ``entities/*.yaml`` and told apart by their own key; a conversion, a
# writeback, a linkage and a rule are nested under one key of their file, so the exported
# schema is that of the file (requirement 1.1).
ROOT_MODELS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "pack": pack.PackManifest,
        "entity": pack.EntityDef,
        "bill": pack.BillDef,
        "conversion": pack.ConversionFile,
        "writeback": pack.WritebackFile,
        "linkage": pack.LinkageFile,
        "rule": pack.RuleFile,
        "exposure": pack.ExposureDef,
        "description": pack.DescriptionFile,
        "seed-generators": pack.SeedGeneratorsFile,
        "shared-data": pack.SharedDataFile,
        "instance": instance.InstanceDef,
        "profile": instance.ProfileDef,
        "dataset": instance.DatasetDef,
    }
)


def _say(message: str) -> None:
    """Write one line of this script's own output to stderr."""
    sys.stderr.write(f"==> {message}\n")
    sys.stderr.flush()


def file_name(kind: str) -> str:
    """The file name of the schema of ``kind``: ``entity`` gives ``entity.schema.json``."""
    return f"{kind}{SUFFIX}"


def build_schema(kind: str) -> JsonSchemaValue:
    """The JSON Schema of the root model of ``kind``, with ``$schema`` and ``$id`` first.

    ``$id`` is the file name, a relative URI reference that resolves against wherever the
    file is read from, so the identifier stays correct in a checkout, in a package and
    behind any URL, and does not name a location that has to exist.
    """
    schema = ROOT_MODELS[kind].model_json_schema(mode="validation")
    return {"$schema": DIALECT, "$id": file_name(kind), **schema}


def render_schema(kind: str) -> bytes:
    """The bytes of the schema file of ``kind``: UTF-8, indented by 2, final newline."""
    return encode_utf8(dumps_pretty(build_schema(kind)))


def stale_files(directory: Path) -> tuple[Path, ...]:
    """The ``*.schema.json`` files of ``directory`` that no root model produces, sorted."""
    if not directory.is_dir():
        return ()
    expected = {file_name(kind) for kind in ROOT_MODELS}
    found = (path for path in directory.iterdir() if path.name.endswith(SUFFIX))
    return tuple(sorted((path for path in found if path.name not in expected), key=str))


def write_schemas(directory: Path = SCHEMAS_DIR) -> tuple[Path, ...]:
    """Write the schema of every root model into ``directory``; return the paths written.

    The directory is created when it does not exist. Each file is announced on stderr.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for kind in ROOT_MODELS:
        path = directory / file_name(kind)
        path.write_bytes(render_schema(kind))
        written.append(path)
        _say(f"{path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}")
    return tuple(written)


def main() -> int:
    """Export every schema and return the exit code of the process."""
    written = write_schemas()
    _say(f"exported {len(written)} schemas to {SCHEMAS_DIR.relative_to(REPO_ROOT)}/")
    stale = stale_files(SCHEMAS_DIR)
    if stale:
        for path in stale:
            _say(f"stale schema, no root model produces it: {path.relative_to(REPO_ROOT)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
