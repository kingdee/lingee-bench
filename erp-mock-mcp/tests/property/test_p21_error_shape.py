"""Property 21: the shape of an error result.

**Validates: Requirements 13.6, 13.7, 13.8**

For any call of a random operation sequence (:mod:`tests.property.opseq`) that ends with an
error code, the result is the one object requirement 13.6 describes, read as a client reads
it - the one text item of the content, parsed:

- the text is a JSON object whose one key is ``error``, and there is no structuredContent
  at all; the error object carries ``code``, ``message``, ``rule_id`` and ``details`` in
  that order and carries all four whatever the error is, so a caller may read any of them
  without looking first (requirement 13.6);
- ``message`` is written in Chinese - it holds at least one Han character - and says what
  about the call did not work: where a detail item names an input field, the message
  carries that path or the Chinese label of what it names, and where a pack rule decided
  the error the message is that rule's ``message.zh`` word for word (requirement 13.7); no
  message names an exception class, a stack or a source file (requirement 13.10);
- ``rule_id`` is the rule that decided the error and null where none did: an error of
  BUSINESS_RULE_VIOLATION always names one, a NOT_FOUND, a CONFLICT, a LIMIT_EXCEEDED and
  an INTERNAL_ERROR never do, and every rule an error names is one of the seven engine
  rules or an enforced rule of the business pack (requirement 13.8);
- every detail item carries exactly the keys design.md data model 6 gives its ``reason``,
  in that order: ``field`` first where the item is about an input field, then ``reason``,
  then the extra keys of that reason and nothing else; an error result is not truncated, so
  no ``truncated`` appears anywhere in it (requirements 13.5 and 13.6).

The reference model is :data:`SHAPES`: the table of design.md data model 6, written out
here, one :class:`DetailShape` per reason. It says two things about every reason - which
keys an item of it carries and in which order, and which ``rule_id`` the error carrying it
must have - and it is written here rather than imported, so a reason that lost its
``field``, grew a key of another reason, reordered its keys or changed the rule it belongs
to is caught. Only the reason strings themselves and the rule ids are imported, as the
vocabulary the two sides share; which error codes must and must not carry a rule is
:data:`RULED_ALWAYS` and :data:`RULED_NEVER`, written out from requirement 13.8.

The Chinese label of a field path (:class:`Named`) is read off the field view of the
configuration: a path is resolved to the header field, the line field or the entity field
it names, and the ``label.zh`` of that field is what requirement 13.7 lets a message carry
instead of the path itself. One path names no field of the record: the ``<short name>_id``
of a push tool, which names the *source* bill (requirement 11.13), and whose label is that
bill's - which is how the R-GEN-004 of a push satisfies requirement 13.7, by naming the
source bill rather than the parameter.

What a run covers, each failed call reporting its code and rule as a Hypothesis event:
VALIDATION_FAILED, NOT_FOUND, INVALID_STATE and BUSINESS_RULE_VIOLATION, the engine rules
R-GEN-001, R-GEN-003 to R-GEN-007, the enforced rules of the test business pack, and the
INVALID_STATE of a state operation whose pre-state does not fit, whose rule is null
(requirement 7.4). Three codes the sequences cannot reach: LIMIT_EXCEEDED, which
:class:`~tests.property.opseq.session.BlankProbe` lets every call past, INTERNAL_ERROR,
which a passing suite has no cause for, and CONFLICT, which needs a unique field or a
referenced record the ten operations of a sequence do not write. Their rows of the model -
the ``limit`` and ``max`` item of requirement 23.8 among them - are part of the table all
the same, so the table reads as the whole of data model 6 and covers the codes the second
starting point of task 19.8 or a later tool brings.

The "exactly one text item" of requirement 13.6 is not read here: the tools layer returns
one text (:class:`~erp_mock.tools.layer.ToolOutcome`) and the gateway builds the content
array from it, which is where that half of the requirement is checked.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, event, given, note, settings
from hypothesis import strategies as st

from erp_mock.common.errors import ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact
from erp_mock.config.model import DomainModel, FieldModel
from erp_mock.domain.conversion import (
    EXCEEDS_AMOUNT_REASON,
    EXCEEDS_QTY_REASON,
    NOT_IN_SOURCE_REASON,
    QUOTA_RULE,
    SOURCE_STATUS_RULE,
)
from erp_mock.domain.crud import (
    DISABLED_REASON,
    DISABLED_RULE,
    DUPLICATE_REASON,
    LINES_NOT_EDITABLE_REASON,
    NOT_FOUND_REASON,
    REFERENCED_REASON,
)
from erp_mock.domain.inventory import NEGATIVE_STOCK_REASON, NEGATIVE_STOCK_RULE
from erp_mock.domain.rules import RULE_CHECK_FAILED_REASON
from erp_mock.domain.state_machine import (
    DRAFT_RULE,
    INVALID_STATE_REASON,
    REQUIRED_MISSING_REASON,
    REQUIRED_RULE,
)
from erp_mock.domain.unit_conversion import (
    INVALID_UNIT_REASON,
    MISSING_REASON,
    NOT_POSITIVE_REASON,
)
from erp_mock.domain.writeback import HAS_DOWNSTREAM_REASON, PUSHED_RULE
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.layer import LIMIT_KEY, MAX_KEY
from erp_mock.tools.results import CODE, DETAILS, ERROR_KEY, MESSAGE, RULE_ID
from erp_mock.tools.validate import REASONS
from tests.property.opseq import (
    MAX_OPS,
    MIN_OPS,
    Performed,
    Start,
    fixture_start,
    next_call,
    open_sandbox,
)

# How many sequences the property draws: properties 3 and 17 draw 500, every other one 100
# (tests/conftest.py).
EXAMPLES: Final = 100

# The four keys of the error object, in the order requirement 13.6 asks for them.
ERROR_KEYS: Final[tuple[str, ...]] = (CODE, MESSAGE, RULE_ID, DETAILS)

# The keys a detail item carries (design.md data model 6): the input field path it is
# about, the reason, and the extra keys the reasons of the table below add.
FIELD: Final = "field"
REASON: Final = "reason"
EXPECTED: Final = "expected"
STATUS: Final = "status"
SOURCE_LINE_ID: Final = "source_line_id"
PUSHABLE_QTY: Final = "pushable_qty"
PUSHABLE_AMOUNT: Final = "pushable_amount"
MATERIAL_ID: Final = "material_id"
WAREHOUSE_ID: Final = "warehouse_id"
QTY_BASE: Final = "qty_base"
BILL_TYPE: Final = "bill_type"
BILL_ID: Final = "bill_id"

# The key requirement 13.5 adds to a shortened result, which an error result never is.
TRUNCATED: Final = "truncated"

# The suffix of the parameter a push tool names its source bill under (requirement 11.13)
# and the prefix of a path inside the lines of a call (requirement 5.6).
LINE_PATH: Final = re.compile(r"lines\[[0-9]+\]\.(?P<name>.+)")


# --- the reference model: the detail items of design.md data model 6 ----------------------

# The ``rule_id`` an error must carry: ``None`` for one no rule decides, one rule id for a
# reason that belongs to a single rule, a set where more than one value is allowed, and
# :data:`PACK_RULE` for an error any enforced rule of a business pack decided. The sentinel
# is no rule id of itself: a pack rule prefix is letters and digits (requirement 2.8).
PACK_RULE: Final = "<包级规则>"

type RuleExpectation = str | None | frozenset[str | None]


@dataclass(frozen=True, slots=True)
class DetailShape:
    """What one detail item of a reason carries, and which rule its error names.

    - ``keys``: every key the item may carry, in the order it must carry them - ``field``
      first where the item is about an input field, then ``reason``, then the extra keys of
      the reason (design.md data model 6);
    - ``optional``: the keys of ``keys`` an item may leave out, the rest being always
      there;
    - ``rule``: the ``rule_id`` of the error the item belongs to (:data:`RuleExpectation`,
      requirement 13.8).
    """

    keys: tuple[str, ...]
    optional: frozenset[str] = frozenset()
    rule: RuleExpectation = None


# The item of a schema check, whose ``expected`` is there only where the check has a bound,
# a type or a set of values to name, and the item of every check that only names a path.
_SCHEMA_ITEM: Final = DetailShape((FIELD, REASON, EXPECTED), frozenset({EXPECTED}))
_PATH_ITEM: Final = DetailShape((FIELD, REASON))

# The table of design.md data model 6: the keys of each reason and the rule its error
# names. The schema reasons of requirement 12.2 and 12.3 come first, as one row
# (:data:`~erp_mock.tools.validate.REASONS`), then the reasons the domain steps report.
SHAPES: Final[Mapping[str, DetailShape]] = FrozenMap(
    {
        **{reason: _SCHEMA_ITEM for reason in REASONS},
        # No rule decides any of these: a reference that is not there, a source line of
        # another bill, the lines of a pushed bill, a missing unit conversion, a conversion
        # record whose factor or unit does not fit, a unique value taken and a record still
        # referred to (requirements 5.4, 5.6, 5.8, 6.11, 8.4 and 13.8).
        NOT_FOUND_REASON: _PATH_ITEM,
        NOT_IN_SOURCE_REASON: _PATH_ITEM,
        LINES_NOT_EDITABLE_REASON: _PATH_ITEM,
        MISSING_REASON: _PATH_ITEM,
        # ``must_be_positive`` is reported by the input validation and by the check of a
        # conversion factor, and neither names an ``expected``, so this row deliberately
        # tightens the schema row above: the item is the path and the reason, nothing else.
        NOT_POSITIVE_REASON: _PATH_ITEM,
        INVALID_UNIT_REASON: _PATH_ITEM,
        DUPLICATE_REASON: _PATH_ITEM,
        REFERENCED_REASON: _PATH_ITEM,
        # The state of a bill, which carries the state it is in: R-GEN-001 for a write to a
        # bill that is not a draft, R-GEN-004 for the source bill of a push - the one item
        # of the table that names an input field, the parameter the source came from - and
        # null for a state operation whose pre-state does not fit (requirements 7.3, 7.4,
        # 7.9 and 8.3).
        INVALID_STATE_REASON: DetailShape(
            (FIELD, REASON, STATUS),
            frozenset({FIELD}),
            frozenset({None, DRAFT_RULE, SOURCE_STATUS_RULE}),
        ),
        # The engine rules with a reason of their own (requirements 7.5, 8.10, 8.5, 8.6,
        # 5.7 and 9.4).
        REQUIRED_MISSING_REASON: DetailShape((FIELD, REASON), rule=REQUIRED_RULE),
        HAS_DOWNSTREAM_REASON: DetailShape((REASON, BILL_TYPE, BILL_ID), rule=PUSHED_RULE),
        EXCEEDS_QTY_REASON: DetailShape(
            (FIELD, REASON, SOURCE_LINE_ID, PUSHABLE_QTY), rule=QUOTA_RULE
        ),
        EXCEEDS_AMOUNT_REASON: DetailShape((REASON, PUSHABLE_AMOUNT), rule=QUOTA_RULE),
        DISABLED_REASON: DetailShape((FIELD, REASON), rule=DISABLED_RULE),
        NEGATIVE_STOCK_REASON: DetailShape(
            (REASON, MATERIAL_ID, WAREHOUSE_ID, QTY_BASE), rule=NEGATIVE_STOCK_RULE
        ),
        # An enforced rule of a business pack, whatever its error code (requirement 10.7).
        RULE_CHECK_FAILED_REASON: DetailShape((FIELD, REASON), rule=PACK_RULE),
    }
)

# The one item of design.md data model 6 that carries no reason at all: the resource limit
# of requirement 23.8, which names the limit that was reached and its integer value.
LIMIT_SHAPE: Final = DetailShape((LIMIT_KEY, MAX_KEY))

# The seven rules of the engine (requirement 13.8); every other rule an error may name is
# an enforced rule of a business pack.
ENGINE_RULES: Final[frozenset[str]] = frozenset(
    {
        DRAFT_RULE,
        REQUIRED_RULE,
        PUSHED_RULE,
        SOURCE_STATUS_RULE,
        QUOTA_RULE,
        DISABLED_RULE,
        NEGATIVE_STOCK_RULE,
    }
)

# Which error codes requirement 13.8 settles on their own: a business rule violation always
# names the rule it violated, and these four never name one - a reference that is not
# there, a value already taken, a resource limit and a defect of the framework.
RULED_ALWAYS: Final[frozenset[str]] = frozenset({ErrorCode.BUSINESS_RULE_VIOLATION})
RULED_NEVER: Final[frozenset[str]] = frozenset(
    {ErrorCode.NOT_FOUND, ErrorCode.CONFLICT, ErrorCode.LIMIT_EXCEEDED, ErrorCode.INTERNAL_ERROR}
)

# The codes an error result may carry at all (requirement 12.8).
CODES: Final[frozenset[str]] = frozenset(code.value for code in ErrorCode)

# A message is written in simplified Chinese (requirement 13.7): it holds at least one Han
# character, which is the part of "Chinese" a test can read off the text. Full-width
# punctuation is not a Han character, so a message of punctuation alone does not pass.
HAN: Final = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

# What no message carries, which requirement 13.10 spells out for INTERNAL_ERROR and which
# holds of every message: a line break or another control character, a stack, a source file
# and the name of an exception class.
CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
STACK: Final = "Traceback"
SOURCE_FILE: Final = re.compile(r"[\w/\\.-]+\.py\b")
EXCEPTION_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z_0-9.]*(?:Error|Exception)\b")


# --- what a message may name a field path by (requirement 13.7) --------------------------


@dataclass(frozen=True, slots=True)
class Named:
    """The texts a message of one tool may name a field path by (requirement 13.7).

    ``fields`` and ``lines`` are the field view of the record the tool works on - the header
    and the lines of a bill, or the fields of an entity record - which a path is resolved
    against to get the ``label.zh`` the requirement allows instead of the path itself;
    ``others`` holds the label of a path that names no field of that record, which is the
    ``<short name>_id`` of a push tool (module docstring).
    """

    fields: Mapping[str, FieldModel] = FrozenMap()
    lines: Mapping[str, FieldModel] = FrozenMap()
    others: Mapping[str, str] = FrozenMap()

    def texts(self, path: str) -> tuple[str, ...]:
        """The path itself and the Chinese label of what it names, where it has one."""
        label = self.label(path)
        return (path,) if label is None else (path, label)

    def label(self, path: str) -> str | None:
        """The ``label.zh`` of the field ``path`` names, None where it names none.

        None for a path the configuration declares no label for: the ``lines`` array
        itself, the ``id`` of a record and every standard field of a bill, all of which a
        message names by the path.
        """
        found = self.others.get(path)
        if found is not None:
            return found
        match = LINE_PATH.fullmatch(path)
        field = self.lines.get(match.group("name")) if match is not None else self.fields.get(path)
        return None if field is None or field.label is None else field.label.zh


def named_of(start: Start) -> Mapping[str, Named]:
    """The :class:`Named` of every tool of ``start``, by the name of the tool.

    Built once per module: the field view of a bill does not change over a run, and
    resolving it per failed call would cost more than the call does.
    """
    sources = {push.spec.name: (push.source_param, push.source.label) for push in start.pushes}
    found: dict[str, Named] = {}
    for name, spec in start.tools.items():
        others = sources.get(name)
        extra = (
            FrozenMap[str, str]()
            if others is None or others[1] is None
            else FrozenMap({others[0]: others[1].zh})
        )
        found[name] = _named_record(start.domain, spec, extra)
    return found


def _named_record(domain: DomainModel, spec: ToolSpec, others: Mapping[str, str]) -> Named:
    """The field view of the record ``spec`` writes, with the labels ``others`` adds.

    A push works on the target bill of its conversion (requirement 8.1), an operation on a
    bill or an entity record on that record, and anything else - a search, a query, the
    built-in tool - on no record at all, which leaves no path to resolve.
    """
    target = spec.target
    if spec.kind == "push":
        conversion = domain.conversions.get(target)
        target = "" if conversion is None else conversion.target
    bill = domain.bills.get(target)
    if bill is not None:
        return Named(fields=bill.header, lines=bill.lines, others=others)
    entity = domain.entities.get(target)
    if entity is not None:
        return Named(fields=entity.fields, others=others)
    return Named(others=others)


# --- the checks --------------------------------------------------------------------------


def error_object(performed: Performed, where: str) -> Mapping[str, JsonValue]:
    """The error object of a failed call as a client reads it (requirement 13.6).

    The one text item of the result, parsed: an object whose only key is ``error``, whose
    value carries the four keys of requirement 13.6 in order. The result carries no
    structuredContent, and the text is the compact JSON of exactly that object - compared
    against the object the layer kept, whose keys must be the same four in the same order.
    """
    outcome = performed.outcome
    assert outcome.is_error, where
    assert outcome.structured is None, f"{where} returned structuredContent"
    kept = outcome.error
    assert kept is not None, f"{where} returned no error object"
    assert outcome.text == dumps_compact({ERROR_KEY: kept}), f"{where}: the text is not the error"
    parsed = json.loads(outcome.text)
    assert isinstance(parsed, dict), f"{where}: the text is no JSON object"
    assert list(parsed) == [ERROR_KEY], f"{where}: the text carries the keys {list(parsed)}"
    found = parsed[ERROR_KEY]
    assert isinstance(found, dict), f"{where}: the error is no JSON object"
    assert tuple(found) == ERROR_KEYS, f"{where}: the error carries the keys {tuple(found)}"
    assert tuple(kept) == ERROR_KEYS, f"{where}: the kept error carries the keys {tuple(kept)}"
    assert TRUNCATED not in found, f"{where}: the error result was truncated"
    return found


def check_message(error: Mapping[str, JsonValue], named: Named, where: str) -> None:
    """``message`` is Chinese and says what did not work (requirement 13.7).

    Non-empty and holding at least one Han character, without the padding, the control
    characters, the stack, the source file and the exception class name a framework message
    never carries (requirement 13.10); where a detail item names an input field, the
    message carries that path or the Chinese label of what the path names, and where an
    enforced pack rule decided the error, :func:`check_pack_message` compares the message
    with that rule's text instead.
    """
    message = error[MESSAGE]
    assert isinstance(message, str) and message, f"{where}: the message is {message!r}"
    assert message.strip() == message, f"{where}: the message is padded: {message!r}"
    assert HAN.search(message) is not None, f"{where}: no Han character in {message!r}"
    assert CONTROL.search(message) is None, f"{where}: a control character in {message!r}"
    assert STACK not in message, f"{where}: a stack in the message: {message!r}"
    assert SOURCE_FILE.search(message) is None, f"{where}: a source file in {message!r}"
    assert EXCEPTION_NAME.search(message) is None, f"{where}: an exception class in {message!r}"
    paths = field_paths(error)
    if not paths:
        return
    texts = tuple(text for path in paths for text in named.texts(path))
    assert any(text in message for text in texts), (
        f"{where}: the message names none of {texts}: {message!r}"
    )


def check_pack_message(
    error: Mapping[str, JsonValue], rules: Mapping[str, str], where: str
) -> None:
    """A pack rule's error carries that rule's ``message.zh`` word for word (req 13.7)."""
    rule = error[RULE_ID]
    declared = rules.get(rule) if isinstance(rule, str) else None
    if declared is None:
        return
    assert error[MESSAGE] == declared, (
        f"{where}: {rule} reads {error[MESSAGE]!r} and not {declared!r}"
    )


def check_rule_id(error: Mapping[str, JsonValue], packs: Mapping[str, str], where: str) -> None:
    """``code`` and ``rule_id`` fit each other and name a rule there is (req 13.8)."""
    code = error[CODE]
    rule = error[RULE_ID]
    assert code in CODES, f"{where}: {code!r} is no error code"
    assert rule is None or isinstance(rule, str), f"{where}: the rule is {rule!r}"
    if code in RULED_NEVER:
        assert rule is None, f"{where}: {code} named the rule {rule}"
    if code in RULED_ALWAYS:
        assert rule is not None, f"{where}: {code} named no rule"
    if rule is not None:
        assert rule in ENGINE_RULES or rule in packs, f"{where}: {rule} is no rule of the instance"


def check_details(error: Mapping[str, JsonValue], packs: Mapping[str, str], where: str) -> None:
    """Every detail item carries the keys its reason declares (design.md data model 6)."""
    details = error[DETAILS]
    assert isinstance(details, list), f"{where}: the details are {details!r}"
    for index, item in enumerate(details):
        assert isinstance(item, dict), f"{where}: the detail {index} is {item!r}"
        check_item(item, error[RULE_ID], packs, f"{where}, detail {index}")


def check_item(
    item: Mapping[str, JsonValue],
    rule: JsonValue,
    packs: Mapping[str, str],
    where: str,
) -> None:
    """One detail item: the keys of its reason, in order, and the rule it belongs to."""
    found = item.get(REASON)
    if found is None:
        # The one item that carries no reason: the resource limit of requirement 23.8.
        shape = LIMIT_SHAPE
    else:
        assert isinstance(found, str), f"{where}: the reason is {found!r}"
        declared = SHAPES.get(found)
        assert declared is not None, f"{where}: {found!r} is no reason of data model 6"
        shape = declared
    assert list(item) == [key for key in shape.keys if key in item], (
        f"{where}: the keys {list(item)} are not {list(shape.keys)} in order"
    )
    missing = set(shape.keys) - set(item) - shape.optional
    assert not missing, f"{where}: the item left out {sorted(missing)}"
    check_item_rule(shape, rule, packs, where)


def check_item_rule(
    shape: DetailShape, rule: JsonValue, packs: Mapping[str, str], where: str
) -> None:
    """The ``rule_id`` the reason of the item belongs to (requirement 13.8)."""
    allowed = shape.rule
    if allowed == PACK_RULE:
        assert isinstance(rule, str) and rule in packs, (
            f"{where}: a pack rule decided it and the rule is {rule!r}"
        )
        return
    if isinstance(allowed, frozenset):
        assert rule in allowed, (
            f"{where}: the rule is {rule!r} and not one of {sorted_text(allowed)}"
        )
        return
    assert rule == allowed, f"{where}: the rule is {rule!r} and not {allowed!r}"


def field_paths(error: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """The ``field`` of every detail item that names an input field (requirement 13.6)."""
    details = error[DETAILS]
    items = details if isinstance(details, list) else ()
    return tuple(
        found
        for item in items
        if isinstance(item, dict) and isinstance(found := item.get(FIELD), str)
    )


def sorted_text(values: frozenset[str | None]) -> str:
    """A set of allowed rule ids as a failure prints it, null first."""
    return ", ".join(sorted((value or "null") for value in values))


def enforced_rules(start: Start) -> Mapping[str, str]:
    """The ``message.zh`` of every enforced rule of the instance, by rule id (req 10.7).

    The rules an error may name besides the engine's seven, and the text requirement 13.7
    asks the message of such an error to be. A rule without a message is no enforced rule:
    step 5 requires one (requirement 10.2).
    """
    return FrozenMap(
        (rule.id, rule.message.zh)
        for rule in start.domain.rules.values()
        if rule.enforcement == "enforced" and rule.message is not None
    )


# --- the starting point and the sandboxes of the run -------------------------------------


@pytest.fixture(scope="module")
def start(tmp_path_factory: pytest.TempPathFactory) -> Start:
    """The fixture starting point, built once for the module (requirement 25.2)."""
    return fixture_start(tmp_path_factory.mktemp("p21"))


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example is created below."""
    return tmp_path_factory.mktemp("p21-sandboxes")


