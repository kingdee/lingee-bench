"""erp_mock.seed.cache: the Snapshot cache below the data directory (reqs 20.11, 20.12).

Every test works on a data directory of its own and on a small dataset like the fixture's -
the Shared_Data of ``fx_base`` and nothing else - since what is inside a Snapshot has no
part in how it is cached. The builder is a :class:`_Counting` one: it builds for real and
notes each build, which is how a test tells an entry that was taken up from one that was
built again.

The tests cover the three things the cache decides: where an entry goes and when it is
taken up (requirement 20.11), every condition that makes one stale (requirement 20.12),
and what a build that failed leaves behind, which is nothing (requirement 20.11). The
locks and the in-process queue of design.md 6.2 are checked through the lock file itself:
``flock`` belongs to the open file, so a second descriptor on it in this process conflicts
with the cache's just as another process would.
"""

import fcntl
import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Final

import pytest

from erp_mock.common.jsonio import JsonValue
from erp_mock.config.instance import DatasetDef
from erp_mock.loader.instance import LoadedInstance
from erp_mock.seed.builder import (
    ALIASES_FILENAME,
    META_FILENAME,
    SNAPSHOT_FILENAME,
    SeedBuilder,
    SnapshotKey,
    SnapshotRef,
    snapshot_key,
)
from erp_mock.seed.cache import LOCK_SUFFIX, SNAPSHOTS_DIR, TMP_DIR, TRASH_DIR, SnapshotCache
from erp_mock.seed.resolve import SeedError
from tests.helpers.fixtures import ANCHOR_DATE, BASE_PACK, DATASET, INSTANCE_DIR, load_fixture

# The key every test builds: the fixture's dataset at the fixture's anchor date.
KEY: Final = SnapshotKey(dataset=f"{DATASET}@1.0.0", anchor=ANCHOR_DATE)

# Two employees of the fixture's Shared_Data, so a dataset can change its current user
# without changing anything else (requirement 20.12).
USER: Final = "opr_000001"
OTHER_USER: Final = "opr_000002"

# Records whose build fails at an engine operation: a new order for a disabled supplier
# breaks R-GEN-006 (requirement 5.7), which the build reports as a SeedError after it has
# created the records before it (requirement 20.15).
BAD_ENTITIES: Final[Mapping[str, object]] = {
    "vendor:retired": {"kind": "vendor", "name": "夹具停用供货单位", "status": "disabled"},
    "article:part": {
        "kind": "article",
        "name": "夹具甲存货",
        "grade": "plain",
        "base_measure": "PCS",
    },
    "supply_order:bad": {
        "kind": "supply_order",
        "vendor": "vendor:retired",
        "biz_date": "today",
        "need_date": "today+7d",
        "lines": [{"article": "article:part", "measure": "PCS", "qty": "1"}],
    },
}

# How long a test waits for a thread that must get through, and how long it waits on one
# that must not: a build of this dataset takes a few dozen milliseconds, so a second that
# passes without it finishing means it is waiting on the lock, and 30 seconds is an upper
# bound that only a hang reaches.
JOIN_TIMEOUT: Final = 30.0
BLOCKED_FOR: Final = 1.0


@cache
def _loaded() -> LoadedInstance:
    """The fixture's instance configuration, loaded once for the module."""
    return load_fixture().assemble()


def definition(**changes: object) -> DatasetDef:
    """A dataset definition like the fixture's, with ``changes`` written into it."""
    written = {
        "dataset": DATASET,
        "version": "1.0.0",
        "random_seed": 7,
        "include_shared": [BASE_PACK],
        "current_user": USER,
        **changes,
    }
    return DatasetDef.model_validate(written)


class _Counting(SeedBuilder):
    """The fixture's builder, noting the key of every build it is asked for."""

    def __init__(self, loaded: LoadedInstance, instance_dir: Path) -> None:
        super().__init__(loaded, instance_dir)
        self.builds: list[SnapshotKey] = []

    def build(
        self, dataset: DatasetDef, anchor: date, out_dir: Path, *, file: str | None = None
    ) -> SnapshotRef:
        self.builds.append(snapshot_key(dataset, anchor))
        return super().build(dataset, anchor, out_dir, file=file)


