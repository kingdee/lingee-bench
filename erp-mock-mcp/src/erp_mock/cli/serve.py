"""The serve command: validate, then serve the two ports of one process (req 22.1).

``erp-mock-mcp serve --instance <目录> [--host] [--port] [--admin-port] [--data-dir]
[--packs-dir]`` is the command a deployer runs: it validates the instance configuration the
way :mod:`erp_mock.cli.validate` does, and then serves the Streamable HTTP MCP endpoint on
``--host``/``--port`` and the Admin API on the loopback address alone (requirement 19.1).
The serving itself is :func:`erp_mock.gateway.serve.serve`; this module is everything that
has to be settled before it can run, in the one order those decisions allow:

=====  ===========================================  ==================  ==================
step   what                                         what can go wrong   code
=====  ===========================================  ==================  ==================
1      read the environment once                    a value of the      2
       (:func:`~erp_mock.cli.options.read_env`,     wrong form
       requirements 23.9 and 23.10)                 (req 23.10)
2      a non-loopback ``--host`` needs an MCP       no MCP endpoint     2
       endpoint token (:func:`check_host`)          token (req 23.2)
3      the 8 validation steps and the cross-check   a step found        1
       (requirements 2.1 and 22.7)                  errors; a Snapshot
                                                    would not build
4      settle the Admin token, which may mean       the token file      1
       writing the token file (:func:`admin_token`) cannot be written
5      assemble and serve both ports                a port cannot be    1
       (:func:`~erp_mock.gateway.serve.serve`)      bound
=====  ===========================================  ==================  ==================

Why that order. The environment comes first because requirement 23.10 refuses a bad value
"在加载校验之前", and step 2 is in front of the loading for the same reason
(requirement 23.2): a service that would be reachable from the network without a token must
not get as far as reading a document, binding a port or creating a sandbox. The loading is
in front of the token file because a configuration that does not validate leaves nothing
behind - no file in the data directory, no port bound (requirement 22.7) - and the token is
settled before the sockets, since requirement 23.5 writes that file "在开始监听前".

The Admin token (requirements 23.4 and 23.5):

===================================  ================================================
``ERP_MOCK_ADMIN_TOKEN`` is set      that value is the token; no token file is written
it is unset or empty                 ``secrets.token_urlsafe(32)`` - 256 bits of
                                     system randomness over the ASCII letters, the
                                     digits, ``_`` and ``-`` - written to
                                     ``<data-dir>/admin-token`` with mode 0600, and
                                     its absolute path on stderr
===================================  ================================================

The value itself reaches stdout, stderr, the log and the trace nowhere (requirement 23.6):
what is logged is the path of the file, and the file is the only place on disk the token is
written to (:func:`write_token_file`).

What this module injects into the gateway, which builds neither itself (design.md 7.4):

- the Snapshot source: :class:`CachedSnapshots` over
  :class:`~erp_mock.seed.cache.SnapshotCache`, so a sandbox of a dataset the cache has no
  Snapshot for builds one on the way (requirement 20.11) and the gateway imports nothing of
  the seed layer;
- the tools layer: :class:`~erp_mock.tools.layer.ToolLayer` over the engine loading already
  built from the syntax trees of step 5 (:func:`~erp_mock.loader.step8.engine_of`), so no
  rule, writeback or query is parsed a second time.

What serve does not have: ``--anchor-date``. The anchor date of its sandboxes is the one the
``clock`` of the instance configuration decides (requirement 16.1), which for mode today
moves with the day the sandbox is created on; the three commands that only ever work on one
date take the option instead (:mod:`erp_mock.cli.options`).
"""

import ipaddress
import logging
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path
from typing import Final

import anyio
import click

