"""Property 20: the shape of a successful result.

**Validates: Requirements 5.16, 11.7, 13.1, 13.2, 13.3, 13.4, 13.9**

For any successful call of a random operation sequence (:mod:`tests.property.opseq`), the
result is the one its own outputSchema describes and the one a get of the same record
returns:

- it validates against the outputSchema of the tool, and every object inside it - the
  header, each line, each nested reference - carries exactly the keys that schema declares
  for it, in declaration order (requirements 11.7 and 13.1);
- the one text item is the compact JSON of exactly that object and parses back to it, with
  the same key order (requirement 13.1);
- a reference is a nested object of ``id``, ``code`` and ``name`` for basic data and of
  ``id`` and ``number`` for a bill, under the key without ``_id``, while
  ``source_line_id`` keeps its key and its string value (requirement 13.3);
- a decimal is the canonical text of its field type and no JSON number, a date is
  ``YYYY-MM-DD``, an int is an integer, a bool a boolean, an enum its code, and an empty
  value is null with its key kept (requirement 13.4);
- a create, an update, a push and a state operation return the object the get of the same
  record returns right after (requirements 5.16 and 13.2), and a delete the object the get
  of that record returned right before it (requirement 13.9).

The reference model is the configuration, read twice over. What keys a result object holds
and what each value looks like comes from the field view of the bill
(:class:`~erp_mock.config.model.FieldModel`): :class:`Shape` is the header fields of the
bill plus its lines, keyed by the result key of each field, and :func:`check_value` decides
from the type of the field alone how its value must be written. That is the same source
:mod:`erp_mock.tools.output_schema` and :mod:`erp_mock.domain.views` are built from, and
nothing of either is imported here, so a key the two of them agree on but the
configuration does not have is caught. The key *order* is checked against the outputSchema
of the tool rather than against the field view, which is what requirement 11.7 ties the two
together with, and the schema itself is read as a client reads it: its canonical JSON,
parsed (:func:`schema_json`).

The get is the model of requirements 5.16, 13.2 and 13.9, and it is performed, not
reconstructed: after every successful write the sequence makes, the get of the same record
is called through the same tools layer and the two results must be the same object. A read
changes nothing, so the sequence that follows is the sequence that would have followed
without it; the get of a delete has to come *before* the call, since the record is gone
afterwards, so a drawn delete is preceded by the get of the record it names and compared
with that. A sandbox whose profile exposes no get of a bill is left out of this half of the
property - the fixture start exposes the get of every bill it writes, so it never is.

Every one of those get results is itself a successful call and goes through the same
checks, which is why the property covers the get of a bill as well as the ten operations of
the sequences.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final, assert_never

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from erp_mock.common.decimals import format_decimal
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact
from erp_mock.config.model import BillModel, DomainModel, EntityModel, FieldModel
from erp_mock.tools.generator import ToolSpec
from tests.property.opseq import (
    MAX_OPS,
    MIN_OPS,
    Call,
    Performed,
    Session,
    Start,
    fixture_start,
    next_call,
    open_sandbox,
)

# How many sequences the property draws (design.md, property testing).
EXAMPLES: Final = 100

# The keys of a result the checks name: the id of a record, the two keys of a bill
# reference, the two optional keys of a basic data reference, the lines of a bill and the
# key a truncated result carries (requirements 13.3, 13.4 and 13.5).
ID: Final = "id"
NUMBER: Final = "number"
CODE: Final = "code"
NAME: Final = "name"
LINES: Final = "lines"
TRUNCATED: Final = "truncated"

# The suffix a ref field's name ends with and a result drops, and the one line field that
# keeps it: the source line of a pushed line is the line ID itself (requirement 13.3).
ID_SUFFIX: Final = "_id"
SOURCE_LINE_ID: Final = "source_line_id"

# The two schema keywords the key order is read from (requirement 11.7).
PROPERTIES: Final = "properties"
ITEMS: Final = "items"

# The one action whose result is the object the record had *before* the call (req 13.9).
DELETE: Final = "delete"

# The text a decimal value and a period value have (requirements 13.4 and 4.8): plain
# notation without a sign on zero, leading zeros, an exponent or whitespace, and a month
# between 01 and 12.
DECIMAL_TEXT: Final = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")
PERIOD_TEXT: Final = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


# --- what the configuration says one result object holds ---------------------------------


@dataclass(frozen=True, slots=True)
class Shape:
    """What one object of a result holds: the field behind each key, and its arrays.

    ``fields`` is the field model of each scalar and each reference of the object, by the
    key it takes in a result (:func:`result_key`), and ``arrays`` the shape of the elements
    of each array it carries - the ``lines`` of a bill. Read off the field view of the
    configuration and nothing else (module docstring).
    """

    fields: FrozenMap[str, FieldModel] = FrozenMap()
    arrays: FrozenMap[str, "Shape"] = FrozenMap()

    @property
    def keys(self) -> frozenset[str]:
        """Every key the object must carry: a result leaves none out (requirement 13.4)."""
        return frozenset(self.fields) | frozenset(self.arrays)


def result_key(field: FieldModel) -> str:
    """The key ``field`` takes in a result: a ref field without its ``_id`` (req 13.3)."""
    return field.name.removesuffix(ID_SUFFIX) if field.nested else field.name


def level_shape(fields: Mapping[str, FieldModel]) -> Shape:
    """The shape of one level of a record: its fields, by the key each one takes."""
    return Shape(fields=FrozenMap((result_key(field), field) for field in fields.values()))


def bill_shape(bill: BillModel) -> Shape:
    """The shape of a bill: its header fields, then its lines (requirement 5.12)."""
    header = level_shape(bill.header)
    return Shape(fields=header.fields, arrays=FrozenMap({LINES: level_shape(bill.lines)}))


def bill_of(domain: DomainModel, spec: ToolSpec) -> BillModel | None:
    """The bill the result of ``spec`` is one record of, None for another result shape.

    An operation of a bill returns that bill and a push the target bill of its conversion
    (requirements 13.2 and 13.9); a search, a query, an operation of an entity and the
    built-in tool return something else, which the sequences never call.
    """
    match spec.kind:
        case "entity_op":
            return domain.bills.get(spec.target)
        case "push":
            conversion = domain.conversions.get(spec.target)
            return None if conversion is None else domain.bills.get(conversion.target)
        case _:
            return None


def ref_entity(domain: DomainModel, field: FieldModel) -> EntityModel | None:
    """The entity ``field`` refers to, None for a reference to a bill (requirement 13.3).

    None covers the header's ``source_bill_id``, whose target is the bill its
    ``source_bill_type`` names, and a ref field whose target names a bill directly.
    """
    return None if field.ref is None else domain.entities.get(field.ref)


def ref_keys(domain: DomainModel, field: FieldModel) -> tuple[str, ...]:
    """The keys the nested object of a reference to ``field``'s target carries (13.3).

    Basic data: ``id``, then ``code`` and ``name`` as far as the entity declares them; a
    bill: ``id`` and ``number``.
    """
    entity = ref_entity(domain, field)
    if entity is None:
        return (ID, NUMBER)
    declared = [
        name
        for name in (CODE, NAME)
        if (found := entity.fields.get(name)) is not None and not found.nested
    ]
    return (ID, *declared)


# --- reading the results of one starting point -------------------------------------------


@dataclass(frozen=True, slots=True)
class Reader:
    """The schema, the shape and the get tool of every tool of one starting point.

    Built once per module by :func:`reader_of`: parsing an outputSchema and compiling a
    validator per call would cost more than the calls do. ``gets`` is the get tool of each
    bill, by the name of the bill, which requirements 5.16, 13.2 and 13.9 compare a write
    with.
    """

    domain: DomainModel
    schemas: Mapping[str, Mapping[str, JsonValue]]
    validators: Mapping[str, Draft202012Validator]
    shapes: Mapping[str, Shape | None]
    gets: Mapping[str, str]

    def check(self, performed: Performed) -> dict[str, JsonValue]:
        """One successful call returns the result its outputSchema describes (req 13.1).

        Returns the structuredContent, so a caller may compare it with another result.
        """
        tool = performed.tool
        outcome = performed.outcome
        structured = outcome.structured
        assert structured is not None and outcome.error is None, tool
        # The schema of the tool, the key order of the schema, and the text (req 13.1).
        self.validators[tool].validate(structured)
        check_key_order(structured, self.schemas[tool], tool)
        assert outcome.text == dumps_compact(structured), f"{tool}: the text is not the result"
        parsed = json.loads(outcome.text)
        assert parsed == structured, f"{tool}: the text does not parse back to the result"
        check_key_order(parsed, self.schemas[tool], f"{tool} text")
        # The keys and the values the configuration calls for (requirements 13.3, 13.4).
        shape = self.shapes[tool]
        assert shape is not None, f"{tool} returns no record of a bill"
        check_object(self.domain, structured, shape, tool)
        return structured

    def get_tool(self, spec: ToolSpec) -> str | None:
        """The get tool of the bill ``spec`` works on, None where the profile exposes none."""
        bill = bill_of(self.domain, spec)
        return None if bill is None else self.gets.get(bill.name)

    def get_before(self, session: Session, call: Call) -> Performed | None:
        """The get of the record a drawn delete names, performed before it (req 13.9).

        None for any other call, for a bill with no get tool and for a delete of a record
        that is not there, whose get and whose delete both end with NOT_FOUND
        (requirement 5.6). The result of the get is checked like any other.
        """
        spec = session.start.spec(call.tool)
        tool = self.get_tool(spec)
        record = None if call.args is None else call.args.get(ID)
        if spec.action != DELETE or tool is None or not isinstance(record, str):
            return None
        got = session.call(tool, {ID: record})
        if got.failed:
            return None
        self.check(got)
        return got

    def check_same_as_get(
        self, session: Session, performed: Performed, before: Performed | None
    ) -> None:
        """A write returns what a get of the same record returns (req 5.16, 13.2, 13.9).

        The get right after the call for a create, an update, a push and a state
        operation, and the get right before it for a delete, whose record is gone
        afterwards. A bill the profile exposes no get of is left out (module docstring).
        """
        structured = performed.outcome.structured
        assert structured is not None, performed.tool
        tool = self.get_tool(performed.spec)
        if tool is None:
            return
        if performed.action == DELETE:
            assert before is not None, f"{performed.tool} deleted a record no get found"
            same_object(structured, before.outcome.structured, f"{performed.tool} and the get")
            return
        record = structured.get(ID)
        assert isinstance(record, str), f"{performed.tool} returned no id"
        got = session.call(tool, {ID: record})
        assert not got.failed, f"the get after {performed.tool} failed: {got.error_code}"
        same_object(structured, self.check(got), f"{performed.tool} and the get")


def reader_of(start: Start) -> Reader:
    """The :class:`Reader` of ``start``: every tool of the profile, read once."""
    domain = start.domain
    schemas: dict[str, Mapping[str, JsonValue]] = {}
    validators: dict[str, Draft202012Validator] = {}
    shapes: dict[str, Shape | None] = {}
    for name, spec in start.tools.items():
        schema = schema_json(spec)
        schemas[name] = schema
        validators[name] = Draft202012Validator(schema)
        bill = bill_of(domain, spec)
        shapes[name] = None if bill is None else bill_shape(bill)
    return Reader(
        domain=domain,
        schemas=schemas,
        validators=validators,
        shapes=shapes,
        gets={tools.name: tools.get.name for tools in start.bills if tools.get is not None},
    )


def schema_json(spec: ToolSpec) -> Mapping[str, JsonValue]:
    """The outputSchema of ``spec`` as a client reads it: its canonical JSON, parsed."""
    found = json.loads(dumps_compact(spec.output_schema))
    assert isinstance(found, dict), spec.name
    return found


# --- the checks --------------------------------------------------------------------------


def check_key_order(
    value: Mapping[str, JsonValue], schema: Mapping[str, JsonValue], where: str
) -> None:
    """Every object inside ``value`` carries its keys in declaration order (req 13.1).

    The declaration order of the ``properties`` of the schema object that describes it,
    down through the nested objects and the elements of the arrays. A key the schema does
    not declare is reported by the validator first (``additionalProperties: false``).
    """
    properties = schema.get(PROPERTIES)
    assert isinstance(properties, dict), where
    assert list(value) == [name for name in properties if name in value], where
    for name, found in value.items():
        declared = properties[name]
        assert isinstance(declared, dict), f"{where}.{name}"
        if isinstance(found, dict):
            check_key_order(found, declared, f"{where}.{name}")
        if isinstance(found, list):
            element = declared.get(ITEMS)
            assert isinstance(element, dict), f"{where}.{name}"
            for index, item in enumerate(found):
                assert isinstance(item, dict), f"{where}.{name}[{index}]"
                check_key_order(item, element, f"{where}.{name}[{index}]")


def check_object(
    domain: DomainModel, value: Mapping[str, JsonValue], shape: Shape, where: str
) -> None:
    """``value`` carries exactly the keys ``shape`` calls for, each written as req 13.4 asks.

    No key is left out and none is added - an empty value is null and keeps its key - and
    ``truncated`` is the one key that may be there without being a field of the level
    (requirements 13.4 and 13.5).
    """
    found = set(value)
    assert found <= shape.keys | {TRUNCATED}, (
        f"{where}: keys no field declares: {found - shape.keys}"
    )
    assert shape.keys <= found, f"{where}: keys the result left out: {shape.keys - found}"
    for name, item in value.items():
        if name == TRUNCATED:
            assert item is True, f"{where}.{name}"
            continue
        array = shape.arrays.get(name)
        if array is None:
            check_value(domain, item, shape.fields[name], f"{where}.{name}")
            continue
        assert isinstance(item, list), f"{where}.{name} is no array"
        for index, element in enumerate(item):
            assert isinstance(element, dict), f"{where}.{name}[{index}]"
            check_object(domain, element, array, f"{where}.{name}[{index}]")


def check_value(domain: DomainModel, value: JsonValue, field: FieldModel, where: str) -> None:
    """One value of a result object: a nested reference, or a scalar (req 13.3, 13.4)."""
    if field.nested:
        check_ref(domain, value, field, where)
        return
    if field.name == SOURCE_LINE_ID:
        # The one key that keeps its ``_id``: the value is the line ID itself and no
        # nested object (requirement 13.3).
        assert where.endswith(f".{SOURCE_LINE_ID}"), where
        assert value is None or isinstance(value, str), f"{where} is no line id"
    check_scalar(value, field, where)


def check_ref(domain: DomainModel, value: JsonValue, field: FieldModel, where: str) -> None:
    """A reference is the nested object of the record it names (requirement 13.3)."""
    if value is None:
        return
    assert isinstance(value, dict), f"{where} is no nested object"
    assert list(value) == list(ref_keys(domain, field)), where
    assert isinstance(value[ID], str) and value[ID], f"{where}.{ID}"
    entity = ref_entity(domain, field)
    for name, found in value.items():
        if name == ID:
            continue
        if entity is None:
            # A bill: its number, which the engine assigns (requirement 4.4).
            assert isinstance(found, str) and found, f"{where}.{name}"
            continue
        declared = entity.fields.get(name)
        assert declared is not None, f"{where}.{name}"
        check_scalar(found, declared, f"{where}.{name}")


def check_scalar(value: JsonValue, field: FieldModel, where: str) -> None:
    """One scalar value, written the way its field type is returned (requirement 13.4).

    An empty value is null, which every key but the ones the engine assigns itself may be;
    that the key is there at all is :func:`check_object`.
    """
    if value is None:
        return
    match field.type:
        case "qty" | "price" | "money" | "rate":
            assert isinstance(value, str), f"{where} is a JSON number, not a decimal string"
            assert DECIMAL_TEXT.fullmatch(value), f"{where} is no decimal text: {value!r}"
            assert format_decimal(field.type, Decimal(value)) == value, (
                f"{where} is not the canonical text of a {field.type}: {value!r}"
            )
        case "date":
            assert isinstance(value, str) and is_date(value), f"{where} is no date: {value!r}"
        case "period":
            assert isinstance(value, str) and PERIOD_TEXT.fullmatch(value), (
                f"{where} is no period: {value!r}"
            )
        case "int":
            assert isinstance(value, int) and not isinstance(value, bool), f"{where}: {value!r}"
        case "bool":
            assert isinstance(value, bool), f"{where} is no boolean: {value!r}"
        case "enum":
            assert isinstance(value, str), f"{where} is no enum code: {value!r}"
            assert not field.enum_values or value in field.enum_values, (
                f"{where} is no value of the enum: {value!r}"
            )
        case "string" | "text":
            assert isinstance(value, str), f"{where} is no string: {value!r}"
        case "ref":
            raise AssertionError(f"{where} is a ref field and no scalar")
        case _ as unknown:
            assert_never(unknown)


def is_date(text: str) -> bool:
    """Whether ``text`` is a date written ``YYYY-MM-DD`` (requirement 13.4)."""
    try:
        return date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def same_object(
    found: Mapping[str, JsonValue], other: Mapping[str, JsonValue] | None, where: str
) -> None:
    """Two results are the same object: the same keys in the same order, the same values."""
    assert other is not None, where
    assert found == other, where
    assert list(found) == list(other), where


# --- the starting point and the sandboxes of the run -------------------------------------


@pytest.fixture(scope="module")
def start(tmp_path_factory: pytest.TempPathFactory) -> Start:
    """The fixture starting point, built once for the module (requirement 25.2)."""
    return fixture_start(tmp_path_factory.mktemp("p20"))


@pytest.fixture(scope="module")
def sandboxes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The directory the sandbox of each example is created below."""
    return tmp_path_factory.mktemp("p20-sandboxes")


@pytest.fixture(scope="module")
def reader(start: Start) -> Reader:
    """The schema, the shape and the get tool of every tool of the starting point."""
    return reader_of(start)


# Feature: erp-mock-mcp-m0, Property 20: 成功结果的形态
@pytest.mark.req("5.16", "11.7", "13.1", "13.2", "13.3", "13.4", "13.9")
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data())
def test_a_successful_result_is_the_object_its_schema_and_a_get_describe(
    data: st.DataObject, start: Start, reader: Reader, sandboxes: Path
) -> None:
    with open_sandbox(start, sandboxes) as session:
        steps = data.draw(st.integers(min_value=MIN_OPS, max_value=MAX_OPS), label="操作个数")
        for _ in range(steps):
            call = next_call(data.draw, session)
            if call is None:
                break
            # A delete returns the object the record had before it, so the get of that
            # record runs first; a read changes nothing the sequence depends on (req 13.9).
            before = reader.get_before(session, call)
            performed = session.perform(call)
            if performed.failed:
                continue
            reader.check(performed)
            reader.check_same_as_get(session, performed, before)
