"""erp_mock.loader.step1: instance.yaml, the clock and the manifest of every pack.

Step 1 is run through :class:`~erp_mock.loader.loader.Loader`, the way loading runs it, on
a tree written below ``tmp_path`` by tests.helpers.tree, so each test writes exactly the
one thing that is wrong. There is one test per class of error of requirements 2.3, 2.6,
2.12 and 16.3; each asserts the place of the error, the (pack, file, config path) the
loader reports, since that place is what points the deployer at the offending item
(requirement 2.2).
"""

from pathlib import Path

import pytest

from erp_mock.loader.errors import ConfigError, ConfigErrors
from erp_mock.loader.loader import Loader
from tests.helpers.tree import pack_doc, write_instance, write_pack, write_tree

INSTANCE_DIR = "instance"
PACKS_DIR = "packs"

# One place of a reported error: the pack, the file and the config path.
type Place = tuple[str | None, str, str | None]


def loader(tmp_path: Path) -> Loader:
    """A loader for the tree below ``tmp_path``, with no step run yet."""
    return Loader(tmp_path / INSTANCE_DIR, tmp_path / PACKS_DIR)


def failures(target: Loader) -> tuple[ConfigError, ...]:
    """The errors step 1 reports for ``target``, in report order; it must find some."""
    with pytest.raises(ConfigErrors) as caught:
        target.run_step(1)
    assert caught.value.step == 1
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


# --- a tree step 1 accepts ---------------------------------------------------------------


@pytest.mark.req("2.1", "2.3", "2.12")
def test_step1_accepts_an_instance_and_the_packs_it_references(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0, purchase: 0.2.1 }")
    write_pack(tmp_path / PACKS_DIR, "core")
    write_pack(tmp_path / PACKS_DIR, "purchase", "0.2.1", rule_prefix="PUR", tool_prefixes="[pur_]")
    target = loader(tmp_path)

    target.run_step(1)

    assert target.instance is not None
    assert target.instance.instance == "p2p-demo"
    # The order of instance.yaml, which sorts the errors of every step (requirement 2.2).
    assert target.pack_order == ("core", "purchase")
    assert [(pack.name, pack.version) for pack in target.packs] == [
        ("core", "1.0.0"),
        ("purchase", "0.2.1"),
    ]
    assert target.packs[1].directory == tmp_path / PACKS_DIR / "purchase"
    assert target.packs[1].manifest.rule_prefix == "PUR"