from erp_mock.cli.loading import load_steps, prepare
from erp_mock.cli.options import (
    ADMIN_TOKEN_VAR,
    EXIT_FAILED,
    MCP_TOKEN_VAR,
    EnvValues,
    data_dir_option,
    instance_option,
    packs_dir_option,
    read_env,
)
from erp_mock.gateway.sandbox import SnapshotSource
from erp_mock.gateway.serve import (
    ADMIN_HOST,
    DEFAULT_ADMIN_PORT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ServeOptions,
)
from erp_mock.gateway.serve import serve as serve_ports
from erp_mock.loader.errors import CROSS
from erp_mock.loader.instance import DatasetModel
from erp_mock.loader.loader import Loader
from erp_mock.loader.step5_expr import ParsedDomain
from erp_mock.loader.step8 import engine_of
from erp_mock.seed.builder import SeedBuilder
from erp_mock.seed.cache import SnapshotCache
from erp_mock.tools.layer import ToolLayer

__all__ = [
    "HOST_OPTION",
    "LOOPBACK_NAME",
    "MAX_PORT",
    "MIN_PORT",
    "TOKEN_BYTES",
    "TOKEN_FILENAME",
    "TOKEN_MODE",
    "CachedSnapshots",
    "TokenFileFailed",
    "admin_port_option",
    "admin_token",
    "check_host",
    "host_option",
    "is_loopback",
    "port_option",
    "serve",
    "write_token_file",
]

_LOG: Final = logging.getLogger(__name__)

# The option the refusal of requirement 23.2 names, so the message and the declaration
# below cannot drift apart.
HOST_OPTION: Final = "--host"

# What a port may be (requirement 22.8): a TCP port number. 0 - "any free port" - is not
# among them, so a service always listens where it was told to.
MIN_PORT: Final = 1
MAX_PORT: Final = 65535

# The host name that is a loopback address without being one (requirement 23.2), compared
# without case, host names being case-insensitive.
LOOPBACK_NAME: Final = "localhost"

# The token file of requirement 23.5: its name below ``--data-dir`` (design.md data model
# 2), the mode it carries before anything is written into it, and the number of random
# bytes the token is made of - ``secrets.token_urlsafe(32)`` is 32 bytes of system
# randomness, 256 bits, written as the ASCII letters, the digits, ``_`` and ``-``.
TOKEN_FILENAME: Final = "admin-token"
TOKEN_MODE: Final = 0o600
TOKEN_BYTES: Final = 32

# The temporary file the token is written to before it is renamed into place: a name of
# this process in the directory the token file is in, so the rename stays inside one file
# system (:func:`write_token_file`).
_TEMP_NAME: Final = ".{name}.{random}"

_HOST_NEEDS_TOKEN: Final = (
    "{option} 的值 {value} 不是回环地址（127.0.0.0/8 中的 IPv4 地址、::1 或 localhost）；"
    "在这样的地址上提供 MCP 端点必须用环境变量 {variable} 配置 MCP 端点令牌"
)
_TOKEN_WRITE_FAILED: Final = "无法写入 Admin 令牌文件 {path}：{error}"
_TOKEN_FROM_ENV: Final = "Admin 令牌取自环境变量 %s"
_TOKEN_WRITTEN: Final = "Admin 令牌已写入 %s"
_NO_DOMAIN: Final = "第 5 步没有报错也没有产出领域模型"


class TokenFileFailed(click.ClickException):
    """The token file of requirement 23.5 could not be written: exit code 1 (req 22.8).

    A data directory that cannot be created or written in is neither a configuration error
    of requirement 2.2 nor anything the command line can mend - it is a service that could
    not start, like a port that cannot be bound. The message names the file and what the
    operating system reported and carries no token (requirement 23.6).
    """

    exit_code = EXIT_FAILED

    def __init__(self, path: Path, error: OSError) -> None:
        super().__init__(_TOKEN_WRITE_FAILED.format(path=path, error=error))
        self.path = path
        self.error = error


host_option = click.option(
    "--host",
    "host",
    default=DEFAULT_HOST,
    show_default=True,
    help="MCP 端点监听的地址；不是回环地址时必须配置环境变量 ERP_MOCK_MCP_TOKEN。",
)

port_option = click.option(
    "--port",
    "port",
    type=click.IntRange(MIN_PORT, MAX_PORT),
    default=DEFAULT_PORT,
    show_default=True,
    help="MCP 端点的端口。",
)

