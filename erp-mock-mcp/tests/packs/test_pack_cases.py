"""The pack level test cases of every business pack (requirement 21.9).

A business pack holds no code, so its tests are declarative too: one YAML file per case in
``<pack>/tests/``, collected and executed here (design.md, pack level tests). A case is a
``setup``, which the Seed_Builder writes into a database, and ``steps``, which the tools
layer replays against a sandbox copied from it::

    case: r_fxt_001_rejects_one_depot
    anchor_date: 2026-10-09                  # optional, the instance's own date otherwise
    setup:                                   # dataset records with aliases, on top of the
      article:part:                          # Shared_Data of the packs
        kind: article
        name: 回放用紧固件
        base_measure: PCS
    steps:
      - call: stock_move_draft_create
        args: { from_depot_id: dep_000001, to_depot_id: dep_000001,
                lines: [{ article_id: "$article:part", move_qty: "10" }] }
        expect: { is_error: true, error_code: VALIDATION_FAILED, rule_id: R-FXT-001 }

What each key means (:class:`CaseDoc`, :class:`StepDoc`, :class:`ExpectDoc`):

=================  ====================================================================
key                what it is
=================  ====================================================================
``case``           the name of the case, which is also the name of the dataset its
                   ``setup`` is built as, so it is written like a dataset name
``anchor_date``    the date the ``setup`` is built under and every step calls on, the
                   anchor date of the temporary instance where the case writes none
``setup``          the records of ``entities`` of a dataset definition, keyed by alias
                   (seed-data.md section 2): everything a dataset may write, a
                   ``status`` included, which runs the state operations that lead to it
                   and so performs an operation M0 exposes no tool for - the approval of
                   a stock adjustment, say (requirements 20.2 and 20.16)
``steps``          the tools/call of the replay, in order
``steps[].call``   the tool name, as tools/list returns it (requirement 11.9)
``steps[].args``   the ``arguments`` of the request, with ``$<alias>`` resolved
``steps[].alias``  an alias the ``id`` of the result is bound to, for the steps after it
``steps[].expect`` what the result must be; a step without one must succeed
``expect.is_error``    whether the call ends with an error code (requirement 13.6)
``expect.error_code``  the ``code`` of that error, unchecked when it is not written
``expect.rule_id``     the rule that decided it, unchecked when it is not written
``expect.result``      a subset of the structuredContent of a successful call: every key
                       written here must be there and match, arrays element by element
                       (:func:`subset_misses`)
=================  ====================================================================

``$<alias>`` names a record by the alias it was declared with, in any argument and at any
depth: the alias is replaced by the internal ID and whatever follows it is kept, so
``$supply_order:first`` is the bill and ``$supply_order:first-1`` its first line
(:func:`~tests.helpers.pack_instance.substitute`, requirement 4.2). An alias of a
``steps[].alias`` works the same way from the step after the one that bound it, which is
how a replay chains a create into the submit, the approve and the push that follow it.

What every case is checked for besides its own ``expect`` (requirement 21.9):

- INV-1, INV-2, INV-3 and INV-6 hold right after the setup and after every single step, so
  a configuration that leaves the stock, a writeback or a bill's amounts inconsistent is
  reported at the step that did it (:meth:`~tests.helpers.pack_instance.PackSandbox.violations`);
- no name and no code the ``setup`` writes occurs in any description of the profile: a
  description tells the model what a tool does and must not name a record of the data it
  works on (requirement 3.5), so a case that writes a name a description uses as an example
  would make that rule unkeepable for the pack (:func:`named_in_descriptions`).

Each pack is loaded in a temporary instance of its own, together with the packs it depends
on (:func:`~tests.helpers.pack_instance.pack_instance`): that instance passes every
validation step before any case of the pack runs, and the cases of one pack share it. Two
roots are collected, ``packs/`` and the packs of the engine test fixture
(:mod:`tests.helpers.fixtures`); ``packs/`` holds nothing until task 19.2 writes the first
example pack, and until then the fixture cases are what exercises the executor.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact
from erp_mock.config.base import FrozenList, FrozenModel
from erp_mock.config.instance import Alias, ConfigName
from erp_mock.loader.yaml_io import list_files, load_yaml
from erp_mock.tools.layer import ToolOutcome
from tests.helpers.fixtures import PACKS_DIR as FIXTURE_PACKS_DIR
from tests.helpers.pack_instance import (
    DatasetSpec,
    DescribedText,
    PackInstance,
    PackSandbox,
    pack_instance,
    substitute,
)

# The two roots the cases are collected from: the example business packs of the repository
# and the packs of the engine test fixture. The first one is written by task 19.2 and is
# not there yet, which collects nothing (module docstring).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PACKS_DIR: Final = REPO_ROOT / "packs"
CASE_ROOTS: Final[tuple[Path, ...]] = (PACKS_DIR, FIXTURE_PACKS_DIR)

# Where the cases of a pack are, below its own directory (requirement 21.9).
CASES_DIR: Final = "tests"

# The standard text fields of a record whose value is a name of the Business_Data, which no
# description may contain (requirement 3.5): the ``name`` and the ``code`` of a record. The
# ``number`` of a bill is the third one, which a dataset cannot write - it is read-only.
NAME_FIELDS: Final[tuple[str, ...]] = ("name", "code")

# The key of the internal ID in the result of a call, which a ``steps[].alias`` binds
# (requirement 13.3).
ID_KEY: Final = "id"


# --- the case file (design.md, pack level tests) -----------------------------------------


class ExpectDoc(FrozenModel):
    """``steps[].expect``: what the result of one step must be.

    ``is_error`` is whether the call ends with an error code, which is the ``isError`` of
    the result (requirement 13.6); ``error_code`` and ``rule_id`` are checked only when
    they are written, and ``result`` is a subset of the structuredContent of a successful
    call (:func:`subset_misses`).
    """

    is_error: bool = False
    error_code: str | None = None
    rule_id: str | None = None
    result: dict[str, Any] | None = None


class StepDoc(FrozenModel):
    """One item of ``steps``: one tools/call of the replay (requirement 12.1).

    ``call`` is the tool name and ``args`` the ``arguments`` the request carries, None for
    a call that carries none; ``alias`` binds the ``id`` of the result for the steps after
    this one and ``expect`` is what the result must be - a step that writes none must
    succeed.
    """

    call: str
    args: dict[str, Any] | None = None
    alias: Alias | None = None
    expect: ExpectDoc = ExpectDoc()


class CaseDoc(FrozenModel):
    """One ``<pack>/tests/<name>.yaml``: a setup and the steps replayed on top of it.

    ``case`` is the name, which the setup is built as a dataset under, so it is written
    like a dataset name (:data:`~erp_mock.config.instance.CONFIG_NAME_PATTERN`);
    ``anchor_date`` is the date the case works on, written as ``YYYY-MM-DD`` and left to
    the instance where the case writes none; ``setup`` are dataset records keyed by alias
    and ``steps`` the calls (module docstring).
    """

    case: ConfigName
    anchor_date: str | None = None
    setup: dict[Alias, dict[str, Any]] = {}
    steps: FrozenList[StepDoc] = ()


@dataclass(frozen=True, slots=True)
class PackCase:
    """One case file of one business pack, read and ready to run.

    ``root`` is the packs directory the pack was found in and ``pack`` its name, which
    together decide the temporary instance the case runs in (:class:`Instances`); ``file``
    is the case file, as an assertion names it.
    """

    root: Path
    pack: str
    file: Path
    document: CaseDoc

    @property
    def name(self) -> str:
        """The name of the case, which is the name of its dataset too."""
        return self.document.case

    @property
    def id(self) -> str:
        """How the parametrisation of the test names the case."""
        return f"{self.pack}/{self.name}"

    @property
    def anchor_date(self) -> date | None:
        """The date the case is built and called under, None taking the instance's own."""
        text = self.document.anchor_date
        return None if text is None else date.fromisoformat(text)

    @property
    def steps(self) -> tuple[StepDoc, ...]:
        """The calls of the replay, in the order the file writes them."""
        return tuple(self.document.steps)

    @property
    def dataset(self) -> DatasetSpec:
        """The ``setup`` of the case as an initial dataset (seed-data.md section 2)."""
        return DatasetSpec(name=self.name, records=dict(self.document.setup))

    @property
    def written_names(self) -> tuple[str, ...]:
        """Every name and code the ``setup`` writes, in the order it writes them.

        The values of :data:`NAME_FIELDS` of each record and of each of its lines, which is
        the Business_Data the setup adds (requirement 3.5); a value that is no plain text,
        a generator declaration among them, contributes nothing.
        """
        return tuple(_names_of(list(self.document.setup.values())))

    def about(self, step: int | None = None) -> str:
        """How an assertion names this case, and the step ``step`` of it, if any."""
        where = f"{self.file.relative_to(self.root.parent)}"
        return where if step is None else f"{where} 第 {step} 步"


