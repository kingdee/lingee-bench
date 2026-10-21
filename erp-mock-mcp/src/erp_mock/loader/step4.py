"""Loading step 4: every configuration file against its model (requirements 1.9 to 1.13, 2.13).

The fourth step of design.md 2.3, and the first one that reads more than a manifest. It
reads the configuration files of every business pack instance.yaml references, together
with the profiles and the initial datasets of the instance configuration, and validates
each document against the model of its kind (:mod:`erp_mock.config.pack` and
:mod:`erp_mock.config.instance`), which is the JSON Schema of that kind of document
(requirement 1.11, ``scripts/gen_schemas.py``). The structure of every document is settled
here, so the steps after this one work on validated documents.

A pack is a directory (business-pack.md section 3), and each of its directories holds one
kind of document:

=====================  ==================  =============================
file                   document model       kept in RawPack as
=====================  ==================  =============================
entities/**/*.yaml     EntityDef           ``entities``, key ``entity``
entities/**/*.yaml     BillDef             ``bills``, key ``bill``
conversions/**/*.yaml  ConversionFile      ``conversions``
writebacks/**/*.yaml   WritebackFile       ``writebacks``
linkages/**/*.yaml     LinkageFile         ``linkages``
rules/**/*.yaml        RuleFile            ``rules``
tools/exposure.yaml    ExposureDef         ``exposure``
tools/descriptions/*   DescriptionFile     ``descriptions``
seed/generators.yaml   SeedGeneratorsFile  ``generators``
seed/shared/*.yaml     SharedDataFile      ``shared_data``
=====================  ==================  =============================

An entity and a bill are both written in ``entities/``, so the step tells them apart by the
top-level key of the document, ``entity`` or ``bill``, and validates it against that model;
a document that names neither, or both, is one error of its own. A conversion, a writeback,
a linkage and a rule nest their object under one key, so the ``*File`` model is validated
and the object below it is what :class:`RawPack` keeps, while the errors of the document
keep the config path of the file (``conversion.limit_by``). pack.yaml is not read again:
step 1 validated it and :class:`~erp_mock.loader.step1.PackSource` carries it.

What is not read is as much part of the step: ``seed/examples/`` serves the authors of the
tool descriptions and never reaches the framework (requirement 20.8), ``tests/`` holds the
pack's own tests, and a directory of ``--packs-dir`` that instance.yaml does not reference
takes no part in loading (requirement 2.1) - the step reads the packs step 1 left behind,
so such a directory is never opened. Every file and every directory is optional here: the
step reports what a document says, not which documents a pack ought to have, which is what
the steps after it settle. Directories are walked with
:func:`~erp_mock.loader.yaml_io.list_files`, which sorts by path, so files are read in the
same order on every file system (requirement 24).

On the instance side (requirement 2.13), every name of ``profiles`` and of ``datasets``
must have a definition below ``profiles/`` or ``datasets/``, found by the ``profile`` or
``dataset`` field of the definition and not by the file name, and that definition must fit
:class:`~erp_mock.config.instance.ProfileDef` or
:class:`~erp_mock.config.instance.DatasetDef`. A name without a definition is reported at
its item of instance.yaml (``profiles[0]``), a name declared by two files at the ``profile``
key of each of them, since which of the two would be read is not something to leave to the
order of a directory. M0 serves one tool surface, so ``profiles`` must hold
:data:`SEMANTIC_PROFILE` alone, and each profile definition must declare the surface
:data:`SURFACE`, the description variant :data:`DESCRIPTION_VARIANT` and
``include_deprecated`` false; the models accept the other surface and the other variants,
which is why these three values are checked here and reported at the item of the profile
file that holds them.

Every file is read and every document validated, so one run reports the errors of all of
them (requirement 2.2): a pydantic error becomes one error at its config path
(:func:`~erp_mock.loader.errors.config_path`), a file that cannot be read or parsed one
error at its line, and a document that is skipped for either reason is simply absent from
the result. The step leaves one :class:`RawPack` per pack and the profile and dataset
definitions in the order instance.yaml lists them (:class:`Step4Result`); step 5 resolves
the references between those documents into the domain model, step 6 and 7 read the
exposures and the descriptions, and step 8 builds the datasets.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ValidationError

from erp_mock.config.instance import DatasetDef, InstanceDef, ProfileDef
from erp_mock.config.pack import (
    BillDef,
    ConversionDef,
    ConversionFile,
    DescriptionFile,
    EntityDef,
    ExposureDef,
    InventoryLinkageDef,
    LinkageFile,
    PackManifest,
    RuleFile,
    RuleObject,
    SeedGeneratorsFile,
    SharedDataFile,
    WritebackDef,
    WritebackFile,
)
from erp_mock.loader.errors import Location, StepErrors
from erp_mock.loader.step1 import INSTANCE_FILE, PackSource
from erp_mock.loader.yaml_io import YamlLoadError, YamlValue, list_files, load_yaml

__all__ = [
    "BILL_KEY",
    "CONVERSIONS_DIR",
    "CONVERSION_KEY",
    "DATASETS_DIR",
    "DATASET_KEY",
    "DESCRIPTIONS_DIR",
    "DESCRIPTION_VARIANT",
    "ENTITIES_DIR",
    "ENTITY_KEY",
    "EXPOSURE_FILE",
    "GENERATORS_FILE",
    "LINKAGES_DIR",
    "LINKAGE_KEY",
    "PROFILES_DIR",
    "PROFILE_KEY",
    "RULES_DIR",
    "RULE_KEY",
    "SEMANTIC_PROFILE",
    "SHARED_DIR",
    "SURFACE",
    "WRITEBACKS_DIR",
    "WRITEBACK_KEY",
    "RawDoc",
    "RawPack",
    "Step4Result",
    "check",
]

# The directories and files of a business pack this step reads, relative to the pack
# directory (business-pack.md section 3). ``tests/`` and ``seed/examples/`` are not read.
ENTITIES_DIR: Final = "entities"
CONVERSIONS_DIR: Final = "conversions"
WRITEBACKS_DIR: Final = "writebacks"
LINKAGES_DIR: Final = "linkages"
RULES_DIR: Final = "rules"
EXPOSURE_FILE: Final = "tools/exposure.yaml"
DESCRIPTIONS_DIR: Final = "tools/descriptions"
GENERATORS_FILE: Final = "seed/generators.yaml"
SHARED_DIR: Final = "seed/shared"

# The top-level key that names the object of a document: the two kinds of ``entities/``,
# and the key a conversion, a writeback or a rule document nests its object under, which is
# also the first part of the config path of every error of such a document.
ENTITY_KEY: Final = "entity"
BILL_KEY: Final = "bill"
CONVERSION_KEY: Final = "conversion"
WRITEBACK_KEY: Final = "writeback"
LINKAGE_KEY: Final = "linkage"
RULE_KEY: Final = "rule"

# The directories of an instance configuration and the field a definition names itself
# with; the directory name is also the key of instance.yaml that lists those names
# (``profiles``, ``datasets``), so one constant serves both (business-pack.md section 8).
PROFILES_DIR: Final = "profiles"
DATASETS_DIR: Final = "datasets"
PROFILE_KEY: Final = "profile"
DATASET_KEY: Final = "dataset"

# The one profile of M0 and the three values its definition must declare
# (requirement 2.13); ``include_deprecated`` must be false.
SEMANTIC_PROFILE: Final = "semantic"
SURFACE: Final = "semantic"
DESCRIPTION_VARIANT: Final = "zh-detailed"


@dataclass(frozen=True, slots=True)
class RawDoc[D]:
    """One validated configuration document together with the file it was read from.

    ``file`` is the path of the file relative to the pack directory or, for a profile or a
    dataset, to the instance directory, written with ``/`` as a
    :class:`~erp_mock.loader.errors.Location` needs it (``entities/supplier.yaml``), so a
    later step can report an error at the file that declares the object. ``document`` is
    the object the file declares: the document itself where the model is the object, and
    the object below the document's single key for a conversion, a writeback, a linkage and
    a rule.
    """

    file: str
    document: D


@dataclass(frozen=True, slots=True)
class RawPack:
    """The configuration documents of one business pack, each with its file.

    ``source`` is what step 1 read: the pack as instance.yaml references it, its directory
    and its validated manifest. The other fields hold the documents of the directories of
    the module docstring, in the order the files were read (by path, so the same order
    everywhere). A pack without a directory, or with an empty one, simply has no document
    of that kind, and a document that step 4 reported an error for is absent.
    """

    source: PackSource
    entities: tuple[RawDoc[EntityDef], ...]
    bills: tuple[RawDoc[BillDef], ...]
    conversions: tuple[RawDoc[ConversionDef], ...]
    writebacks: tuple[RawDoc[WritebackDef], ...]
    linkages: tuple[RawDoc[InventoryLinkageDef], ...]
    rules: tuple[RawDoc[RuleObject], ...]
    exposure: RawDoc[ExposureDef] | None
    descriptions: tuple[RawDoc[DescriptionFile], ...]
    generators: RawDoc[SeedGeneratorsFile] | None
    shared_data: tuple[RawDoc[SharedDataFile], ...]

    @property
    def name(self) -> str:
        """The pack name, as instance.yaml references it."""
        return self.source.name

    @property
    def version(self) -> str:
        """The version instance.yaml pins the pack to."""
        return self.source.version

    @property
    def directory(self) -> Path:
        """The pack directory, ``<packs dir>/<name>``."""
        return self.source.directory

    @property
    def manifest(self) -> PackManifest:
        """The validated pack.yaml."""
        return self.source.manifest


@dataclass(frozen=True, slots=True)
class Step4Result:
    """What step 4 leaves for the steps after it.

    ``packs`` are in the order of instance.yaml, one per pack step 1 accepted.
    ``profiles`` and ``datasets`` are the definitions of the names instance.yaml lists, in
    its order and without a repeated name; a name whose definition is missing, ambiguous or
    in error is absent, in which case step 4 also reported an error.
    """

    packs: tuple[RawPack, ...]
    profiles: tuple[RawDoc[ProfileDef], ...]
    datasets: tuple[RawDoc[DatasetDef], ...]


def check(
    instance_dir: Path, instance: InstanceDef, packs: Sequence[PackSource], errors: StepErrors
) -> Step4Result:
    """Run step 4 on the packs of ``packs`` and on the instance configuration.

    ``instance`` is the instance.yaml step 1 validated and ``packs`` the packs it
    references, in its order. Returns the documents that validated; the step passed only
    when ``errors`` stayed empty.
    """
    return Step4Result(
        packs=tuple(_read_pack(source, errors) for source in packs),
        profiles=_read_profiles(instance_dir, instance, errors),
        datasets=_read_definitions(
            instance_dir, DATASETS_DIR, DATASET_KEY, DatasetDef, instance.datasets, errors
        ),
    )


# --- business pack -----------------------------------------------------------------------


def _read_pack(source: PackSource, errors: StepErrors) -> RawPack:
    """Read and validate every configuration file of one business pack."""
    entities, bills = _read_entities(source, errors)
    return RawPack(
        source=source,
        entities=entities,
        bills=bills,
        conversions=_read_objects(
            source, CONVERSIONS_DIR, ConversionFile, lambda file: file.conversion, errors
        ),
        writebacks=_read_objects(
            source, WRITEBACKS_DIR, WritebackFile, lambda file: file.writeback, errors
        ),
        linkages=_read_objects(
            source, LINKAGES_DIR, LinkageFile, lambda file: file.linkage, errors
        ),
        rules=_read_objects(source, RULES_DIR, RuleFile, lambda file: file.rule, errors),
        exposure=_read_single(source, EXPOSURE_FILE, ExposureDef, errors),
        descriptions=_read_objects(source, DESCRIPTIONS_DIR, DescriptionFile, _itself, errors),
        generators=_read_single(source, GENERATORS_FILE, SeedGeneratorsFile, errors),
        shared_data=_read_objects(source, SHARED_DIR, SharedDataFile, _itself, errors),
    )


def _itself[M: BaseModel](document: M) -> M:
    """The document itself, for a file whose model is already the object it declares."""
    return document


def _read_entities(
    source: PackSource, errors: StepErrors
) -> tuple[tuple[RawDoc[EntityDef], ...], tuple[RawDoc[BillDef], ...]]:
    """Read ``entities/``, telling an entity document from a bill document by its key.

    Returns the entities and the bills, each in the order their files were read. A document
    that is no mapping, or that names neither or both of the two keys, is one error at the
    file and gives neither.
    """
    entities: list[RawDoc[EntityDef]] = []
    bills: list[RawDoc[BillDef]] = []
    for file in _files(source.directory, ENTITIES_DIR, source.name, errors):
        try:
            document = load_yaml(source.directory / file)
        except YamlLoadError as error:
            _report_unread(error, source.name, file, errors)
            continue
        match _kind(document, source.name, file, errors):
            case "entity":
                entity = _validate(EntityDef, document, source.name, file, errors)
                if entity is not None:
                    entities.append(RawDoc(file=file, document=entity))
            case "bill":
                bill = _validate(BillDef, document, source.name, file, errors)
                if bill is not None:
                    bills.append(RawDoc(file=file, document=bill))
            case None:
                continue
    return tuple(entities), tuple(bills)


def _kind(
    document: YamlValue, pack: str, file: str, errors: StepErrors
) -> Literal["entity", "bill"] | None:
    """Which of the two kinds of ``entities/`` ``document`` declares, or None with an error.

    The key alone decides; whether its value is a name is left to the model, so a document
    that declares ``entity`` is validated as an entity however that key is written.
    """
    if not isinstance(document, dict):
        errors.add_at(pack, file, None, f"{ENTITIES_DIR}/ 下的配置文件必须是映射")
        return None
    has_entity = ENTITY_KEY in document
    has_bill = BILL_KEY in document
    if has_entity and not has_bill:
        return "entity"
    if has_bill and not has_entity:
        return "bill"
    both = f"不能同时以 {ENTITY_KEY} 和 {BILL_KEY} 为顶层键"
    missing = f"必须以 {ENTITY_KEY} 或 {BILL_KEY} 为顶层键声明实体或单据"
    errors.add_at(
        pack, file, None, f"{ENTITIES_DIR}/ 下的配置文件{both if has_entity else missing}"
    )
    return None


def _read_objects[M: BaseModel, D](
    source: PackSource,
    directory: str,
    model: type[M],
    select: Callable[[M], D],
    errors: StepErrors,
) -> tuple[RawDoc[D], ...]:
    """Read every YAML file below ``directory`` of the pack and validate it against ``model``.

    ``select`` takes the declared object out of the validated document: the document itself,
    or the object below its single key.
    """
    found: list[RawDoc[D]] = []
    for file in _files(source.directory, directory, source.name, errors):
        document = _read(model, source.directory / file, source.name, file, errors)
        if document is not None:
            found.append(RawDoc(file=file, document=select(document)))
    return tuple(found)


def _read_single[M: BaseModel](
    source: PackSource, file: str, model: type[M], errors: StepErrors
) -> RawDoc[M] | None:
    """Read the one file ``file`` of the pack, or None when the pack does not have it."""
    path = source.directory / file
    if not path.exists():
        return None
    document = _read(model, path, source.name, file, errors)
    return None if document is None else RawDoc(file=file, document=document)


# --- instance configuration --------------------------------------------------------------


def _read_profiles(
    instance_dir: Path, instance: InstanceDef, errors: StepErrors
) -> tuple[RawDoc[ProfileDef], ...]:
    """Read the profiles instance.yaml lists and check the values M0 fixes (req 2.13)."""
    if tuple(instance.profiles) != (SEMANTIC_PROFILE,):
        listed = f" {'、'.join(instance.profiles)}" if instance.profiles else "空"
        errors.add_at(
            None,
            INSTANCE_FILE,
            PROFILES_DIR,
            f"profiles 只能含 {SEMANTIC_PROFILE} 一项，当前为{listed}",
        )
    profiles = _read_definitions(
        instance_dir, PROFILES_DIR, PROFILE_KEY, ProfileDef, instance.profiles, errors
    )
    for raw in profiles:
        _check_fixed_values(raw, errors)
    return profiles


def _check_fixed_values(raw: RawDoc[ProfileDef], errors: StepErrors) -> None:
    """Check the three values of a profile definition that M0 fixes (requirement 2.13)."""
    profile = raw.document
    name = profile.profile
    if profile.surface != SURFACE:
        reason = f"profile {name} 的 surface 必须是 {SURFACE}"
        errors.add_at(None, raw.file, "surface", reason)
    if profile.description_variant != DESCRIPTION_VARIANT:
        reason = f"profile {name} 的 description_variant 必须是 {DESCRIPTION_VARIANT}"
        errors.add_at(None, raw.file, "description_variant", reason)
    if profile.include_deprecated:
        reason = f"profile {name} 的 include_deprecated 必须是 false"
        errors.add_at(None, raw.file, "include_deprecated", reason)


def _read_definitions[M: BaseModel](
    instance_dir: Path,
    directory: str,
    key: str,
    model: type[M],
    names: Sequence[str],
    errors: StepErrors,
) -> tuple[RawDoc[M], ...]:
    """Read the definitions of ``names`` below ``directory`` of the instance (req 2.13).

    A definition is matched by the name its ``key`` declares, so ``names`` are looked up in
    the documents of the directory and not in its file names. ``directory`` is also the key
    of instance.yaml that lists the names, which locates a name without a definition. A
    repeated name is read once.
    """
    documents = _definitions(instance_dir, directory, key, errors)
    found: list[RawDoc[M]] = []
    read: set[str] = set()
    for index, name in enumerate(names):
        if name in read:
            continue
        read.add(name)
        matches = documents.get(name, ())
        if not matches:
            reason = f"{directory}/ 中没有 {key} 为 {name} 的定义"
            errors.add_at(None, INSTANCE_FILE, f"{directory}[{index}]", reason)
            continue
        if len(matches) > 1:
            locations = tuple(Location(file, key) for file, _ in matches)
            errors.add(None, locations, f"{directory}/ 中有多个 {key} 为 {name} 的定义")
            continue
        file, document = matches[0]
        validated = _validate(model, document, None, file, errors)
        if validated is not None:
            found.append(RawDoc(file=file, document=validated))
    return tuple(found)


def _definitions(
    instance_dir: Path, directory: str, key: str, errors: StepErrors
) -> Mapping[str, tuple[tuple[str, YamlValue], ...]]:
    """The documents below ``directory``, grouped by the name their ``key`` declares.

    Every file of the directory is parsed, since the name a file declares is what matches
    it with instance.yaml; a file that cannot be read or parsed is one error at its line,
    and a document that declares no name under ``key`` matches nothing, which makes the
    name that is missing its definition the error the deployer reads.
    """
    found: dict[str, tuple[tuple[str, YamlValue], ...]] = {}
    for file in _files(instance_dir, directory, None, errors):
        try:
            document = load_yaml(instance_dir / file)
        except YamlLoadError as error:
            _report_unread(error, None, file, errors)
            continue
        name = document.get(key) if isinstance(document, dict) else None
        if isinstance(name, str):
            found[name] = (*found.get(name, ()), (file, document))
    return found


# --- reading and validating one file -----------------------------------------------------


def _files(root: Path, directory: str, pack: str | None, errors: StepErrors) -> tuple[str, ...]:
    """The YAML files below ``root / directory``, as paths relative to ``root``, sorted.

    ``root`` is the pack or the instance directory, so the returned paths are what a
    :class:`~erp_mock.loader.errors.Location` reports. A directory that does not exist gives
    no file; one that cannot be listed, such as a file of that name, is one error at it.
    """
    try:
        paths = list_files(root / directory)
    except YamlLoadError as error:
        errors.add_at(pack, directory, None, error.reason)
        return ()
    return tuple(path.relative_to(root).as_posix() for path in paths)


def _report_unread(error: YamlLoadError, pack: str | None, file: str, errors: StepErrors) -> None:
    """Record a file that could not be read, is not UTF-8 or does not parse, at its line.

    The line is the config path of such an error (requirement 2.2,
    :attr:`~erp_mock.loader.yaml_io.YamlLoadError.config_path`).
    """
    errors.add_at(pack, file, error.config_path, error.reason)


def _validate[M: BaseModel](
    model: type[M], document: YamlValue, pack: str | None, file: str, errors: StepErrors
) -> M | None:
    """Validate ``document`` against ``model``, recording one error per pydantic error.

    Each error is located at the config path of its ``loc``, so a required key that is
    missing, a key the model does not define and a value of the wrong type are reported at
    the item they are about (requirements 1.9 and 1.11 to 1.13). Returns None when the
    document does not fit the model, having recorded every error of it. An empty document is
    None and fits no model, which the model reports.
    """
    try:
        return model.model_validate(document)
    except ValidationError as error:
        errors.add_model_errors(pack, file, error)
        return None


def _read[M: BaseModel](
    model: type[M], path: Path, pack: str | None, file: str, errors: StepErrors
) -> M | None:
    """Read ``path`` and validate the document it holds against ``model``."""
    try:
        document = load_yaml(path)
    except YamlLoadError as error:
        _report_unread(error, pack, file, errors)
        return None
    return _validate(model, document, pack, file, errors)