admin_port_option = click.option(
    "--admin-port",
    "admin_port",
    type=click.IntRange(MIN_PORT, MAX_PORT),
    default=DEFAULT_ADMIN_PORT,
    show_default=True,
    help=f"Admin 接口的端口；Admin 接口始终只在 {ADMIN_HOST} 上监听。",
)


@click.command(name="serve")
@instance_option
@host_option
@port_option
@admin_port_option
@data_dir_option
@packs_dir_option
@click.pass_context
def serve(
    ctx: click.Context,
    instance_dir: Path,
    host: str,
    port: int,
    admin_port: int,
    data_dir: Path,
    packs_dir: Path,
) -> None:
    """校验实例配置，然后提供 MCP 端点与 Admin 接口，直到收到 SIGINT 或 SIGTERM。"""
    # The five steps of the module docstring, in the order the requirements fix.
    env = read_env()
    check_host(host, env.mcp_token)
    loader = prepare(instance_dir, packs_dir, data_dir)
    load_steps(loader, CROSS)
    loaded = loader.assemble()
    settings = env.settings(admin_token(env, data_dir))
    # The engine of the loading, which both the Snapshot builder of a dataset the cache has
    # nothing for and the tools layer of every call work through (module docstring).
    engine = engine_of(_parsed_of(loader))
    options = ServeOptions(data_dir=data_dir, host=host, port=port, admin_port=admin_port)
    # Annotated so that what the gateway asks for and what the CLI hands it are checked
    # against each other here rather than at the first sandbox a session creates.
    snapshots: SnapshotSource = CachedSnapshots(
        SnapshotCache(data_dir, SeedBuilder(loaded, instance_dir, engine))
    )
    layer = ToolLayer(loaded, engine, limits=settings.resources)
    # The one event loop of the process, started here and left when the service has
    # stopped; ``anyio.run`` passes positional arguments alone, hence the partial.
    exit_code = anyio.run(
        partial(serve_ports, loaded, options, settings, snapshots=snapshots, layer=layer)
    )
    # The gateway returns the exit code rather than raising: 0 for a stop that was asked
    # for, 1 for a port that could not be bound, which it has reported on stderr already
    # (requirements 22.1 and 22.8).
    ctx.exit(exit_code)


# --- step 2: the host and the MCP endpoint token (requirement 23.2) ----------------------


def check_host(host: str, mcp_token: str | None) -> None:
    """Refuse a ``--host`` that is reachable from the network without a token (req 23.2).

    ``host`` is ``--host`` and ``mcp_token`` the ``ERP_MOCK_MCP_TOKEN`` of the environment,
    None where it is unset or empty - that is, where the MCP port would require no
    Authorization header of any request (requirement 23.3).

    Raises :class:`click.UsageError` - :data:`~erp_mock.cli.options.EXIT_USAGE`, a line on
    stderr naming the option, its value and the variable, nothing on stdout - for a host
    that is not a loopback address while no token is configured. Called before anything is
    loaded, bound or created, which is what the "在加载校验之前拒绝启动" of that requirement
    asks for.
    """
    if mcp_token is not None or is_loopback(host):
        return
    raise click.UsageError(
        _HOST_NEEDS_TOKEN.format(option=HOST_OPTION, value=host, variable=MCP_TOKEN_VAR)
    )


def is_loopback(host: str) -> bool:
    """Whether ``host`` is a loopback address as requirement 23.2 defines one.

    The three the requirement lists: an IPv4 address of 127.0.0.0/8, the IPv6 address
    ``::1``, and the name ``localhost`` whatever its case. Anything else is not one -
    ``0.0.0.0`` and ``::`` included, which are every address of the machine rather than its
    loopback, and a name this does not resolve, a name being whatever the name service of
    the machine says it is at the moment it is asked.
    """
    if host.lower() == LOOPBACK_NAME:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --- step 4: the Admin token (requirements 23.4 and 23.5) --------------------------------


