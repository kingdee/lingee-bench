"""Business names of the packs in the engine source (requirement 1.10).

The engine takes entity names, bill names and account codes from the configuration a
business pack declares and from nowhere else. Requirement 1.10 words the check for it: a
string literal of a ``.py`` file below src/erp_mock that is identical to a name declared in
``packs/`` fails, names that are meta-model keywords or primitive names excepted.

What :func:`declared_names` collects from ``packs/`` and the rule each kind is reported
under:

- ``entity-name``: the ``entity`` of a document of ``<pack>/entities/``;
- ``bill-name``: the ``bill`` of such a document - the two keys are what tells the two kinds
  of document apart (:mod:`erp_mock.loader.step4`), so the scan reads the names where they
  are declared and not where they are referenced;
- ``account-code``: any value of any YAML document of a pack written like an accounting
  account code, four or more ASCII digits (:data:`ACCOUNT_CODE_SHAPE`), as ``1403`` and
  ``2202`` of requirement 1.10. The meta model of M0 has no chart of accounts - no
  configuration object declares an account, a balance or a voucher - so the example packs
  declare none and this kind collects nothing
  (:func:`test_the_packs_declare_no_account_code`); the rule is here for the pack that
  brings one.

The two exceptions of requirement 1.10, which the scan never reports:

- :data:`META_MODEL_EXEMPT`: a name that is a meta-model keyword or a primitive name, which
  the engine may write because the word is its own. The requirement names ``org`` and
  ``unit_conversion`` as examples, and the vocabulary an exemption has to come out of is
  derived rather than written down: :func:`meta_model_vocabulary` takes the property names
  and the enumerated values of the JSON Schema of every configuration object
  (:data:`~scripts.gen_schemas.ROOT_MODELS`, generated from the models themselves) together
  with the primitive names (:data:`~erp_mock.config.pack.PRIMITIVES`).
  :func:`test_every_meta_model_exemption_is_a_meta_model_keyword` then keeps a name that is
  no part of that vocabulary off the list, so a business name cannot be exempted by writing
  it here.
- :data:`ENGINE_WORDS`: a word the engine uses for something of its own that a pack happens
  to name an entity after. Such a word is exempt in the one module that owns it and
  reported in every other, which keeps the exemption to the place that needs it. M0 has
  one: ``unit`` is the unit of a date expression (``month(N)``, ``quarter(N)``, ``year(N)``)
  in :mod:`erp_mock.common.date_expr`, and ``unit`` is also the measuring unit entity of
  ``packs/core``.

Known limitations: the comparison is equality of the whole literal, so a docstring or a
message that mentions a name in a sentence is not a hit while a docstring that is nothing
but the name is; a name assembled at runtime (``f"{kind}_order"``) or written in another
case (``"Widget"``) is not seen; and the field names requirement 1.10 also speaks of are
not compared, most of them being standard fields of the meta model.
"""

import textwrap
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final, NamedTuple

import pytest

from erp_mock.config.pack import PRIMITIVES
from erp_mock.loader.step1 import PACK_FILE
from erp_mock.loader.step4 import BILL_KEY, ENTITIES_DIR, ENTITY_KEY
from erp_mock.loader.yaml_io import YamlValue, list_files, load_yaml
from scripts.gen_schemas import ROOT_MODELS, build_schema
from tests.helpers.source_scan import (
    ERP_MOCK_SRC,
    Violation,
    as_triples,
    expected_violations,
    format_violations,
    iter_modules,
    iter_string_literals,
    write_package,
)

# The example business packs of the repository, which the scan compares the engine against.
PACKS_DIR: Final = Path(__file__).resolve().parents[2] / "packs"

# The three kinds of name of requirement 1.10, each the rule a literal of it is reported
# under, and how a violation message calls them.
ENTITY_NAME: Final = "entity-name"
BILL_NAME: Final = "bill-name"
ACCOUNT_CODE: Final = "account-code"
LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {ENTITY_NAME: "entity name", BILL_NAME: "bill name", ACCOUNT_CODE: "account code"}
)

