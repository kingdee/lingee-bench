"""Loading step 8 and the cross-check that follows it (design.md 2.3).

The seven steps before this one read documents; the eighth one reads data. For every
initial dataset instance.yaml lists, the step takes up the Snapshot of that dataset at the
step 8 anchor date - building it when the cache has none that still fits - and checks what
is in it (requirement 2.14):

=========================================================  =========================  ====
check                                                      where it is reported       req
=========================================================  =========================  ====
INV-1, INV-2, INV-3 and INV-6 hold in the Snapshot         the dataset definition     2.14
the current user of the dataset is set, in the dataset     the item that sets it, or  2.10
or in instance.yaml                                        both files where neither
that user is a record of the user context entity           the item that sets it      2.10
exactly one record of the org context entity has its       the dataset definition     2.10
default flag true
=========================================================  =========================  ====

The invariants are checked over every Snapshot the step takes up, the ones that came out of
the cache included (requirement 2.14): the Seed_Builder refuses to write a Snapshot whose
records break one (requirement 20.9), so a cached entry that breaks one is a file that was
changed behind the cache's back, and that is exactly the case this check is here for. Every
dataset is checked and every check runs, so one step reports the errors of all of them
(requirement 2.2); a build that cannot be carried out is no configuration error but a
:class:`~erp_mock.seed.resolve.SeedError`, which carries its own file, config path and
reason and is left to the caller (design.md error handling).

The anchor date is the caller's (:func:`anchor_of`): the ``--anchor-date`` of the command,
or, where none was given, the ``anchor_date`` of a fixed clock. A clock in mode today needs
the system time, which only the gateway's SystemClock reads (requirement 24.7), so the CLI
resolves that case before it loads and passes the date in.

How a Snapshot is read: the three files of a cache entry are never written to, and opening
one as a sandbox database would put a WAL sidecar next to it (requirement 15.13), so the
Snapshot is copied into a temporary directory of its own and read from there as a sandbox
is (:func:`~erp_mock.state.snapshot.copy_snapshot`, which reads it read-only and immutable);
the copy is thrown away with the directory. The shared lock of the cache key is held while
that copy is made, so a replacement in another process cannot rename the entry away under
the reader (design.md 6.2). One transaction over the copy does the whole reading: the
invariants, the current user, the default organization and the Business_Data of the
cross-check.

The cross-check (:func:`cross_check`) is the last thing loading does: no tool description
and no parameter description of any declared variant may contain the name or the code of an
entity record or the number of a bill of the Business_Data - the records of the Snapshots
this step took up (requirement 3.5). A description tells the model what a tool does; a
value of the data it is to work on would make it guess. The comparison is the one
requirement 3.4 uses for the guiding phrases: the text occurs as a substring, ASCII letters
compared without regard to case (:func:`~erp_mock.tools.descriptions.ascii_folded`), with
empty values and empty strings left out of it.

The step leaves behind the datasets of the instance - one
:class:`~erp_mock.loader.instance.DatasetModel` each, with the Snapshot it took up - which
the read-only configuration object carries (design.md 2.4), and the Business_Data the
cross-check compares against.
"""

import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.instance import DatasetDef, InstanceDef
from erp_mock.config.model import DomainModel
from erp_mock.config.pack import ToolDescription
from erp_mock.domain.engine import Engine
from erp_mock.domain.invariants import InvariantViolation, check_invariants
from erp_mock.loader.errors import Location, StepErrors
from erp_mock.loader.instance import DatasetModel, LoadedInstance
from erp_mock.loader.step1 import INSTANCE_FILE
from erp_mock.loader.step4 import RawDoc, RawPack
from erp_mock.loader.step5_expr import ParsedDomain
from erp_mock.loader.step6 import GeneratedTools
from erp_mock.loader.step7 import entries, known_variants, variant_file
from erp_mock.seed.builder import SeedBuilder, SnapshotRef
from erp_mock.seed.cache import SnapshotCache
from erp_mock.state.schema import build_schema
from erp_mock.state.snapshot import copy_snapshot
from erp_mock.state.store import Record, SandboxStore, UnitOfWork
from erp_mock.tools.descriptions import DescriptionIndex, ascii_folded

__all__ = [
    "CODE",
    "CURRENT_USER",
    "NAME",
    "NUMBER",
    "BusinessData",
    "Step8",
    "anchor_of",
    "business_texts",
    "check",
    "cross_check",
    "engine_of",
]

# The item of a dataset definition and of instance.yaml that names the current user
# (requirement 20.5), which an error about it is reported at.
CURRENT_USER: Final = "current_user"

