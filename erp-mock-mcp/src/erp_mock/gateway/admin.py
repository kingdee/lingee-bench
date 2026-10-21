"""The Admin port of the serve command: an exact table of method and path (design.md 7.7).

The Admin API is the second ASGI application of a serve process, on its own socket bound to
127.0.0.1 (requirement 19.1). The middlewares of task 23.1 check the Admin token and the
size of the body, and what passes them reaches :class:`AdminRouter`, which is a table of
(method, path pattern) pairs and nothing else::

    admin = AdminRouter(mcp, loaded)        # mcp is the McpRouter of the same process

Not a Starlette ``Router``, and the reason is one requirement: a path that exists with
another method is answered with 405 by any framework router, while requirement 19.15 asks
for 404 for every combination of method and path that is not one of the 13 interfaces. The
table is therefore matched on the pair, and a request that matches no entry gets the same
404 as a path nobody has ever served (:meth:`AdminRouter._dispatch`).

The seven interfaces of task 23.2, in the order the table lists them, and what each of them
answers with:

==================================  =================================  =====
method and path                     success                            req
==================================  =================================  =====
GET /admin/health                   200, the six version fields        19.9
GET /admin/datasets/{name}/aliases  200, the alias map of the dataset  19.10
POST /admin/sandboxes               201, the sandbox and its mcp_url   19.3
GET /admin/sandboxes                200, ``{"items": [...]}``          19.11
GET /admin/sandboxes/{id}           200, the sandbox metadata          19.12
POST /admin/sandboxes/{id}/freeze   200, the metadata, now frozen      19.13
DELETE /admin/sandboxes/{id}        204, no body at all                19.13
==================================  =================================  =====

The six interfaces of task 23.3, which complete the 13 of requirement 19.1, in the same
order and with the same reading:

=========================================  ==================================  ==========
method and path                            success                             req
=========================================  ==================================  ==========
POST /admin/sandboxes/{id}/reset           200, the metadata, reset            19.4
POST /admin/sandboxes/{id}/snapshots       201, ``{"sandbox_id", "name"}``     19.5
POST /admin/sandboxes/{id}/restore         200, the metadata, restored         19.6
GET /admin/sandboxes/{id}/trace            200, the trace lines as they are    19.7
GET /admin/sandboxes/{id}/diff             200, the diff of requirement 18     19.8
POST /admin/exports                        200, one zip of trace and diff      19.14
=========================================  ==================================  ==========

The first three change a sandbox and go through the Sandbox_Manager, which takes the
sandbox lock, runs the work in the shared thread pool and writes the trace event of a
reset and a restore (design.md 7.7). The last three only read, and they read without the
lock (:meth:`AdminRouter._in_pool`): the trace is read to its last newline, so a line that
is half written is not in the answer, and the diff reads through a connection of its own
(design.md 3.4), which in WAL mode sees exactly the calls that had finished when the read
began (requirement 19.8). Probe V8 of tests/probes/test_sqlite.py is what allows this: a
plain connection with ``query_only`` reads the database of an idle sandbox, whose ``-shm``
file is gone, without writing to it. Had that not held, the diff would have had to be read
through :meth:`~erp_mock.gateway.sandbox.SandboxManager.run` like a call, which would make
a diff wait for the request in flight and - with ``activity=False`` - still have to keep
the idle timing out of it. Neither read moves ``last_done``, so asking for a trace or a
diff does not keep a sandbox from turning idle (requirements 19.7 and 19.8).

What is refused, and with which status:

======  ==========================================================================  ============
status  when                                                                        req
======  ==========================================================================  ============
404     the method and path are not one of the interfaces; a sandbox_id that is     19.15
        not a sandbox of this process; a dataset of a path that is not in the
        instance configuration; the Named_Snapshot a restore or a diff names
400     an undefined key, a value of the wrong type or a body that is no JSON       19.17, 19.20
        object; a profile or dataset that is not configured; an anchor_date that
        is no calendar date; a status that is not one of the three; a time without
        a UTC offset; a Named_Snapshot name that is missing, of another shape or
        the reserved ``initial``
409     a reset, a restore or a Named_Snapshot of a frozen sandbox; a               19.16
        Named_Snapshot whose name the sandbox already has
503     the sandboxes that are not frozen are all taken (requirement 15.10)         19.18
500     the Snapshot of the new sandbox could not be built; a trace or a diff       19.19
        that could not be read
======  ==========================================================================  ============

Every one of them is one JSON object, ``{"error": <short code>, "message": <Chinese>}``
(:class:`AdminError`, :func:`_error`), and none of them names a token, a request header or
the text of an exception (design.md error handling). Request bodies are parsed by strict
pydantic models that forbid undefined keys, which is requirement 19.17 read as a model:
a body that does not fit one is a 400 whatever is wrong with it. Query parameters go
through the same kind of model (:class:`SandboxFilter`), so an undefined parameter is a 400
as well rather than a filter that is silently dropped.

Two readings of requirement 19.17 are worth naming, both chosen for the stricter answer:

- the clause "profile 或 dataset 不在实例配置中" is applied to the ``dataset`` query
  parameter of GET /admin/sandboxes too, not only to the body of POST /admin/sandboxes: a
  filter by a dataset the instance does not have would otherwise answer 200 with an empty
  list, which says nothing about the mistake;
- a body on an interface that defines none (POST .../freeze, POST .../reset, DELETE) must
  be empty or ``{}`` (:class:`EmptyBody`), every key of it being an undefined key.

A query string, on the other hand, is checked where the interface defines parameters -
GET /admin/sandboxes and GET .../diff, whose models forbid what they do not define - and
ignored where it defines none, as on POST .../freeze and GET .../trace.

The one byte-for-byte promise of this module: the diff of GET .../diff with base
``initial`` is the same bytes as the ``diff.json`` the stdio runtime writes on exit and as
the ``diff.json`` of an export (requirement 24.2). The three therefore go through one
serialization - :func:`~erp_mock.state.diff.compute_diff`, then
:meth:`~erp_mock.state.diff.DiffBody.as_json` with the four values of the sandbox, then
:func:`~erp_mock.common.jsonio.dumps_pretty` - and the test module of this file compares
the response with what :mod:`erp_mock.gateway.stdio` wrote for the same sandbox.

What this module leaves to others, deliberately: the Admin token and the body limit of
task 23.1, and the two sockets, the ``GatewayState`` and the 404 that the MCP port answers
an ``/admin`` path with of task 23.4. It takes the sandboxes from the
:class:`~erp_mock.gateway.router.McpRouter` of the same process rather than from a manager
of its own, which is what keeps the two ports talking about the same sandboxes: freezing
and deleting one has to end its sessions (requirement 19.13), and
:meth:`~erp_mock.gateway.router.McpRouter.terminate_sandbox` is that entry point.

What it does not know, structurally: the seed layer (requirement 24.8, design.md 7.7). A
Snapshot reaches a new sandbox through the ``SnapshotSource`` the Sandbox_Manager was built
with, so the exception of a failed build cannot be named here and every failure of a
creation that is not the concurrency limit is answered with the 500 of requirement 19.19.
For the same reason the name of the alias map file is repeated here
(:data:`ALIASES_FILENAME`) instead of imported from :mod:`erp_mock.seed.builder`, which
imports the domain layer; the test module of this file asserts the two agree. The name of
the diff file is repeated for a smaller reason (:data:`DIFF_FILENAME`): the Admin API and
the stdio runtime happen to write the same file name, and the export of this module has no
other business with :mod:`erp_mock.gateway.stdio`.
"""

import asyncio
import io
import json
import logging
import re
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Final