def _names_of(records: Sequence[Mapping[str, Any]]) -> Iterator[str]:
    """The names and codes of ``records`` and of their lines (:attr:`PackCase.written_names`)."""
    for record in records:
        for field_name in NAME_FIELDS:
            value = record.get(field_name)
            if isinstance(value, str) and value:
                yield value
        lines = record.get("lines")
        if isinstance(lines, list):
            yield from _names_of([line for line in lines if isinstance(line, dict)])


def read_case(root: Path, pack: str, file: Path) -> PackCase:
    """Read and validate one case file of the pack ``pack`` of ``root``.

    Raises AssertionError for a file that is no case document, naming the file and what
    pydantic found: a key the format does not have, a missing ``call``, an alias that is no
    ``<kind>:<name>`` (:class:`CaseDoc`).
    """
    try:
        return PackCase(
            root=root, pack=pack, file=file, document=CaseDoc.model_validate(load_yaml(file))
        )
    except ValidationError as error:
        raise AssertionError(f"{file} 不是包级测试用例文件：\n{error}") from None


def collect_cases(root: Path) -> tuple[PackCase, ...]:
    """Every ``<pack>/tests/*.yaml`` of the packs directory ``root``, in path order.

    The packs are taken in the code point order of their directory names and the cases of a
    pack in that of their file names (:func:`~erp_mock.loader.yaml_io.list_files`), so the
    tests are collected in the same order on every file system. A ``root`` that is not
    there, or a pack without a ``tests/`` directory, contributes nothing.
    """
    if not root.is_dir():
        return ()
    found: list[PackCase] = []
    for pack_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for file in list_files(pack_dir / CASES_DIR, recursive=False):
            found.append(read_case(root, pack_dir.name, file))
    return tuple(found)