@dataclass(frozen=True, slots=True)
class Cached:
    """One cache over one data directory, and the builder that counts its builds."""

    cache: SnapshotCache
    builder: _Counting

    @property
    def builds(self) -> int:
        """How many builds the cache has asked for so far."""
        return len(self.builder.builds)

    def get(self, **changes: object) -> SnapshotRef:
        """``get_or_build`` for a dataset like the fixture's with ``changes`` in it."""
        return self.cache.get_or_build(definition(**changes), ANCHOR_DATE)

    def rebuild(self, **changes: object) -> SnapshotRef:
        """``rebuild`` for a dataset like the fixture's with ``changes`` in it."""
        return self.cache.rebuild(definition(**changes), ANCHOR_DATE)

    def failure(self, rebuild: bool = False) -> SeedError:
        """The error a dataset whose build fails at an engine operation raises."""
        with pytest.raises(SeedError) as caught:
            if rebuild:
                self.rebuild(entities=dict(BAD_ENTITIES))
            else:
                self.get(entities=dict(BAD_ENTITIES))
        return caught.value

    def content(self, ref: SnapshotRef) -> list[bytes]:
        """The bytes of the three files of ``ref``, in their order."""
        return [path.read_bytes() for path in ref.files]

    def meta(self, ref: SnapshotRef) -> dict[str, JsonValue]:
        """The parsed ``meta.json`` of the entry ``ref``."""
        payload = json.loads(ref.meta.read_text("utf-8"))
        assert isinstance(payload, dict)
        return payload

    def tamper(self, ref: SnapshotRef, **changes: JsonValue) -> None:
        """Write ``changes`` into the ``meta.json`` of the entry ``ref``.

        What an entry built by another framework version, another pack version or from
        another definition file looks like, which no dataset of this configuration can
        bring about (requirement 20.12).
        """
        ref.meta.write_text(json.dumps(self.meta(ref) | changes), "utf-8")

    def spare(self, name: str) -> list[Path]:
        """What is left in ``.tmp`` or ``.trash``; both are empty between calls."""
        directory = self.cache.root / name
        return [] if not directory.exists() else sorted(directory.iterdir())


@pytest.fixture
def cached(tmp_path: Path) -> Cached:
    """A cache over an empty data directory, with a builder that counts its builds."""
    builder = _Counting(_loaded(), INSTANCE_DIR)
    return Cached(cache=SnapshotCache(tmp_path / "data", builder), builder=builder)


# --- where an entry goes and when it is taken up (requirement 20.11) ---------------------


@pytest.mark.req("20.11")
def test_the_first_call_builds_the_entry_of_its_key(cached: Cached, tmp_path: Path) -> None:
    ref = cached.get()
    assert cached.builder.builds == [KEY]
    # snapshots/<dataset>@<version>/<YYYY-MM-DD>/, with the lock file beside it.
    assert ref.directory == tmp_path / "data" / SNAPSHOTS_DIR / KEY.dataset / "2026-10-09"
    assert cached.cache.lock_path(KEY) == ref.directory.parent / f"2026-10-09{LOCK_SUFFIX}"
    assert sorted(path.name for path in ref.directory.iterdir()) == [
        ALIASES_FILENAME,
        META_FILENAME,
        SNAPSHOT_FILENAME,
    ]
    assert sorted(path.name for path in ref.directory.parent.iterdir()) == [
        "2026-10-09",
        f"2026-10-09{LOCK_SUFFIX}",
    ]
    # The build happened in .tmp and left nothing there; nothing was replaced.
    assert cached.spare(TMP_DIR) == []
    assert cached.spare(TRASH_DIR) == []


@pytest.mark.req("20.11")
def test_an_entry_that_still_fits_is_taken_up_without_building(cached: Cached) -> None:
    ref = cached.get()
    before = cached.content(ref)
    again = cached.get()
    assert again == ref
    assert cached.builds == 1
    assert cached.content(again) == before


# --- what makes an entry stale (requirement 20.12) ---------------------------------------