def admin_token(env: EnvValues, data_dir: Path) -> str:
    """The Admin token of this process, written to the token file where it was generated.

    ``ERP_MOCK_ADMIN_TOKEN`` where the environment set one, and no file is written then
    (requirement 23.4); otherwise :data:`TOKEN_BYTES` bytes of system randomness, written
    to ``<data-dir>/admin-token`` (requirement 23.5, :func:`write_token_file`). What goes
    to the log is which of the two happened and, for the second, the absolute path of the
    file - never the value, whichever it came from (requirement 23.6).

    Raises :class:`TokenFileFailed` where the file could not be written.
    """
    from_env = env.admin_token
    if from_env is not None:
        _LOG.info(_TOKEN_FROM_ENV, ADMIN_TOKEN_VAR)
        return from_env
    token = secrets.token_urlsafe(TOKEN_BYTES)
    _LOG.info(_TOKEN_WRITTEN, write_token_file(data_dir, token))
    return token


def write_token_file(data_dir: Path, token: str) -> Path:
    """Write ``token`` to ``<data-dir>/admin-token`` and return that path (req 23.5).

    The file holds the token and nothing else - no newline, no second line - and carries
    mode :data:`TOKEN_MODE` from before the token is in it: it is created with
    ``O_CREAT | O_EXCL`` and that mode under a name of its own, ``fchmod`` makes the mode
    exact whatever the umask of the process would have taken off, the bytes are written and
    fsynced, and ``os.replace`` then makes it the token file. So a reader of the token file
    sees a complete token under 0600 or nothing at all, never a file being written or one a
    wider mode was corrected on, and a file left by an earlier process is replaced rather
    than appended to or truncated in place.

    Raises :class:`TokenFileFailed` where the directory cannot be created or the file
    cannot be written; the temporary file is removed when that happens, so a failed start
    leaves nothing behind.
    """
    path = data_dir / TOKEN_FILENAME
    temporary = data_dir / _TEMP_NAME.format(name=TOKEN_FILENAME, random=secrets.token_hex(8))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            _write_exclusive(temporary, token)
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    except OSError as error:
        raise TokenFileFailed(path, error) from error
    return path


def _write_exclusive(path: Path, token: str) -> None:
    """Create ``path``, which must not exist, with mode 0600 and ``token`` in it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, TOKEN_MODE)
    try:
        # The mode of ``os.open`` is what the umask of the process leaves of it; this is
        # the mode of requirement 23.5 itself, and it is set before the token is written.
        os.fchmod(fd, TOKEN_MODE)
        os.write(fd, token.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


# --- step 5: what the gateway is given (design.md 7.4) -----------------------------------


@dataclass(frozen=True, slots=True)
class CachedSnapshots:
    """The Snapshot cache as the gateway's :class:`SnapshotSource` (design.md 6.2).

    The four lines the gateway asks of a Snapshot source: the Snapshot of this dataset at
    this anchor date, built where the cache holds none that still fits (requirement 20.11),
    and kept where it is while the caller copies it - the shared lock of the cache key,
    which a replacement in this or another process waits for (design.md 6.2).

    One per process, over the cache of ``--data-dir``. The method is called in a worker
    thread, a build being the replay of a whole dataset through the engine, and the cache
    is what keeps the builds of one key apart; a build that fails raises
    :class:`~erp_mock.seed.resolve.SeedError` out of the method, which the gateway answers
    with the 500 or the -32603 of requirements 15.14 and 19.19.
    """

    cache: SnapshotCache

    @contextmanager
    def snapshot(self, dataset: DatasetModel, anchor: date) -> Iterator[Path]:
        """The Snapshot file of ``dataset`` at ``anchor``, readable for the block."""
        ref = self.cache.get_or_build(dataset.definition, anchor, file=dataset.file)
        with self.cache.shared(ref):
            yield ref.db


def _parsed_of(loader: Loader) -> ParsedDomain:
    """The domain model and expression trees of ``loader``; step 5 has run and passed.

    The engine of the service is built from those trees rather than from the model alone, so
    no rule, writeback or query is parsed a second time
    (:func:`~erp_mock.loader.step8.engine_of`).
    """
    parsed = loader.parsed
    if parsed is None:
        raise AssertionError(_NO_DOMAIN)
    return parsed
