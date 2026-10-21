"""erp_mock.tools.layer: one tools/call from the resource limits to the result (req 12.1).

The pipeline is exercised end to end against a real sandbox: the tools of the engine test
fixture, loaded through every validation step, are called through
:meth:`~erp_mock.tools.layer.ToolLayer.call` on a sandbox over the standard basic data
(:mod:`tests.helpers.engine_env`), so what is checked here is the pipeline of design.md 5.3
as a client reaches it. Four parts:

- the resource limits: the three of requirement 23.12 are asked about in order, the first
  one over its bound ends the call and is the only item of the details, and a call that
  passes the check is counted before anything else of it is looked at (requirement 23.8);
- the transaction boundary: input validation runs outside the transaction, a business error
  and an injected defect both leave the sandbox database exactly as the call found it, and
  a defect becomes the preset INTERNAL_ERROR (requirements 12.7, 12.8 and 12.10);
- the dispatch: each kind of tool becomes the operation of the engine requirement 11.1
  gives it, a push names its source bill by the short name of the tool and an update writes
  no ``id``;
- the outcome: ``writes`` and ``coerced`` of a successful call, and a read-only tool that
  takes no operation sequence number and writes nothing.

The limits are probed through a double (:class:`FakeProbe`), which records what was asked
in which order: the real probe is the sandbox runtime of the gateway, whose counts live
outside the database (design.md 7.5). Every call of one test runs under its own trace
sequence number (:class:`Caller`), the way the gateway numbers the events of a session
(requirement 18.10).
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest

from erp_mock.common.errors import ErrorCode
from erp_mock.domain.engine import Engine, Operation, OperationContext, OpResult
from erp_mock.domain.numbering import OP_COUNTER_KEY
from erp_mock.loader.instance import LoadedInstance
from erp_mock.state.store import Record, SandboxStore
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET, ToolSpec
from erp_mock.tools.layer import (
    CALLS_PER_SECOND,
    DB_BYTES,
    LIMIT_KEY,
    LIMITS,
    MAX_KEY,
    SANDBOX_CALLS,
    CallContext,
    ResourceLimits,
    ToolLayer,
    ToolOutcome,
)
from erp_mock.tools.results import INTERNAL_MESSAGE
from tests.helpers.engine_env import EngineEnv, engine_env
from tests.helpers.fixtures import fixture_loaded

# The session the calls of a test arrive on; nothing of the pipeline depends on the value.
SESSION: Final = "se_0123456789abcdef"

# The tools the tests call (tests/fixtures/packs/fx_trade). The push tool is named after the
# short name ``order`` the pack gives the supply order, and so is its source parameter
# (requirement 11.13).
ORDER: Final = "supply_order"
ORDER_LINE: Final = "supply_order_line"
ENTRY_FROM_ORDER: Final = "goods_entry_create_from_order"
SOURCE_PARAM: Final = "order_id"
COUNT: Final = "count_sheet"
PRICE_QUERY: Final = "supply_price_query"
BALANCE_QUERY: Final = "stock_level_query"

# The ids the basic data of the environment gives the records a call names, which are fixed
# by the order they are created in (:data:`~tests.helpers.engine_env.BASIC_DATA`).
VENDOR: Final = "ven_000001"
WASHER: Final = "art_000002"
DEPOT: Final = "dep_000001"

# A valid call of ``supply_order_create``: the two required header fields and one line, the
# quantity and the price written as decimal strings, which the normalization records as
# coerced (requirements 12.5 and 17.11).
NEW_ORDER: Final[Mapping[str, Any]] = {
    "vendor_id": VENDOR,
    "need_date": "2026-10-20",
    "lines": [{"article_id": WASHER, "qty": "10", "price": "0.85"}],
}

# A valid call of ``count_sheet_create``, the bill a test creates and deletes again.
NEW_COUNT: Final[Mapping[str, Any]] = {
    "depot_id": DEPOT,
    "count_kind": "gain",
    "lines": [{"article_id": WASHER, "diff_qty": 3}],
}


# --- the doubles of one call (design.md 5.3) ---------------------------------------------


@dataclass(slots=True)
class FakeProbe:
    """A :class:`~erp_mock.tools.layer.LimitProbe` that reports what a test set up.

    ``calls``, ``size`` and ``recent`` are what the three methods return; ``probed`` records
    which limits were asked about, in order, so a test sees both the order of the checks
    and that the check stopped at the first limit that was over (requirement 23.12).
    ``passed`` counts the calls that reached ``mark_passed``.
    """

    calls: int = 0
    size: int = 0
    recent: int = 0
    probed: list[str] = field(default_factory=list)
    passed: int = 0

    def sandbox_calls(self) -> int:
        self.probed.append(SANDBOX_CALLS)
        return self.calls

    def db_bytes(self) -> int:
        self.probed.append(DB_BYTES)
        return self.size

    def recent_session_calls(self) -> int:
        self.probed.append(CALLS_PER_SECOND)
        return self.recent

    def mark_passed(self) -> None:
        self.passed += 1


class FailingEngine(Engine):
    """An engine whose pipeline writes and then fails with an exception of its own.

    The injected defect of requirement 12.8: a record and a counter are written inside the
    transaction of the operation and a RuntimeError follows, so a test sees what the tools
    layer does with an exception that is no :class:`~erp_mock.common.errors.ErpError` - the
    transaction rolls back and the call ends with INTERNAL_ERROR.
    """

    def run(self, store: SandboxStore, ctx: OperationContext, op: Operation) -> OpResult:
        with store.transaction() as uow:
            uow.bump_counter("op", OP_COUNTER_KEY)
            uow.insert("vendor", {"id": "ven_999999", "code": "V9999", "name": "注入的供货单位"})
            raise RuntimeError("注入的流水线故障")


@dataclass(slots=True)
class Caller:
    """Performs tools/call against one sandbox, each call under its own trace seq.

    ``probe`` is the limit probe of every call it makes, so a test reads off it what the
    whole sequence asked about and how many calls were counted.
    """

    layer: ToolLayer
    env: EngineEnv
    specs: Mapping[str, ToolSpec]
    probe: FakeProbe = field(default_factory=FakeProbe)
    seq: int = 0

    def context(self) -> CallContext:
        """The context of the next call: the sandbox's anchor date and user, a fresh seq."""
        self.seq += 1
        return CallContext(
            session_id=SESSION,
            anchor_date=self.env.anchor_date,
            current_user_id=self.env.current_user_id,
            call_seq=self.seq,
            limits=self.probe,
        )

    def call(self, tool: str, raw: Mapping[str, Any] | None = None) -> ToolOutcome:
        """Perform one tools/call of ``tool`` with the arguments ``raw``."""
        return self.layer.call(self.env.store, self.context(), self.specs[tool], raw)

    def ok(self, tool: str, raw: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """The structuredContent of a call of ``tool`` that went through."""
        return succeeded(self.call(tool, raw))

    def id_of(self, tool: str, raw: Mapping[str, Any] | None = None) -> str:
        """The internal id of the record a write of ``tool`` returned (requirement 5.16)."""
        return record_id(self.ok(tool, raw))


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def loaded() -> LoadedInstance:
    """The read-only configuration of the fixture, what the tools layer is built on."""
    return fixture_loaded()


@pytest.fixture(scope="module")
def layer(loaded: LoadedInstance) -> ToolLayer:
    """The tools layer of the fixture's instance, with the bounds of a plain process."""
    return ToolLayer(loaded)


@pytest.fixture(scope="module")
def specs(loaded: LoadedInstance) -> Mapping[str, ToolSpec]:
    """Every tool the default profile exposes, by the name tools/list returns (11.9)."""
    return {spec.name: spec for spec in loaded.default_profile.tools}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[EngineEnv]:
    """An open sandbox with the standard basic data (tests.helpers.engine_env)."""
    with engine_env(tmp_path / "sandbox") as environment:
        yield environment


@pytest.fixture
def caller(layer: ToolLayer, env: EngineEnv, specs: Mapping[str, ToolSpec]) -> Caller:
    """A client of the sandbox of this test, calling through the tools layer."""
    return Caller(layer, env, specs)


# --- reading an outcome ------------------------------------------------------------------


def succeeded(outcome: ToolOutcome) -> Mapping[str, Any]:
    """The structuredContent of a call that went through (requirement 13.1)."""
    assert not outcome.is_error, f"unexpected error: {outcome.error}"
    assert outcome.error is None
    assert outcome.error_code is None
    structured = outcome.structured
    assert structured is not None
    return structured


def failed(outcome: ToolOutcome, code: ErrorCode) -> Mapping[str, Any]:
    """The error object of a call that ended with ``code`` (requirement 13.6)."""
    assert outcome.is_error
    assert outcome.structured is None, "an error result carries no structuredContent"
    assert outcome.error_code == code.value, outcome.error
    assert outcome.writes == (), "a failed call reports no writes"
    error = outcome.error
    assert error is not None
    return error


def details(outcome: ToolOutcome) -> Sequence[Mapping[str, Any]]:
    """The detail items of a failed call, in report order (requirement 13.6)."""
    error = outcome.error
    assert error is not None
    items = error["details"]
    assert isinstance(items, list)
    return [item for item in items if isinstance(item, dict)]


def fields(outcome: ToolOutcome) -> list[str]:
    """The field path of each detail item of a failed call (requirement 12.12)."""
    return [str(item["field"]) for item in details(outcome)]


def record_id(structured: Mapping[str, Any]) -> str:
    """The internal id of the record a write returned (requirement 5.16)."""
    found = structured["id"]
    assert isinstance(found, str)
    return found


def database(env: EngineEnv) -> dict[str, list[Record]]:
    """Every table of the sandbox with its records, the internal tables included (12.7)."""
    with env.store.transaction() as uow:
        return {name: uow.select(name) for name in env.schema.tables}


def operation_seq(env: EngineEnv) -> int:
    """The operation sequence numbers the sandbox has handed out so far (req 4.11)."""
    return env.counters().get(("op", OP_COUNTER_KEY), 0)


# --- the resource limits (requirements 23.8 and 23.12) -----------------------------------


@pytest.mark.req("12.1", "23.12")
def test_the_three_limits_are_asked_about_in_order_before_the_call_runs(caller: Caller) -> None:
    outcome = caller.call(f"{ORDER}_create", NEW_ORDER)

    # The order of requirement 23.12, which is the order the module lists the limits in.
    assert caller.probe.probed == [SANDBOX_CALLS, DB_BYTES, CALLS_PER_SECOND]
    assert caller.probe.probed == list(LIMITS)
    # Counted once, after the whole check passed and before the call was looked at.
    assert caller.probe.passed == 1
    assert not outcome.is_error


@pytest.mark.req("23.8", "23.12")
@pytest.mark.parametrize(
    ("probe", "limit", "maximum", "asked"),
    [
        pytest.param(
            FakeProbe(calls=500, size=10**9, recent=20),
            SANDBOX_CALLS,
            500,
            [SANDBOX_CALLS],
            id="all-three-over",
        ),
        pytest.param(
            FakeProbe(calls=499, size=67108865, recent=20),
            DB_BYTES,
            67108864,
            [SANDBOX_CALLS, DB_BYTES],
            id="size-and-rate-over",
        ),
        pytest.param(
            FakeProbe(calls=499, size=67108864, recent=21),
            CALLS_PER_SECOND,
            20,
            [SANDBOX_CALLS, DB_BYTES, CALLS_PER_SECOND],
            id="rate-over",
        ),
    ],
)
def test_only_the_first_limit_that_is_over_is_reported(
    layer: ToolLayer,
    env: EngineEnv,
    specs: Mapping[str, ToolSpec],
    probe: FakeProbe,
    limit: str,
    maximum: int,
    asked: list[str],
) -> None:
    caller = Caller(layer, env, specs, probe)
    before = database(env)

    outcome = caller.call(f"{ORDER}_create", NEW_ORDER)

    error = failed(outcome, ErrorCode.LIMIT_EXCEEDED)
    # The details carry exactly one item: the first limit that was over, with its bound.
    assert details(outcome) == [{LIMIT_KEY: limit, MAX_KEY: maximum}]
    assert error["rule_id"] is None
    assert outcome.rule_id is None
    # The check stopped at that limit, so no later one was even asked about.
    assert probe.probed == asked
    # Nothing of the call ran: it was not counted, nothing was normalized, nothing written.
    assert probe.passed == 0
    assert outcome.coerced == ()
    assert database(env) == before


@pytest.mark.req("23.12")
@pytest.mark.parametrize(
    ("probe", "accepted"),
    [
        pytest.param(FakeProbe(calls=2), True, id="calls-below"),
        pytest.param(FakeProbe(calls=3), False, id="calls-at-the-bound"),
        pytest.param(FakeProbe(size=4096), True, id="size-at-the-bound"),
        pytest.param(FakeProbe(size=4097), False, id="size-above-the-bound"),
        pytest.param(FakeProbe(recent=1), True, id="rate-below"),
        pytest.param(FakeProbe(recent=2), False, id="rate-at-the-bound"),
    ],
)
def test_the_calls_and_the_rate_are_over_at_the_bound_and_the_size_above_it(
    loaded: LoadedInstance,
    env: EngineEnv,
    specs: Mapping[str, ToolSpec],
    probe: FakeProbe,
    accepted: bool,
) -> None:
    # Bounds small enough to reach: 3 calls per sandbox, 4096 bytes, 2 calls per second.
    narrow = ResourceLimits(sandbox_calls=3, db_bytes=4096, calls_per_second=2)
    caller = Caller(ToolLayer(loaded, limits=narrow), env, specs, probe)

    outcome = caller.call(BUSINESS_CONTEXT_GET)

    assert outcome.is_error is not accepted
    assert probe.passed == (1 if accepted else 0)


# --- the transaction boundary (requirements 12.7, 12.8 and 12.10) ------------------------


@pytest.mark.req("12.1", "12.7")
def test_input_validation_runs_outside_the_transaction(caller: Caller, env: EngineEnv) -> None:
    # A transaction of the sandbox is open while the call runs, and the store refuses to
    # nest one: the call goes through all the same, because the limits, the schema and the
    # normalization touch no database at all (design.md 5.3 steps 1 to 3).
    with env.store.transaction():
        outcome = caller.call(f"{ORDER}_create", {"vendor_id": 7})

    failed(outcome, ErrorCode.VALIDATION_FAILED)
    assert fields(outcome) == ["vendor_id", "need_date", "lines"]
    assert caller.probe.passed == 1


@pytest.mark.req("12.7")
def test_a_business_error_leaves_the_database_as_the_call_found_it(
    caller: Caller, env: EngineEnv
) -> None:
    order = caller.id_of(f"{ORDER}_create", NEW_ORDER)
    caller.ok(f"{ORDER}_submit", {"id": order})
    before = database(env)

    # R-GEN-001: a bill that is no draft is not changed (requirement 7.3).
    outcome = caller.call(f"{ORDER}_update", {"id": order, "remark": "改期"})

    error = failed(outcome, ErrorCode.INVALID_STATE)
    assert error["rule_id"] == "R-GEN-001"
    assert outcome.rule_id == "R-GEN-001"
    assert database(env) == before


@pytest.mark.req("12.8", "13.10")
def test_an_injected_defect_rolls_back_and_ends_with_the_preset_internal_error(
    loaded: LoadedInstance,
    env: EngineEnv,
    specs: Mapping[str, ToolSpec],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caller = Caller(ToolLayer(loaded, FailingEngine(loaded.domain)), env, specs)
    before = database(env)

    outcome = caller.call(f"{ORDER}_create", NEW_ORDER)

    error = failed(outcome, ErrorCode.INTERNAL_ERROR)
    # The preset message of the framework: no exception, no stack, no path, no statement,
    # and no detail item (requirement 13.10).
    assert error["message"] == INTERNAL_MESSAGE
    assert error["rule_id"] is None
    assert details(outcome) == []
    assert "注入的流水线故障" not in outcome.text
    # The record and the counter the injected step wrote are gone again, so the database
    # holds exactly what it did before the call (requirement 12.7).
    assert database(env) == before
    # The call passed the limit check, so it stays counted outside the database, and the
    # stack went to the log the CLI points at stderr (requirement 12.8).
    assert caller.probe.passed == 1
    assert "注入的流水线故障" in caplog.text


@pytest.mark.req("12.10", "11.13")
def test_a_push_names_its_source_bill_by_the_short_name_of_its_tool(caller: Caller) -> None:
    lines = [{"source_line_id": "so_999999-1", "entry_qty": 1, "depot_id": DEPOT}]

    outcome = caller.call(ENTRY_FROM_ORDER, {SOURCE_PARAM: "so_999999", "lines": lines})

    # The reference check comes first, before the ownership of the source line and before
    # the state of the source bill (requirement 12.10); the field path of the source bill
    # is the parameter the caller sent, not the name of the source bill (requirement 11.13).
    error = failed(outcome, ErrorCode.NOT_FOUND)
    assert fields(outcome) == [SOURCE_PARAM, "lines[0].source_line_id"]
    assert SOURCE_PARAM in str(error["message"])


# --- the dispatch (requirement 11.1) -----------------------------------------------------


@pytest.mark.req("12.1")
def test_every_kind_of_tool_becomes_the_operation_of_its_action(
    caller: Caller, env: EngineEnv
) -> None:
    # create, update and get of a bill: the update writes no id, which is no writable field.
    order = caller.id_of(f"{ORDER}_create", NEW_ORDER)
    updated = caller.ok(f"{ORDER}_update", {"id": order, "need_date": "2026-10-21"})
    assert updated["need_date"] == "2026-10-21"
    assert caller.ok(f"{ORDER}_get", {"id": order}) == updated

    # search: the page of requirement 5.11.
    page = caller.ok(f"{ORDER}_search", {"page": 1, "page_size": 10})
    assert [page["total"], page["page"], page["page_size"], page["has_more"]] == [1, 1, 10, False]

    # the state operations, in the order the standard machine allows them (req 7.2).
    assert caller.ok(f"{ORDER}_submit", {"id": order})["status"] == "submitted"
    assert caller.ok(f"{ORDER}_approve", {"id": order})["status"] == "approved"

    # the push: the draft of the target bill, pointing back at its source (req 8.1).
    lines = [{"source_line_id": f"{order}-1", "entry_qty": "4", "depot_id": DEPOT}]
    pushed = caller.ok(ENTRY_FROM_ORDER, {SOURCE_PARAM: order, "lines": lines})
    assert pushed["source_bill"] == {"id": order, "number": updated["number"]}

    # delete: the object the record had before it was removed (requirement 13.9).
    sheet = caller.id_of(f"{COUNT}_create", NEW_COUNT)
    assert record_id(caller.ok(f"{COUNT}_delete", {"id": sheet})) == sheet
    assert env.record(COUNT, sheet) is None

    # the two kinds of query tool and the built-in one (requirements 5.13, 9.7 and 16.10).
    priced = caller.ok(PRICE_QUERY, {"vendor_id": VENDOR, "article_id": WASHER})
    assert list(priced) == ["latest", "history"]
    assert list(caller.ok(BALANCE_QUERY)) == ["items", "total", "page", "page_size", "has_more"]
    assert list(caller.ok(BUSINESS_CONTEXT_GET)) == [
        "business_date",
        "current_period",
        "org",
        "user",
    ]


@pytest.mark.req("12.1", "12.5")
def test_a_successful_call_reports_its_writes_and_its_coerced_paths(caller: Caller) -> None:
    outcome = caller.call(f"{ORDER}_create", NEW_ORDER)

    order = record_id(succeeded(outcome))
    # The net change of each (record, cause): the header and its one line, both direct.
    assert [(write.entity, write.op, write.id, write.cause) for write in outcome.writes] == [
        (ORDER, "insert", order, "direct"),
        (ORDER_LINE, "insert", f"{order}-1", "direct"),
    ]
    # The two number parameters that arrived as decimal strings, in the order of the
    # details (requirements 12.5 and 12.12).
    assert outcome.coerced == ("lines[0].qty", "lines[0].price")
    assert outcome.text


@pytest.mark.req("12.1", "13.1")
def test_a_read_only_tool_takes_no_operation_sequence_number_and_writes_nothing(
    caller: Caller, env: EngineEnv
) -> None:
    order = caller.id_of(f"{ORDER}_create", NEW_ORDER)
    before = operation_seq(env)
    snapshot = database(env)

    reads: list[tuple[str, Mapping[str, Any] | None]] = [
        (f"{ORDER}_get", {"id": order}),
        (f"{ORDER}_search", {}),
        (PRICE_QUERY, {"vendor_id": VENDOR, "article_id": WASHER}),
        (BALANCE_QUERY, {}),
        (BUSINESS_CONTEXT_GET, None),
    ]
    for tool, raw in reads:
        outcome = caller.call(tool, raw)
        succeeded(outcome)
        assert outcome.writes == (), tool
        assert outcome.coerced == (), tool

    # The operation sequence numbers count the write operations alone (requirement 4.11).
    assert operation_seq(env) == before
    assert database(env) == snapshot