@pytest.mark.req("20.12")
@pytest.mark.parametrize("filename", [SNAPSHOT_FILENAME, META_FILENAME, ALIASES_FILENAME])
def test_a_cache_file_that_is_missing_is_rebuilt(cached: Cached, filename: str) -> None:
    ref = cached.get()
    before = cached.content(ref)
    (ref.directory / filename).unlink()
    assert cached.content(cached.get()) == before
    assert cached.builds == 2


@pytest.mark.req("20.12")
@pytest.mark.parametrize(
    "written",
    [pytest.param(b"{not json", id="no-json"), pytest.param(b"[]\n", id="no-object")],
)
def test_metadata_that_cannot_be_read_is_rebuilt(cached: Cached, written: bytes) -> None:
    ref = cached.get()
    before = cached.content(ref)
    ref.meta.write_bytes(written)
    assert cached.content(cached.get()) == before
    assert cached.builds == 2


@pytest.mark.req("20.12")
@pytest.mark.parametrize(
    ("key", "stale"),
    [
        pytest.param("framework_version", "0.0.1", id="framework-version"),
        pytest.param("meta_model_version", "0.0.1", id="meta-model-version"),
        pytest.param("packs", {"fx_base": "0.9.0", "fx_trade": "1.0.0"}, id="pack-version"),
        pytest.param("dataset_sha256", "0" * 64, id="dataset-digest"),
        pytest.param("random_seed", 99, id="random-seed"),
        pytest.param("current_user", OTHER_USER, id="current-user"),
    ],
)
def test_metadata_that_differs_from_the_current_values_is_rebuilt(
    cached: Cached, key: str, stale: JsonValue
) -> None:
    ref = cached.get()
    current = cached.meta(ref)[key]
    assert current != stale
    cached.tamper(ref, **{key: stale})
    # The entry is built again and its metadata carries the current value once more.
    assert cached.meta(cached.get())[key] == current
    assert cached.builds == 2


@pytest.mark.req("20.12")
def test_a_key_that_is_missing_from_the_metadata_is_rebuilt(cached: Cached) -> None:
    ref = cached.get()
    without = {key: value for key, value in cached.meta(ref).items() if key != "current_user"}
    ref.meta.write_text(json.dumps(without), "utf-8")
    assert cached.meta(cached.get())["current_user"] == USER
    assert cached.builds == 2


@pytest.mark.req("20.12")
def test_a_definition_with_another_random_seed_is_rebuilt(cached: Cached) -> None:
    ref = cached.get()
    assert cached.meta(ref)["random_seed"] == 7
    # The dataset's own random_seed is part of the metadata, so changing it in the
    # definition - the key staying the same - makes the entry stale.
    assert cached.meta(cached.get(random_seed=8))["random_seed"] == 8
    assert cached.builder.builds == [KEY, KEY]
    assert cached.content(cached.get(random_seed=8)) == cached.content(ref)
    assert cached.builds == 2


@pytest.mark.req("20.12")
def test_a_definition_with_another_current_user_is_rebuilt(cached: Cached) -> None:
    ref = cached.get()
    assert cached.meta(ref)["current_user"] == USER
    assert cached.meta(cached.get(current_user=OTHER_USER))["current_user"] == OTHER_USER
    assert cached.builds == 2


# --- the replacement (requirement 20.11) -------------------------------------------------


@pytest.mark.req("20.11")
def test_a_replacement_leaves_one_whole_entry_and_nothing_beside_it(cached: Cached) -> None:
    ref = cached.get()
    cached.tamper(ref, framework_version="0.0.1")
    replaced = cached.get()
    assert replaced == ref
    assert cached.builds == 2
    # The new entry is in the place of the old one, which is gone from .trash as well.
    assert sorted(path.name for path in replaced.directory.iterdir()) == [
        ALIASES_FILENAME,
        META_FILENAME,
        SNAPSHOT_FILENAME,
    ]
    assert cached.spare(TMP_DIR) == []
    assert cached.spare(TRASH_DIR) == []


# --- rebuild (requirement 22.6) ----------------------------------------------------------


@pytest.mark.req("20.11")
def test_rebuild_builds_an_entry_it_would_have_taken_up(cached: Cached) -> None:
    ref = cached.get()
    before = cached.content(ref)
    again = cached.rebuild()
    assert again == ref
    assert cached.builder.builds == [KEY, KEY]
    # The same dataset at the same anchor date, so the files come out as they were
    # (requirement 20.13); the entry is one a later call takes up.
    assert cached.content(again) == before
    assert cached.get() == ref
    assert cached.builds == 2
    assert cached.spare(TMP_DIR) == []
    assert cached.spare(TRASH_DIR) == []