# How an accounting account code is written: four or more ASCII digits, as ``1403`` and
# ``2202`` of requirement 1.10 (a sub-account is longer, ``140301``). Scalars of a pack
# document are matched against this shape, strings and integers alike, so a code written
# ``code: 1403`` counts as well.
ACCOUNT_CODE_DIGITS: Final = 4
ACCOUNT_CODE_SHAPE: Final = f"{ACCOUNT_CODE_DIGITS} or more ASCII digits"

# Names that are meta-model keywords or primitive names, which requirement 1.10 excepts
# everywhere in the engine. Both are declared as entities by ``packs/core``:
# ``org`` is the organization role of an entity (``role: org``) and ``unit_conversion`` is
# both the declaration an entity makes about its base unit and the name of the primitive
# behind it. Every entry has to be part of :func:`meta_model_vocabulary`.
META_MODEL_EXEMPT: Final[frozenset[str]] = frozenset({"org", "unit_conversion"})

# Words the engine uses for something of its own, each exempt in the module that owns the
# word and reported in every other module. ``unit`` is the unit of the date expressions
# ``month(N)``, ``quarter(N)`` and ``year(N)``, which the parser names in the error of a
# malformed expression (:class:`~erp_mock.common.date_expr.Edge`); the measuring unit entity
# of ``packs/core`` is called the same.
ENGINE_WORDS: Final[Mapping[str, str]] = MappingProxyType({"unit": "erp_mock/common/date_expr.py"})

# The entities and the bills the example packs declare, which is what the engine source is
# compared against: the master data of example-p2p.md section 3 and the four bills of its
# sections 4 to 7. A pack that declares a new entity or bill belongs here, so that a
# collection which silently reads nothing cannot pass the scan.
ENTITIES: Final[frozenset[str]] = frozenset(
    {
        # packs/core
        "department",
        "employee",
        "material",
        "org",
        "supplier",
        "unit",
        "unit_conversion",
        "warehouse",
    }
)
BILLS: Final[frozenset[str]] = frozenset(
    {"purchase_requisition", "purchase_order", "purchase_receipt", "stock_adjustment"}
)

# The schema keys that list the values a configuration item may take.
_VALUE_KEYS: Final[frozenset[str]] = frozenset({"enum", "const"})

_NO_PACKS_DIR: Final = "not a packs directory: {path}"


class DeclaredName(NamedTuple):
    """A name a business pack declares, with the rule a literal of it is reported under.

    ``rule`` is :data:`ENTITY_NAME`, :data:`BILL_NAME` or :data:`ACCOUNT_CODE` and ``where``
    the file that declares the name, written ``<pack>/<path in the pack>``.
    """

    name: str
    rule: str
    where: str

    @property
    def label(self) -> str:
        """How a violation message calls this kind of name (:data:`LABELS`)."""
        return LABELS[self.rule]


def declared_names(packs_dir: Path) -> tuple[DeclaredName, ...]:
    """Every entity name, bill name and account code declared below ``packs_dir``, sorted.

    A pack is a directory of ``packs_dir`` that holds a ``pack.yaml``, named after the
    directory the way instance.yaml references it; a directory without that file is no pack
    and is skipped. Names are collected once: the first declaration in path order wins, so
    a code that two documents write gives one entry. Sorted by name, then rule, then file.

    Raises NotADirectoryError at once when ``packs_dir`` is not a directory, so a wrong path
    cannot pass the scan by collecting nothing.
    """
    packs_dir = packs_dir.resolve()
    if not packs_dir.is_dir():
        raise NotADirectoryError(_NO_PACKS_DIR.format(path=packs_dir))
    found: dict[str, DeclaredName] = {}
    for pack_dir in sorted(path for path in packs_dir.iterdir() if path.is_dir()):
        if not (pack_dir / PACK_FILE).is_file():
            continue
        for declared in _pack_names(pack_dir):
            found.setdefault(declared.name, declared)
    return tuple(sorted(found.values()))


