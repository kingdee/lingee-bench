"""Snapshot files and the database files of one sandbox (design.md 3.3).

A Snapshot is the content of one initial dataset under one anchor date, kept as a SQLite
file below ``<data-dir>/snapshots/`` and never modified in place. This module is the whole
life of such a file: :func:`build_snapshot_db` opens the database a build writes into,
:func:`finalize_snapshot` writes the Snapshot out of it, and :func:`copy_snapshot` and
:func:`link_snapshot` give a new sandbox its ``db.sqlite`` and its ``base.sqlite``
(design.md data model 2). A sandbox's own copies - reset, restore and Named_Snapshot - are
:meth:`~erp_mock.state.store.SandboxStore.restore_from` and
:meth:`~erp_mock.state.store.SandboxStore.save_to`, because they work on an open database.

Byte determinism (requirement 20.13). Two builds of one dataset under one anchor date
must produce the same file byte for byte, whatever the process, the working directory or
the ``-data-dir`` was, and whatever order the pages happened to be allocated in. The
engine gets that from ``VACUUM INTO``, which writes the rows of every table in primary key
order into a fresh file with no free pages: by tests/probes/test_sqlite.py the output then
depends on the content and the SQLite version alone, not on how many transactions the
build ran or in which order it inserted, updated and deleted. Two things the build must
hold up its end of:

- the pragmas of :data:`BUILD_PRAGMAS`, because the output keeps the page size, the
  encoding, ``auto_vacuum`` and ``user_version`` of its source;
- one and the same DDL sequence, because the output's schema cookie is the build
  database's plus one. :meth:`~erp_mock.state.store.SandboxStore.create_tables` creates
  every table of the schema once, in the fixed order of :mod:`erp_mock.state.schema`, so
  this holds as long as a build creates its tables that way and no other way.

Were ``VACUUM INTO`` to lose that determinism in a later SQLite version, the probe test
fails first and :func:`finalize_snapshot` becomes a table-by-table copy in primary key
order into a new database; nothing outside this module would change.

A snapshot file is read through :func:`~erp_mock.state.store.immutable_uri` only, so
copying from it, resetting from it and diffing against it leave its bytes alone and create
no sidecar file next to it (requirement 15.13). The shared file lock a reader of the
Snapshot cache holds while copying (design.md 6.2) belongs to the cache, not here.
"""

import errno
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Final

from erp_mock.state.schema import StorageSchema
from erp_mock.state.store import SandboxStore, immutable_uri

__all__ = [
    "BASE_DB_FILENAME",
    "BUILD_PRAGMAS",
    "PAGE_SIZE",
    "STORAGE_FORMAT_VERSION",
    "build_snapshot_db",
    "copy_snapshot",
    "finalize_snapshot",
    "link_snapshot",
]

# The initial basis of one sandbox, below its directory (design.md data model 2): a hard
# link to the Snapshot the sandbox was created from. The Snapshot cache replaces a cache
# entry by renaming directories and never writes into a file that is there, so the link
# keeps this sandbox's basis byte for byte as it was at creation, which is what a reset
# and a diff work from (requirement 15.13).
BASE_DB_FILENAME: Final = "base.sqlite"

# The version of the storage format, written into the database header as its
# ``user_version``. A Snapshot of an older format is not read: the cache rebuilds an entry
# whose framework version differs from the current one (requirement 20.12), so this number
# only has to change with the layout of the tables, never with the data in them.
STORAGE_FORMAT_VERSION: Final = 1

# The page size of every snapshot and sandbox database. SQLite's own default has changed
# between versions; fixing it keeps a snapshot's bytes independent of the build's SQLite
# defaults and keeps ``db_bytes`` (requirement 23.12) comparable between sandboxes.
PAGE_SIZE: Final = 4096

# Pragmas the connection of a build database asserts, in the order design.md 3.3 lists
# them. page_size, auto_vacuum and encoding only take hold while the file is still empty,
# and writing user_version is the first write, which initialises it with them; the three
# are part of the output of ``VACUUM INTO``, so a build must set them before its first
# table. journal_mode=MEMORY keeps the rollback a failed step needs without putting a
# journal file next to the database, and synchronous=OFF leaves out the fsyncs a database
# that is thrown away after the build does not need - the Snapshot written out of it is
# fsynced instead (:func:`finalize_snapshot`).
BUILD_PRAGMAS: Final = (
    f"PRAGMA page_size={PAGE_SIZE}",
    "PRAGMA auto_vacuum=NONE",
    "PRAGMA encoding='UTF-8'",
    f"PRAGMA user_version={STORAGE_FORMAT_VERSION}",
    "PRAGMA journal_mode=MEMORY",
    "PRAGMA synchronous=OFF",
)

