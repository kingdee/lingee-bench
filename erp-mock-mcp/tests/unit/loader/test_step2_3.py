"""erp_mock.loader.step2 and step3: requires_meta_model and the pack dependencies.

Both steps are run through :class:`~erp_mock.loader.loader.Loader`, the way loading runs
them, on a tree written below ``tmp_path`` by tests.helpers.tree; step 1 runs first, since
these steps read the manifests it validated. Each test writes exactly the one thing that is
wrong and asserts the place of the error, the (pack, file, config path) the loader reports
(requirement 2.2).

The ranges of the step 2 tests are built from :data:`~erp_mock.META_MODEL_VERSION`, so the
tests state what matching means instead of the version that happens to be current; the
syntax of a range is covered by tests/unit/common/test_versions.py, and only its reporting
is covered here.
"""

from pathlib import Path

import pytest

from erp_mock import META_MODEL_VERSION
from erp_mock.common.versions import SemVer
from erp_mock.loader.errors import ConfigError, ConfigErrors, Step
from erp_mock.loader.loader import Loader
from tests.helpers.tree import write_instance, write_pack

INSTANCE_DIR = "instance"
PACKS_DIR = "packs"

# The meta model version the engine implements, and the first version of the next major
# release, which no range around the current version matches.
CURRENT = SemVer.parse(META_MODEL_VERSION)
NEXT_MAJOR = SemVer(CURRENT.major + 1, 0, 0)

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]


def loaded(tmp_path: Path) -> Loader:
    """A loader for the tree below ``tmp_path`` with step 1 run; it must pass."""
    target = Loader(tmp_path / INSTANCE_DIR, tmp_path / PACKS_DIR)
    target.run_step(1)
    return target


def failures(target: Loader, step: Step) -> tuple[ConfigError, ...]:
    """The errors ``step`` reports for ``target``, in report order; it must find some."""
    with pytest.raises(ConfigErrors) as caught:
        target.run_step(step)
    assert caught.value.step == step
    return caught.value.errors


def places(errors: tuple[ConfigError, ...]) -> list[Place]:
    """The places of ``errors``, in report order."""
    return [
        (error.pack, location.file, location.config_path)
        for error in errors
        for location in error.locations
    ]


def reasons(errors: tuple[ConfigError, ...]) -> list[str]:
    """The reasons of ``errors``, in report order."""
    return [error.reason for error in errors]


# --- step 2: requires_meta_model (requirements 2.4 and 2.5) ------------------------------


@pytest.mark.req("2.4", "2.5")
@pytest.mark.parametrize(
    "text",
    [
        f"{CURRENT}",
        f"={CURRENT}",
        f">={CURRENT}",
        f"<={CURRENT}",
        f">={CURRENT} <{NEXT_MAJOR}",
        f">=0.0.0 <={CURRENT}",
    ],
    ids=["bare", "equal", "at_least", "at_most", "half_open", "two_bounds"],
)
def test_a_range_the_current_meta_model_version_matches_passes(tmp_path: Path, text: str) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", requires_meta_model=f'"{text}"')
    target = loaded(tmp_path)

    target.run_step(2)


@pytest.mark.req("2.4", "2.5")
@pytest.mark.parametrize(
    "text",
    [
        f"{NEXT_MAJOR}",
        f">={NEXT_MAJOR} <{SemVer(NEXT_MAJOR.major + 1, 0, 0)}",
        f"<{CURRENT}",
        f">{CURRENT}",
        # Every comparator must hold: the first one does, the second one does not.
        f">=0.0.0 <{CURRENT}",
    ],
    ids=["bare", "half_open", "below", "above", "one_comparator_fails"],
)
def test_a_range_the_current_meta_model_version_does_not_match(tmp_path: Path, text: str) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", requires_meta_model=f'"{text}"')

    found = failures(loaded(tmp_path), 2)

    assert places(found) == [("core", "pack.yaml", "requires_meta_model")]
    assert reasons(found) == [f"要求的元模型版本范围 {text} 不匹配当前元模型版本 {CURRENT}"]


@pytest.mark.req("2.4", "2.5")
@pytest.mark.parametrize(
    "text",
    [f"^{CURRENT}", f">= {CURRENT}", ">=0.1", f" {CURRENT}", "latest", "0.1.0-rc1"],
    ids=["operator", "space_after_operator", "two_segments", "leading_space", "word", "suffix"],
)
def test_a_requires_meta_model_that_is_not_a_range(tmp_path: Path, text: str) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", requires_meta_model=f'"{text}"')

    found = failures(loaded(tmp_path), 2)

    assert places(found) == [("core", "pack.yaml", "requires_meta_model")]
    assert found[0].reason.startswith("不是合法的版本范围：")