def _pack_names(pack_dir: Path) -> list[DeclaredName]:
    """The names the pack at ``pack_dir`` declares, its entity documents first."""
    found = [
        declared
        for path in list_files(pack_dir / ENTITIES_DIR)
        for declared in _entity_or_bill(pack_dir, path)
    ]
    for path in list_files(pack_dir):
        where = _where(pack_dir, path)
        found.extend(
            DeclaredName(code, ACCOUNT_CODE, where)
            for code in sorted(_account_codes(load_yaml(path)))
        )
    return found


def _entity_or_bill(pack_dir: Path, path: Path) -> list[DeclaredName]:
    """The entity or the bill the document ``path`` of ``entities/`` declares.

    The top-level key decides which of the two kinds the document is
    (:mod:`erp_mock.loader.step4`); a document that is no mapping, that names neither key
    or whose value is no name declares nothing, which loading step 4 reports as an error of
    the pack.
    """
    document = load_yaml(path)
    if not isinstance(document, dict):
        return []
    rules = {ENTITY_KEY: ENTITY_NAME, BILL_KEY: BILL_NAME}
    return [
        DeclaredName(name, rule, _where(pack_dir, path))
        for key, rule in rules.items()
        if isinstance(name := document.get(key), str)
    ]


def _account_codes(node: YamlValue) -> set[str]:
    """Every scalar below ``node`` written like an account code (:data:`ACCOUNT_CODE_SHAPE`).

    Keys count as well as values: a document may key the records of a chart of accounts by
    their code.
    """
    if isinstance(node, dict):
        keys = {code for key in node if (code := _as_account_code(key)) is not None}
        return keys | {code for value in node.values() for code in _account_codes(value)}
    if isinstance(node, list):
        return {code for item in node for code in _account_codes(item)}
    found = _as_account_code(node)
    return set() if found is None else {found}


def _as_account_code(value: YamlValue) -> str | None:
    """``value`` as an account code, or None when it is not written like one."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None  # a bool is an int; a float, a date and None are no code
    text = str(value)
    return text if len(text) >= ACCOUNT_CODE_DIGITS and text.isascii() and text.isdigit() else None


def _where(pack_dir: Path, path: Path) -> str:
    """The file ``path`` as a violation names it: ``<pack>/<path in the pack>``."""
    return "/".join((pack_dir.name, *path.relative_to(pack_dir).parts))


def check_business_names(root: Path, names: Iterable[DeclaredName]) -> list[Violation]:
    """Return the literals of the package below ``root`` that are business names, sorted.

    ``names`` is what a packs directory declares (:func:`declared_names`); a name of
    :data:`META_MODEL_EXEMPT` is left out of the comparison altogether and a word of
    :data:`ENGINE_WORDS` is left out in the module that owns it. Every string constant is
    compared, docstrings and the constant parts of f-strings included
    (:func:`~tests.helpers.source_scan.iter_string_literals`), and the whole constant has to
    be the name.
    """
    declared = {item.name: item for item in names if item.name not in META_MODEL_EXEMPT}
    violations: list[Violation] = []
    for module in iter_modules(root):
        for literal in iter_string_literals(module.tree):
            found = declared.get(literal.value)
            if found is None or ENGINE_WORDS.get(found.name) == module.rel_path:
                continue
            message = (
                f"string literal {literal.value!r} is the {found.label} declared in"
                f" {found.where}; the engine takes business names from the configuration"
            )
            violations.append(Violation(module.rel_path, literal.line, found.rule, message))
    return sorted(violations)


def meta_model_vocabulary() -> frozenset[str]:
    """Every keyword of the meta model and every primitive name (requirement 1.10).

    The property names and the enumerated values of the JSON Schema of each kind of
    configuration document, which is generated from the model that defines it so the
    vocabulary cannot drift from it, together with the names of the engine primitives. A
    word a pack writes to declare something is in here; a name a pack invents is not.
    """
    found: set[str] = set(PRIMITIVES)
    for kind in ROOT_MODELS:
        _schema_words(build_schema(kind), found)
    return frozenset(found)


def _schema_words(node: object, found: set[str]) -> None:
    """Add the property names and the enumerated values of the schema ``node`` to ``found``."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "properties" and isinstance(value, Mapping):
                found.update(name for name in value if isinstance(name, str))
            elif key in _VALUE_KEYS:
                values = value if isinstance(value, list) else [value]
                found.update(item for item in values if isinstance(item, str))
            _schema_words(value, found)
    elif isinstance(node, list):
        for item in node:
            _schema_words(item, found)