@pytest.fixture(scope="module")
def named(start: Start) -> Mapping[str, Named]:
    """The field paths of every tool of the starting point, resolved once."""
    return named_of(start)


@pytest.fixture(scope="module")
def packs(start: Start) -> Mapping[str, str]:
    """The enforced rules of the instance, by rule id (requirement 10.7)."""
    return enforced_rules(start)


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 21: 错误结果的形态
@pytest.mark.req("13.6", "13.7", "13.8")
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_an_error_result_carries_the_four_keys_and_the_details_of_its_reason(
    data: st.DataObject,
    start: Start,
    named: Mapping[str, Named],
    packs: Mapping[str, str],
    sandboxes: Path,
) -> None:
    with open_sandbox(start, sandboxes) as session:
        steps = data.draw(st.integers(min_value=MIN_OPS, max_value=MAX_OPS), label="操作个数")
        for _ in range(steps):
            call = next_call(data.draw, session)
            if call is None:
                break
            performed = session.perform(call)
            if not performed.failed:
                continue
            rule = f" ({performed.rule_id})" if performed.rule_id else ""
            event(f"{performed.error_code}{rule} from a {performed.action}")
            note(f"call {performed.seq}: {performed.tool} {performed.call.args}")
            where = f"call {performed.seq}, {performed.tool}"
            error = error_object(performed, where)
            check_rule_id(error, packs, where)
            check_details(error, packs, where)
            check_message(error, named[performed.tool], where)
            check_pack_message(error, packs, where)