@pytest.mark.req("2.2", "2.5")
def test_step_2_checks_every_pack_and_reports_them_in_instance_order(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ purchase: 1.0.0, core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "purchase", requires_meta_model=f'"^{CURRENT}"')
    write_pack(tmp_path / PACKS_DIR, "core", requires_meta_model=f'">={NEXT_MAJOR}"')

    found = failures(loaded(tmp_path), 2)

    assert places(found) == [
        ("purchase", "pack.yaml", "requires_meta_model"),
        ("core", "pack.yaml", "requires_meta_model"),
    ]


# --- step 3: depends_on ranges, packs and versions (requirements 2.4 and 2.5) ------------


@pytest.mark.req("2.4", "2.5")
@pytest.mark.parametrize(
    "text",
    ["1.2.3", "=1.2.3", ">=1.0.0 <2.0.0", ">1.2.2 <=1.2.3"],
    ids=["bare", "equal", "half_open", "two_bounds"],
)
def test_a_dependency_whose_referenced_version_satisfies_the_range_passes(
    tmp_path: Path, text: str
) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.2.3, purchase: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", version="1.2.3")
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on=f'{{ core: "{text}" }}')
    target = loaded(tmp_path)

    target.run_step(3)


@pytest.mark.req("2.5")
def test_a_manifest_without_depends_on_passes(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core")
    target = loaded(tmp_path)

    target.run_step(3)


@pytest.mark.req("2.4", "2.5")
@pytest.mark.parametrize(
    "text",
    ["~1.0.0", ">= 1.0.0", "1.0", "1.0.0 ", "任意"],
    ids=["operator", "space_after_operator", "two_segments", "trailing_space", "word"],
)
def test_a_depends_on_range_that_is_not_a_range(tmp_path: Path, text: str) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0, purchase: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core")
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on=f'{{ core: "{text}" }}')

    found = failures(loaded(tmp_path), 3)

    # The entry names the pack it depends on, so the config path does too.
    assert places(found) == [("purchase", "pack.yaml", "depends_on.core")]
    assert found[0].reason.startswith("不是合法的版本范围：")


@pytest.mark.req("2.5")
def test_a_pack_depended_on_that_instance_yaml_does_not_reference(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ purchase: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ gl: ">=1.0.0 <2.0.0" }')
    # The pack exists below --packs-dir; what counts is that the instance does not load it.
    write_pack(tmp_path / PACKS_DIR, "gl")

    found = failures(loaded(tmp_path), 3)

    assert places(found) == [("purchase", "pack.yaml", "depends_on.gl")]
    assert reasons(found) == ["所依赖的业务包 gl 不在 instance.yaml 的 packs 中"]


@pytest.mark.req("2.4", "2.5")
@pytest.mark.parametrize(
    "text",
    [">=2.0.0 <3.0.0", "1.2.3", "<1.0.0", ">=1.0.0 <1.1.0"],
    ids=["above", "equal", "below", "upper_bound"],
)
def test_a_referenced_version_that_does_not_satisfy_the_range(tmp_path: Path, text: str) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.1.0, purchase: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", version="1.1.0")
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on=f'{{ core: "{text}" }}')

    found = failures(loaded(tmp_path), 3)

    assert places(found) == [("purchase", "pack.yaml", "depends_on.core")]
    assert reasons(found) == [f"instance.yaml 引用的 core 版本 1.1.0 不满足范围 {text}"]


@pytest.mark.req("2.2", "2.5")
def test_step_3_reports_every_entry_of_every_pack(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ purchase: 1.0.0, core: 1.0.0, gl: 1.0.0 }")
    # A version that is not satisfied and an unknown pack in one manifest, a range that is
    # not a range in another.
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ core: ">=2.0.0", ap: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "core", depends_on='{ gl: "^1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "gl")

    found = failures(loaded(tmp_path), 3)

    # Both entries of purchase, which instance.yaml lists first, then the one of core; the
    # two entries of purchase by their config path.
    assert places(found) == [
        ("purchase", "pack.yaml", "depends_on.ap"),
        ("purchase", "pack.yaml", "depends_on.core"),
        ("core", "pack.yaml", "depends_on.gl"),
    ]


@pytest.mark.req("2.2", "2.5")
def test_an_entry_with_an_unknown_pack_and_a_bad_range_gives_both_errors(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ purchase: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ gl: "^1.0.0" }')

    found = failures(loaded(tmp_path), 3)

    assert places(found) == [
        ("purchase", "pack.yaml", "depends_on.gl"),
        ("purchase", "pack.yaml", "depends_on.gl"),
    ]
    assert found[0].reason.startswith("不是合法的版本范围：")
    assert found[1].reason == "所依赖的业务包 gl 不在 instance.yaml 的 packs 中"