# Every case of both roots, read at collection time so each one is its own test.
CASES: Final[tuple[PackCase, ...]] = tuple(
    case for root in CASE_ROOTS for case in collect_cases(root)
)


# --- the temporary instance of each pack -------------------------------------------------


@dataclass(slots=True)
class Instances:
    """The temporary instance of each pack whose cases are run, built once and shared.

    Building one means writing an instance configuration over the pack and the packs it
    depends on and running every validation step on it, the Snapshot of its base dataset
    included (:func:`~tests.helpers.pack_instance.pack_instance`), so it is done once per
    pack and not once per case.
    """

    workdir: Path
    made: dict[tuple[Path, str], PackInstance] = field(default_factory=dict)

    def of(self, case: PackCase) -> PackInstance:
        """The instance the cases of ``case``'s pack run in; build it when it is the first."""
        key = (case.root, case.pack)
        found = self.made.get(key)
        if found is None:
            where = self.workdir / f"{len(self.made)}-{case.pack}"
            found = pack_instance(where, case.root, [case.pack])
            self.made[key] = found
        return found


@pytest.fixture(scope="session")
def instances(tmp_path_factory: pytest.TempPathFactory) -> Instances:
    """The instances of the packs whose cases this module runs, one directory per pack."""
    return Instances(workdir=tmp_path_factory.mktemp("pack-cases"))


