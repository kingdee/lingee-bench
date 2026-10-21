"""The cache of the built Snapshots below the data directory (requirements 20.11, 20.12).

A Snapshot is what one initial dataset amounts to under one anchor date, and building it
replays the whole definition through the engine (:mod:`erp_mock.seed.builder`). Every
sandbox of that dataset starts from it, so it is built once and kept::

    cache = SnapshotCache(data_dir, SeedBuilder(loaded, instance_dir))
    ref = cache.get_or_build(dataset, anchor, file="datasets/purchase-basic.yaml")
    with cache.shared(ref):             # while the files are read
        copy_snapshot(ref.db, sandbox / "db.sqlite")

The key is (dataset name@version, anchor date) and the layout below the data directory is
the one of design.md data model 2::

    <data-dir>/snapshots/
    ├── <dataset>@<version>/
    │   ├── <YYYY-MM-DD>/          the entry: snapshot.sqlite, meta.json, aliases.json
    │   └── <YYYY-MM-DD>.lock      the lock file of that key, beside the entry
    ├── .tmp/<random>/             a build in progress
    └── .trash/<random>/           an entry that was replaced, waiting to be removed

The lock file is a sibling of the entry and not a file inside it, because the entry
directory is replaced by a rename: the lock has to stay the same file across a
replacement. Nothing below ``snapshots/`` can collide with ``.tmp`` and ``.trash``, since
a dataset name starts with a letter or a digit
(:data:`~erp_mock.config.instance.CONFIG_NAME_PATTERN`).

An entry is taken up when its three files are there and readable and the six values of
requirement 20.12 in its ``meta.json`` are the ones a build would write now
(:data:`INVALIDATING_KEYS`): the framework version, the meta model version, the version of
every business pack, the ``random_seed`` and the current user of the dataset, and the
SHA-256 of the bytes of the dataset definition file. Anything else - a file missing, a
``meta.json`` that cannot be read or is not an object, one of those six values different -
is a miss, and a miss builds (requirement 20.12). Which anchor date and which dataset an
entry is of is the directory it is in; what is inside the definition is covered by its
digest.

A build never writes into an entry. It writes into ``.tmp/<random>/``, which the builder
fills and fsyncs, and only a build that got through replaces the entry: under the exclusive
lock of the key, the entry directory is renamed into ``.trash/<random>/`` and the temporary
directory is renamed to the entry, two renames inside one file system, so a reader sees
either the old entry or the new one and never half of each. The old directory is removed
after the lock is released. A build that failed leaves the cache exactly as it was: the
temporary directory, with the build database and whatever records it had, is removed and
nothing was renamed (requirement 20.11).

A reader holds the shared lock of the key while it reads the files of an entry
(:meth:`SnapshotCache.shared`), which is what keeps a replacement from removing the
directory it is reading from; the hit check takes that lock too. Within one process, the
requests for one key queue on a lock of their own, and the one that gets through checks the
entry again, so a key is built once however many threads asked for it at the same time
(design.md 6.2). Everything here is synchronous and may wait on a lock or on a build, so a
server hands a call to a worker thread rather than running it on its event loop.

What is not here: the Snapshot itself (:mod:`erp_mock.seed.builder`), the copy a sandbox is
created from (:func:`~erp_mock.state.snapshot.copy_snapshot`) and the checks loading step 8
runs over an entry it takes up, cached or freshly built (requirement 2.14).
"""

import fcntl
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Final

from erp_mock.common.jsonio import JsonValue
from erp_mock.config.instance import DatasetDef
from erp_mock.seed.builder import (
    SeedBuilder,
    SnapshotKey,
    SnapshotMeta,
    SnapshotRef,
    snapshot_key,
    snapshot_ref,
)

__all__ = [
    "INVALIDATING_KEYS",
    "LOCK_SUFFIX",
    "SNAPSHOTS_DIR",
    "TMP_DIR",
    "TRASH_DIR",
    "SnapshotCache",
]

# The directory of the cache below ``--data-dir`` and the two it keeps beside its entries
# (design.md data model 2). Both of the latter are below ``snapshots/`` so that a rename
# between them and an entry stays inside one file system, which is what makes the
# replacement atomic.
SNAPSHOTS_DIR: Final = "snapshots"
TMP_DIR: Final = ".tmp"
TRASH_DIR: Final = ".trash"

# The lock file of one key, named after the anchor date beside the entry directory.
LOCK_SUFFIX: Final = ".lock"