@pytest.mark.req("2.1")
def test_a_directory_of_packs_dir_that_is_not_referenced_is_not_read(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core")
    # An unreferenced pack whose manifest cannot even be parsed takes no part in loading.
    write_tree(tmp_path / PACKS_DIR, {"legacy/pack.yaml": "pack: [\n"})
    target = loader(tmp_path)

    target.run_step(1)

    assert [pack.name for pack in target.packs] == ["core"]


@pytest.mark.req("16.3")
def test_a_fixed_clock_keeps_its_anchor_date_and_time_zone(tmp_path: Path) -> None:
    write_instance(
        tmp_path / INSTANCE_DIR,
        clock="{ mode: fixed, anchor_date: 2026-10-09, timezone: Asia/Shanghai }",
    )
    write_pack(tmp_path / PACKS_DIR, "core")
    target = loader(tmp_path)

    target.run_step(1)

    assert target.instance is not None
    assert target.instance.clock.mode == "fixed"
    assert target.instance.clock.anchor_date == "2026-10-09"
    assert target.instance.clock.timezone == "Asia/Shanghai"


# --- instance configuration (requirement 2.12) -------------------------------------------


@pytest.mark.req("2.12")
def test_missing_instance_directory(tmp_path: Path) -> None:
    found = failures(loader(tmp_path))

    assert places(found) == [(None, "instance.yaml", None)]
    assert reasons(found) == ["--instance 指定的目录不存在"]


@pytest.mark.req("2.12")
def test_instance_directory_without_instance_yaml(tmp_path: Path) -> None:
    (tmp_path / INSTANCE_DIR).mkdir()

    found = failures(loader(tmp_path))

    assert places(found) == [(None, "instance.yaml", None)]
    assert reasons(found) == ["--instance 指定的目录中没有 instance.yaml"]


@pytest.mark.req("2.2", "2.12")
def test_unparseable_instance_yaml_is_reported_at_its_line(tmp_path: Path) -> None:
    write_tree(tmp_path / INSTANCE_DIR, {"instance.yaml": "instance: *missing\n"})

    found = failures(loader(tmp_path))

    assert places(found) == [(None, "instance.yaml", "line 1")]
    assert found[0].reason.startswith("YAML 无法解析：")


@pytest.mark.req("2.12")
def test_instance_yaml_that_does_not_fit_the_model(tmp_path: Path) -> None:
    # A required key left out, and a key the model does not define.
    write_instance(tmp_path / INSTANCE_DIR, default_profile=None, surface="semantic")

    found = failures(loader(tmp_path))

    assert places(found) == [
        (None, "instance.yaml", "default_profile"),
        (None, "instance.yaml", "surface"),
    ]


@pytest.mark.req("2.3", "2.12")
def test_a_pack_reference_must_pin_a_concrete_version(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs='{ core: ">=1.0.0 <2.0.0" }')
    write_pack(tmp_path / PACKS_DIR, "core")

    found = failures(loader(tmp_path))

    # The config path names the pack, the reason the referenced version.
    assert places(found) == [(None, "instance.yaml", "packs.core")]
    assert ">=1.0.0 <2.0.0 不是 <主>.<次>.<修订> 形式的具体版本" in found[0].reason


@pytest.mark.req("2.12")
def test_no_pack_is_checked_when_instance_yaml_is_in_error(tmp_path: Path) -> None:
    # Two packs that would each be an error of their own, and no profiles key.
    write_instance(tmp_path / INSTANCE_DIR, profiles=None, packs="{ core: 1.0.0, purchase: 1.0.0 }")
    write_tree(tmp_path / PACKS_DIR, {"core/pack.yaml": pack_doc(pack="basis")})
    target = loader(tmp_path)

    found = failures(target)

    assert places(found) == [(None, "instance.yaml", "profiles")]
    # Without instance.yaml there is no pack order either, so the step sorts by the places.
    assert target.pack_order == ()
    assert target.instance is None
    assert target.packs == ()


@pytest.mark.req("16.3")
@pytest.mark.parametrize(
    ("clock", "config_path"),
    [
        ("{ mode: later }", "clock.mode"),
        ("{ mode: fixed }", "clock.anchor_date"),
        ("{ mode: fixed, anchor_date: 2026-02-30 }", "clock.anchor_date"),
        ("{ mode: today, timezone: asia/shanghai }", "clock.timezone"),
    ],
    ids=["mode", "anchor_date_missing", "anchor_date_invalid", "timezone"],
)
def test_clock_errors_name_the_config_path_below_clock(
    tmp_path: Path, clock: str, config_path: str
) -> None:
    write_instance(tmp_path / INSTANCE_DIR, clock=clock)
    write_pack(tmp_path / PACKS_DIR, "core")

    found = failures(loader(tmp_path))

    assert places(found) == [(None, "instance.yaml", config_path)]


# --- pack references (requirements 2.3 and 2.6) ------------------------------------------


@pytest.mark.req("2.3")
def test_packs_dir_without_a_directory_of_the_pack_name(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    (tmp_path / PACKS_DIR).mkdir()

    found = failures(loader(tmp_path))

    assert places(found) == [(None, "instance.yaml", "packs.core")]
    assert reasons(found) == ["--packs-dir 下没有业务包 core 的目录（引用版本 1.0.0）"]


@pytest.mark.req("2.3")
def test_pack_directory_without_pack_yaml(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    (tmp_path / PACKS_DIR / "core").mkdir(parents=True)

    found = failures(loader(tmp_path))

    assert places(found) == [("core", "pack.yaml", None)]
    assert reasons(found) == ["业务包目录中没有 pack.yaml（引用版本 1.0.0）"]


@pytest.mark.req("2.2", "2.3")
def test_unparseable_pack_yaml_is_reported_at_its_line(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_tree(tmp_path / PACKS_DIR, {"core/pack.yaml": "pack: core\npack: basis\n"})

    found = failures(loader(tmp_path))

    assert places(found) == [("core", "pack.yaml", "line 2")]
    assert found[0].reason.startswith("YAML 无法解析：")


@pytest.mark.req("2.6")
def test_pack_yaml_that_does_not_fit_the_manifest_model(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", rule_prefix=None)

    found = failures(loader(tmp_path))

    assert places(found) == [("core", "pack.yaml", "rule_prefix")]


@pytest.mark.req("2.6")
def test_primitives_outside_m0_are_reported_at_their_item(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", primitives="[crud, voucher, period_control]")

    found = failures(loader(tmp_path))

    assert places(found) == [
        ("core", "pack.yaml", "primitives[1]"),
        ("core", "pack.yaml", "primitives[2]"),
    ]


@pytest.mark.req("2.3")
def test_manifest_pack_differs_from_the_referenced_name(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_tree(tmp_path / PACKS_DIR, {"core/pack.yaml": pack_doc(pack="basis")})
    target = loader(tmp_path)

    found = failures(target)

    assert places(found) == [("core", "pack.yaml", "pack")]
    assert reasons(found) == ["pack 为 basis，与 instance.yaml 引用的包名 core 不同"]
    assert target.packs == ()


@pytest.mark.req("2.3")
def test_manifest_version_differs_from_the_referenced_version(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", version="1.1.0")

    found = failures(loader(tmp_path))

    assert places(found) == [("core", "pack.yaml", "version")]
    assert reasons(found) == ["version 为 1.1.0，与 instance.yaml 引用的版本 1.0.0 不同"]


@pytest.mark.req("2.2", "2.3")
def test_every_reference_is_checked_and_the_errors_follow_the_instance_order(
    tmp_path: Path,
) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ purchase: 1.0.0, core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "purchase", version="2.0.0", rule_prefix="PUR")
    write_pack(tmp_path / PACKS_DIR, "core", license=None)
    target = loader(tmp_path)

    found = failures(target)

    # Both references are checked, and the errors are sorted by the order of instance.yaml,
    # not by pack name; step 1 filled that order in when it read instance.yaml.
    assert target.pack_order == ("purchase", "core")
    assert places(found) == [
        ("purchase", "pack.yaml", "version"),
        ("core", "pack.yaml", "license"),
    ]


@pytest.mark.req("2.2")
def test_a_step_1_error_is_reported_as_one_line(tmp_path: Path) -> None:
    write_instance(tmp_path / INSTANCE_DIR, packs="{ core: 1.0.0 }")
    write_pack(tmp_path / PACKS_DIR, "core", version="1.1.0")

    with pytest.raises(ConfigErrors) as caught:
        loader(tmp_path).run_step(1)

    assert caught.value.lines() == (
        "[step 1] core: pack.yaml: version: version 为 1.1.0，与 instance.yaml 引用的版本 1.0.0 不同",
    )


@pytest.mark.req("2.12")
@pytest.mark.parametrize("text", ["", "- p2p-demo\n"], ids=["empty", "sequence"])
def test_instance_yaml_that_is_not_a_mapping(tmp_path: Path, text: str) -> None:
    write_tree(tmp_path / INSTANCE_DIR, {"instance.yaml": text})

    found = failures(loader(tmp_path))

    # An error about the document as a whole has no config path (requirement 2.2).
    assert places(found) == [(None, "instance.yaml", None)]