# --- what a case asserts -----------------------------------------------------------------


def named_in_descriptions(
    names: Sequence[str], described: Sequence[DescribedText]
) -> tuple[tuple[str, DescribedText], ...]:
    """The names of ``names`` that occur in a description of ``described`` (req 3.5).

    One pair per (name, description), in the order of ``names`` and then of the
    descriptions; the comparison is the one of the cross-check - a substring, ASCII letters
    compared without regard to case (:meth:`~tests.helpers.pack_instance.DescribedText.contains`).
    """
    return tuple((name, text) for name in names for text in described if text.contains(name))


def subset_misses(expected: JsonValue, found: JsonValue, path: str = "") -> tuple[str, ...]:
    """Where ``found`` does not hold ``expected``, one text per place, in document order.

    ``expected`` is a subset of ``found``: an object must hold every key written in it and
    match below each of them, an array must have the same length and match element by
    element, and any other value must be equal. A decimal and a date come back as text
    (requirements 4.8 and 13.4), so an expectation is written as text too, and an empty
    tuple means the result holds everything the case expects.
    """
    at = path or "result"
    if isinstance(expected, Mapping):
        if not isinstance(found, Mapping):
            return (f"{at} 不是对象：{dumps_compact(found)}",)
        misses: list[str] = []
        for key, value in expected.items():
            if key not in found:
                misses.append(f"{at}.{key} 不在结果中")
                continue
            misses.extend(subset_misses(value, found[key], f"{at}.{key}"))
        return tuple(misses)
    if isinstance(expected, Sequence) and not isinstance(expected, str):
        if not isinstance(found, Sequence) or isinstance(found, str):
            return (f"{at} 不是数组：{dumps_compact(found)}",)
        if len(expected) != len(found):
            return (f"{at} 有 {len(found)} 个元素，期望 {len(expected)} 个",)
        return tuple(
            miss
            for index, (value, item) in enumerate(zip(expected, found, strict=True))
            for miss in subset_misses(value, item, f"{at}[{index}]")
        )
    if expected != found:
        return (f"{at} 为 {dumps_compact(found)}，期望 {dumps_compact(expected)}",)
    return ()


def assert_outcome(case: PackCase, step: int, doc: StepDoc, outcome: ToolOutcome) -> None:
    """Assert that ``outcome`` is what the step expects (:class:`ExpectDoc`)."""
    expect = doc.expect
    about = case.about(step)
    if expect.is_error:
        assert outcome.is_error, f"{about} {doc.call} 本应失败，却成功了：{outcome.text}"
    else:
        assert not outcome.is_error, f"{about} {doc.call} 本应成功，却失败了：{outcome.text}"
    if expect.error_code is not None:
        assert outcome.error_code == expect.error_code, f"{about} 的错误码：{outcome.text}"
    if expect.rule_id is not None:
        assert outcome.rule_id == expect.rule_id, f"{about} 的 rule_id：{outcome.text}"
    if expect.result is not None:
        misses = subset_misses(expect.result, outcome.structured)
        assert not misses, f"{about} 的结果不符合 expect.result：" + "；".join(misses)