# The three standard fields the cross-check compares (requirement 3.5): the name and the
# code of an entity record and the number of a bill. An entity that declares no ``name``
# and one whose ``code`` the engine does not assign simply hold neither.
NAME: Final = "name"
CODE: Final = "code"
NUMBER: Final = "number"

# The reasons of the step and of the cross-check, one per check.
_NO_USER: Final = "初始数据集 {dataset} 没有当前用户：数据集与 instance.yaml 的 {item} 都没有设置"
_UNKNOWN_USER: Final = "初始数据集 {dataset} 的当前用户 {user} 不是 Snapshot 中 {entity} 的记录"
_DEFAULT_ORG_COUNT: Final = (
    "初始数据集 {dataset} 的 Snapshot 中 {entity} 的 {flag} 为 true 的记录有 {count} 条，"
    "必须恰好 1 条"
)
_INVARIANT: Final = "初始数据集 {dataset} 的 Snapshot 不满足不变量：{violation}"
_BUSINESS_TEXT: Final = "{about}含初始数据集 {dataset} 的业务数据文本 {text}"

# The prefix of the temporary directory a Snapshot is read in, so a copy that outlives a
# crash can be recognised.
_READ_PREFIX: Final = "erp-mock-step8-"


# --- what the step leaves behind ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BusinessData:
    """The names, codes and numbers of the Snapshots step 8 took up (requirement 3.5).

    ``texts`` maps each text to the initial dataset it was found in - the first one that
    holds it, in the order instance.yaml lists them - so an error can name where the text a
    description contains comes from. The texts are in the order they were read: per dataset,
    the entities and then the bills in model order, and the records of each by internal id.
    Empty values and empty strings are not in the map: they are left out of the comparison
    (requirement 3.5).
    """

    texts: FrozenMap[str, str] = FrozenMap()

    def hits(self, text: str) -> tuple[tuple[str, str], ...]:
        """The texts of the Business_Data that occur in ``text``, with their dataset.

        A text occurs when it is a substring of ``text``, ASCII letters compared without
        regard to case (requirement 3.5, the comparison of requirement 3.4). The hits come
        in the order of :attr:`texts`, so what is reported does not depend on where in the
        description they are.
        """
        folded = ascii_folded(text)
        return tuple(
            (found, dataset)
            for found, dataset in self.texts.items()
            if ascii_folded(found) in folded
        )


@dataclass(frozen=True, slots=True)
class Step8:
    """What step 8 leaves for the cross-check and for the read-only configuration object.

    ``datasets`` holds one :class:`~erp_mock.loader.instance.DatasetModel` per initial
    dataset, in the order instance.yaml lists them, and ``data`` the Business_Data of their
    Snapshots. Both are what the step found, whether it reported anything or not, so a test
    can read them; the loading goes on only when it reported nothing.
    """

    datasets: tuple[DatasetModel, ...] = ()
    data: BusinessData = BusinessData()


# --- the anchor date of the step (requirement 2.14) --------------------------------------


def anchor_of(instance: InstanceDef, anchor_date: date | None) -> date:
    """The anchor date step 8 takes up the Snapshots under (requirement 2.14).

    ``anchor_date`` is the ``--anchor-date`` of the command, which wins over the clock
    configuration; without one, a clock in mode fixed gives its ``anchor_date``
    (requirement 16.3).

    Raises AssertionError for a clock in mode today and no ``anchor_date``: that date is the
    current date in the clock's time zone, which only the gateway's SystemClock may read
    (requirement 24.7), so the caller resolves it and passes it in.
    """
    if anchor_date is not None:
        return anchor_date
    clock = instance.clock
    if clock.mode == "fixed" and clock.anchor_date is not None:
        return date.fromisoformat(clock.anchor_date)
    raise AssertionError(
        f"clock.mode 为 {clock.mode} 时，第 8 步锚点日期由调用方确定后传入（需求 2 第 14 条）"
    )


def engine_of(parsed: ParsedDomain) -> Engine:
    """The engine the Snapshots of ``parsed`` are built through (design.md component 6.1).

    Built with the syntax trees step 5 parsed and checked, so no text of a rule, a writeback
    or a query is parsed a second time (requirement 10.2).
    """
    return Engine(
        parsed.domain,
        filters=parsed.queries,
        values=parsed.writebacks,
        checks=parsed.rules,
    )


# --- the step ----------------------------------------------------------------------------