def write_packs(
    base: Path, packs: Mapping[str, Mapping[str, str]], *, manifest: bool = True
) -> Path:
    """Write the packs ``packs`` below ``base`` and return the packs directory.

    ``packs`` maps a pack name to the files of that pack, by path relative to the pack
    directory; each text is dedented and loses one leading newline, so a triple-quoted
    sample reads like the file it stands for. Every pack gets a ``pack.yaml``, which is what
    makes a directory a pack (:func:`declared_names`); ``manifest`` false leaves it out, so
    the directory is written the same way and is no pack. Called more than once with the
    same ``base``, every call writes into the same packs directory.
    """
    root = base / "packs"
    root.mkdir(parents=True, exist_ok=True)
    for name, files in packs.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        if manifest:
            (root / name / PACK_FILE).write_text(f"pack: {name}\n", encoding="utf-8")
        for rel_path, text in files.items():
            path = root / name / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text).removeprefix("\n"), encoding="utf-8")
    return root


# --- the engine source and the example packs ---------------------------------------------


@pytest.mark.req("1.10")
def test_source_tree_holds_no_business_name() -> None:
    violations = check_business_names(ERP_MOCK_SRC, declared_names(PACKS_DIR))
    assert violations == [], format_violations(violations)


@pytest.mark.req("1.10")
def test_the_packs_declare_the_names_the_scan_compares_against() -> None:
    found = declared_names(PACKS_DIR)
    assert {item.name for item in found if item.rule == ENTITY_NAME} == ENTITIES
    assert {item.name for item in found if item.rule == BILL_NAME} == BILLS
    # Every name was read where it is declared: a document of the pack's ``entities/``.
    for item in found:
        assert f"/{ENTITIES_DIR}/" in item.where and item.where.endswith(".yaml"), item


@pytest.mark.req("1.10")
def test_the_packs_declare_no_account_code() -> None:
    # The meta model of M0 has no chart of accounts, so no pack declares an account code
    # (module docstring). The scan still collects them, for the pack that brings one.
    found = [item for item in declared_names(PACKS_DIR) if item.rule == ACCOUNT_CODE]
    assert found == [], f"values of {ACCOUNT_CODE_SHAPE} declared by a pack: {found}"


# --- the exemptions ----------------------------------------------------------------------


@pytest.mark.req("1.10")
def test_every_meta_model_exemption_is_a_meta_model_keyword() -> None:
    vocabulary = meta_model_vocabulary()
    unknown = sorted(META_MODEL_EXEMPT - vocabulary)
    assert unknown == [], f"not a meta-model keyword or primitive name: {unknown}"
    # The two names of requirement 1.10 are the ones M0 needs, and both are needed: a pack
    # of the repository declares each of them as an entity.
    assert META_MODEL_EXEMPT == frozenset({"org", "unit_conversion"})
    assert META_MODEL_EXEMPT <= ENTITIES | BILLS


@pytest.mark.req("1.10")
def test_every_engine_word_is_owned_by_the_module_it_is_exempt_in() -> None:
    vocabulary = meta_model_vocabulary()
    modules = {module.rel_path: module for module in iter_modules(ERP_MOCK_SRC)}
    for word, rel_path in ENGINE_WORDS.items():
        # The word is no meta-model keyword - it would be exempt everywhere otherwise - and
        # a pack declares it, so the exemption is not dead weight.
        assert word not in vocabulary, f"{word} belongs in META_MODEL_EXEMPT"
        assert word in ENTITIES | BILLS, f"no pack declares {word}"
        # The module that owns the word is there and still writes it.
        module = modules.get(rel_path)
        assert module is not None, f"{rel_path} is no module of the engine"
        literals = {literal.value for literal in iter_string_literals(module.tree)}
        assert word in literals, f"{rel_path} no longer writes {word!r}"


# --- the scan itself ---------------------------------------------------------------------