# --- a build that fails (requirement 20.11) ----------------------------------------------


@pytest.mark.req("20.11")
def test_a_build_that_fails_leaves_no_entry_behind(cached: Cached) -> None:
    error = cached.failure()
    assert error.config_path == "entities.supply_order:bad"
    assert "rule_id R-GEN-006" in error.reason
    # Nothing was renamed into place and the directory the build worked in is gone.
    assert not cached.cache.directory(KEY).exists()
    assert cached.spare(TMP_DIR) == []
    assert cached.spare(TRASH_DIR) == []


@pytest.mark.req("20.11")
def test_a_build_that_fails_leaves_the_entry_as_it_was(cached: Cached) -> None:
    ref = cached.get()
    before = cached.content(ref)
    cached.failure(rebuild=True)
    # The three files are the ones of the entry that was there, byte for byte, and the
    # entry is still one the cache takes up without building.
    assert cached.content(ref) == before
    assert cached.get() == ref
    assert cached.builds == 2
    assert cached.spare(TMP_DIR) == []
    assert cached.spare(TRASH_DIR) == []


# --- the locks of one key (design.md 6.2) ------------------------------------------------


@pytest.mark.req("20.11")
def test_a_reader_holds_the_shared_lock_of_the_key(cached: Cached) -> None:
    ref = cached.get()
    lock = cached.cache.lock_path(ref.key)
    with cached.cache.shared(ref) as held:
        assert held == ref
        # Another reader gets the lock as well; a replacement has to wait.
        assert _can_lock(lock, fcntl.LOCK_SH)
        assert not _can_lock(lock, fcntl.LOCK_EX)
    assert _can_lock(lock, fcntl.LOCK_EX)


@pytest.mark.req("20.11")
def test_nothing_of_a_key_moves_while_its_lock_is_held_exclusively(cached: Cached) -> None:
    ref = cached.get()
    cached.tamper(ref, framework_version="0.0.1")
    stale = cached.content(ref)
    done = threading.Event()

    def replace() -> None:
        cached.get()
        done.set()

    with _locked(cached.cache.lock_path(ref.key), fcntl.LOCK_EX):
        thread = threading.Thread(target=replace)
        thread.start()
        # The entry is stale, so the call has to read it and then replace it, and both
        # take the lock of the key; a build of this dataset is a fraction of the wait
        # below, so what the call is still doing is waiting.
        assert not done.wait(BLOCKED_FOR)
        assert cached.content(ref) == stale
    thread.join(JOIN_TIMEOUT)
    assert done.is_set()
    assert cached.meta(ref)["framework_version"] != "0.0.1"
    assert cached.builds == 2


@pytest.mark.req("20.11")
def test_one_key_is_built_once_however_many_threads_ask_for_it(cached: Cached) -> None:
    # Two threads asking for the same key at the same time: the first one through builds
    # and replaces the entry, the second takes up what it left (design.md 6.2).
    start = threading.Barrier(2)
    refs: list[SnapshotRef] = []
    guard = threading.Lock()

    def ask() -> None:
        start.wait(JOIN_TIMEOUT)
        ref = cached.get()
        with guard:
            refs.append(ref)

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN_TIMEOUT)
        assert not thread.is_alive()
    assert cached.builder.builds == [KEY]
    assert len(refs) == 2 and refs[0] == refs[1]
    assert all(path.is_file() for path in refs[0].files)


# --- holding the lock file from the test (design.md 6.2) ---------------------------------


def _can_lock(path: Path, operation: int) -> bool:
    """Whether ``operation`` can be taken on ``path`` right now, without waiting.

    A descriptor of its own, which is what makes this a test of the lock and not of this
    process: ``flock`` belongs to the open file, so this conflicts with the cache's lock
    the way another process would.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


@contextmanager
def _locked(path: Path, operation: int) -> Iterator[None]:
    """Hold ``operation`` on ``path`` for the block, the way another process would."""
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, operation)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