def check(
    loaded: LoadedInstance,
    datasets: Sequence[RawDoc[DatasetDef]],
    instance_dir: Path,
    data_dir: Path,
    errors: StepErrors,
    *,
    engine: Engine | None = None,
) -> Step8:
    """Run step 8 on the initial datasets of the instance; return what it took up.

    ``loaded`` is the configuration the steps before this one amount to, which carries the
    anchor date of the step and everything a build reads - the domain model, the documents
    of each pack and the versions (its own ``datasets`` are what this step produces);
    ``datasets`` are the definitions step 4 read, in the order instance.yaml lists them;
    ``instance_dir`` is the directory they were read from and ``data_dir`` the data
    directory whose ``snapshots/`` holds the cache (:mod:`erp_mock.seed.cache`). ``engine``
    is the engine of the same configuration, which the loader builds from the trees it
    parsed at step 5 (:func:`engine_of`); None builds one from the domain model.

    Everything found is recorded in ``errors``, so the step passed only when ``errors``
    stayed empty. Raises :class:`~erp_mock.seed.resolve.SeedError` where a dataset cannot
    be built and OSError where the data directory cannot be written.
    """
    cache = SnapshotCache(data_dir, SeedBuilder(loaded, instance_dir, engine))
    models: list[DatasetModel] = []
    texts: dict[str, str] = {}
    for raw in datasets:
        model, found = _dataset(loaded, cache, raw, errors)
        models.append(model)
        for text in found:
            texts.setdefault(text, model.name)
    return Step8(datasets=tuple(models), data=BusinessData(FrozenMap(texts)))


def _dataset(
    loaded: LoadedInstance,
    cache: SnapshotCache,
    raw: RawDoc[DatasetDef],
    errors: StepErrors,
) -> tuple[DatasetModel, tuple[str, ...]]:
    """Take up the Snapshot of one dataset and check it; return the dataset and its texts.

    The Snapshot of ``raw`` at the anchor date of ``loaded``, built where the cache has none
    that still fits (requirement 20.12), then read once: the invariants, the current user,
    the default organization and the Business_Data of the cross-check.
    """
    definition = raw.document
    user = definition.current_user or loaded.instance.current_user
    ref = cache.get_or_build(definition, loaded.step8_anchor, file=raw.file)
    with _opened(cache, ref, loaded.domain) as uow:
        for violation in check_invariants(uow, loaded.domain):
            _report_invariant(definition, raw.file, violation, errors)
        _check_user(loaded, definition, raw.file, user, uow, errors)
        _check_default_org(loaded.domain, definition, raw.file, uow, errors)
        texts = tuple(business_texts(uow, loaded.domain))
    model = DatasetModel(
        definition=definition, file=raw.file, current_user=user, snapshot=ref.directory
    )
    return model, texts


def _report_invariant(
    dataset: DatasetDef, file: str, violation: InvariantViolation, errors: StepErrors
) -> None:
    """Report one invariant that does not hold in the Snapshot of ``dataset`` (req 2.14).

    One error per place, in the order :func:`~erp_mock.domain.invariants.check_invariants`
    reports them. No configuration item corresponds to it - the records are the result of a
    build, not something a document writes - so the error names the definition file alone
    (requirement 2.2).
    """
    reason = _INVARIANT.format(dataset=dataset.dataset, violation=violation.text)
    errors.add_at(None, file, None, reason)


def _check_user(
    loaded: LoadedInstance,
    dataset: DatasetDef,
    file: str,
    user: str | None,
    uow: UnitOfWork,
    errors: StepErrors,
) -> None:
    """The current user of ``dataset`` is set and is a record of the Snapshot (req 2.10).

    The dataset's own ``current_user`` or, where it declares none, that of instance.yaml
    (requirement 20.5): where neither sets one, the error names both items, since either of
    them could carry it; where the one that is set names no record of the user context
    entity, the error is at the item that sets it. The record is read by internal id, which
    is the only way a current user is written (requirement 20.16), so an id of another
    entity finds nothing and is reported here as well.
    """
    name = dataset.dataset
    if user is None:
        places = (Location(file, CURRENT_USER), Location(INSTANCE_FILE, CURRENT_USER))
        errors.add(None, places, _NO_USER.format(dataset=name, item=CURRENT_USER))
        return
    entity = loaded.domain.user_entity
    if entity is None:
        # Step 5 reports an instance without exactly one user context entity
        # (requirement 1.14), so there is nothing left to read the user against.
        return
    if uow.get(entity.name, user) is None:
        at = file if dataset.current_user is not None else INSTANCE_FILE
        reason = _UNKNOWN_USER.format(dataset=name, user=user, entity=entity.name)
        errors.add_at(None, at, CURRENT_USER, reason)