def assert_invariants(case: PackCase, sandbox: PackSandbox, step: int | None = None) -> None:
    """Assert that INV-1, INV-2, INV-3 and INV-6 hold in ``sandbox`` (requirement 21.9).

    ``step`` is the step that has just run, None for the state the setup left behind.
    """
    violations = sandbox.violations()
    broken = "；".join(violation.text for violation in violations)
    where = case.about(step) if step is not None else f"{case.about()} 的 setup"
    assert not violations, f"{where} 之后不变量不成立：{broken}"


def run_case(case: PackCase, instance: PackInstance) -> None:
    """Build the setup of ``case``, replay its steps and assert everything it expects.

    The order of the checks (requirement 21.9 and the module docstring): the names of the
    setup against the descriptions of the profile, then the build - which the Seed_Builder
    checks the invariants of as well (requirement 20.9) - then the invariants of the
    sandbox the setup produced, and then each step with its own expectations and the
    invariants after it.
    """
    named = named_in_descriptions(case.written_names, instance.described)
    assert not named, f"{case.about()} 的 setup 写了描述中出现的文本：" + "；".join(
        f"{name} 出现在{text.about}" for name, text in named
    )

    snapshot = instance.build(case.dataset, case.anchor_date)
    aliases: dict[str, str] = dict(snapshot.aliases)
    with instance.sandbox(snapshot) as sandbox:
        assert_invariants(case, sandbox)
        for step, doc in enumerate(case.steps, start=1):
            assert doc.call in instance.tools, (
                f"{case.about(step)} 调用了 profile 未暴露的工具 {doc.call}"
            )
            args = _args(doc, aliases)
            outcome = sandbox.call(doc.call, args)
            assert_outcome(case, step, doc, outcome)
            assert_invariants(case, sandbox, step)
            if doc.alias is not None:
                aliases[doc.alias] = _result_id(case, step, doc, outcome)


def _args(doc: StepDoc, aliases: Mapping[str, str]) -> Mapping[str, JsonValue] | None:
    """The arguments of one step, with every ``$<alias>`` replaced by its internal ID."""
    if doc.args is None:
        return None
    resolved = substitute(doc.args, aliases)
    assert isinstance(resolved, Mapping)
    return resolved


def _result_id(case: PackCase, step: int, doc: StepDoc, outcome: ToolOutcome) -> str:
    """The ``id`` of the result of one step, which its ``alias`` is bound to (req 13.3)."""
    structured = outcome.structured or {}
    found = structured.get(ID_KEY)
    assert isinstance(found, str), (
        f"{case.about(step)} 声明了别名 {doc.alias}，但 {doc.call} 的结果没有 {ID_KEY}："
        f"{outcome.text}"
    )
    return found


# --- the tests ---------------------------------------------------------------------------


@pytest.mark.req("21.9")
@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_pack_case(case: PackCase, instances: Instances) -> None:
    run_case(case, instances.of(case))


@pytest.mark.req("21.9")
def test_the_case_names_of_one_pack_differ() -> None:
    # The executor is exercised by the cases of the engine test fixture until task 19.2
    # writes the first example pack, so there is always at least one case to run.
    assert CASES, "没有收集到任何包级测试用例"
    by_pack: dict[tuple[Path, str], list[str]] = {}
    for case in CASES:
        assert case.file.parent == case.root / case.pack / CASES_DIR
        by_pack.setdefault((case.root, case.pack), []).append(case.name)
    # A case name is the name of the dataset its setup is built as, and the cases of one
    # pack share one instance, so no two of them may carry the same name.
    for (_, pack), names in by_pack.items():
        assert len(set(names)) == len(names), f"{pack} 的用例名重复：{sorted(names)}"