# The keys of ``meta.json`` an entry is taken up by: all six have to be what a build would
# write now, or the entry is rebuilt (requirement 20.12). The dataset name, its version and
# the anchor date are the key itself, and ``closed_before`` follows from the definition and
# the anchor date, so neither is compared.
INVALIDATING_KEYS: Final = (
    "random_seed",
    "current_user",
    "framework_version",
    "meta_model_version",
    "packs",
    "dataset_sha256",
)

# The locks the requests for one key queue on inside one process, by (cache, key): the
# first one through builds and replaces the entry, and the ones behind it check the entry
# again and take up what it left, so one key is built once (design.md 6.2). The dictionary
# is module level, so two cache objects over one data directory share the lock of a key;
# ``_REGISTRY`` guards it, and a lock is never dropped, there being one per dataset and
# anchor date.
_BUILD_LOCKS: Final[dict[tuple[Path, SnapshotKey], threading.Lock]] = {}
_REGISTRY: Final = threading.Lock()


class SnapshotCache:
    """The Snapshots of one instance configuration below one data directory.

    ``data_dir`` is ``--data-dir``, whose ``snapshots/`` holds the entries, and ``builder``
    the builder of the instance the datasets belong to; the cache compares an entry against
    what that builder would write and hands a miss to it (module docstring).

    One cache object per data directory and instance is enough - it holds nothing of an
    entry - but two of them work together: the lock files keep the processes and the
    threads of a replacement apart whichever object they went through.
    """

    __slots__ = ("_builder", "_identity", "_root")

    def __init__(self, data_dir: Path, builder: SeedBuilder) -> None:
        self._builder = builder
        self._root = data_dir / SNAPSHOTS_DIR
        # What two caches over one directory are recognised by, so the requests for a key
        # queue on one lock whichever object they came through; resolved, since a data
        # directory is reached by more than one path (a symlink, a relative path).
        self._identity = self._root.resolve()

    @property
    def root(self) -> Path:
        """``<data-dir>/snapshots/``, the directory the entries are below."""
        return self._root

    @property
    def builder(self) -> SeedBuilder:
        """The builder a miss is handed to (design.md component 6.1)."""
        return self._builder

    def directory(self, key: SnapshotKey) -> Path:
        """The entry directory of ``key``: ``snapshots/<dataset>@<version>/<YYYY-MM-DD>/``."""
        return self._root / key.dataset / key.anchor.isoformat()

    def lock_path(self, key: SnapshotKey) -> Path:
        """The lock file of ``key``, beside its entry directory (design.md 6.2)."""
        return self._root / key.dataset / f"{key.anchor.isoformat()}{LOCK_SUFFIX}"

    def get_or_build(
        self, dataset: DatasetDef, anchor: date, *, file: str | None = None
    ) -> SnapshotRef:
        """The Snapshot of ``dataset`` at ``anchor``, built if the cache has none to take up.

        ``file`` is the definition file of ``dataset`` relative to the instance directory,
        as :meth:`~erp_mock.seed.builder.SeedBuilder.build` takes it; None derives
        ``datasets/<name>.yaml`` from the definition. An entry that is there and still fits
        is returned as it is and nothing is built (requirement 20.11); otherwise the dataset
        is built and the entry replaced (requirement 20.12).

        Returns the three files of the entry, all of them on disk. Raises
        :class:`~erp_mock.seed.resolve.SeedError` where the build fails, the cache then
        being as it was, and OSError where the data directory cannot be written.
        """
        meta = self._builder.metadata(dataset, anchor, file=file)
        with self._build_lock(meta.key):
            found = self._cached(meta)
            if found is not None:
                return found
            return self._build(dataset, anchor, meta.key, file)

    def rebuild(self, dataset: DatasetDef, anchor: date, *, file: str | None = None) -> SnapshotRef:
        """Build the Snapshot of ``dataset`` at ``anchor`` and replace the entry of its key.

        What the ``seed`` command runs (requirement 22.6): the entry that is there is not
        looked at, whether it would have been taken up or not, and the build replaces it the
        way :meth:`get_or_build` replaces a stale one. Arguments, result and errors are
        those of :meth:`get_or_build`.
        """
        key = snapshot_key(dataset, anchor)
        with self._build_lock(key):
            return self._build(dataset, anchor, key, file)

    @contextmanager
    def shared(self, ref: SnapshotRef) -> Iterator[SnapshotRef]:
        """Hold the shared lock of ``ref``'s key while the block reads its files.

        What a sandbox being created from a Snapshot and loading step 8 hold while they
        read one (design.md 6.2): a replacement takes the exclusive lock of the same key,
        so the entry cannot be renamed away under a reader. Yields ``ref`` itself.
        """
        with _locked(self.lock_path(ref.key), fcntl.LOCK_SH):
            yield ref

    # --- what the cache holds (requirement 20.12) ----------------------------------------

    def _cached(self, meta: SnapshotMeta) -> SnapshotRef | None:
        """The entry of ``meta``'s key if it can be taken up, None if it has to be built.

        The three files have to be readable files and the six values of
        :data:`INVALIDATING_KEYS` in ``meta.json`` have to be the ones of ``meta``, which is
        what a build would write now. The shared lock is held while they are read, so a
        replacement in another process is seen whole or not at all.
        """
        ref = snapshot_ref(self.directory(meta.key), meta.key)
        with _locked(self.lock_path(meta.key), fcntl.LOCK_SH):
            payload = _entry_meta(ref)
        if payload is None:
            return None
        return ref if _checked(payload) == _checked(meta.as_json()) else None

    # --- building and replacing (requirement 20.11) --------------------------------------

    def _build(
        self, dataset: DatasetDef, anchor: date, key: SnapshotKey, file: str | None
    ) -> SnapshotRef:
        """Build ``dataset`` at ``anchor`` in ``.tmp`` and make it the entry of ``key``.

        The temporary directory is removed either way: the replacement renames it away, so
        there is nothing left to remove after one, and a build that failed leaves it with
        the build database and the records it got to - none of which the cache keeps
        (requirement 20.11).
        """
        tmp = self._reserve(TMP_DIR)
        try:
            self._builder.build(dataset, anchor, tmp, file=file)
            self._replace(key, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return snapshot_ref(self.directory(key), key)

    def _replace(self, key: SnapshotKey, tmp: Path) -> None:
        """Make ``tmp`` the entry of ``key``, holding the exclusive lock of that key.

        Two renames inside ``snapshots/``: the entry that is there goes to ``.trash/`` and
        ``tmp`` takes its place, so a reader of the key sees one entry or the other. The old
        directory is removed once the lock is released, since a reader that took the shared
        lock before the renames may still be reading from it.
        """
        directory = self.directory(key)
        old: Path | None = None
        with _locked(self.lock_path(key), fcntl.LOCK_EX):
            if directory.exists():
                # rename refuses a directory that is not empty as its target, so the old
                # entry is moved out of the way first; the reserved directory is empty,
                # which rename does accept.
                old = self._reserve(TRASH_DIR)
                os.replace(directory, old)
            os.replace(tmp, directory)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)

    def _reserve(self, name: str) -> Path:
        """A new empty directory below ``snapshots/<name>/``, created with a random name.

        A reserved directory becomes the entry of a key by being renamed, so it is given
        the mode of its parent - the mode of the data directory's own directories, umask
        and all - rather than the private one a temporary directory is created with.
        """
        parent = self._root / name
        parent.mkdir(parents=True, exist_ok=True)
        reserved = Path(tempfile.mkdtemp(dir=parent))
        os.chmod(reserved, parent.stat().st_mode & 0o777)
        return reserved

    def _build_lock(self, key: SnapshotKey) -> threading.Lock:
        """The lock the requests for ``key`` queue on inside this process (design.md 6.2)."""
        with _REGISTRY:
            return _BUILD_LOCKS.setdefault((self._identity, key), threading.Lock())


