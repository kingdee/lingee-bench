"""erp_mock.cli.options and erp_mock.cli.main: the options, the environment, the codes.

The four commands of requirement 22 belong to tasks 24.2 to 24.4, so what is tested here
is the frame they will hang off, and it is tested through the real command group: the
probe commands below carry the common options, read the environment and fail the way a
command fails, and the ``cli`` fixture adds them to :data:`~erp_mock.cli.main.main` for
the length of one test. So every usage error passes through the same click group, the same
group callback and the same exception handling the installed command has.

What each test asserts about the output, besides the exit code, is that stdout stayed
empty: requirement 22.2 leaves the stdout of stdio to MCP messages and requirement 22.10
leaves the stdout of seed to one line per dataset, so the usage errors of requirement 22.8
and the error list of requirement 2.2 have to be on stderr - which
:meth:`click.testing.Result.stdout` and ``.stderr`` keep apart.

No test reads the system clock or the current date (requirement 25.10): the anchor dates
are written into this file and the environment is set with ``monkeypatch``, never read
from the machine the tests run on.
"""

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Final

import click
import pytest
from click.testing import CliRunner, Result

from erp_mock import FRAMEWORK_VERSION
from erp_mock.cli.main import COMMANDS, PROGRAM_NAME, ErpMockGroup, main
from erp_mock.cli.options import (
    ADMIN_TOKEN_VAR,
    ALLOWED_ORIGINS_VAR,
    DEFAULT_DATA_DIR,
    DEFAULT_PACKS_DIR,
    ENV_VARS,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    FREEZE_SECONDS_VAR,
    IDLE_SECONDS_VAR,
    MAX_BODY_BYTES_VAR,
    MAX_CALLS_PER_SECOND_VAR,
    MAX_DB_BYTES_VAR,
    MAX_SANDBOX_CALLS_VAR,
    MAX_SANDBOXES_VAR,
    MCP_TOKEN_VAR,
    NUMERIC_DEFAULTS,
    RETENTION_SECONDS_VAR,
    SERVICE_LOGGER,
    EnvValues,
    anchor_date_option,
    data_dir_option,
    instance_option,
    packs_dir_option,
    read_env,
    setup_logging,
)
from erp_mock.gateway.sandbox import DEFAULT_SANDBOX_LIMITS
from erp_mock.gateway.serve import MAX_BODY_BYTES
from erp_mock.loader.errors import ConfigError, ConfigErrors, Location
from erp_mock.tools.layer import DEFAULT_LIMITS

# The nine variables of requirement 23.9 and the two tokens of requirements 23.3 and 23.4,
# written out here so that the list the CLI reads is compared with the requirement rather
# than with itself.
EXPECTED_ENV_VARS: Final = (
    "ERP_MOCK_MAX_BODY_BYTES",
    "ERP_MOCK_MAX_SANDBOX_CALLS",
    "ERP_MOCK_MAX_DB_BYTES",
    "ERP_MOCK_MAX_CALLS_PER_SECOND",
    "ERP_MOCK_ALLOWED_ORIGINS",
    "ERP_MOCK_IDLE_SECONDS",
    "ERP_MOCK_FREEZE_SECONDS",
    "ERP_MOCK_RETENTION_SECONDS",
    "ERP_MOCK_MAX_SANDBOXES",
    "ERP_MOCK_ADMIN_TOKEN",
    "ERP_MOCK_MCP_TOKEN",
)

# Which field of :class:`EnvValues` each numeric variable ends up in, so that a variable
# that landed in the wrong one is caught rather than hidden by an equal default.
NUMERIC_FIELDS: Final[Mapping[str, Callable[[EnvValues], int]]] = MappingProxyType(
    {
        MAX_BODY_BYTES_VAR: lambda env: env.max_body_bytes,
        MAX_SANDBOX_CALLS_VAR: lambda env: env.resources.sandbox_calls,
        MAX_DB_BYTES_VAR: lambda env: env.resources.db_bytes,
        MAX_CALLS_PER_SECOND_VAR: lambda env: env.resources.calls_per_second,
        IDLE_SECONDS_VAR: lambda env: env.sandbox.idle_seconds,
        FREEZE_SECONDS_VAR: lambda env: env.sandbox.freeze_seconds,
        RETENTION_SECONDS_VAR: lambda env: env.sandbox.retention_seconds,
        MAX_SANDBOXES_VAR: lambda env: env.sandbox.max_sandboxes,
    }
)