@pytest.mark.req("21.9")
def test_the_fixture_pack_provides_a_rule_case_and_a_replay_case() -> None:
    # The two cases that exercise the executor itself: one about an enforced rule, with a
    # violating and a passing call, and one that replays a linkage end to end.
    fixture = {case.name: case for case in CASES if case.root == FIXTURE_PACKS_DIR}
    rule = fixture["fxt-001-depots-differ"]
    assert [step.expect.rule_id for step in rule.steps] == ["R-FXT-001", None]
    assert [step.expect.is_error for step in rule.steps] == [True, False]

    replay = fixture["stock-move-transfer"]
    assert [step.call for step in replay.steps] == [
        "stock_move_draft_create",
        "stock_move_submit",
        "stock_move_approve",
        "stock_level_query",
        "stock_level_query",
    ]
    # The setup approves a goods entry, so the build performs the inventory linkage of that
    # bill before the first step runs (requirement 20.2).
    assert replay.document.setup["goods_entry:opening"]["status"] == "approved"


@pytest.mark.req("21.9")
def test_a_packs_directory_without_cases_collects_nothing(tmp_path: Path) -> None:
    # What ``packs/`` is until task 19.2 writes the first example pack: the root is not
    # there, or holds packs that have no ``tests/`` directory yet.
    assert collect_cases(tmp_path / "missing") == ()
    (tmp_path / "core" / CASES_DIR).mkdir(parents=True)
    (tmp_path / "purchase").mkdir()
    assert collect_cases(tmp_path) == ()


@pytest.mark.req("21.9")
def test_the_names_of_a_setup_are_compared_against_the_descriptions() -> None:
    case = next(found for found in CASES if found.name == "stock-move-transfer")

    # The name and the code of every record of the setup, and of its lines, in written
    # order; no other field takes part in the comparison.
    assert case.written_names == ("回放用例专用供货单位", "回放用例专用紧固件")

    described = (
        DescribedText("vendor_create", None, "新建往来单位，如回放用例专用供货单位那一条。"),
        DescribedText("vendor_create", "name", "往来单位名称"),
    )
    found = named_in_descriptions(case.written_names, described)
    assert [(name, text.about) for name, text in found] == [
        ("回放用例专用供货单位", "工具 vendor_create 的工具描述")
    ]
    assert named_in_descriptions(("夹具主料仓",), described) == ()


@pytest.mark.req("21.9")
def test_an_alias_of_a_step_is_resolved_in_the_steps_after_it() -> None:
    aliases: FrozenMap[str, str] = FrozenMap({"supply_order:first": "so_000001"})

    # The alias is replaced where it starts the value, at any depth, and what follows it is
    # kept, so a line of the record is named by its line number (requirement 4.2).
    assert substitute("$supply_order:first", aliases) == "so_000001"
    assert substitute("$supply_order:first-2", aliases) == "so_000001-2"
    assert substitute({"lines": [{"id": "$supply_order:first-1"}]}, aliases) == {
        "lines": [{"id": "so_000001-1"}]
    }
    # Anything else is left as it is, a value that holds a $ elsewhere included.
    assert substitute({"remark": "单价 $ 10", "qty": "3", "ok": True}, aliases) == {
        "remark": "单价 $ 10",
        "qty": "3",
        "ok": True,
    }
    with pytest.raises(KeyError, match="supply_order:second"):
        substitute("$supply_order:second", aliases)
    with pytest.raises(ValueError, match="别名"):
        substitute("$Supply", aliases)


@pytest.mark.req("21.9")
def test_expect_result_is_a_subset_of_the_structured_content() -> None:
    found: JsonValue = {"items": [{"qty_base": "60", "warehouse": {"code": "D002"}}], "total": 1}

    assert subset_misses({"total": 1}, found) == ()
    assert subset_misses({"items": [{"warehouse": {"code": "D002"}}]}, found) == ()
    assert subset_misses({"total": 2}, found) == ("result.total 为 1，期望 2",)
    assert subset_misses({"page": 1}, found) == ("result.page 不在结果中",)
    assert subset_misses({"items": []}, found) == ("result.items 有 1 个元素，期望 0 个",)
    assert subset_misses({"items": [{"qty_base": "70"}]}, found) == (
        'result.items[0].qty_base 为 "60"，期望 "70"',
    )