# A packs directory with one entity, one bill and a chart of accounts, and beside it
# ``notes``, a directory without a pack.yaml: it is no pack, so the entity it writes is not
# collected and the engine may hold that literal.
SAMPLE_PACKS: Final[Mapping[str, Mapping[str, str]]] = {
    "widgets": {
        "entities/widget.yaml": "entity: widget\n",
        "entities/widget_order.yaml": "bill: widget_order\n",
        "entities/ledger_account.yaml": "entity: ledger_account\n",
        "seed/shared/01-accounts.yaml": """
            entities:
              ledger_account:
                - { code: "1403", name: 原材料 }
                - { code: 2202, name: 应付账款 }
                - { code: NOTACODE, name: 不是科目编码 }
            """,
    }
}
NOT_A_PACK: Final[Mapping[str, Mapping[str, str]]] = {
    "notes": {"entities/sketch.yaml": "entity: sketch\n"}
}

SAMPLE: Final = '''
    """Widgets and the account 1403: prose that names them is no literal of them."""

    from typing import Final

    KIND: Final = "widget"  # expect: entity-name
    BILL: Final = "widget_order"  # expect: bill-name
    ACCOUNT: Final = "1403"  # expect: account-code
    PAYABLE: Final = "2202"  # expect: account-code
    FIELD: Final = "widget_id"
    PREFIX: Final = "widget_"
    OTHER: Final = "Widget"
    SKETCH: Final = "sketch"
    CODE: Final = "NOTACODE"
    TABLE: Final = f"{KIND}_rows"
    JOINED: Final = (
        "widget"  # expect: bill-name
        "_order"
    )


    def kind() -> str:
        """widget"""  # expect: entity-name
        return KIND
    '''


@pytest.mark.req("1.10")
def test_business_names_in_literals_are_reported(tmp_path: Path) -> None:
    packs_dir = write_packs(tmp_path, SAMPLE_PACKS)
    write_packs(tmp_path, NOT_A_PACK, manifest=False)
    root = write_package(tmp_path, {"domain/widgets.py": SAMPLE})
    expected = expected_violations(root)
    assert expected, "the sample must mark at least one violation"
    assert as_triples(check_business_names(root, declared_names(packs_dir))) == expected


EXEMPT_PACKS: Final[Mapping[str, Mapping[str, str]]] = {
    "widgets": {
        "entities/org.yaml": "entity: org\n",
        "entities/unit.yaml": "entity: unit\n",
        "entities/unit_conversion.yaml": "entity: unit_conversion\n",
        "entities/widget.yaml": "entity: widget\n",
    }
}

# The same four literals in the module that owns the word ``unit`` and in another one.
EXEMPT_SAMPLE: Final[Mapping[str, str]] = {
    "common/date_expr.py": """
        from typing import Final

        UNIT: Final = "unit"
        ROLE: Final = "org"
        DECLARED: Final = "unit_conversion"
        OTHER: Final = "widget"  # expect: entity-name
        """,
    "domain/engine.py": """
        from typing import Final

        UNIT: Final = "unit"  # expect: entity-name
        ROLE: Final = "org"
        DECLARED: Final = "unit_conversion"
        """,
}


@pytest.mark.req("1.10")
def test_meta_model_keywords_and_engine_words_are_excepted(tmp_path: Path) -> None:
    packs_dir = write_packs(tmp_path, EXEMPT_PACKS)
    root = write_package(tmp_path, EXEMPT_SAMPLE)
    expected = expected_violations(root)
    assert expected, "the sample must mark at least one violation"
    assert as_triples(check_business_names(root, declared_names(packs_dir))) == expected


@pytest.mark.req("1.10")
def test_missing_roots_are_an_error(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        declared_names(tmp_path / "nowhere")
    # An empty packs directory declares nothing, and a missing package root is an error of
    # its own (:func:`~tests.helpers.source_scan.iter_modules`).
    empty = declared_names(write_packs(tmp_path, {}))
    assert empty == ()
    with pytest.raises(NotADirectoryError):
        check_business_names(tmp_path / "erp_mock", empty)