# A value no default of requirement 23.9 happens to have, so a field that kept its default
# is told apart from one that took the value.
SET_NUMBER: Final = 7

# Values that are not a positive decimal integer without a leading zero (requirement
# 23.10): zero, a leading zero, a sign, a fraction, text, a space and a digit of another
# script, which ``int`` would read and no deployer means to write.
BAD_NUMBERS: Final = ("0", "01", "-1", "+1", "1.5", "abc", " 2", "2 ", "1_000", "٣")

# The level and the logger of the line the ``logs`` probe writes: a child of the logger
# :func:`setup_logging` configures, as every module of the service is.
LOG_LINE: Final = "探针写出的服务日志"
LOG_SOURCE: Final = f"{SERVICE_LOGGER}.gateway.serve"


# --- the probe commands ------------------------------------------------------------------


@click.command(name="probe")
@instance_option
@packs_dir_option
@data_dir_option
@anchor_date_option
def probe(instance_dir: Path, packs_dir: Path, data_dir: Path, anchor_date: date | None) -> None:
    """Write what the common options were parsed into on stdout, as JSON."""
    click.echo(
        json.dumps(
            {
                "instance_dir": str(instance_dir),
                "packs_dir": str(packs_dir),
                "data_dir": str(data_dir),
                "anchor_date": None if anchor_date is None else anchor_date.isoformat(),
            }
        )
    )


@click.command(name="probe-env")
def probe_env() -> None:
    """Read the environment the way serve and stdio do (requirement 23.9)."""
    click.echo(json.dumps({"max_body_bytes": read_env().max_body_bytes}))


@click.command(name="probe-loading")
def probe_loading() -> None:
    """Fail the way a validation step fails (requirements 2.2 and 22.7)."""
    raise failed_step()


@click.command(name="probe-log")
def probe_log() -> None:
    """Log one line the way a module of the service does (requirement 22.2)."""
    logging.getLogger(LOG_SOURCE).info(LOG_LINE)


PROBES: Final[tuple[click.Command, ...]] = (probe, probe_env, probe_loading, probe_log)


def failed_step() -> ConfigErrors:
    """The errors of one failed validation step, as the loader raises them.

    Two errors of step 5, one in the instance config and one in a business pack, so the
    report shows both the order :class:`ConfigErrors` sorted them into and the two shapes
    of a line (requirement 2.2).
    """
    return ConfigErrors(
        5,
        [
            ConfigError(
                5,
                "fx_purchase",
                (Location("entities/fx_order.yaml", "header.delivery_date.default"),),
                "缺省值与字段类型 date 不符",
            ),
            ConfigError(
                5,
                None,
                (Location("instance.yaml", "datasets[0]"),),
                "初始数据集 fx-basic 不在业务包中",
            ),
        ],
        ("fx_base", "fx_purchase"),
    )


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def service_log() -> Iterator[logging.Logger]:
    """Restore the ``erp_mock`` logger after a test that let :func:`setup_logging` run."""
    logger = logging.getLogger(SERVICE_LOGGER)
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)
    logger.propagate = propagate


@pytest.fixture
def cli(service_log: logging.Logger) -> Iterator[ErpMockGroup]:
    """The installed command group with the probe commands added for one test.

    They are removed again afterwards, so the group the service installs stays the one
    task 24.1 leaves behind: :data:`COMMANDS` and nothing else.
    """
    for command in PROBES:
        main.add_command(command)
    yield main
    for command in PROBES:
        main.commands.pop(str(command.name), None)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the test with none of the variables of requirement 23.9 set."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --- helpers -----------------------------------------------------------------------------


