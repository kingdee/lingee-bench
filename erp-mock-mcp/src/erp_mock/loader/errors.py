"""Configuration errors of the validation steps and their one-line report (requirement 2.2).

Loading runs the 8 steps of business-pack.md section 6 followed by the cross-check
(design.md 2.3). A step collects every error it finds in a :class:`StepErrors` and ends
with :class:`ConfigErrors` when that collector is not empty, so one step reports its full
list at once and the steps after it do not run; the CLI writes the list to stderr and
exits with code 1 (requirements 2.2, 22.5 and 22.7).

A :class:`ConfigError` carries the business pack it belongs to (None for an error in the
instance config), every place it involves and the reason. A place is a :class:`Location`:
the configuration file, written relative to the pack or the instance directory, and the
config path inside it, such as ``header.delivery_date.default``. The config path is None
where no config item corresponds to the error, as for an invariant that does not hold; a
YAML error that cannot be located in the structure uses its line instead
(``line 12``, :attr:`~erp_mock.loader.yaml_io.YamlLoadError.config_path`). An error that
involves several places, such as a namespace conflict or a dependency cycle, lists each of
them.

Each error is reported as one line, ``[step N] <pack>: <file>: <config path>: <reason>``,
where the pack and the config path are left out when they are None and the places of a
multi-place error are separated by ``" | "``::

    [step 5] purchase: entities/purchase_order.yaml: header.delivery_date.default: 缺省值与字段类型 date 不符
    [step 1] instance.yaml: packs[0].version: 包引用的版本必须是具体版本
    [step 8] datasets/purchase-basic.yaml: 初始数据集 purchase-basic 的 Snapshot 不满足不变量：INV-1 不成立……
    [step 3] core: pack.yaml: depends_on | purchase: pack.yaml: depends_on: 循环依赖

:class:`ConfigErrors` sorts the errors by (step, position of the pack in instance.yaml,
file, config path), so the same configuration always produces the same output
(requirement 24).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError

__all__ = [
    "CROSS",
    "KEY_MARKER",
    "ConfigError",
    "ConfigErrors",
    "Location",
    "Step",
    "StepErrors",
    "config_path",
    "sort_errors",
]

# The cross-check of descriptions against Business_Data, which runs after step 8
# (requirement 2.1); it is a step of its own, reported as "[step cross]".
CROSS: Final[Literal["cross"]] = "cross"

# A validation step: 1 to 8, or the cross-check.
type Step = int | Literal["cross"]

# pydantic locates an error about a mapping key, not about its value, at
# ``(..., <key>, "[key]")``.
KEY_MARKER: Final = "[key]"

# The parts of the sort key of one error: (step, pack, places, reason).
type SortKey = tuple[tuple[int, int], tuple[int, str], tuple[tuple[str, str], ...], str]


def config_path(loc: Iterable[str | int]) -> str | None:
    """Return the config path of a pydantic error ``loc``, or None when it names no item.

    A mapping key becomes ``.<key>`` (the first part without the dot) and a list index
    ``[<index>]``, so ``("header", "delivery_date", "default")`` gives
    ``header.delivery_date.default`` and ``("packs", 0, "version")`` gives
    ``packs[0].version``. A :data:`KEY_MARKER` part is dropped: the key the error is
    about is already the last part of the path (``values.Bad-Code``). An empty ``loc``,
    that is, an error about the document as a whole, gives None, and the error is then
    reported without a config path.
    """
    parts: list[str] = []
    for entry in loc:
        if isinstance(entry, int):
            parts.append(f"[{entry}]")
        elif entry != KEY_MARKER:
            parts.append(f".{entry}" if parts else entry)
    return "".join(parts) or None


@dataclass(frozen=True, slots=True)
class Location:
    """One place a configuration error involves.

    ``file`` is the configuration file, relative to the business pack directory or, for
    the instance config, to the instance directory. ``config_path`` locates the item
    inside the file and is None when no item corresponds to the error.
    """

    file: str
    config_path: str | None = None

    def __str__(self) -> str:
        """``"<file>: <config path>"``, or just the file when there is no config path."""
        return self.file if self.config_path is None else f"{self.file}: {self.config_path}"


@dataclass(frozen=True, slots=True)
class ConfigError:
    """One configuration error found by one validation step (requirement 2.2).

    - ``step``: the step that found it, 1 to 8 or :data:`CROSS`.
    - ``pack``: the business pack the error belongs to, None for the instance config. An
      error that involves several packs, such as a namespace conflict, names them all in
      its reason and takes the first of them (instance.yaml order) as its ``pack``, so
      that it is reported and sorted with that pack.
    - ``locations``: every place the error involves, in report order.
    - ``reason``: a one-line description in simplified Chinese.
    """

    step: Step
    pack: str | None
    locations: tuple[Location, ...]
    reason: str

    def line(self) -> str:
        """The error as one line (see the module docstring)."""
        places = " | ".join(str(location) for location in self.locations)
        parts = [part for part in (self.pack, places, self.reason) if part]
        return f"[step {self.step}] " + ": ".join(parts)

    def __str__(self) -> str:
        return self.line()


def _sort_key(error: ConfigError, positions: Mapping[str, int]) -> SortKey:
    """Return the sort key of ``error``; ``positions`` holds the index of each pack.

    The key is (step, pack, places, reason): the cross-check sorts after the numbered
    steps; the instance config sorts before every pack, and a pack that instance.yaml
    does not list (step 1 can report one) after every listed pack, by name; the places
    are compared one after another, a missing config path before any config path; the
    reason decides between errors that are alike in every other part, so the order never
    depends on the order in which the checks ran.
    """
    step_key = (0, error.step) if isinstance(error.step, int) else (1, 0)
    pack = error.pack
    pack_key = (-1, "") if pack is None else (positions.get(pack, len(positions)), pack)
    places = tuple((place.file, place.config_path or "") for place in error.locations)
    return (step_key, pack_key, places, error.reason)


def sort_errors(
    errors: Iterable[ConfigError], packs: Sequence[str] = ()
) -> tuple[ConfigError, ...]:
    """Return ``errors`` in report order; ``packs`` are the pack names in instance.yaml order."""
    positions = {pack: index for index, pack in enumerate(packs)}
    return tuple(sorted(errors, key=lambda error: _sort_key(error, positions)))


class ConfigErrors(Exception):
    """The errors of one validation step, raised at the end of that step.

    Fields:

    - ``step``: the step that found them.
    - ``errors``: the errors in report order, never empty.
    - ``packs``: the pack names in instance.yaml order, which decided that order.

    ``lines()`` gives one line per error and ``str(...)`` joins them with newlines, the
    list the CLI writes to stderr before it exits with code 1 (requirements 22.5
    and 22.7).
    """

    step: Step
    errors: tuple[ConfigError, ...]
    packs: tuple[str, ...]

    def __init__(
        self, step: Step, errors: Iterable[ConfigError], packs: Sequence[str] = ()
    ) -> None:
        order = tuple(packs)
        sorted_errors = sort_errors(errors, order)
        if not sorted_errors:
            raise ValueError("ConfigErrors needs at least one error")
        # All fields go to args, so the exception pickles and copies.
        super().__init__(step, sorted_errors, order)
        self.step = step
        self.errors = sorted_errors
        self.packs = order

    def lines(self) -> tuple[str, ...]:
        """The report lines of the errors, in order."""
        return tuple(error.line() for error in self.errors)

    def __str__(self) -> str:
        return "\n".join(self.lines())


class StepErrors:
    """Collector of every error one validation step finds (requirement 2.2).

    The loader creates one collector per step, hands it to the checks of that step so
    that they record what they find instead of stopping at the first error, and ends the
    step with :meth:`raise_if_any`.
    """

    __slots__ = ("step", "_errors")

    def __init__(self, step: Step) -> None:
        self.step = step
        self._errors: list[ConfigError] = []

    @property
    def errors(self) -> tuple[ConfigError, ...]:
        """The errors recorded so far, in the order the checks recorded them."""
        return tuple(self._errors)

    def __len__(self) -> int:
        """The number of errors recorded so far; a collector with none is falsy."""
        return len(self._errors)

    def add(self, pack: str | None, locations: Iterable[Location], reason: str) -> None:
        """Record an error at every place it involves (requirement 2.2)."""
        self._errors.append(ConfigError(self.step, pack, tuple(locations), reason))

    def add_at(self, pack: str | None, file: str, config_path: str | None, reason: str) -> None:
        """Record an error at one place; ``config_path`` is None when no item corresponds."""
        self.add(pack, (Location(file, config_path),), reason)

    def add_model_errors(self, pack: str | None, file: str, error: ValidationError) -> None:
        """Record one error per line error of ``error``, each at the config path of its ``loc``."""
        for detail in error.errors():
            self.add_at(pack, file, config_path(detail["loc"]), detail["msg"])

    def extend(self, errors: Iterable[ConfigError]) -> None:
        """Record errors built elsewhere, such as by the checks of another module."""
        self._errors.extend(errors)

    def raise_if_any(self, packs: Sequence[str] = ()) -> None:
        """Raise :class:`ConfigErrors` with every recorded error, sorted by ``packs`` order.

        Does nothing when the step found no error, so that the next step runs.
        """
        if self._errors:
            raise ConfigErrors(self.step, self._errors, packs)