import anyio.to_thread
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError
from pydantic_core import PydanticCustomError
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from erp_mock.common.jsonio import JsonValue, dumps_compact, dumps_pretty, encode_utf8
from erp_mock.config.fields import DateText
from erp_mock.gateway.clock import resolve_anchor
from erp_mock.gateway.router import SNAPSHOT_FAILED_MESSAGE, McpRouter, sandbox_path
from erp_mock.gateway.sandbox import (
    NAMED_NAME_PATTERN,
    NamedSnapshotExists,
    NamedSnapshotMissing,
    SandboxFrozen,
    SandboxLimitReached,
    SandboxManager,
    SandboxRuntime,
    Status,
)
from erp_mock.gateway.trace import TRACE_FILENAME
from erp_mock.loader.instance import DatasetModel, LoadedInstance, ProfileModel
from erp_mock.state.diff import INITIAL_BASE, compute_diff
from erp_mock.state.schema import StorageSchema, build_schema

__all__ = [
    "ALIASES_FILENAME",
    "DIFF_FILENAME",
    "AdminError",
    "AdminRouter",
    "DiffQuery",
    "EmptyBody",
    "ExportFilter",
    "SandboxCreate",
    "SandboxFilter",
    "SnapshotName",
]

# Where a failure this module does not answer with goes: the service log on stderr.
_LOG: Final = logging.getLogger(__name__)

# The alias map of a Snapshot, below the cache directory loading step 8 took up for the
# dataset (:attr:`~erp_mock.loader.instance.DatasetModel.snapshot`). The name is the one
# :data:`erp_mock.seed.builder.ALIASES_FILENAME` writes; it is repeated rather than
# imported because the gateway does not import the seed layer (module docstring), and
# tests/unit/gateway/test_admin_lifecycle.py asserts the two are the same string.
ALIASES_FILENAME: Final = "aliases.json"

# The ``<sandbox_id>/diff.json`` of an export (requirement 19.14). The name is the one
# :data:`erp_mock.gateway.stdio.DIFF_FILENAME` writes on exit, and the content is the same
# bytes; it is repeated rather than imported (module docstring), and
# tests/unit/gateway/test_admin_state.py asserts the two are the same string.
DIFF_FILENAME: Final = "diff.json"

# The media type and the header every answer of this port carries, and the status line
# header of a 204, which carries nothing at all (requirement 19.13).
_JSON_MEDIA_TYPE: Final = "application/json"
_CONTENT_TYPE: Final = "Content-Type"

# What the two interfaces that do not answer with a JSON object carry: the trace is JSONL,
# one event per line (integration.md section 5), and the export one zip file
# (requirement 19.14).
_NDJSON_MEDIA_TYPE: Final = "application/x-ndjson"
_ZIP_MEDIA_TYPE: Final = "application/zip"

# What makes the zip of an export the same bytes for the same sandboxes: every entry
# carries the earliest timestamp the format can write (DOS time starts in 1980), the same
# permissions, the same host system - so the bytes do not depend on the machine - and one
# fixed compression level. Without them two exports of one unchanged sandbox would differ
# in their entry headers alone.
_ZIP_TIMESTAMP: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)
_ZIP_MODE: Final = 0o644
_ZIP_UNIX: Final = 3
_ZIP_LEVEL: Final = 9
_ZIP_MODE_SHIFT: Final = 16

# What a WebSocket upgrade of the Admin port is answered with: the port serves HTTP alone.
_WS_CLOSE_CODE: Final = 1000

# The methods the table uses, written once so an entry cannot misspell one.
_GET: Final = "GET"
_POST: Final = "POST"
_DELETE: Final = "DELETE"

# The two keys of an error response, in the order design.md 7.7 writes them.
_ERROR_KEY: Final = "error"
_MESSAGE_KEY: Final = "message"

# The one key of the listing of requirement 19.11.
_ITEMS_KEY: Final = "items"

# What a request this module refuses is told, one message per reason. Each names what was
# asked for - the path, the name, the id - so the caller sees which part of its request was
# refused; none of them names a token, a header value or the text of an exception
# (design.md error handling).
_UNKNOWN_ROUTE: Final = "{method} {path} 不是 Admin API 的接口"
_UNKNOWN_SANDBOX: Final = "{sandbox_id} 不是本服务现存的沙箱"
_UNKNOWN_DATASET_PATH: Final = "实例配置中没有名为 {name} 的初始数据集"
_UNKNOWN_PROFILE: Final = "profile {name} 不在实例配置中"
_UNKNOWN_DATASET: Final = "初始数据集 {name} 不在实例配置中"
_BAD_JSON: Final = "请求体不是 JSON 文本"
_NOT_OBJECT: Final = "请求体必须是 JSON 对象"
_BAD_BODY: Final = "请求体不符合要求：{details}"
_BAD_QUERY: Final = "查询参数不符合要求：{details}"
_NO_ALIASES: Final = "初始数据集 {name} 的别名映射无法读取"
_BAD_NAME: Final = "Named_Snapshot 的名称 {name} 不匹配 ^[a-z0-9][a-z0-9_-]{{0,63}}$"
_RESERVED_NAME: Final = f"{INITIAL_BASE} 是 diff 基准的保留名称，不能用作 Named_Snapshot 的名称"
_FROZEN_SANDBOX: Final = "沙箱 {sandbox_id} 已冻结，不能重置、恢复或创建 Named_Snapshot"
_NAME_EXISTS: Final = "沙箱 {sandbox_id} 中已存在名为 {name} 的 Named_Snapshot"
_NAME_MISSING: Final = "沙箱 {sandbox_id} 中不存在名为 {name} 的 Named_Snapshot"
_NO_TRACE: Final = "沙箱 {sandbox_id} 的 trace 无法读取"
_NO_DIFF: Final = "沙箱 {sandbox_id} 相对基准 {base} 的 diff 无法计算"

# How one pydantic line error is named in the message of a 400: the key it is about and the
# code of what is wrong with it, never the value that was sent (module docstring).
_DETAIL: Final = "{where} {kind}"
_DETAIL_ROOT: Final = "-"
_DETAIL_SEPARATOR: Final = "、"
_LOC_SEPARATOR: Final = "."

# What a failure that is logged rather than answered with says.
_ALIASES_FAILED: Final = "初始数据集 %s 的别名映射 %s 无法读取：%s"
_CREATE_FAILED: Final = "初始数据集 %s 在锚点日期 %s 的沙箱预建失败：%s"
_TRACE_FAILED: Final = "沙箱 %s 的 trace 文件 %s 无法读取：%s"
_DIFF_FAILED: Final = "沙箱 %s 相对基准 %s 的 diff 计算失败：%s"


class AdminError(StrEnum):
    """The short code of an error response, the ``error`` of its object (design.md 7.7).

    A StrEnum member is a str, so it serializes as its value in JSON; the values are
    written like the error codes of a tool result (:class:`~erp_mock.common.errors.
    ErrorCode`), so one kind of code is read across the service.

    - ``NOT_FOUND``: the 404 of requirement 19.15 - a method and path that are not an
      interface, a ``sandbox_id`` that is not a sandbox, a dataset that is not in the
      instance configuration, a Named_Snapshot the sandbox does not have;
    - ``BAD_REQUEST``: the 400 of requirements 19.17 and 19.20;
    - ``CONFLICT``: the 409 of requirement 19.16 - a reset, a restore or a Named_Snapshot
      of a frozen sandbox, and a Named_Snapshot whose name is taken;
    - ``AT_CAPACITY``: the 503 of requirement 19.18;
    - ``SNAPSHOT_FAILED``: the 500 of requirement 19.19;
    - ``ALIASES_UNREADABLE``: the alias map of a dataset could not be read, which is a
      service whose data directory has lost a file rather than a request that was wrong;
    - ``READ_FAILED``: the trace or the diff of a sandbox could not be read, which is the
      same kind of thing: nothing of the request was wrong and nothing was changed.
    """

    NOT_FOUND = "NOT_FOUND"
    BAD_REQUEST = "BAD_REQUEST"
    CONFLICT = "CONFLICT"
    AT_CAPACITY = "AT_CAPACITY"
    SNAPSHOT_FAILED = "SNAPSHOT_FAILED"
    ALIASES_UNREADABLE = "ALIASES_UNREADABLE"
    READ_FAILED = "READ_FAILED"


