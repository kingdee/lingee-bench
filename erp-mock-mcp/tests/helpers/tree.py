"""Instance configurations and business pack trees written into a temporary directory.

The loading steps read directories, so their tests need one. :func:`write_instance` and
:func:`write_pack` write the smallest tree step 1 accepts, an ``instance.yaml`` and one
``pack.yaml`` per referenced pack, and let a test replace, add or drop one key at a time,
the way the config model tests build their documents::

    write_instance(tmp_path / "instance", packs="{ core: 1.0.0 }")
    write_pack(tmp_path / "packs", "core", primitives="[crud, numbering]")

Values are YAML text, not Python objects, so a test writes exactly the document it wants
to validate, an unparseable one included; :func:`write_tree` writes such text to any path
of the tree, which is also how a test writes a pack.yaml whose ``pack`` differs from the
name of its directory.

The other documents of a tree are written the same way, each with the required keys of its
kind and the overrides a test gives: :func:`entity_doc` and :func:`bill_doc` for the two
kinds of ``entities/``, :func:`profile_doc` and :func:`dataset_doc` for the definitions of
an instance configuration, which step 4 reads. They are passed to ``files``::

    write_pack(tmp_path / "packs", "core", files={"entities/supplier.yaml": entity_doc()})
    write_instance(tmp_path / "instance", {"profiles/semantic.yaml": profile_doc()})

The default manifest of :data:`PACK_KEYS` takes the namespaces of a pack named ``core``:
every M0 primitive, so a tree does not have to state the primitives its documents use
(requirement 2.6), and the ID and number prefixes of the default documents of this module
(requirement 2.7). A test that writes several packs, or a document with another prefix,
gives each pack its own ``rule_prefix``, ``id_prefixes``, ``number_prefixes`` and
``tool_prefixes``; a test that runs step 5 also writes :data:`CONTEXT_FILES`, the two
context entities an instance declares exactly one of each of (requirement 1.14)::

    write_pack(tmp_path / "packs", "core", files={**CONTEXT_FILES, "entities/x.yaml": ...})
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

__all__ = [
    "BILL_KEYS",
    "CONTEXT_FILES",
    "DATASET_KEYS",
    "ENTITY_KEYS",
    "INSTANCE_FILE",
    "INSTANCE_KEYS",
    "M0_PRIMITIVES",
    "PACK_FILE",
    "PACK_KEYS",
    "PROFILE_KEYS",
    "bill_doc",
    "dataset_doc",
    "document",
    "entity_doc",
    "instance_doc",
    "pack_doc",
    "profile_doc",
    "write_instance",
    "write_pack",
    "write_tree",
]

INSTANCE_FILE: Final = "instance.yaml"
PACK_FILE: Final = "pack.yaml"

# The required keys of an instance.yaml (business-pack.md section 8), each value written as
# the YAML text it has in the document.
INSTANCE_KEYS: Final[Mapping[str, str]] = {
    "instance": "p2p-demo",
    "version": "1.0.0",
    "title": "{ zh: 采购到付款示例 }",
    "packs": "{ core: 1.0.0 }",
    "profiles": "[semantic]",
    "default_profile": "semantic",
    "datasets": "[purchase-basic]",
    "default_dataset": "purchase-basic",
}

# The nine M0 primitives (:data:`~erp_mock.config.pack.PRIMITIVES`), which the default
# manifest declares so that a document of a tree may use any of them (requirement 2.6).
M0_PRIMITIVES: Final = (
    "[crud, numbering, pricing, unit_conversion, state_machine, conversion, writeback,"
    " inventory, rule_expr]"
)

# The required keys of a pack.yaml (business-pack.md section 4). The two prefix lists hold
# the prefixes of the default entity and bill documents below and of CONTEXT_FILES.
PACK_KEYS: Final[Mapping[str, str]] = {
    "pack": "core",
    "version": "1.0.0",
    "title": "{ zh: 基础 }",
    "requires_meta_model": '">=0.1.0 <1.0.0"',
    "primitives": M0_PRIMITIVES,
    "rule_prefix": "CORE",
    "id_prefixes": "[sup, po, orgu, emp]",
    "number_prefixes": "[CGDD]",
    "tool_prefixes": "[core_]",
    "description_variants": "[zh-detailed]",
    "license": "Apache-2.0",
    "maintainers": '["示例团队"]',
}


# The required keys of an entity document, written in ``entities/`` (meta-model.md 3).
ENTITY_KEYS: Final[Mapping[str, str]] = {
    "entity": "supplier",
    "id_prefix": "sup",
    "fields": "{ name: { type: string, required: true } }",
}

# The required keys of a bill document, written in ``entities/`` too (meta-model.md 4).
BILL_KEYS: Final[Mapping[str, str]] = {
    "bill": "purchase_order",
    "id_prefix": "po",
    "number": "{ prefix: CGDD }",
    "state_machine": "standard",
    "header": "{ supplier_id: { type: ref, ref: supplier, required: true } }",
    "lines": "{ material_id: { type: ref, ref: material, required: true } }",
}

# The required keys of a profile definition (tool-design.md section 8). The values M0 fixes
# are the ones step 4 checks: surface semantic, description_variant zh-detailed and
# include_deprecated false, which is the default of the model (requirement 2.13).
PROFILE_KEYS: Final[Mapping[str, str]] = {
    "profile": "semantic",
    "version": "1.0.0",
    "surface": "semantic",
    "description_variant": "zh-detailed",
    "packs": "[core]",
}

# The required keys of an initial dataset definition (seed-data.md section 2).
DATASET_KEYS: Final[Mapping[str, str]] = {
    "dataset": "purchase-basic",
    "version": "1.0.0",
    "random_seed": "20261009",
}


def document(required: Mapping[str, str], overrides: Mapping[str, str | None]) -> str:
    """A YAML document of ``required``, with ``overrides`` applied.

    An override replaces the value of a required key, adds an optional key, or, when its
    value is None, drops the key, which is how a test leaves out a required one. Keys keep
    the order of ``required``, the added ones following in the order they are given.
    """
    keys = {**required, **overrides}
    return "".join(f"{key}: {value}\n" for key, value in keys.items() if value is not None)


def instance_doc(**overrides: str | None) -> str:
    """An instance.yaml with the required keys, with ``overrides`` applied."""
    return document(INSTANCE_KEYS, overrides)


def pack_doc(**overrides: str | None) -> str:
    """A pack.yaml with the required keys, with ``overrides`` applied."""
    return document(PACK_KEYS, overrides)


def entity_doc(**overrides: str | None) -> str:
    """An entity document with the required keys, with ``overrides`` applied."""
    return document(ENTITY_KEYS, overrides)


def bill_doc(**overrides: str | None) -> str:
    """A bill document with the required keys, with ``overrides`` applied."""
    return document(BILL_KEYS, overrides)


def profile_doc(**overrides: str | None) -> str:
    """A profile definition with the required keys, with ``overrides`` applied."""
    return document(PROFILE_KEYS, overrides)


def dataset_doc(**overrides: str | None) -> str:
    """A dataset definition with the required keys, with ``overrides`` applied."""
    return document(DATASET_KEYS, overrides)


# The two context entities of an instance, as the files of a pack: the organization the
# default organization comes from, with the bool field that marks it, and the user the
# current user is a record of (requirement 1.8). Step 5 requires exactly one of each
# (requirement 1.14), so a tree that runs that step holds both; their ID prefixes are in
# the ``id_prefixes`` of :data:`PACK_KEYS`.
CONTEXT_FILES: Final[Mapping[str, str]] = {
    "entities/org_unit.yaml": entity_doc(
        entity="org_unit",
        id_prefix="orgu",
        context="{ role: org, default_flag: is_default }",
        fields="{ name: { type: string }, is_default: { type: bool } }",
    ),
    "entities/employee.yaml": entity_doc(
        entity="employee",
        id_prefix="emp",
        context="{ role: user }",
        fields="{ name: { type: string } }",
    ),
}


def write_tree(root: Path, files: Mapping[str, str]) -> Path:
    """Write every ``<relative path>: <text>`` of ``files`` below ``root`` as UTF-8.

    Missing directories are created, an existing file is replaced, and ``root`` is
    returned, so a tree can be written in one call::

        write_tree(tmp_path / "packs", {"core/pack.yaml": pack_doc(), "core/x.yaml": "{}"})
    """
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def write_instance(
    instance_dir: Path, files: Mapping[str, str] | None = None, **overrides: str | None
) -> Path:
    """Write an instance.yaml, and ``files``, below ``instance_dir``; return the directory.

    ``overrides`` apply to the document (:func:`instance_doc`) and ``files`` holds the
    other documents of the instance configuration, such as ``profiles/semantic.yaml``.
    """
    return write_tree(instance_dir, {INSTANCE_FILE: instance_doc(**overrides)} | dict(files or {}))


def write_pack(
    packs_dir: Path,
    name: str,
    version: str = "1.0.0",
    files: Mapping[str, str] | None = None,
    **overrides: str | None,
) -> Path:
    """Write the pack ``name`` below ``packs_dir``; return its directory.

    The manifest declares ``name`` and ``version``, as a reference of instance.yaml
    expects; ``overrides`` apply to its other keys (:func:`pack_doc`) and ``files`` holds
    the configuration files of the pack, such as ``entities/supplier.yaml``.
    """
    manifest = pack_doc(pack=name, version=version, **overrides)
    write_tree(packs_dir / name, {PACK_FILE: manifest} | dict(files or {}))
    return packs_dir / name