def run(group: click.Group, *args: str) -> Result:
    """Invoke ``group`` with ``args`` and refuse anything but a clean exit of click."""
    result = CliRunner().invoke(group, list(args))
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"命令以未预期的异常结束：{result.exception!r}"
    )
    return result


def usage_error(group: click.Group, *args: str) -> str:
    """The stderr of a usage error: exit code 2 and nothing on stdout (req 22.8)."""
    result = run(group, *args)

    assert result.exit_code == EXIT_USAGE, f"用法错误的退出码必须是 2：{result.stderr}"
    assert result.stdout == "", "用法错误时 stdout 不输出任何内容"
    return result.stderr


def parsed_options(group: click.Group, *args: str) -> Mapping[str, str | None]:
    """The options the ``probe`` command parsed from ``args``."""
    result = run(group, "probe", *args)

    assert result.exit_code == EXIT_OK, f"probe 应成功退出：{result.stderr}"
    parsed: Mapping[str, str | None] = json.loads(result.stdout)
    return parsed


# --- the paths of a command (requirement 22.1) -------------------------------------------


@pytest.mark.req("22.1")
def test_relative_paths_are_resolved_against_the_working_directory(
    cli: ErpMockGroup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    here = Path.cwd()

    parsed = parsed_options(
        cli,
        "--instance",
        "instances/p2p",
        "--packs-dir",
        "./packs",
        "--data-dir",
        "../var/run",
    )

    assert parsed["instance_dir"] == str(here / "instances/p2p"), "--instance 按工作目录解析"
    assert parsed["packs_dir"] == str(here / "packs"), "--packs-dir 的 ./ 前缀被规范化"
    assert parsed["data_dir"] == str(here.parent / "var/run"), "--data-dir 的 .. 被规范化"
    for value in parsed.values():
        assert value is None or value.startswith("/"), "每个路径选项都是绝对路径"


@pytest.mark.req("22.1")
def test_an_absolute_path_is_taken_as_it_was_written(
    cli: ErpMockGroup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    elsewhere = tmp_path / "elsewhere"

    parsed = parsed_options(cli, "--instance", str(elsewhere))

    assert parsed["instance_dir"] == str(elsewhere), "绝对路径不受工作目录影响"


@pytest.mark.req("22.1")
def test_the_defaults_are_packs_and_var_of_the_working_directory(
    cli: ErpMockGroup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    here = Path.cwd()

    parsed = parsed_options(cli, "--instance", "inst")

    assert parsed["packs_dir"] == str(here / "packs"), "--packs-dir 缺省为 ./packs"
    assert parsed["data_dir"] == str(here / "var"), "--data-dir 缺省为 ./var"


@pytest.mark.req("22.1")
def test_the_help_shows_the_options_the_way_requirement_22_writes_them(
    cli: ErpMockGroup,
) -> None:
    result = run(cli, "probe", "--help")

    assert result.exit_code == EXIT_OK, "--help 以退出码 0 结束"
    assert "--instance DIRECTORY" in result.stdout, "--instance 的取值是一个目录"
    assert "--anchor-date YYYY-MM-DD" in result.stdout, "--anchor-date 的取值写法见需求 22"
    assert DEFAULT_PACKS_DIR in result.stdout, "--help 显示 ./packs 而不是解析后的路径"
    assert DEFAULT_DATA_DIR in result.stdout, "--help 显示 ./var 而不是解析后的路径"


# --- the usage errors of the command line (requirement 22.8) -----------------------------


@pytest.mark.req("22.8")
def test_a_missing_instance_is_a_usage_error(cli: ErpMockGroup) -> None:
    stderr = usage_error(cli, "probe")

    assert "--instance" in stderr, "缺少 --instance 的错误须指出该选项"


@pytest.mark.req("22.8")
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(("probe", "--instance", "inst", "--nope"), "--nope", id="unknown-option"),
        pytest.param(("probe", "--instance"), "--instance", id="option-without-value"),
        pytest.param(("probe", "--instance", "inst", "extra"), "extra", id="extra-argument"),
        pytest.param(("nope",), "nope", id="unknown-command"),
        pytest.param((), "Usage", id="no-arguments"),
    ],
)
def test_a_command_line_that_is_not_defined_is_a_usage_error(
    cli: ErpMockGroup, args: tuple[str, ...], expected: str
) -> None:
    stderr = usage_error(cli, *args)

    assert expected in stderr, f"用法错误须指出 {expected}"


# --- the anchor date (requirement 16.13) -------------------------------------------------


@pytest.mark.req("16.13", "22.8")
@pytest.mark.parametrize(
    "value",
    ["2026-2-3", "2026-10-9", "2026/10/09", "20261009", "today", "", " 2026-10-09", "2026-10-09 "],
)
def test_an_anchor_date_of_the_wrong_shape_is_a_usage_error(cli: ErpMockGroup, value: str) -> None:
    stderr = usage_error(cli, "probe", "--instance", "inst", "--anchor-date", value)

    assert "--anchor-date" in stderr, "错误须指出 --anchor-date"
    assert value in stderr, "错误须指出该选项的值"
    assert "YYYY-MM-DD" in stderr, "形状不对的值按形状报错"


@pytest.mark.req("16.13", "22.8")
@pytest.mark.parametrize(
    "value", ["2026-02-30", "2026-13-01", "2026-00-10", "2026-10-32", "2100-02-29", "0000-01-01"]
)
def test_an_anchor_date_that_is_not_in_the_calendar_is_a_usage_error(
    cli: ErpMockGroup, value: str
) -> None:
    stderr = usage_error(cli, "probe", "--instance", "inst", "--anchor-date", value)

    assert "--anchor-date" in stderr, "错误须指出 --anchor-date"
    assert value in stderr, "错误须指出该选项的值"
    assert "日历" in stderr, "形状正确但日历中不存在的日期按日历报错"


@pytest.mark.req("16.13")
@pytest.mark.parametrize(
    "value", ["2026-10-09", "2000-02-29", "2096-02-29", "2099-12-31", "0001-01-01", "9999-12-31"]
)
def test_a_valid_anchor_date_becomes_that_date(cli: ErpMockGroup, value: str) -> None:
    parsed = parsed_options(cli, "--instance", "inst", "--anchor-date", value)

    assert parsed["anchor_date"] == value, "有效的锚点日期按原值解析为日期"


@pytest.mark.req("16.13")
def test_without_the_option_there_is_no_anchor_date(cli: ErpMockGroup) -> None:
    parsed = parsed_options(cli, "--instance", "inst")

    assert parsed["anchor_date"] is None, "未指定 --anchor-date 时由实例配置的 clock 决定"


# --- the environment variables (requirement 23.9) ----------------------------------------


@pytest.mark.req("23.9")
def test_the_variables_read_are_the_ones_requirement_23_lists() -> None:
    assert ENV_VARS == EXPECTED_ENV_VARS, "读取的环境变量清单须与需求 23 第 9 条一致"
    assert set(NUMERIC_FIELDS) == set(NUMERIC_DEFAULTS), "数值环境变量的清单须与缺省值表一致"


@pytest.mark.req("23.9")
def test_an_environment_that_sets_nothing_gives_the_defaults(clean_env: None) -> None:
    env = read_env()

    assert env == EnvValues(), "未设置任何变量时每个字段取缺省值"
    assert env.admin_token is None, "未设置 Admin 令牌时由 serve 生成（需求 23 第 5 条）"
    assert env.mcp_token is None, "未设置 MCP 令牌时 MCP 端口不要求 Authorization 头"
    assert env.allowed_origins == (), "允许来源缺省为空"
    assert env.max_body_bytes == MAX_BODY_BYTES, "请求体上限缺省为 262144 字节"
    assert env.sandbox == DEFAULT_SANDBOX_LIMITS, "四个生命周期阈值取需求 15 第 11 条的缺省值"
    assert env.resources == DEFAULT_LIMITS, "三个资源上限取需求 23 第 9 条的缺省值"


@pytest.mark.req("23.9")
def test_an_empty_variable_counts_as_unset(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ENV_VARS:
        monkeypatch.setenv(name, "")

    assert read_env() == EnvValues(), "空字符串与未设置同样取缺省值"


@pytest.mark.req("23.9")
@pytest.mark.parametrize("name", list(NUMERIC_FIELDS))
def test_each_numeric_variable_lands_in_one_field(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, str(SET_NUMBER))

    env = read_env()

    assert NUMERIC_FIELDS[name](env) == SET_NUMBER, f"{name} 的取值未进入对应字段"
    for other, field in NUMERIC_FIELDS.items():
        if other != name:
            assert field(env) == NUMERIC_DEFAULTS[other], f"{name} 不应改变 {other} 的取值"


@pytest.mark.req("23.9")
def test_the_tokens_are_carried_as_they_were_written(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Values a token may well have and no parsing may touch: the character set of
    # requirement 23.5, and a value whose case matters (requirements 19.2 and 23.3).
    monkeypatch.setenv(ADMIN_TOKEN_VAR, "Abc-123_xyz")
    monkeypatch.setenv(MCP_TOKEN_VAR, " mCp Token ")

    env = read_env()

    assert env.admin_token == "Abc-123_xyz", "Admin 令牌按原值读取"
    assert env.mcp_token == " mCp Token ", "MCP 令牌按原值读取，不做任何修剪"


@pytest.mark.req("23.9")
def test_the_admin_token_is_the_one_serve_settles(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MCP_TOKEN_VAR, "mcp-token")
    monkeypatch.setenv(ALLOWED_ORIGINS_VAR, "https://app.example")
    monkeypatch.setenv(IDLE_SECONDS_VAR, str(SET_NUMBER))
    env = read_env()

    settings = env.settings("generated-by-serve")

    assert env.admin_token is None, "环境未设置 Admin 令牌时读出的是 None"
    assert settings.admin_token == "generated-by-serve", "serve 生成的令牌进入 EnvSettings"
    assert settings.mcp_token == "mcp-token", "其余字段原样传递"
    assert settings.allowed_origins == ("https://app.example",), "允许来源原样传递"
    assert settings.sandbox.idle_seconds == SET_NUMBER, "生命周期阈值原样传递"
    assert settings.max_body_bytes == env.max_body_bytes, "请求体上限原样传递"
    assert settings.resources == env.resources, "资源上限原样传递"
    with pytest.raises(ValueError, match="Admin 令牌"):
        env.settings("")


@pytest.mark.req("23.9")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("http://app.example", ("http://app.example",), id="one"),
        pytest.param(
            "https://app.example:8443,http://localhost:5173",
            ("https://app.example:8443", "http://localhost:5173"),
            id="two-with-ports",
        ),
        pytest.param(
            "http://b.example,http://a.example",
            ("http://b.example", "http://a.example"),
            id="order-kept",
        ),
    ],
)
def test_the_allowed_origins_are_the_items_of_the_list(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str, expected: tuple[str, ...]
) -> None:
    monkeypatch.setenv(ALLOWED_ORIGINS_VAR, value)

    assert read_env().allowed_origins == expected, "允许来源按逗号分隔、保持书写顺序"


# --- the variables that are not valid (requirement 23.10) --------------------------------


@pytest.mark.req("23.10")
@pytest.mark.parametrize("name", list(NUMERIC_FIELDS))
@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_a_number_that_is_not_a_positive_integer_is_refused(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(click.UsageError) as raised:
        read_env()

    assert name in str(raised.value), "错误须指出该环境变量"
    assert value in str(raised.value), "错误须指出该环境变量的值"


@pytest.mark.req("23.10")
@pytest.mark.parametrize(
    "item",
    [
        "ftp://app.example",
        "app.example",
        "//app.example",
        "http://",
        "http://app.example/",
        "http://app.example/mcp",
        "http://app.example:port",
        "http://user:pw@app.example",
        "http://app.example?q=1",
        "http://app.example#f",
        " http://app.example",
        "",
    ],
)
def test_an_item_that_is_not_an_origin_is_refused(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, item: str
) -> None:
    value = f"http://localhost:5173,{item}"
    monkeypatch.setenv(ALLOWED_ORIGINS_VAR, value)

    with pytest.raises(click.UsageError) as raised:
        read_env()

    assert ALLOWED_ORIGINS_VAR in str(raised.value), "错误须指出该环境变量"
    assert value in str(raised.value), "错误须指出该环境变量的值"


@pytest.mark.req("22.8", "23.10")
def test_a_variable_that_is_not_valid_ends_the_command_with_exit_code_2(
    cli: ErpMockGroup, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_DB_BYTES_VAR, "0")

    stderr = usage_error(cli, "probe-env")

    assert MAX_DB_BYTES_VAR in stderr, "错误须指出该环境变量"
    assert "0" in stderr, "错误须指出该环境变量的值"


@pytest.mark.req("23.9")
def test_a_valid_environment_lets_the_command_run(
    cli: ErpMockGroup, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_BODY_BYTES_VAR, str(SET_NUMBER))

    result = run(cli, "probe-env")

    assert result.exit_code == EXIT_OK, f"环境变量合规时命令照常运行：{result.stderr}"
    assert json.loads(result.stdout) == {"max_body_bytes": SET_NUMBER}


# --- a failed validation step (requirements 22.7 and 22.8) -------------------------------


@pytest.mark.req("22.8")
def test_a_validation_step_that_failed_ends_the_command_with_exit_code_1(
    cli: ErpMockGroup,
) -> None:
    expected = failed_step().lines()

    result = run(cli, "probe-loading")

    assert result.exit_code == EXIT_FAILED, "加载校验失败的退出码必须是 1"
    assert result.stdout == "", "加载校验失败时 stdout 不输出任何内容"
    assert result.stderr.splitlines() == list(expected), "stderr 是需求 2 第 2 条的错误清单"
    assert result.stderr.startswith("[step 5] instance.yaml"), "错误按加载器排好的顺序输出"
    assert "Error:" not in result.stderr, "错误清单不带 click 自己的前缀"


# --- where the output goes (requirement 22.2) --------------------------------------------


@pytest.mark.req("22.2")
def test_the_log_of_the_service_goes_to_stderr_alone(cli: ErpMockGroup) -> None:
    result = run(cli, "probe-log")

    assert result.exit_code == EXIT_OK, f"probe-log 应成功退出：{result.stderr}"
    assert result.stdout == "", "日志不写 stdout"
    assert f"INFO {LOG_SOURCE}: {LOG_LINE}" in result.stderr, "日志写入 stderr，含级别与模块名"


@pytest.mark.req("22.2")
def test_setting_up_the_log_twice_leaves_one_handler(service_log: logging.Logger) -> None:
    setup_logging()
    setup_logging()

    assert len(service_log.handlers) == 1, "重复配置不叠加处理器"
    assert isinstance(service_log.handlers[0], logging.StreamHandler), "处理器写入流"
    assert service_log.propagate is False, "服务日志不传给 root logger"


# --- the command group itself ------------------------------------------------------------


@pytest.mark.req("22.1")
def test_the_commands_are_registered_in_one_place() -> None:
    assert isinstance(main, ErpMockGroup), "命令组是处理 ConfigErrors 的那个组"
    assert tuple(main.commands) == tuple(str(command.name) for command in COMMANDS), (
        "已注册的命令与 COMMANDS 清单一致，后续任务只在该清单中增加"
    )


@pytest.mark.req("22.8")
def test_the_version_and_the_help_of_the_group_end_with_exit_code_0() -> None:
    version = run(main, "--version")
    usage = run(main, "--help")

    assert version.exit_code == EXIT_OK, "--version 以退出码 0 结束"
    assert version.stdout.strip() == f"{PROGRAM_NAME}, version {FRAMEWORK_VERSION}"
    assert usage.exit_code == EXIT_OK, "--help 以退出码 0 结束"
    assert PROGRAM_NAME in usage.stdout, "--help 的用法行是安装后的命令名"