# --- what a request may carry (requirements 19.17 and 19.20) -----------------------------

_OFFSET_REQUIRED: Final = "不是带时区偏移的 ISO 8601 时间"


def _check_offset_time(text: str) -> str:
    """Check that ``text`` is an ISO 8601 instant with a UTC offset (requirement 19.17).

    The ``created_at`` of a sandbox is such a text (requirement 19.12), and the bounds of a
    listing are compared against it as instants, which a text without an offset does not
    name. ``fromisoformat`` reads every form the service writes and rejects the rest.
    """
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise PydanticCustomError("offset_time", _OFFSET_REQUIRED) from None
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise PydanticCustomError("offset_time", _OFFSET_REQUIRED)
    return text


# An instant with a UTC offset, as the bounds of requirements 19.11 and 19.14 are written.
OffsetTime = Annotated[str, AfterValidator(_check_offset_time)]


class _Body(BaseModel):
    """Base of every Admin request model: strict, frozen, no undefined keys.

    Requirement 19.17 as a model configuration: a key the model does not define, a value of
    the wrong type (``"1"`` is not an int, ``1`` is not a str) and an alias instead of a
    field name are each a ValidationError, which :func:`_parse_body` and
    :func:`_parse_query` turn into one 400. The configuration models of
    :mod:`erp_mock.config` are strict in the same way and for the same reason
    (:class:`~erp_mock.config.base.FrozenModel`); this base is its own so that the bodies
    of an HTTP interface and the documents of a configuration stay separate things.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, populate_by_name=False)


class EmptyBody(_Body):
    """The body of an interface that defines none: empty or ``{}`` (requirement 19.17).

    POST /admin/sandboxes/{id}/freeze and DELETE /admin/sandboxes/{id} take no parameters,
    so every key of their body is an undefined key and makes the request a 400.
    """


class SandboxCreate(_Body):
    """The body of POST /admin/sandboxes, every field optional (requirement 19.3).

    ``profile`` and ``dataset`` are names of the instance configuration, compared
    case-sensitively, and a missing one takes the ``default_profile`` or the
    ``default_dataset``; ``anchor_date`` is the date of this one sandbox, which wins over
    the clock configuration (requirement 16.1). A name that is not in the configuration is
    a 400 of the handler rather than of the model: the model cannot know the instance, and
    the message that names the configuration is the more useful one (requirement 19.17).
    """

    profile: str | None = None
    dataset: str | None = None
    anchor_date: DateText | None = None


class SandboxFilter(_Body):
    """The query parameters of GET /admin/sandboxes (requirement 19.11).

    A sandbox is listed when it satisfies every parameter that was provided: ``status`` is
    its state, ``dataset`` the name of its initial dataset, and ``created_from`` and
    ``created_to`` bound its creation time at both ends inclusively. A parameter that was
    not provided limits nothing.
    """

    status: Status | None = None
    dataset: str | None = None
    created_from: OffsetTime | None = None
    created_to: OffsetTime | None = None


class SnapshotName(_Body):
    """The body of POST .../snapshots and POST .../restore (requirements 19.5 and 19.6).

    One required key, and a string: a body without ``name``, with a ``name`` that is not a
    string or with any other key is a 400 of the model (requirement 19.20). What the
    string may hold is checked by :func:`_snapshot_name` rather than by a validator of the
    field, so that the two reasons a name is refused - the shape and the reserved
    ``initial`` - each get a message of their own.
    """

    name: str


class DiffQuery(_Body):
    """The query parameters of GET .../diff (requirement 19.8).

    ``base`` is the basis the sandbox is compared against: missing or
    :data:`~erp_mock.state.diff.INITIAL_BASE` is the initial dataset, every other value is
    a Named_Snapshot of that sandbox, and a name the sandbox does not have is a 404
    (requirement 19.15). The name is not checked against
    :data:`~erp_mock.gateway.sandbox.NAMED_NAME_PATTERN` here: requirement 19.20 is about
    the ``name`` of a snapshot and a restore, and a basis of any other shape is simply a
    basis the sandbox does not have. :func:`_basis` looks the value up among the
    Named_Snapshots the sandbox really has rather than building a path out of it, so a
    value that is no name at all cannot reach outside the sandbox directory either.
    """

    base: str | None = None


class ExportFilter(_Body):
    """The body of POST /admin/exports, both fields optional (requirement 19.14).

    ``created_from`` and ``created_to`` bound the creation time of the sandboxes the zip
    holds, inclusively at both ends, and an end that was not given does not limit - the
    same reading as the listing of requirement 19.11, and the same comparison of instants.
    """

    created_from: OffsetTime | None = None
    created_to: OffsetTime | None = None


class _Refused(Exception):
    """A request the Admin API answers itself, carrying the response to send.

    Raised by every check of this module and caught in :meth:`AdminRouter.__call__`, which
    sends ``response`` and returns. Private on purpose - it never leaves this module - and
    the reason it exists is that the checks of an interface are a sequence of guards whose
    first failure is the answer (design.md error handling).
    """

    __slots__ = ("response",)

    response: Response

    def __init__(self, response: Response) -> None:
        super().__init__(f"{response.status_code}")
        self.response = response


# --- the route table (requirement 19.15) -------------------------------------------------

# What a handler is given: the request, and the named groups its path pattern captured.
type _Handler = Callable[[Request, Mapping[str, str]], Awaitable[Response]]

# One segment of a path, matched as a whole: a name or an id, whatever it looks like. A
# dataset name or a sandbox id that cannot be one is therefore not a path that fails to
# match but a name the handler does not find, which is the 404 of requirement 19.15.
_SEGMENT: Final = r"[^/]+"

_HEALTH_PATH: Final = re.compile(r"/admin/health")
_ALIASES_PATH: Final = re.compile(rf"/admin/datasets/(?P<name>{_SEGMENT})/aliases")
_SANDBOXES_PATH: Final = re.compile(r"/admin/sandboxes")
_SANDBOX_PATH: Final = re.compile(rf"/admin/sandboxes/(?P<sandbox_id>{_SEGMENT})")
_FREEZE_PATH: Final = re.compile(rf"/admin/sandboxes/(?P<sandbox_id>{_SEGMENT})/freeze")
_RESET_PATH: Final = re.compile(rf"/admin/sandboxes/(?P<sandbox_id>{_SEGMENT})/reset")
_SNAPSHOTS_PATH: Final = re.compile(rf"/admin/sandboxes/(?P<sandbox_id>{_SEGMENT})/snapshots")
_RESTORE_PATH: Final = re.compile(rf"/admin/sandboxes/(?P<sandbox_id>{_SEGMENT})/restore")
_TRACE_PATH: Final = re.compile(rf"/admin/sandboxes/(?P<sandbox_id>{_SEGMENT})/trace")
_DIFF_PATH: Final = re.compile(rf"/admin/sandboxes/(?P<sandbox_id>{_SEGMENT})/diff")
_EXPORTS_PATH: Final = re.compile(r"/admin/exports")


@dataclass(frozen=True, slots=True)
class _Route:
    """One entry of the table: a method, a path pattern and what serves the pair.

    ``pattern`` is matched against the whole path (``fullmatch``), so a prefix, a trailing
    slash and an extra segment match nothing; ``method`` is compared literally. A request
    is served by the first entry both of which fit, and by nothing otherwise.
    """

    method: str
    pattern: re.Pattern[str]
    handler: _Handler


class AdminRouter:
    """The Admin port of the serve command: a pure ASGI application (design.md 7.7).

    - ``mcp``: the :class:`~erp_mock.gateway.router.McpRouter` of the same process, which
      brings the Sandbox_Manager every interface works on and is what ends the sessions of
      a sandbox that was frozen or deleted (requirement 19.13);
    - ``loaded``: the validated instance configuration, which says which profile and which
      initial dataset a request may name, what the defaults are (requirement 19.3), which
      versions ``/admin/health`` reports (requirement 19.9) and whose ``clock`` an anchor
      date is resolved against (requirement 16.1);
    - ``schema``: the tables of that instance, which the diff of requirement 19.8 compares
      table by table. None builds them from ``loaded`` - the schema is a function of the
      domain model and nothing else (:func:`~erp_mock.state.schema.build_schema`) - so a
      caller that has none need not make one; the serve command passes the one every
      sandbox of the process was created with.

    Assembled by the serve command (task 23.4), which puts the middlewares of task 23.1 in
    front of it and runs it on the Admin socket.
    """

    __slots__ = ("_instance_ref", "_loaded", "_mcp", "_routes", "_schema")

    def __init__(
        self, mcp: McpRouter, loaded: LoadedInstance, schema: StorageSchema | None = None
    ) -> None:
        self._mcp = mcp
        self._loaded = loaded
        self._schema = build_schema(loaded.domain) if schema is None else schema
        self._instance_ref = _ref(loaded.instance.instance, loaded.instance.version)
        # The table of requirement 19.15, built per instance because the handlers are bound
        # methods. The order is the order of requirement 19.1; no two entries can match the
        # same pair, the patterns differing in their number of segments or in their last
        # one, which is a literal wherever a sandbox path has one.
        self._routes: tuple[_Route, ...] = (
            _Route(_GET, _HEALTH_PATH, self._health),
            _Route(_GET, _ALIASES_PATH, self._aliases),
            _Route(_POST, _SANDBOXES_PATH, self._create),
            _Route(_GET, _SANDBOXES_PATH, self._list),
            _Route(_GET, _SANDBOX_PATH, self._metadata),
            _Route(_POST, _RESET_PATH, self._reset),
            _Route(_POST, _SNAPSHOTS_PATH, self._snapshots),
            _Route(_POST, _RESTORE_PATH, self._restore),
            _Route(_GET, _TRACE_PATH, self._trace),
            _Route(_GET, _DIFF_PATH, self._diff),
            _Route(_POST, _FREEZE_PATH, self._freeze),
            _Route(_DELETE, _SANDBOX_PATH, self._delete),
            _Route(_POST, _EXPORTS_PATH, self._exports),
        )

    @property
    def manager(self) -> SandboxManager:
        """The Sandbox_Manager this API works on, which is the MCP port's (design.md 7.7)."""
        return self._mcp.manager

    # --- the ASGI entry point ----------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one HTTP request of the Admin port (:meth:`_dispatch`).

        Every answer is written here, the refusals of :class:`_Refused` included, so a
        handler either returns the response of a request it served or raises the response of
        one it refused.
        """
        if scope["type"] != "http":
            # The Admin port serves HTTP alone, and uvicorn runs with the lifespan protocol
            # off (design.md 7.4), so this is a WebSocket upgrade and nothing else.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": _WS_CLOSE_CODE})
            return
        request = Request(scope, receive)
        try:
            response = await self._dispatch(request, str(scope["method"]), str(scope["path"]))
        except _Refused as refused:
            response = refused.response
        await response(scope, receive, send)

    async def _dispatch(self, request: Request, method: str, path: str) -> Response:
        """The handler of the pair (``method``, ``path``), or a 404 (requirement 19.15).

        Both halves have to fit one entry of the table: a path of an interface with another
        method is answered with 404, not with the 405 a framework router would write, which
        is the whole reason this table exists (module docstring). The named groups of the
        pattern are handed to the handler as they arrived, the handler deciding what a name
        that is not in the configuration or an id that is not a sandbox means.
        """
        for route in self._routes:
            if route.method != method:
                continue
            found = route.pattern.fullmatch(path)
            if found is not None:
                return await route.handler(request, found.groupdict())
        raise _Refused(_not_found(_UNKNOWN_ROUTE.format(method=method, path=path)))

    # --- GET /admin/health (requirement 19.9) ------------------------------------------

    async def _health(self, request: Request, params: Mapping[str, str]) -> Response:
        """The versions of everything the service exposes, in the order of req 19.9.

        ``framework_version`` and ``meta_model_version`` are the engine's own, ``instance``
        the instance configuration as ``<name>@<version>``, and the last three one
        ``<name>@<version>`` per pack, profile and initial dataset, each in the order
        instance.yaml lists them (:class:`~erp_mock.loader.instance.VersionInfo`). Nothing
        of a sandbox is read, so no lock is taken and a frozen or busy sandbox changes no
        answer (design.md 7.7).
        """
        instance = self._loaded.instance
        versions = self._loaded.versions
        return _json(
            HTTPStatus.OK,
            {
                "framework_version": versions.framework,
                "meta_model_version": versions.meta_model,
                "instance": _ref(instance.instance, instance.version),
                "packs": [_ref(name, version) for name, version in versions.packs.items()],
                "profiles": _refs(instance.profiles, versions.profiles),
                "datasets": _refs(instance.datasets, versions.datasets),
            },
        )

    # --- GET /admin/datasets/{name}/aliases (requirements 19.10, 20.14) ----------------

    async def _aliases(self, request: Request, params: Mapping[str, str]) -> Response:
        """The alias map of one initial dataset, as its Snapshot wrote it (req 19.10).

        The file is the ``aliases.json`` of the cache entry loading step 8 took up for the
        dataset, read and written back out as one JSON object: its ``dataset`` and its
        ``aliases``, whose keys the Seed_Builder sorted by code point (requirement 20.10).
        Which anchor date that entry was built under does not matter - two builds of one
        dataset write the same alias map whatever the date (requirement 20.14) - so the
        interface takes no date and reads the one entry the service loaded with.

        A name that is not an initial dataset of the instance configuration is a 404
        (requirement 19.15); a file that is not there or cannot be read is a 500, the
        service having lost a file of its data directory rather than the caller having
        asked for something wrong.
        """
        dataset = self._dataset_of_path(params["name"])
        payload = await anyio.to_thread.run_sync(lambda: _read_aliases(dataset))
        return _json(HTTPStatus.OK, payload)

    # --- POST /admin/sandboxes (requirements 19.3, 19.18, 19.19) -----------------------

    async def _create(self, request: Request, params: Mapping[str, str]) -> Response:
        """Prebuild one sandbox that is bound to no session (requirement 19.3).

        The profile, the initial dataset and the anchor date are the body's or the
        configuration's defaults, and the sandbox is created the way an initialize creates
        one (requirement 15.1) except for its origin, which is ``prebuilt``: it stays when
        the sessions that connect to it end (requirement 15.8) and ``/s/<id>/mcp`` reaches
        it (requirement 14.3). The answer is 201 and the five fields of requirement 19.3,
        ``mcp_url`` being that address (:func:`~erp_mock.gateway.router.sandbox_path`).

        The anchor date is resolved here rather than left to the Sandbox_Manager, which
        would resolve the same one: it is the date the 500 of requirement 19.19 has to name,
        and resolving it once means the message names the date that was tried.

        Raises :class:`_Refused` with 503 where the sandboxes that are not frozen are all
        taken (requirement 19.18) and with 500 where the sandbox could not be created
        (requirement 19.19). The second one catches every failure but the first, because the
        gateway does not know the Snapshot source it was given (module docstring) and
        because every one of them means the same thing to the caller: there is no new
        sandbox, and no existing one was touched. The exception itself is logged, never
        answered with.
        """
        body = await _parse_body(request, SandboxCreate)
        profile = self._profile(body.profile)
        dataset = self._dataset(body.dataset)
        explicit = None if body.anchor_date is None else date.fromisoformat(body.anchor_date)
        anchor = resolve_anchor(explicit, self._loaded.instance.clock, self.manager.clock)
        try:
            sandbox = await self.manager.create(profile, dataset, anchor=anchor, origin="prebuilt")
        except SandboxLimitReached as error:
            raise _Refused(
                _error(HTTPStatus.SERVICE_UNAVAILABLE, AdminError.AT_CAPACITY, str(error))
            ) from error
        except Exception as error:
            _LOG.warning(_CREATE_FAILED, dataset.name, anchor.isoformat(), error)
            message = SNAPSHOT_FAILED_MESSAGE.format(
                dataset=dataset.name, anchor=anchor.isoformat()
            )
            raise _Refused(
                _error(HTTPStatus.INTERNAL_SERVER_ERROR, AdminError.SNAPSHOT_FAILED, message)
            ) from error
        return _json(
            HTTPStatus.CREATED,
            {
                "sandbox_id": sandbox.sandbox_id,
                "mcp_url": sandbox_path(sandbox.sandbox_id),
                "profile": sandbox.profile_ref,
                "dataset": sandbox.dataset_ref,
                "anchor_date": sandbox.anchor_date.isoformat(),
            },
        )

    # --- GET /admin/sandboxes and GET /admin/sandboxes/{id} (req 19.11, 19.12) ---------

    async def _list(self, request: Request, params: Mapping[str, str]) -> Response:
        """The sandboxes that satisfy every query parameter given (requirement 19.11).

        One object, with ``items`` and nothing else; each item is what
        :meth:`_metadata` answers for that sandbox, and the items are ordered by creation
        time and then by ``sandbox_id``, both ascending. The creation times are compared as
        instants, so a bound written in another UTC offset than the service's selects the
        same sandboxes.
        """
        query = _parse_query(request, SandboxFilter)
        if query.dataset is not None:
            # Requirement 19.17 read as the stricter answer (module docstring): a filter by
            # a dataset the instance does not have is a mistake, not an empty list.
            self._dataset(query.dataset)
        start = _instant(query.created_from)
        end = _instant(query.created_to)
        kept = [
            sandbox
            for sandbox in self.manager.sandboxes()
            if (query.status is None or sandbox.status == query.status)
            and (query.dataset is None or sandbox.dataset.name == query.dataset)
            and _within(sandbox, start, end)
        ]
        ordered = sorted(kept, key=lambda sandbox: (_created(sandbox), sandbox.sandbox_id))
        return _json(HTTPStatus.OK, {_ITEMS_KEY: [_sandbox_json(sandbox) for sandbox in ordered]})

    async def _metadata(self, request: Request, params: Mapping[str, str]) -> Response:
        """The metadata of one sandbox, in the order of requirement 19.12.

        Answered for a sandbox in any state, a frozen one included (requirement 15.12), and
        without taking its lock: nothing of it is changed and a request of it that is
        running is not waited for. A ``sandbox_id`` that is not a sandbox of this process,
        one that was deleted included, is a 404 (requirement 19.15).
        """
        return _json(HTTPStatus.OK, _sandbox_json(self._sandbox(params["sandbox_id"])))

    # --- POST .../freeze and DELETE .../{id} (requirements 19.13, 19.9) ----------------

    async def _freeze(self, request: Request, params: Mapping[str, str]) -> Response:
        """Turn one sandbox frozen before answering (requirements 19.13 and 15.7).

        The sandbox is read-only from here on, its database and its trace are closed, and
        its files stay until it is deleted (requirement 15.12); freezing a frozen sandbox
        changes nothing and is answered the same way. Every session bound to it ends with
        it (requirement 14.7), which is what
        :meth:`~erp_mock.gateway.router.McpRouter.terminate_sandbox` does, and the answer is
        200 and the object :meth:`_metadata` would answer with, whose ``status`` is
        therefore ``frozen``.
        """
        await _parse_body(request, EmptyBody)
        sandbox = self._sandbox(params["sandbox_id"])
        await self.manager.freeze(sandbox)
        await self._mcp.terminate_sandbox(sandbox.sandbox_id)
        return _json(HTTPStatus.OK, _sandbox_json(sandbox))

    async def _delete(self, request: Request, params: Mapping[str, str]) -> Response:
        """Delete one sandbox before answering (requirements 19.13 and 15.9).

        Its database, trace, Named_Snapshots and metadata are removed, a sandbox in any
        state included, every session bound to it ends (requirement 14.7) and its id is a
        404 from then on (requirement 19.15). The answer is 204 and no body at all.
        """
        await _parse_body(request, EmptyBody)
        sandbox = self._sandbox(params["sandbox_id"])
        await self.manager.delete(sandbox)
        await self._mcp.terminate_sandbox(sandbox.sandbox_id)
        return Response(status_code=HTTPStatus.NO_CONTENT)

    # --- POST .../reset, .../snapshots, .../restore (requirements 19.4 to 19.6) --------

    async def _reset(self, request: Request, params: Mapping[str, str]) -> Response:
        """Put the sandbox back to the Snapshot it was created from (requirement 19.4).

        The whole database becomes the content of ``base.sqlite`` - the records, the
        counters and the effective calls of requirement 18.10 - the call count of the
        sandbox goes back to 0 (requirement 23.12) and a ``sandbox_reset`` event is
        appended to the trace, whose ``seq`` therefore goes on where it was
        (requirement 17.7). The anchor date, the Named_Snapshots, the status and the
        session bindings are untouched (requirement 16.2), which is why the answer - 200
        and the object :meth:`_metadata` would answer with - reports the same metadata as
        before the reset.

        The work is the Sandbox_Manager's :meth:`~erp_mock.gateway.sandbox.SandboxManager.
        reset`, so it runs under the sandbox lock and after the request in flight
        (design.md 7.7). A frozen sandbox is a 409 and stays exactly as it was
        (requirement 19.16).
        """
        await _parse_body(request, EmptyBody)
        sandbox = self._sandbox(params["sandbox_id"])
        with _not_frozen(sandbox):
            await self.manager.reset(sandbox)
        return _json(HTTPStatus.OK, _sandbox_json(sandbox))

    async def _snapshots(self, request: Request, params: Mapping[str, str]) -> Response:
        """Save the content of the sandbox under a name (requirement 19.5).

        ``named/<name>.sqlite`` gets the database as it is now, counters and effective
        calls included, and ``named/<name>.json`` the call count of this moment, which a
        restore of this snapshot takes up again (requirement 23.12); the sandbox database
        and its trace are not changed and no event is written. The answer is 201 and the
        ``sandbox_id`` and ``name`` of what was saved.

        Refused with 400 for a name that is missing, of another shape or the reserved
        ``initial`` (requirement 19.20), and with 409 for a frozen sandbox or a name the
        sandbox already uses, neither of which changes anything of it (requirement 19.16).
        """
        body = await _parse_body(request, SnapshotName)
        name = _snapshot_name(body.name)
        sandbox = self._sandbox(params["sandbox_id"])
        with _not_frozen(sandbox):
            try:
                await self.manager.save_named(sandbox, name)
            except NamedSnapshotExists as error:
                message = _NAME_EXISTS.format(sandbox_id=sandbox.sandbox_id, name=name)
                raise _Refused(_conflict(message)) from error
        return _json(HTTPStatus.CREATED, {"sandbox_id": sandbox.sandbox_id, "name": name})

    async def _restore(self, request: Request, params: Mapping[str, str]) -> Response:
        """Put the sandbox back to one of its Named_Snapshots (requirement 19.6).

        The database becomes the content of that snapshot, its counters and its effective
        calls among it, the call count becomes the one the snapshot was saved with
        (requirement 23.12) and a ``snapshot_restore`` event naming it is appended. The
        anchor date, the trace, the bindings and every Named_Snapshot of the sandbox stay
        as they are, so the answer is 200 and the metadata object again.

        Refused with 400 for a name of another shape or the reserved ``initial``
        (requirement 19.20), with 409 for a frozen sandbox (requirement 19.16) and with
        404 for a name the sandbox does not have (requirement 19.15). The frozen check
        comes first, a frozen sandbox being unable to restore anything at all.
        """
        body = await _parse_body(request, SnapshotName)
        name = _snapshot_name(body.name)
        sandbox = self._sandbox(params["sandbox_id"])
        with _not_frozen(sandbox):
            try:
                await self.manager.restore(sandbox, name)
            except NamedSnapshotMissing as error:
                message = _NAME_MISSING.format(sandbox_id=sandbox.sandbox_id, name=name)
                raise _Refused(_not_found(message)) from error
        return _json(HTTPStatus.OK, _sandbox_json(sandbox))

    # --- GET .../trace and GET .../diff (requirements 19.7 and 19.8) -------------------

    async def _trace(self, request: Request, params: Mapping[str, str]) -> Response:
        """The events of the sandbox as the trace file holds them (requirement 19.7).

        The bytes of the file up to its last newline and nothing else: every event that
        was written before the request arrived, in order, one JSON object per line, and an
        empty body for a sandbox that has written none. The last newline is where it ends
        because a line that is being written is not an event yet - the answer carries whole
        events or nothing of them.

        Read without the sandbox lock and without going through
        :meth:`~erp_mock.gateway.sandbox.SandboxManager.run`, so the state and the idle
        timing of the sandbox are what they were (module docstring); a frozen sandbox is
        read like any other, its trace file staying until it is deleted
        (requirement 15.12).
        """
        sandbox = self._sandbox(params["sandbox_id"])
        return _bytes(
            HTTPStatus.OK, await self._in_pool(lambda: _read_trace(sandbox)), _NDJSON_MEDIA_TYPE
        )

    async def _diff(self, request: Request, params: Mapping[str, str]) -> Response:
        """The net change of the sandbox against a basis (requirement 19.8).

        The diff of requirement 18, serialized the way the stdio runtime writes its
        ``diff.json`` (module docstring): the basis is the initial dataset unless ``base``
        names a Named_Snapshot of this sandbox, and the answer holds the calls that had
        finished when the read began and no part of one that had not
        (:func:`~erp_mock.state.diff.compute_diff`).

        Read without the sandbox lock, through a connection of its own, so neither the
        state nor the idle timing of the sandbox changes and a request in flight is not
        waited for; a frozen or idle sandbox is read the same way. A ``base`` the sandbox
        does not have is a 404 (requirement 19.15).
        """
        query = _parse_query(request, DiffQuery)
        sandbox = self._sandbox(params["sandbox_id"])
        base = INITIAL_BASE if query.base is None else query.base
        payload = await self._in_pool(lambda: self._diff_bytes(sandbox, base))
        return _bytes(HTTPStatus.OK, payload, _JSON_MEDIA_TYPE)

    # --- POST /admin/exports (requirement 19.14) ---------------------------------------

    async def _exports(self, request: Request, params: Mapping[str, str]) -> Response:
        """One zip of the trace and the diff of every sandbox in range (req 19.14).

        Two entries per sandbox - ``<sandbox_id>/trace.jsonl`` and
        ``<sandbox_id>/diff.json`` - whose bytes are what :meth:`_trace` and
        :meth:`_diff` with base ``initial`` answer for that sandbox, written in the order
        of the listing of requirement 19.11: by creation time, then by ``sandbox_id``. A
        range that holds no sandbox gives a zip with no entries, which is a zip all the
        same.

        The sandboxes are chosen on the event loop, off the registry as it is at this
        moment, and read in the pool without any sandbox lock, like the two interfaces the
        entries come from. A sandbox whose trace or diff cannot be read makes the whole
        export a 500: half an export is worse than none, and the request changed nothing
        either way.
        """
        body = await _parse_body(request, ExportFilter)
        start = _instant(body.created_from)
        end = _instant(body.created_to)
        chosen = tuple(
            sorted(
                (sb for sb in self.manager.sandboxes() if _within(sb, start, end)),
                key=lambda sb: (_created(sb), sb.sandbox_id),
            )
        )
        return _bytes(
            HTTPStatus.OK, await self._in_pool(lambda: self._archive(chosen)), _ZIP_MEDIA_TYPE
        )

    # --- what the two read interfaces do, off the event loop ---------------------------

    def _diff_bytes(self, sandbox: SandboxRuntime, base: str) -> bytes:
        """The diff of ``sandbox`` against ``base`` as it is sent (worker thread).

        The basis, the comparison and the serialization of requirement 18.1, in the one
        order the service does them, so that this answer, the ``diff.json`` of the stdio
        runtime and the ``diff.json`` of an export are the same bytes (requirement 24.2).

        Raises :class:`_Refused` with 404 where ``base`` is no Named_Snapshot of the
        sandbox (:func:`_basis`) and with 500 where the comparison itself failed - a
        database file that is gone, a basis built from another instance configuration -
        which is logged with its reason and answered without it.
        """
        basis = _basis(sandbox, base)
        try:
            body = compute_diff(sandbox.store.db_path, basis, self._schema, base)
        except Exception as error:
            _LOG.warning(_DIFF_FAILED, sandbox.sandbox_id, base, error)
            message = _NO_DIFF.format(sandbox_id=sandbox.sandbox_id, base=base)
            raise _Refused(_read_failed(message)) from error
        payload = body.as_json(
            sandbox_id=sandbox.sandbox_id,
            instance=self._instance_ref,
            dataset=sandbox.dataset_ref,
            anchor_date=sandbox.anchor_date,
        )
        return encode_utf8(dumps_pretty(payload))

    def _archive(self, sandboxes: Sequence[SandboxRuntime]) -> bytes:
        """The zip of ``sandboxes``, in that order (worker thread, requirement 19.14).

        Each sandbox contributes its trace and its diff against the initial dataset, and
        every entry is written with the fixed timestamp, mode and compression of
        :data:`_ZIP_TIMESTAMP` and its neighbours, so two exports of the same sandboxes
        are the same bytes. Raises :class:`_Refused` where a trace or a diff could not be
        read (:meth:`_diff_bytes`, :func:`_read_trace`).
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for sandbox in sandboxes:
                _store(archive, sandbox.sandbox_id, TRACE_FILENAME, _read_trace(sandbox))
                diff = self._diff_bytes(sandbox, INITIAL_BASE)
                _store(archive, sandbox.sandbox_id, DIFF_FILENAME, diff)
        return buffer.getvalue()

    async def _in_pool[T](self, job: Callable[[], T]) -> T:
        """Run ``job`` in the Sandbox_Manager's pool, with no sandbox lock held.

        Where the three read interfaces do their work: the pool is the one every SQLite
        statement of the service runs in (design.md 7.5), which the diff needs, and the
        trace and the zip are built beside it rather than in a second pool of their own.
        The sandbox lock is not taken, which is the whole point (module docstring).

        The alias map of :meth:`_aliases` goes through ``anyio.to_thread`` instead: it is
        a file of the dataset cache, not of a sandbox, and no SQLite is involved.
        """
        return await asyncio.get_running_loop().run_in_executor(self.manager.executor, job)

    # --- what a path and a body name ---------------------------------------------------

    def _sandbox(self, sandbox_id: str) -> SandboxRuntime:
        """The sandbox ``sandbox_id``, or raise a 404 (requirement 19.15).

        Every sandbox of this process, whatever its origin and whatever its state: the
        Admin API reaches the sandbox of a session as well as a prebuilt one, unlike
        ``/s/<sandbox_id>/mcp``, which reaches prebuilt ones that are not frozen
        (requirement 14.3). A sandbox that was deleted is gone from the registry and is
        therefore a 404.
        """
        sandbox = self.manager.get(sandbox_id)
        if sandbox is None:
            raise _Refused(_not_found(_UNKNOWN_SANDBOX.format(sandbox_id=sandbox_id)))
        return sandbox

    def _dataset_of_path(self, name: str) -> DatasetModel:
        """The initial dataset ``name`` of a path, or raise a 404 (requirement 19.15)."""
        found = self._loaded.datasets.get(name)
        if found is None:
            raise _Refused(_not_found(_UNKNOWN_DATASET_PATH.format(name=name)))
        return found

    def _profile(self, name: str | None) -> ProfileModel:
        """The profile ``name``, the default one for None; 400 for another name.

        Compared case-sensitively, by looking the name up in the instance configuration
        (requirement 19.17); the default is the ``default_profile`` of instance.yaml
        (requirement 19.3).
        """
        if name is None:
            return self._loaded.default_profile
        found = self._loaded.profiles.get(name)
        if found is None:
            raise _Refused(_bad_request(_UNKNOWN_PROFILE.format(name=name)))
        return found

    def _dataset(self, name: str | None) -> DatasetModel:
        """The initial dataset ``name``, the default for None; 400 for another name.

        The same lookup as :meth:`_profile`, and the same 400: a name of a request body or
        of a query parameter that the instance configuration does not have is
        requirement 19.17, while a name in a path is the 404 of requirement 19.15
        (:meth:`_dataset_of_path`).
        """
        if name is None:
            return self._loaded.default_dataset
        found = self._loaded.datasets.get(name)
        if found is None:
            raise _Refused(_bad_request(_UNKNOWN_DATASET.format(name=name)))
        return found


# --- what the Admin API writes itself ----------------------------------------------------


def _json(status: HTTPStatus, body: JsonValue) -> Response:
    """One JSON response of this port: compact text, UTF-8, keys in insertion order.

    ``dumps_compact`` is what every JSON answer of the Admin API goes through
    (design.md 7.7); the diff of task 23.3 is the one exception and uses ``dumps_pretty``.
    """
    return Response(
        content=encode_utf8(dumps_compact(body)),
        status_code=status,
        headers={_CONTENT_TYPE: _JSON_MEDIA_TYPE},
    )


def _error(status: HTTPStatus, code: AdminError, message: str) -> Response:
    """One error response: ``{"error": <short code>, "message": <Chinese>}`` (7.7).

    The same shape whatever went wrong, so a caller reads one kind of answer; ``message``
    names what was asked for and never a token, a request header or the text of an
    exception (design.md error handling).
    """
    return _json(status, {_ERROR_KEY: code, _MESSAGE_KEY: message})


def _bytes(status: HTTPStatus, payload: bytes, media_type: str) -> Response:
    """One response whose body is bytes this module did not build out of JSON values.

    The trace of requirement 19.7, which is the bytes of a file, the diff of
    requirement 19.8, which is serialized once and compared byte for byte with the file
    the stdio runtime writes (module docstring), and the zip of requirement 19.14.
    """
    return Response(content=payload, status_code=status, headers={_CONTENT_TYPE: media_type})


def _not_found(message: str) -> Response:
    """The 404 of requirement 19.15; no sandbox is changed by a request refused here."""
    return _error(HTTPStatus.NOT_FOUND, AdminError.NOT_FOUND, message)


def _bad_request(message: str) -> Response:
    """The 400 of requirements 19.17 and 19.20; nothing is created and nothing changed."""
    return _error(HTTPStatus.BAD_REQUEST, AdminError.BAD_REQUEST, message)


def _conflict(message: str) -> Response:
    """The 409 of requirement 19.16: the sandbox, its trace and its snapshots are as they were."""
    return _error(HTTPStatus.CONFLICT, AdminError.CONFLICT, message)


def _read_failed(message: str) -> Response:
    """The 500 of a trace or a diff that could not be read; nothing was changed."""
    return _error(HTTPStatus.INTERNAL_SERVER_ERROR, AdminError.READ_FAILED, message)


@contextmanager
def _not_frozen(sandbox: SandboxRuntime) -> Iterator[None]:
    """Turn the refusal of a frozen sandbox into the 409 of requirement 19.16.

    Around the three interfaces that change a sandbox: the Sandbox_Manager raises
    :class:`~erp_mock.gateway.sandbox.SandboxFrozen` before it runs any job of a frozen
    sandbox, so nothing of the sandbox was touched when this answers. The message is this
    module's own rather than the exception's (design.md error handling).
    """
    try:
        yield
    except SandboxFrozen as error:
        raise _Refused(_conflict(_FROZEN_SANDBOX.format(sandbox_id=sandbox.sandbox_id))) from error


def _snapshot_name(name: str) -> str:
    """``name`` as a Named_Snapshot name, or raise a 400 (requirement 19.20).

    The two reasons a name is refused, each with its own message: a name that does not
    match :data:`~erp_mock.gateway.sandbox.NAMED_NAME_PATTERN` and the reserved
    ``initial``, which names the basis of a diff and no snapshot of a sandbox. The
    Sandbox_Manager checks the same two things before it touches the file system, this
    being what keeps a name that never passed a check out of a file name; the check is
    repeated here so that the caller is told which of the two it was.
    """
    if NAMED_NAME_PATTERN.match(name) is None:
        raise _Refused(_bad_request(_BAD_NAME.format(name=name)))
    if name == INITIAL_BASE:
        raise _Refused(_bad_request(_RESERVED_NAME))
    return name


def _basis(sandbox: SandboxRuntime, base: str) -> Path:
    """The database a diff of ``sandbox`` compares against (requirement 19.8).

    ``initial`` is ``base.sqlite``, the Snapshot the sandbox was created from, and every
    other value a Named_Snapshot of this sandbox, looked up among the ones it really has:
    a value that is not one of them is a 404 (requirement 19.15), and no path is built out
    of a value that was never a name.
    """
    if base == INITIAL_BASE:
        return sandbox.base_db
    if base not in sandbox.named_names():
        raise _Refused(_not_found(_NAME_MISSING.format(sandbox_id=sandbox.sandbox_id, name=base)))
    return sandbox.named_db(base)


def _read_trace(sandbox: SandboxRuntime) -> bytes:
    """The whole events of the trace of ``sandbox`` (requirement 19.7, worker thread).

    The file up to its last newline: every event written before this read, and no part of
    a line that was being written when it ran. An empty file - a prebuilt sandbox nobody
    has called - and a file whose first line is still being written both give no bytes at
    all.

    Raises :class:`_Refused` with 500 where the file cannot be read, which is a sandbox
    directory that has lost its trace: the reason goes to the service log and not into the
    answer (design.md error handling).
    """
    path = sandbox.trace.path
    try:
        raw = path.read_bytes()
    except OSError as error:
        _LOG.warning(_TRACE_FAILED, sandbox.sandbox_id, path, error)
        raise _Refused(_read_failed(_NO_TRACE.format(sandbox_id=sandbox.sandbox_id))) from error
    end = raw.rfind(b"\n")
    return b"" if end < 0 else raw[: end + 1]


def _store(archive: zipfile.ZipFile, sandbox_id: str, filename: str, payload: bytes) -> None:
    """Write one entry of an export, deterministically (requirement 19.14).

    ``<sandbox_id>/<filename>`` with the fixed timestamp, permissions, host system and
    compression level of :data:`_ZIP_TIMESTAMP` and its neighbours: the zip holds a
    timestamp and a host per entry, and taking either from the machine would make two
    exports of one unchanged sandbox differ in bytes.
    """
    info = zipfile.ZipInfo(f"{sandbox_id}/{filename}", date_time=_ZIP_TIMESTAMP)
    info.create_system = _ZIP_UNIX
    info.external_attr = _ZIP_MODE << _ZIP_MODE_SHIFT
    archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=_ZIP_LEVEL)


def _ref(name: str, version: str) -> str:
    """``<name>@<version>``, how requirement 19.9 and 19.12 write a configured thing."""
    return f"{name}@{version}"


def _refs(names: Sequence[str], versions: Mapping[str, str]) -> list[str]:
    """``<name>@<version>`` for each of ``names``, in that order (requirement 19.9).

    ``names`` are the profiles or the initial datasets as instance.yaml lists them, which
    is the order the answer carries, and ``versions`` the version of each of them.
    """
    return [_ref(name, versions[name]) for name in names]


def _sandbox_json(sandbox: SandboxRuntime) -> dict[str, JsonValue]:
    """The metadata object of ``sandbox``, its keys in the order of requirement 19.12.

    ``session_ids`` are the sessions that were ever bound to the sandbox, in first-binding
    order: a session that has ended is not taken off the list, the interface reporting the
    sessions that were bound rather than the ones that still are
    (:attr:`~erp_mock.gateway.sandbox.SandboxRuntime.session_ids`).
    """
    return {
        "sandbox_id": sandbox.sandbox_id,
        "session_ids": list(sandbox.session_ids),
        "profile": sandbox.profile_ref,
        "dataset": sandbox.dataset_ref,
        "anchor_date": sandbox.anchor_date.isoformat(),
        "status": sandbox.status,
        "created_at": sandbox.created_at,
    }


def _created(sandbox: SandboxRuntime) -> datetime:
    """The creation instant of ``sandbox``, parsed from its ``created_at``.

    The text the clock wrote carries a UTC offset (requirement 19.12), so two instants
    compare as instants however the texts were written.
    """
    return datetime.fromisoformat(sandbox.created_at)


def _instant(text: str | None) -> datetime | None:
    """``text`` as the instant it names, None for None; the model has checked it."""
    return None if text is None else datetime.fromisoformat(text)


def _within(sandbox: SandboxRuntime, start: datetime | None, end: datetime | None) -> bool:
    """Whether ``sandbox`` was created inside ``[start, end]`` (requirement 19.11).

    Both ends are inclusive and a bound that was not given limits nothing.
    """
    created = _created(sandbox)
    return not (start is not None and created < start) and not (end is not None and created > end)


def _read_aliases(dataset: DatasetModel) -> dict[str, JsonValue]:
    """The object ``aliases.json`` of ``dataset`` holds (requirement 19.10, worker thread).

    Read from the cache entry loading step 8 took up for the dataset and parsed, so the
    answer carries the keys of the file in the order the file wrote them, which is the
    ``dataset`` and then the ``aliases`` sorted by code point (requirement 20.10). Raises
    :class:`_Refused` with a 500 where the entry or the file is not there, or the file is
    not a JSON object: the service has lost a file of its data directory, which the log
    records with the reason and the answer does not (design.md error handling).
    """
    directory = dataset.snapshot
    path = None if directory is None else directory / ALIASES_FILENAME
    try:
        if path is None:
            raise FileNotFoundError(ALIASES_FILENAME)
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise _Refused(_aliases_unreadable(dataset, path, error)) from error
    if not isinstance(payload, dict):
        raise _Refused(_aliases_unreadable(dataset, path, TypeError(type(payload).__name__)))
    # The file was written from JSON values by :func:`~erp_mock.common.jsonio.dumps_pretty`,
    # so what json.loads gives back is a JSON tree and its keys are str.
    return dict(payload)


def _aliases_unreadable(dataset: DatasetModel, path: Path | None, error: Exception) -> Response:
    """The 500 of an alias map that could not be read, logged with its reason."""
    _LOG.warning(_ALIASES_FAILED, dataset.name, path, error)
    return _error(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        AdminError.ALIASES_UNREADABLE,
        _NO_ALIASES.format(name=dataset.name),
    )


# --- what a request carries, parsed (requirements 19.17 and 19.20) -----------------------


async def _parse_body[M: _Body](request: Request, model: type[M]) -> M:
    """The body of ``request`` as ``model``, or raise a 400 (requirement 19.17).

    An empty body is the empty object, which is what makes every field of
    :class:`SandboxCreate` optional and an interface without parameters callable without a
    body. A body that is not JSON, that is JSON but not an object, or that the model does
    not accept - an undefined key, a missing key, a value of the wrong type - is a 400 that
    says which of the three it was; nothing is created and no sandbox is changed.

    The body is read through ``Request.body()``, which the BodyLimit middleware of
    task 23.1 has already buffered, so the size of what arrives here has been checked
    (requirement 23.7).
    """
    raw = await request.body()
    payload: object = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise _Refused(_bad_request(_BAD_JSON)) from error
    if not isinstance(payload, dict):
        raise _Refused(_bad_request(_NOT_OBJECT))
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise _Refused(_bad_request(_BAD_BODY.format(details=_details(error)))) from error


def _parse_query[M: _Body](request: Request, model: type[M]) -> M:
    """The query parameters of ``request`` as ``model``, or raise a 400 (req 19.17).

    Every value of a query string is text, which is what the models of this module declare;
    a parameter the model does not define is an undefined key and therefore a 400, rather
    than a filter that was asked for and silently dropped (module docstring). A parameter
    that was given twice is read as its last value.
    """
    try:
        return model.model_validate(dict(request.query_params))
    except ValidationError as error:
        raise _Refused(_bad_request(_BAD_QUERY.format(details=_details(error)))) from error


def _details(error: ValidationError) -> str:
    """What was wrong with a request, as the message of a 400 names it.

    One item per line error of the model, in the order pydantic reports them: the key it is
    about and the code of what is wrong with it (``extra_forbidden``, ``missing``,
    ``string_type``, ``date_invalid``, ``offset_time``). The value that was sent is left
    out: a message of this service names what was asked for and not what it was asked with
    (design.md error handling).
    """
    return _DETAIL_SEPARATOR.join(
        _DETAIL.format(where=_where(line["loc"]), kind=line["type"]) for line in error.errors()
    )


def _where(loc: Iterable[str | int]) -> str:
    """The location of one line error as a path of keys, ``-`` for the whole document."""
    return _LOC_SEPARATOR.join(str(part) for part in loc) or _DETAIL_ROOT