def _check_default_org(
    domain: DomainModel, dataset: DatasetDef, file: str, uow: UnitOfWork, errors: StepErrors
) -> None:
    """Exactly one record of the org context entity is the default organization (2.10).

    The default organization is the record whose default flag is true (requirement 1.8);
    the business context of every sandbox of this dataset reads it
    (:func:`~erp_mock.domain.views.default_org`), so neither none nor several will do. As
    with the invariants, no configuration item corresponds to it: what the Snapshot holds
    follows from the whole definition and from the Shared_Data it brings in.
    """
    entity = domain.org_entity
    if entity is None or entity.default_flag is None:
        # Step 5 reports both an instance without exactly one org context entity and an org
        # role that declares no default flag (requirements 1.14 and 1.8).
        return
    found = uow.select(entity.name, where={entity.default_flag: True})
    if len(found) != 1:
        reason = _DEFAULT_ORG_COUNT.format(
            dataset=dataset.dataset,
            entity=entity.name,
            flag=entity.default_flag,
            count=len(found),
        )
        errors.add_at(None, file, None, reason)


# --- reading one Snapshot ----------------------------------------------------------------


@contextmanager
def _opened(cache: SnapshotCache, ref: SnapshotRef, domain: DomainModel) -> Iterator[UnitOfWork]:
    """Open a copy of the Snapshot ``ref`` and yield the transaction that reads it.

    The copy goes into a temporary directory of its own and is removed with it, so the files
    of the cache entry are only read and no sidecar file appears beside them
    (requirement 15.13, module docstring). The shared lock of the key is held while the copy
    is made - that is the whole of what this reads from the cache - so a replacement in
    another process cannot rename the entry away under it (design.md 6.2).
    """
    with tempfile.TemporaryDirectory(prefix=_READ_PREFIX) as workdir:
        store = SandboxStore(Path(workdir), build_schema(domain))
        with cache.shared(ref):
            copy_snapshot(ref.db, store.db_path)
        store.open()
        try:
            with store.transaction() as uow:
                yield uow
        finally:
            store.close()


# --- the Business_Data of the cross-check (requirement 3.5) ------------------------------


def business_texts(uow: UnitOfWork, domain: DomainModel) -> Iterator[str]:
    """The names, codes and numbers of the records of one Snapshot (requirement 3.5).

    The ``name`` and the ``code`` of every entity record and the ``number`` of every bill,
    the entities and then the bills in model order and the records of each table by internal
    id; a record without such a field, an empty value and an empty string yield nothing,
    being left out of the comparison. A text two records share is yielded twice, which the
    caller collects into one entry.
    """
    for entity in domain.entities.values():
        for record in uow.select(entity.name):
            yield from _values(record, (NAME, CODE))
    for bill in domain.bills.values():
        for record in uow.select(bill.name):
            yield from _values(record, (NUMBER,))


def _values(record: Record, fields: Sequence[str]) -> Iterator[str]:
    """The non-empty text values ``record`` holds for ``fields``, in that order."""
    for name in fields:
        value = record.get(name)
        if isinstance(value, str) and value:
            yield value


# --- the cross-check (requirement 3.5) ---------------------------------------------------


def cross_check(
    packs: Sequence[RawPack],
    tools: GeneratedTools,
    index: DescriptionIndex,
    data: BusinessData,
    errors: StepErrors,
) -> None:
    """No description contains a name, a code or a number of Business_Data (req 3.5).

    ``packs`` are the packs instance.yaml references, in its order, ``tools`` the tools
    step 6 generated, ``index`` the descriptions step 7 read and ``data`` the Business_Data
    step 8 collected from the Snapshots. Every declared variant of every pack is checked,
    and within a variant every tool of the pack: its tool description and the description of
    each of its parameters (requirement 3.2). The built-in tool is no part of any pack and
    its text is the framework's own, which no dataset can reach (requirement 3.9).

    Everything found is recorded in ``errors``, one error per text a description contains,
    so the cross-check passed only when ``errors`` stayed empty (requirement 2.2).
    """
    for pack in packs:
        specs = tools.packs.get(pack.name, ())
        for variant in known_variants(pack):
            file = variant_file(pack, variant)
            written = index.of(pack.name, variant)
            for spec in specs:
                texts = written.get(spec.name, ToolDescription())
                for entry in entries(file, spec, texts):
                    about = entry.about(spec.name, variant)
                    for text, dataset in data.hits(entry.text):
                        reason = _BUSINESS_TEXT.format(about=about, dataset=dataset, text=text)
                        errors.add(pack.name, (entry.location,), reason)