# --- reading an entry --------------------------------------------------------------------


def _entry_meta(ref: SnapshotRef) -> Mapping[str, JsonValue] | None:
    """The metadata of the entry ``ref``, None where the entry cannot be taken up.

    None when one of the three files is not a readable file, or when ``meta.json`` is not
    JSON or holds something other than an object: each of them is a cache file that is
    missing or cannot be read (requirement 20.12).
    """
    if not all(_readable(path) for path in ref.files):
        return None
    try:
        payload = json.loads(ref.meta.read_bytes())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _readable(path: Path) -> bool:
    """Whether ``path`` is a file this process can open for reading."""
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    os.close(fd)
    return True


def _checked(payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The values of ``payload`` that decide whether an entry still fits (req 20.12).

    A key that is not there at all is left out, so metadata without one of the six does not
    compare equal to metadata that has it, however the value reads.
    """
    return {key: payload[key] for key in INVALIDATING_KEYS if key in payload}


# --- the file lock of one key (design.md 6.2) --------------------------------------------


@contextmanager
def _locked(path: Path, operation: int) -> Iterator[None]:
    """Hold ``operation`` (``LOCK_SH`` or ``LOCK_EX``) on ``path`` for the block.

    ``flock`` locks belong to the open file, not to the process, so two readers of one key
    hold it at once while a replacement waits, whether they are in one process or in
    several. The lock file is created when it is not there yet and is never removed, which
    is what lets the entry directory beside it be replaced by a rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, operation)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