# --- step 3: dependency cycles (requirement 2.5) -----------------------------------------


@pytest.mark.req("2.5")
def test_a_pack_that_depends_on_itself_is_a_cycle(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", depends_on='{ core: ">=1.0.0 <2.0.0" }')

    found = failures(loaded(tmp_path), 3)

    assert places(found) == [("core", "pack.yaml", "depends_on.core")]
    assert reasons(found) == ["依赖关系存在循环，涉及业务包 core"]


@pytest.mark.req("2.2", "2.5")
def test_a_cycle_of_two_packs_is_one_error_that_lists_both(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0, purchase: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", depends_on='{ purchase: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ core: ">=1.0.0" }')

    found = failures(loaded(tmp_path), 3)

    assert len(found) == 1
    # One error for the cycle, locating the entry of each pack that leads into it.
    assert places(found) == [
        ("core", "pack.yaml", "depends_on.purchase"),
        ("core", "pack.yaml", "depends_on.core"),
    ]
    assert reasons(found) == ["依赖关系存在循环，涉及业务包 core、purchase"]


@pytest.mark.req("2.5")
def test_a_cycle_of_three_packs_lists_every_member_in_instance_order(tmp_path: Path) -> None:
    write_instance(
        tmp_path / INSTANCE_DIR, packs="{ purchase: 1.0.0, core: 1.0.0, ap: 1.0.0, gl: 1.0.0 }"
    )
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ core: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "core", depends_on='{ ap: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "ap", depends_on='{ purchase: ">=1.0.0" }')
    # gl depends on the cycle without being part of it.
    write_pack(tmp_path / PACKS_DIR, "gl", depends_on='{ core: ">=1.0.0" }')

    found = failures(loaded(tmp_path), 3)

    assert len(found) == 1
    assert reasons(found) == ["依赖关系存在循环，涉及业务包 purchase、core、ap"]
    assert places(found) == [
        ("purchase", "pack.yaml", "depends_on.core"),
        ("purchase", "pack.yaml", "depends_on.ap"),
        ("purchase", "pack.yaml", "depends_on.purchase"),
    ]


@pytest.mark.req("2.2", "2.5")
def test_two_cycles_are_two_errors(tmp_path: Path) -> None:
    write_instance(
        tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0, purchase: 1.0.0, ap: 1.0.0, gl: 1.0.0 }"
    )
    write_pack(tmp_path / PACKS_DIR, "core", depends_on='{ purchase: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ core: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "ap", depends_on='{ gl: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "gl", depends_on='{ ap: ">=1.0.0" }')

    found = failures(loaded(tmp_path), 3)

    assert reasons(found) == [
        "依赖关系存在循环，涉及业务包 core、purchase",
        "依赖关系存在循环，涉及业务包 ap、gl",
    ]


@pytest.mark.req("2.5")
def test_a_dependency_is_an_edge_even_when_its_range_is_in_error(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0, purchase: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", depends_on='{ purchase: "^1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ core: ">=2.0.0" }')

    found = failures(loaded(tmp_path), 3)

    # The range and the version are reported, and the cycle they form as well.
    assert found[0].reason.startswith("不是合法的版本范围：")
    assert reasons(found)[1:] == [
        "依赖关系存在循环，涉及业务包 core、purchase",
        "instance.yaml 引用的 core 版本 1.0.0 不满足范围 >=2.0.0",
    ]


@pytest.mark.req("2.5")
def test_dependencies_without_a_cycle_pass(tmp_path: Path) -> None:
    write_instance(
        tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0, purchase: 1.0.0, ap: 1.0.0, gl: 1.0.0 }"
    )
    # A diamond and a chain: ap depends on purchase and gl, both of which depend on core.
    write_pack(tmp_path / PACKS_DIR, "core")
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ core: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "gl", depends_on='{ core: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "ap", depends_on='{ purchase: ">=1.0.0", gl: ">=1.0.0" }')
    target = loaded(tmp_path)

    target.run_step(3)


@pytest.mark.req("2.2", "2.5")
def test_a_cycle_is_reported_as_one_line_with_both_places(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0, purchase: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", depends_on='{ purchase: ">=1.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "purchase", depends_on='{ core: ">=1.0.0" }')

    with pytest.raises(ConfigErrors) as caught:
        loaded(tmp_path).run_step(3)

    assert caught.value.lines() == (
        "[step 3] core: pack.yaml: depends_on.purchase | pack.yaml: depends_on.core:"
        " 依赖关系存在循环，涉及业务包 core、purchase",
    )