# The errnos of a hard link that cannot be made because of where the two paths are, rather
# than because of what they are: the destination is on another file system (EXDEV), the
# file system does not support links at all (EPERM on several of them, ENOTSUP elsewhere)
# or the source has as many links as it can hold (EMLINK). Each of them means "copy the
# file instead" (design.md 3.3); every other error is the caller's to see.
_LINK_FALLBACK_ERRNOS: Final = frozenset(
    {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}
)


def build_snapshot_db(path: Path, schema: StorageSchema) -> SandboxStore:
    """Create the build database of one Snapshot at ``path`` and return it open and empty.

    The file is new - its directory is created when it does not exist yet - the connection
    asserts :data:`BUILD_PRAGMAS`, and every table of ``schema`` is created, in the
    schema's order. The caller then writes the records of the dataset through the engine
    and ends with :func:`finalize_snapshot` (design.md 6.1); the store is the caller's to
    close.

    Raises FileExistsError when ``path`` is there already, because the pragmas that decide
    the output's bytes only take hold on an empty file.
    """
    if path.exists():
        raise FileExistsError(f"the build database is already there: {path}")
    store = SandboxStore(path.parent, schema, db_name=path.name, pragmas=BUILD_PRAGMAS)
    store.open()
    try:
        store.create_tables()
    except Exception:
        store.close()
        raise
    return store


def finalize_snapshot(build_db: Path, out: Path) -> None:
    """Write the Snapshot of ``build_db`` to ``out`` and fsync it (requirement 20.13).

    ``VACUUM INTO`` rewrites every page of the build database into the new file ``out``,
    table by table and in primary key order, so the bytes depend on the content and the
    SQLite version alone (see the module docstring). SQLite does not fsync the output
    itself, so this does: the Snapshot cache replaces an entry only once its three files
    are on disk (design.md 6.2).

    ``build_db`` is read through a connection of its own and is not changed, so the store
    the build ran on may still be open, as long as no transaction of it is. Raises
    FileNotFoundError when ``build_db`` is not a file and ``sqlite3.OperationalError``
    when ``out`` is there already, which ``VACUUM INTO`` refuses.
    """
    if not build_db.is_file():
        raise FileNotFoundError(f"no such build database: {build_db}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(build_db, isolation_level=None)) as connection:
        connection.execute("VACUUM INTO ?", (str(out),))
    _fsync(out)


def copy_snapshot(snapshot: Path, dest: Path) -> None:
    """Copy ``snapshot`` to the new database ``dest`` (requirements 15.1 and 15.13).

    This is how a sandbox gets its working database: the Snapshot is opened read-only and
    immutable and the backup API writes its pages into a file of its own, so the sandbox's
    records, counters and effective calls start out as the ones of the initial dataset and
    the Snapshot file is only read. The copy is in a rollback journal mode;
    :meth:`~erp_mock.state.store.SandboxStore.open` switches it to WAL.

    Raises FileNotFoundError when ``snapshot`` is not a file and FileExistsError when
    ``dest`` is there already.
    """
    _check_copy(snapshot, dest)
    with (
        closing(sqlite3.connect(immutable_uri(snapshot), uri=True)) as source,
        closing(sqlite3.connect(dest, isolation_level=None)) as target,
    ):
        source.backup(target)


def link_snapshot(snapshot: Path, dest: Path) -> None:
    """Hard link ``snapshot`` as ``dest``, copying the file when it cannot be linked.

    This is how a sandbox gets its ``base.sqlite`` (:data:`BASE_DB_FILENAME`): a link
    costs nothing and fixes the sandbox's basis at the Snapshot of its creation, whatever
    the cache does to that entry later (requirement 15.13). A link is not always possible
    - the data directory may span file systems, or sit on one that has no links - and the
    file is then copied byte for byte, which gives the same basis at the cost of its size.

    Raises FileNotFoundError when ``snapshot`` is not a file and FileExistsError when
    ``dest`` is there already.
    """
    _check_copy(snapshot, dest)
    try:
        os.link(snapshot, dest)
    except OSError as error:
        if error.errno not in _LINK_FALLBACK_ERRNOS:
            raise
        # copyfile reads and writes the bytes, so the copy is identical to the source;
        # the source is never written to while a reader holds the cache's shared lock.
        shutil.copyfile(snapshot, dest)


def _check_copy(snapshot: Path, dest: Path) -> None:
    """Check that ``snapshot`` can be read and ``dest`` written, and create its directory."""
    if not snapshot.is_file():
        raise FileNotFoundError(f"no such snapshot: {snapshot}")
    if dest.exists():
        raise FileExistsError(f"the database is already there: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)


def _fsync(path: Path) -> None:
    """Flush ``path`` to disk."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
