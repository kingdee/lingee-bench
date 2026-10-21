"""The contract snapshots of tests/contract/snapshots/ and how a run may rewrite them.

A contract snapshot is a file in ``tests/contract/snapshots/`` holding bytes the service
produces right now. A normal run only compares: the test fails when the bytes differ and
reports a unified diff of the two texts, so a change to what a client receives shows up as
a reviewable change of the snapshot file. Rewriting those files takes the option this
module registers (requirement 25.3)::

    uv run pytest tests/contract                      # compare only
    uv run pytest tests/contract --update-snapshots   # rewrite what differs

Without the option, :meth:`Snapshots.check` never opens a snapshot file for writing - a
missing file is reported the same way a differing one is - so a snapshot cannot be blessed
by accident, which is the point of keeping it in the repository: it changes in a commit, on
purpose, next to the change that caused it.

A snapshot says nothing about whether the content is right, only that it has not changed.
What a payload has to be regardless of the file therefore belongs in the test as an
assertion of its own (tests/contract/test_tools_list.py asserts the 22 tools and their
order on the payload, not on the snapshot), so that a careless ``--update-snapshots`` still
fails.
"""

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

# tests/contract/snapshots/: one file per contract snapshot, named after what it holds.
SNAPSHOTS_DIR: Final = Path(__file__).parent / "snapshots"

# The option that makes a run rewrite the snapshot files instead of only comparing.
UPDATE_OPTION: Final = "--update-snapshots"

# How many lines of a unified diff a failure reports. A snapshot is thousands of lines
# long, and a diff of a few hundred of them is already more than a reader needs to see what
# kind of change happened; the file itself holds the rest.
DIFF_LIMIT: Final = 200


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--update-snapshots`` (requirement 25.3).

    Off by default, so every run that does not ask for it - a developer's, CI's - compares
    the snapshots and leaves the files alone.
    """
    parser.addoption(
        UPDATE_OPTION,
        action="store_true",
        default=False,
        help=(
            "改写 tests/contract/snapshots/ 下与本次运行结果不一致的契约快照；"
            "不带该选项时只比较，不写入任何快照文件。"
        ),
    )


@dataclass(frozen=True, slots=True)
class Snapshots:
    """The snapshot directory of a run, and whether that run may rewrite it.

    - ``directory``: where the snapshot files are (:data:`SNAPSHOTS_DIR`).
    - ``update``: whether :data:`UPDATE_OPTION` was given.
    """

    directory: Path
    update: bool

    def check(self, name: str, content: bytes) -> None:
        """Compare ``content`` with the snapshot ``name``, byte for byte.

        With :data:`UPDATE_OPTION`, the file is written instead - and only when its bytes
        differ, so an unchanged snapshot keeps its modification time and a run that
        rewrites nothing touches nothing. Without it, a difference and a missing file both
        fail the test, the difference with a unified diff of the two texts.
        """
        path = self.directory / name
        found = path.read_bytes() if path.is_file() else None
        if found == content:
            return
        if self.update:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return
        pytest.fail(self._report(path, found, content), pytrace=False)

    def _report(self, path: Path, found: bytes | None, content: bytes) -> str:
        """Why ``path`` does not match, and how to accept the current result."""
        head = (
            f"契约快照 {path.name} 不存在"
            if found is None
            else f"契约快照 {path.name} 与当前生成的结果不一致"
        )
        lines = [
            f"{head}（{path}）。",
            f"确认改动符合预期后，用 `uv run pytest tests/contract {UPDATE_OPTION}` 改写快照。",
        ]
        if found is not None:
            lines += ["", *_unified_diff(found, content, path.name)]
        return "\n".join(lines)


@pytest.fixture(scope="session")
def snapshots(pytestconfig: pytest.Config) -> Snapshots:
    """The contract snapshots of this run (:class:`Snapshots`)."""
    return Snapshots(directory=SNAPSHOTS_DIR, update=bool(pytestconfig.getoption(UPDATE_OPTION)))


def _unified_diff(found: bytes, content: bytes, name: str) -> list[str]:
    """A unified diff from the snapshot file to the current result, at most DIFF_LIMIT lines.

    Both sides are decoded as UTF-8 for the diff alone - the comparison itself is on the
    bytes - replacing what does not decode, so a corrupted file is still reported as a
    difference rather than as an error.
    """
    diff = list(
        difflib.unified_diff(
            _text(found).splitlines(keepends=True),
            _text(content).splitlines(keepends=True),
            fromfile=f"{name} (快照)",
            tofile=f"{name} (当前)",
        )
    )
    shown = [line.rstrip("\n") for line in diff[:DIFF_LIMIT]]
    if len(diff) > DIFF_LIMIT:
        shown.append(f"... diff 还有 {len(diff) - DIFF_LIMIT} 行，未全部列出")
    return shown or ["两边的字节不同，但按 UTF-8 解码后的文本相同"]


def _text(content: bytes) -> str:
    """``content`` as text for the diff, with undecodable bytes replaced."""
    return content.decode("utf-8", "replace")
