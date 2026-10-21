"""One tools/call from the resource limits to the result (design.md 5.3).

:class:`ToolLayer` is what the gateway performs a tools/call through: it takes the tool the
request named, the arguments it carried and the sandbox it runs against, and gives back the
:class:`ToolOutcome` the response and the trace event are built from::

    layer = ToolLayer(loaded, engine)
    outcome = layer.call(store, ctx, spec, raw_args)
    outcome.is_error, outcome.structured, outcome.text, outcome.writes, outcome.coerced

The steps and where the transaction begins (design.md 5.3, requirement 12.1):

======  ==================================================  ==========  ==================
step    what runs                                           where       transaction
======  ==================================================  ==========  ==================
1       the resource limits: the calls of the sandbox, the   here        no
        size of its database, the call rate of the session
        (:func:`exceeded_limit`), then ``mark_passed``
2, 3    the inputSchema and the normalization of the         validate    no
        arguments (:func:`~erp_mock.tools.validate.validate_args`)
4 - 13  the ten slots of the engine pipeline and the         domain      one, from
        effective call of what they wrote                                BEGIN IMMEDIATE
        (:meth:`~erp_mock.domain.engine.Engine.run`)                     to COMMIT
14      the structuredContent, the text and the truncation   results     no, the view is
        (:func:`~erp_mock.tools.results.result_payload`)                 already built
======  ==================================================  ==========  ==================

Steps 1 to 3 run outside the transaction, so a call that ends in one of them never opened
one; steps 4 to 13 are one transaction of the engine, so an error in any of them rolls back
the records, the counters and the operation sequence number together and leaves the
database exactly as the call found it (requirement 12.7). Step 1 counts the call as soon as
it passes, in the sandbox state outside the database, which is why a later rollback does
not give the call back (requirement 23.12, design.md issue 4).

What a failure becomes (requirements 12.8 and 13.10). An :class:`~erp_mock.common.errors.ErpError`
- every business error of the engine and every error of input validation - becomes the
error result of :func:`~erp_mock.tools.results.error_payload`. Any other exception is a
defect of the framework: the transaction of the engine has already rolled it back, the
stack goes to the log, which the CLI points at stderr, and the client gets the preset
INTERNAL_ERROR of :func:`~erp_mock.tools.results.internal_error`, which names no exception,
no stack and no statement. Either way the service answers and goes on to the next request.

How a tool becomes an operation of the engine (:func:`operation`, requirement 11.1):

=============  ===================================  ========================================
``spec.kind``  ``spec.action``                      operation
=============  ===================================  ========================================
entity_op      create                               :class:`~erp_mock.domain.engine.CreateOp`
entity_op      update                               :class:`~erp_mock.domain.engine.UpdateOp`
entity_op      get                                  :class:`~erp_mock.domain.engine.GetOp`
entity_op      delete                               :class:`~erp_mock.domain.engine.DeleteOp`
entity_op      search                               :class:`~erp_mock.domain.engine.SearchOp`
entity_op      submit, unsubmit, approve,            :class:`~erp_mock.domain.engine.TransitionOp`
               unapprove, close, unclose
push           create_from                          :class:`~erp_mock.domain.engine.PushOp`
query          whatever the declared name ends with  :class:`~erp_mock.domain.engine.QueryOp`
builtin        get                                  :class:`~erp_mock.domain.engine.BusinessContextOp`
=============  ===================================  ========================================

The push carries the short name of its source bill into the operation
(:func:`source_short_name`): the engine builds the field path of the source parameter from
it, so a NOT_FOUND of the source bill names the parameter the caller actually sent
(requirement 11.13). The short name is read off the tool's own parameters rather than from
the pack's ``source_short_names``, which is an item of one document of one pack and no
property of a resolved object: the first parameter of a push tool is ``<short name>_id``
(:func:`~erp_mock.tools.input_schema.push_input`), so the tool carries the name a rename
cannot change.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final, Protocol, assert_never

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.model import DomainModel
from erp_mock.domain.engine import (
    Arguments,
    BusinessContextOp,
    CreateOp,
    DeleteOp,
    Engine,
    GetOp,
    Operation,
    OperationContext,
    PushOp,
    QueryOp,
    SearchOp,
    StateAction,
    TransitionOp,
    UpdateOp,
)
from erp_mock.loader.instance import LoadedInstance
from erp_mock.state.store import SandboxStore, Write
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import ID_PARAM
from erp_mock.tools.results import (
    CODE,
    RULE_ID,
    ResultPayload,
    error_payload,
    internal_error,
    result_payload,
)
from erp_mock.tools.validate import ValidatedArgs, validate_args

__all__ = [
    "CALLS_PER_SECOND",
    "DB_BYTES",
    "DEFAULT_LIMITS",
    "LIMITS",
    "LIMIT_KEY",
    "MAX_CALLS_PER_SECOND",
    "MAX_DB_BYTES",
    "MAX_KEY",
    "MAX_SANDBOX_CALLS",
    "SANDBOX_CALLS",
    "STATE_ACTIONS",
    "CallContext",
    "LimitProbe",
    "ResourceLimits",
    "ToolLayer",
    "ToolOutcome",
    "exceeded_limit",
    "limit_error",
    "operation",
    "source_short_name",
]

# Where the stack of an unexpected exception goes (requirement 12.8). The handlers are the
# CLI's, and they point at stderr alone: stdout carries MCP messages and nothing else
# (design.md 7.3).
_LOG: Final = logging.getLogger(__name__)

# The three resource limits of requirement 23.12, in the order they are checked, which is
# also the ``limit`` of the one detail item of a LIMIT_EXCEEDED (requirement 23.8).
SANDBOX_CALLS: Final = "sandbox_calls"
DB_BYTES: Final = "db_bytes"
CALLS_PER_SECOND: Final = "calls_per_second"
LIMITS: Final[tuple[str, ...]] = (SANDBOX_CALLS, DB_BYTES, CALLS_PER_SECOND)

# The two keys of that detail item, in order (design.md data model 6): which limit was
# reached and the integer value of it.
LIMIT_KEY: Final = "limit"
MAX_KEY: Final = "max"

# The default of each limit (requirement 23.9): the value that applies when the environment
# variable of the limit is unset or empty. The CLI reads the variables once at startup and
# hands the values over as a :class:`ResourceLimits`.
MAX_SANDBOX_CALLS: Final = 500
MAX_DB_BYTES: Final = 67108864
MAX_CALLS_PER_SECOND: Final = 20

# How each limit reads in the message of the error, in simplified Chinese and naming the
# limit it is about (requirement 13.7). The detail item carries the limit and the maximum,
# so the message says what the caller can do about it.
_LIMIT_MESSAGES: Final[Mapping[str, str]] = {
    SANDBOX_CALLS: "本沙箱的调用次数已达上限 {max} 次，请重置沙箱或改用新的沙箱",
    DB_BYTES: "本沙箱的数据库大小已超过上限 {max} 字节，请重置沙箱",
    CALLS_PER_SECOND: "本会话的调用速率已达上限每秒 {max} 次，请放慢调用后重试",
}

# The state operations a bill tool performs, by the action of its name
# (:data:`~erp_mock.domain.state_machine.StateAction`, requirement 7.2). The map is what
# turns the action of a generated tool, which is a plain string, into the action of a
# :class:`~erp_mock.domain.engine.TransitionOp`.
STATE_ACTIONS: Final[Mapping[str, StateAction]] = FrozenMap(
    {
        "submit": "submit",
        "unsubmit": "unsubmit",
        "approve": "approve",
        "unapprove": "unapprove",
        "close": "close",
        "unclose": "unclose",
    }
)

# The suffix of the parameter that names a record: the ``<short name>_id`` of a push tool
# (:func:`~erp_mock.tools.input_schema.push_input`, requirement 11.13).
_ID_SUFFIX: Final = f"_{ID_PARAM}"


# --- the context of one call (design.md 5.3) ---------------------------------------------


class LimitProbe(Protocol):
    """What the tools layer asks the sandbox about the resource limits (req 23.12).

    Implemented by the sandbox runtime of the gateway (design.md 7.5), which keeps the two
    counts outside the sandbox database, so a call that passed the check stays counted
    however it ends (design.md issue 4):

    - :meth:`sandbox_calls`: how many tools/call of this sandbox have passed this check -
      0 for a new sandbox and after a reset, and the value of the moment a Named_Snapshot
      was taken after a restore of it;
    - :meth:`db_bytes`: the size of the sandbox database as the call starts, its page count
      times its page size (:meth:`~erp_mock.state.store.SandboxStore.db_bytes`);
    - :meth:`recent_session_calls`: how many tools/call of this session passed this check
      within the second before this request arrived, by the arrival times the gateway
      recorded;
    - :meth:`mark_passed`: this call passed the check, which raises the first count and
      records the arrival time of this one.

    Nothing of it is read before the check or after it, so the probe is asked at most once
    per limit and ``mark_passed`` is called exactly once, by the call that passed.
    """

    def sandbox_calls(self) -> int:
        """How many tools/call of this sandbox have passed the check (requirement 23.12)."""
        ...

    def db_bytes(self) -> int:
        """The size of the sandbox database as this call starts (requirement 23.12)."""
        ...

    def recent_session_calls(self) -> int:
        """The passed calls of this session in the second before arrival (req 23.12)."""
        ...

    def mark_passed(self) -> None:
        """Record that this call passed the check (requirement 23.12)."""
        ...


@dataclass(frozen=True, slots=True)
class CallContext:
    """What one tools/call brings with it besides its arguments (design.md 5.3).

    - ``session_id``: the session the call arrived on, which the limits of a session are
      counted per and the trace event names;
    - ``anchor_date``: the anchor date of the sandbox, which the business date and every
      ``today`` default come from (requirement 16.2);
    - ``current_user_id``: the id of the current user, None where the instance configures
      none (requirement 1.8);
    - ``call_seq``: the trace sequence number of the tools_call event of this call, under
      which the records it writes are recorded as one effective call
      (requirement 18.10);
    - ``limits``: the probe of the resource limits of this sandbox and session
      (:class:`LimitProbe`).
    """

    session_id: str
    anchor_date: date
    current_user_id: str | None
    call_seq: int
    limits: LimitProbe

    @property
    def operation_context(self) -> OperationContext:
        """What the engine needs of this call (design.md 4.1)."""
        return OperationContext(
            anchor_date=self.anchor_date,
            current_user_id=self.current_user_id,
            call_seq=self.call_seq,
        )


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """The upper bound of each resource limit, as the process was started with (23.9).

    One per service process: the CLI reads ``ERP_MOCK_MAX_SANDBOX_CALLS``,
    ``ERP_MOCK_MAX_DB_BYTES`` and ``ERP_MOCK_MAX_CALLS_PER_SECOND`` once at startup and the
    values stay what they were for as long as it runs. The field names are the ``limit`` of
    the detail item of a LIMIT_EXCEEDED and the values its ``max`` (requirement 23.8).
    """

    sandbox_calls: int = MAX_SANDBOX_CALLS
    db_bytes: int = MAX_DB_BYTES
    calls_per_second: int = MAX_CALLS_PER_SECOND


# The bounds of a process whose environment configured none (requirement 23.9).
DEFAULT_LIMITS: Final = ResourceLimits()


# --- what one call produced (design.md 5.4) ----------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """The result of one tools/call, as the response and the trace event read it.

    - ``is_error``: whether the call ended with an error code, which is the ``isError`` of
      the tool result (requirement 13.6);
    - ``structured``: the structuredContent of a successful call, None for a failed one,
      which carries none at all;
    - ``text``: the one text item of ``content``, the compact JSON of ``structured`` or of
      ``{"error": <error>}`` (requirements 13.1 and 13.6);
    - ``error``: the value of the one ``error`` key of a failed call, None for a successful
      one;
    - ``writes``: the net change the call made to each (record, cause), empty for a read
      and for every call that ended with an error (requirements 17.4 and 17.5);
    - ``coerced``: the paths of the number parameters that arrived as decimal strings, in
      the order of the details and empty for a call that ended before input validation
      (requirements 12.5 and 17.11).
    """

    is_error: bool
    structured: dict[str, JsonValue] | None = None
    text: str = ""
    error: dict[str, JsonValue] | None = None
    writes: tuple[Write, ...] = ()
    coerced: tuple[str, ...] = ()

    @property
    def error_code(self) -> str | None:
        """The ``code`` of the error, None for a successful call (requirement 17.3)."""
        return _error_text(self.error, CODE)

    @property
    def rule_id(self) -> str | None:
        """The ``rule_id`` of the error, None where no rule decided it (requirement 17.3)."""
        return _error_text(self.error, RULE_ID)


def _error_text(error: Mapping[str, JsonValue] | None, key: str) -> str | None:
    """The string value of ``key`` of an error object, None when there is none."""
    found = None if error is None else error.get(key)
    return found if isinstance(found, str) else None


# --- the resource limits (requirements 23.8 and 23.12) -----------------------------------


def exceeded_limit(probe: LimitProbe, limits: ResourceLimits) -> ErpError | None:
    """The first limit of ``limits`` that ``probe`` reports over, None when none is.

    Step 1 of the pipeline (design.md 5.3). The three limits are checked in the order of
    requirement 23.12 - the calls of the sandbox, the size of its database, the call rate
    of the session - and the first one that is over ends the check, so the details of the
    error carry exactly one item (requirement 23.8) and no later limit is even asked about.

    Where each bound bites (requirement 23.12): the calls of a sandbox and the rate of a
    session are over as soon as they *reach* the bound, since the call being checked would
    be one more than the bound allows; the size of the database is over only above it,
    since a database exactly at the bound is still within it.
    """
    if probe.sandbox_calls() >= limits.sandbox_calls:
        return limit_error(SANDBOX_CALLS, limits.sandbox_calls)
    if probe.db_bytes() > limits.db_bytes:
        return limit_error(DB_BYTES, limits.db_bytes)
    if probe.recent_session_calls() >= limits.calls_per_second:
        return limit_error(CALLS_PER_SECOND, limits.calls_per_second)
    return None


def limit_error(limit: str, maximum: int) -> ErpError:
    """The LIMIT_EXCEEDED of the resource limit ``limit`` (requirement 23.8).

    The one detail item of design.md data model 6: ``limit`` is the name of the limit and
    ``max`` the integer value of its bound, and the item carries no ``field``, since no
    parameter of the call is at fault. ``rule_id`` is null, as it is for every error no
    rule decided (requirement 13.8).
    """
    item: dict[str, JsonValue] = {LIMIT_KEY: limit, MAX_KEY: maximum}
    message = _LIMIT_MESSAGES.get(limit, "本次调用超过资源上限 {max}").format(max=maximum)
    return ErpError(ErrorCode.LIMIT_EXCEEDED, message, details=(item,))


# --- the tool layer ----------------------------------------------------------------------


class ToolLayer:
    """Performs the tools/call pipeline of design.md 5.3 against the sandboxes of one instance.

    One instance per loaded instance configuration, shared by every sandbox and every
    session of it: the layer holds no state of a call, which lives on the
    :class:`CallContext` and the :class:`~erp_mock.tools.validate.ValidatedArgs` of that
    call, and reads the business configuration from ``loaded`` alone (requirement 1.10).

    ``engine`` is the engine of the same configuration, which the loader builds with the
    expression trees it parsed at step 5; None builds one from the domain model, which
    parses the same trees again (:class:`~erp_mock.domain.engine.Engine`). ``limits`` are
    the resource bounds of the process (:class:`ResourceLimits`).
    """

    __slots__ = ("_engine", "_limits", "_loaded")

    def __init__(
        self,
        loaded: LoadedInstance,
        engine: Engine | None = None,
        limits: ResourceLimits = DEFAULT_LIMITS,
    ) -> None:
        self._loaded = loaded
        self._engine = engine if engine is not None else Engine(loaded.domain)
        self._limits = limits

    @property
    def loaded(self) -> LoadedInstance:
        """The read-only configuration of the instance the layer serves (design.md 2.4)."""
        return self._loaded

    @property
    def domain(self) -> DomainModel:
        """The resolved configuration, which validation and result assembly read."""
        return self._loaded.domain

    @property
    def engine(self) -> Engine:
        """The engine the domain steps of a call run through (design.md 4.1)."""
        return self._engine

    @property
    def limits(self) -> ResourceLimits:
        """The resource bounds of the process (requirement 23.9)."""
        return self._limits

    def call(
        self,
        store: SandboxStore,
        ctx: CallContext,
        spec: ToolSpec,
        raw_args: Mapping[str, object] | None = None,
    ) -> ToolOutcome:
        """Perform one tools/call of ``spec`` against ``store`` (requirement 12.1).

        ``raw_args`` is the ``arguments`` of the request, parsed JSON and nothing else,
        None for a call that carried none. The steps of the module docstring run in order
        and the first one that fails ends the call, so no later step sees a call that is
        already wrong.

        Returns the outcome of the call, successful or not (:class:`ToolOutcome`); it
        raises nothing, so the gateway answers every call it accepted and the service goes
        on to the next request (requirement 12.8).
        """
        args = ValidatedArgs()
        try:
            # Step 1: the resource limits, before anything of the call is looked at. The
            # call is counted as soon as it passes, outside the database (req 23.12).
            over = exceeded_limit(ctx.limits, self._limits)
            if over is not None:
                return _failed(over)
            ctx.limits.mark_passed()
            # Steps 2 and 3: the inputSchema and the normalization, outside the transaction.
            args = validate_args(self.domain, spec, raw_args)
            if args.error is not None:
                return _failed(args.error, args.coerced)
            # Steps 4 to 13: the slots of the engine pipeline, in one transaction.
            result = self._engine.run(store, ctx.operation_context, operation(spec, args))
            # Step 14: the result, assembled from the view the transaction built.
            payload = result_payload(self.domain, spec, result.view)
            return _outcome(payload, writes=result.writes, coerced=args.coerced)
        except ErpError as error:
            # Every business error of the engine and of input validation; the transaction
            # of the engine rolled this call's writes back (requirement 12.7).
            return _failed(error, args.coerced)
        except Exception:
            # A defect of the framework, not an error of the call: the transaction rolled
            # back, the stack goes to the log and the client gets the preset message
            # (requirements 12.8 and 13.10).
            _LOG.exception(
                "tools/call %s 以 INTERNAL_ERROR 结束（会话 %s，调用 %d）",
                spec.name,
                ctx.session_id,
                ctx.call_seq,
            )
            return _failed(internal_error(), args.coerced)


def _outcome(
    payload: ResultPayload, writes: tuple[Write, ...] = (), coerced: tuple[str, ...] = ()
) -> ToolOutcome:
    """``payload`` as the outcome of the call that produced it (design.md 5.4)."""
    return ToolOutcome(
        is_error=payload.is_error,
        structured=payload.structured,
        text=payload.text,
        error=payload.error,
        writes=writes,
        coerced=coerced,
    )


def _failed(error: ErpError, coerced: tuple[str, ...] = ()) -> ToolOutcome:
    """The outcome of a call that ended with ``error`` (requirements 13.6 and 17.5).

    No structuredContent, and no writes: a call that ends with an error code changed
    nothing of the sandbox database, so its trace event reports an empty ``writes``. The
    coerced paths are those of the call, empty for one that ended before input validation
    (requirement 17.11).
    """
    return _outcome(error_payload(error), coerced=coerced)


# --- the operation of one tool (requirement 11.1) ----------------------------------------


def operation(spec: ToolSpec, args: ValidatedArgs) -> Operation:
    """The engine operation the tool ``spec`` performs with ``args`` (module docstring).

    ``args`` are the normalized arguments of the call, which input validation accepted, so
    every parameter the operation needs is there and is of the type the schema declares.

    Raises RuntimeError for a tool whose action is none of those requirement 11.1
    generates, which no generated tool has: the names of a valid instance were settled by
    loading step 6 (requirement 11.2).
    """
    match spec.kind:
        case "entity_op":
            return _entity_operation(spec, args)
        case "push":
            return _push_operation(spec, args)
        case "query":
            # The queries of the instance are keyed by the name of the tool that runs them
            # (requirement 11.1), which is the target of a query tool.
            return QueryOp(tool=spec.target, args=args.scalars)
        case "builtin":
            # The one built-in tool of M0 reads the business context (requirement 16.10).
            return BusinessContextOp()
        case _ as unknown:
            assert_never(unknown)


def _entity_operation(spec: ToolSpec, args: ValidatedArgs) -> Operation:
    """The operation of an entity or bill tool: the action of its name (requirement 11.1).

    The record a get, an update, a delete or a state operation works on is named by the
    ``id`` parameter (requirement 11.4), which is therefore not one of the values an update
    writes: ``id`` is no writable field of any level.
    """
    target = spec.target
    match spec.action:
        case "create":
            return CreateOp(target, args.scalars, args.lines)
        case "update":
            return UpdateOp(target, _record_id(spec, args), _written_values(args), args.lines)
        case "get":
            return GetOp(target, _record_id(spec, args))
        case "delete":
            return DeleteOp(target, _record_id(spec, args))
        case "search":
            # The keyword, the filters and the page, under the names requirement 11.5 gives
            # them; the engine reads what it knows and the schema allowed nothing else.
            return SearchOp(target, args.scalars)
        case _:
            state = STATE_ACTIONS.get(spec.action)
            if state is None:
                raise RuntimeError(f"工具 {spec.name} 的动作 {spec.action!r} 不是实体或单据的操作")
            return TransitionOp(target, _record_id(spec, args), state)


def _push_operation(spec: ToolSpec, args: ValidatedArgs) -> PushOp:
    """The push of a ``<target>_create_from_<short name>`` tool (requirements 8.1, 11.13).

    The source bill comes from the ``<short name>_id`` parameter and the lines from
    ``lines``, which a push tool always declares required (requirement 11.4). The short
    name itself is carried into the operation, so the engine builds the field path of the
    source parameter from the name the caller sent rather than from the name of the source
    bill (:func:`source_short_name`).
    """
    short = source_short_name(spec)
    lines = args.lines
    return PushOp(
        conversion=spec.target,
        source_id=_string_argument(spec, args, f"{short}{_ID_SUFFIX}"),
        lines=() if lines is None else lines,
        source_name=short,
    )


def source_short_name(spec: ToolSpec) -> str:
    """The short name the push tool ``spec`` names its source bill under (req 11.13).

    The first parameter of a push tool is ``<short name>_id``
    (:func:`~erp_mock.tools.input_schema.push_input`), so the short name is read off the
    tool itself: it survives a ``rename`` of the tool, and the tools layer needs no access
    to the ``source_short_names`` of the pack, which is an item of one document of one pack
    and no property of the resolved configuration.

    Raises RuntimeError for a tool whose first parameter is no such name, which no
    generated push tool has.
    """
    first = spec.param_paths[0] if spec.param_paths else ""
    if not first.endswith(_ID_SUFFIX) or first == _ID_SUFFIX:
        raise RuntimeError(f"下推工具 {spec.name} 的第一个参数 {first!r} 不是来源单据参数")
    return first.removesuffix(_ID_SUFFIX)


def _written_values(args: ValidatedArgs) -> Arguments:
    """The arguments an update writes: every scalar but the ``id`` of the record (5.2)."""
    return FrozenMap((name, value) for name, value in args.scalars.items() if name != ID_PARAM)


def _record_id(spec: ToolSpec, args: ValidatedArgs) -> str:
    """The ``id`` of the record the tool works on (requirement 11.4)."""
    return _string_argument(spec, args, ID_PARAM)


def _string_argument(spec: ToolSpec, args: ValidatedArgs, name: str) -> str:
    """The value of the required string parameter ``name`` of ``spec``.

    Raises RuntimeError when the parameter is missing or is of another type, which input
    validation has already ruled out: every tool that names a record declares the
    parameter required and of type string (requirement 11.4).
    """
    value = args.values.get(name)
    if not isinstance(value, str):
        raise RuntimeError(f"工具 {spec.name} 的参数 {name} 没有给出记录 ID")
    return value
