"""Which tools an instance exposes and what each one is named (requirement 11.1).

The Tool_Generator of design.md 5.1. Every tool of the semantic surface comes from the
configuration, never from a hand-written list: one tool per operation of an entity or a
bill, one per conversion, one per ``queries`` item of an exposure, and the engine's own
``business_context_get``, which belongs to no pack and is part of every profile
(tool-design.md section 2). :class:`ToolSpec` is what the generator produces per tool and
what every layer above it reads: the tool layer validates a call against it and the
gateway builds the tools/list payload from it.

How a name is built (requirements 11.1 and 11.13)::

    <entity>_<action>                       article_search, supply_order_approve
    <target bill>_create_from_<short name>  goods_entry_create_from_order
    the ``tool`` of a queries item          supply_price_query
    business_context_get                    the one built-in tool

The short name of a push is what the pack's ``source_short_names`` says for the source
bill, and the source bill's own name when it says nothing; the same short name names the
push tool's ``<short name>_id`` parameter, which is why a push tool is not renamed but
given another short name (:data:`push_tool_name`).

``exclude`` drops a generated tool and ``rename`` replaces its name, both named by the
generated name; a renamed tool keeps that name in :attr:`ToolSpec.generated_name`, so
loading step 6 can check that the rename did not change the action and report the two
names together (requirement 11.2). A name in both ``exclude`` and ``rename`` is an error
of step 6; the generator drops the tool, since a tool that is not exposed has no name to
check.

The order of the tools is the order of tools/list (requirement 11.9): inside a pack the
tools of the entities and bills first, by the path of the declaring file and, per object,
in the order of its ``operations``, then the push tools by file path, then the query tools
in declaration order. The built-in tools come first and the packs follow in the order of
the profile, which :mod:`erp_mock.loader.step6` and the profile assembly put together.

What the generator reads: the resolved :class:`~erp_mock.config.model.DomainModel`, which
carries every entity, bill, conversion and query with the pack and the file that declare
it, and the pack's validated ``tools/exposure.yaml``. The exposure is passed in rather
than read from the domain model: ``exclude``, ``rename`` and ``source_short_names`` are
items of one document of one pack, not properties of a resolved object, and loading step 6
- the only caller that needs them - already holds that document, so the model stays keyed
by object name alone.

What each tool takes is built by :mod:`erp_mock.tools.input_schema`, what it returns and
what a client may assume about calling it by :mod:`erp_mock.tools.output_schema`, and both
are assembled into the spec by :func:`_spec`, the one place a generated tool is put
together. What is not here yet: ``description`` (task 9.4), which takes every description
verbatim from the variant of the profile.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.model import BillModel, ConversionModel, DomainModel, EntityModel
from erp_mock.config.pack import ExposureDef
from erp_mock.tools.input_schema import (
    BUILTIN_INPUT,
    ToolInput,
    bill_input,
    entity_input,
    push_input,
    query_input,
)
from erp_mock.tools.output_schema import (
    BUILTIN_OUTPUT,
    ToolAnnotationsSpec,
    ToolOutput,
    object_output,
    push_output,
    query_output,
)

__all__ = [
    "ACTIONS",
    "BUILTIN_TOOLS",
    "BUSINESS_CONTEXT_GET",
    "NAME_PATTERN",
    "PUSH_ACTION",
    "ToolKind",
    "ToolSpec",
    "generate_pack_tools",
    "generated_tool_names",
    "push_tool_name",
    "source_short_name",
    "tool_action",
]

# The action word list of tool-design.md section 2: the only actions a tool name may end
# with (requirement 11.2). ``create_from`` is not in the list: a push tool ends with the
# short name of its source, so its names are checked against :data:`PUSH_ACTION` instead.
ACTIONS: Final[tuple[str, ...]] = (
    "search",
    "get",
    "query",
    "create",
    "update",
    "delete",
    "submit",
    "unsubmit",
    "approve",
    "unapprove",
    "close",
    "unclose",
    "write_off",
)

# The action of a push tool, the ``create_from`` of ``<target>_create_from_<short name>``.
PUSH_ACTION: Final = "create_from"

# The form every tool name has (requirement 11.2): lower case letters, digits and
# underscores, starting with a letter, at most 64 characters, which is what the function
# name of a model API accepts (tool-design.md section 2).
NAME_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"

# The one tool the engine brings itself, exposed by every instance (requirement 11.1).
BUSINESS_CONTEXT_GET: Final = "business_context_get"

# What a tool comes from: an operation of an entity or a bill, a conversion, a queries item
# of an exposure, or the engine itself.
type ToolKind = Literal["entity_op", "push", "query", "builtin"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool of the semantic surface, as the generator produces it (design.md 5.1).

    - ``name``: the exposed name, which is what tools/list returns: the generated name, or
      what ``rename`` made of it.
    - ``generated_name``: the name requirement 11.1 generates, equal to ``name`` unless the
      pack renamed the tool (:attr:`renamed`). Step 6 checks the rename against it and
      names both in its error; nothing at runtime reads it.
    - ``pack``: the business pack the tool belongs to, None for a built-in tool.
    - ``kind``: where the tool comes from (:data:`ToolKind`).
    - ``action``: the action of the word list the tool performs - ``search``, ``get``,
      ``query``, ``create``, ``update`` and the others, :data:`PUSH_ACTION` for a push
      tool, and the action the declared name of a query tool ends with. It is the empty
      string for a query tool whose name ends with no action of the list, which step 6
      reports (requirement 11.2).
    - ``target``: what the tool works on: the entity or bill of an operation, the id of a
      conversion, the tool name of a query, and its own name for a built-in tool.
    - ``input_schema``: what the tool takes, restricted to the 14 keywords of
      requirement 11.3 (:mod:`erp_mock.tools.input_schema`).
    - ``output_schema``: what the tool returns, which every successful result validates
      against and whose property order is the key order of that result
      (:mod:`erp_mock.tools.output_schema`, requirement 11.7).
    - ``annotations``: the hints a client reads before it calls the tool
      (requirement 11.8).
    - ``param_paths``: every parameter of ``input_schema`` that needs a description, in
      declaration order, a parameter of the lines element written ``lines[].<name>``
      (requirement 3.2).

    The rest is filled by the task that generates it, and empty here: ``description`` by
    task 9.4, which takes it verbatim from the variant of the profile (requirement 3.7).
    """

    name: str
    generated_name: str
    pack: str | None
    kind: ToolKind
    action: str
    target: str
    input_schema: FrozenMap[str, JsonValue] = FrozenMap()
    output_schema: FrozenMap[str, JsonValue] = FrozenMap()
    annotations: ToolAnnotationsSpec = ToolAnnotationsSpec()
    description: str = ""
    param_paths: tuple[str, ...] = ()

    @property
    def renamed(self) -> bool:
        """Whether the pack's ``rename`` gave the tool another name (requirement 11.1)."""
        return self.name != self.generated_name


# The built-in tools, in output order: they come before the tools of every pack
# (requirement 11.9). The current business context is read, so the tool is a get.
BUILTIN_TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name=BUSINESS_CONTEXT_GET,
        generated_name=BUSINESS_CONTEXT_GET,
        pack=None,
        kind="builtin",
        action="get",
        target=BUSINESS_CONTEXT_GET,
        input_schema=BUILTIN_INPUT.schema,
        output_schema=BUILTIN_OUTPUT.schema,
        annotations=BUILTIN_OUTPUT.annotations,
        param_paths=BUILTIN_INPUT.param_paths,
    ),
)


def tool_action(name: str) -> str:
    """The action of :data:`ACTIONS` that ``name`` ends with, or ``""`` when it ends with none.

    The action is the last part of the name, separated by an underscore, so ``vendor_get``
    performs ``get`` while ``vendor_forget`` performs nothing of the list. No action of the
    list is the underscore-separated tail of another, so at most one of them matches.
    """
    found = [action for action in ACTIONS if name.endswith(f"_{action}")]
    return max(found, key=len, default="")


def source_short_name(
    conversion: ConversionModel, short_names: Mapping[str, str] = FrozenMap()
) -> str:
    """The short name of the source bill of ``conversion`` (requirement 11.13).

    What ``short_names`` - the ``source_short_names`` of the pack that declares the
    conversion - says for the source bill, and the source bill's own name when it says
    nothing. It names the push tool and the tool's ``<short name>_id`` parameter, so both
    read it here.
    """
    return short_names.get(conversion.source, conversion.source)


def push_tool_name(
    conversion: ConversionModel, short_names: Mapping[str, str] = FrozenMap()
) -> str:
    """The name of the push tool of ``conversion`` (requirements 11.1 and 11.13).

    ``<target bill>_create_from_<short name>``, the short name being the one
    :func:`source_short_name` gives.
    """
    return f"{conversion.target}_{PUSH_ACTION}_{source_short_name(conversion, short_names)}"


def generated_tool_names(
    domain: DomainModel, pack: str, exposure: ExposureDef | None = None
) -> tuple[str, ...]:
    """The names ``pack`` generates, in output order, before ``exclude`` and ``rename``.

    What ``exclude`` and ``rename`` may name (requirement 11.14): a name that is not one of
    these is a tool the pack does not generate, which step 6 reports. ``exposure`` is read
    for ``source_short_names`` alone here, since those names decide the names of the push
    tools (requirement 11.13).
    """
    return tuple(spec.name for spec in _generated(domain, pack, _short_names(exposure)))


def generate_pack_tools(
    domain: DomainModel, pack: str, exposure: ExposureDef | None = None
) -> tuple[ToolSpec, ...]:
    """The tools ``pack`` exposes, in the output order of requirement 11.9.

    Every tool of requirement 11.1 is generated, those of ``exclude`` are dropped and
    ``rename`` replaces the name of the others; a pack without ``tools/exposure.yaml``
    exposes the generated tools unchanged. The built-in tools are not part of a pack
    (:data:`BUILTIN_TOOLS`), and nothing here checks a name: the names of a valid instance
    are what step 6 settles (requirement 11.2).
    """
    exclude = frozenset(() if exposure is None else exposure.exclude)
    rename: Mapping[str, str] = FrozenMap() if exposure is None else exposure.rename
    tools: list[ToolSpec] = []
    for spec in _generated(domain, pack, _short_names(exposure)):
        if spec.name in exclude:
            # A name in exclude and in rename at once is an error of step 6; the tool is
            # dropped, so the name rename would have given it is checked nowhere.
            continue
        name = rename.get(spec.name, spec.name)
        tools.append(spec if name == spec.name else replace(spec, name=name))
    return tuple(tools)


# --- the generated tools of one pack -----------------------------------------------------


def _short_names(exposure: ExposureDef | None) -> Mapping[str, str]:
    """The ``source_short_names`` of ``exposure``, empty when the pack declares none."""
    return FrozenMap() if exposure is None else exposure.source_short_names


def _generated(
    domain: DomainModel, pack: str, short_names: Mapping[str, str]
) -> Iterator[ToolSpec]:
    """Every tool ``pack`` generates, in output order, with its generated name (req 11.1).

    The order of requirement 11.9: the entities and the bills together, by the path of the
    file that declares them and, inside one object, in the order of its ``operations``;
    then the push tools, by the path of the conversion document; then the query tools, in
    the order the exposure declares them, which is the order of the domain model.
    """
    for item in _objects(domain, pack):
        for action in item.operations:
            built = _object_input(item, action)
            out = object_output(domain, item, action)
            name = f"{item.name}_{action}"
            yield _spec(name, pack, "entity_op", action, item.name, built, out)
    for conversion in _conversions(domain, pack):
        short = source_short_name(conversion, short_names)
        target = domain.bills.get(conversion.target)
        built = push_input(conversion, target, short)
        out = push_output(domain, target)
        name = push_tool_name(conversion, short_names)
        yield _spec(name, pack, "push", PUSH_ACTION, conversion.id, built, out)
    for query in domain.queries.values():
        if query.pack == pack:
            tool = query.tool
            built = query_input(query)
            out = query_output(domain, query)
            yield _spec(tool, pack, "query", tool_action(tool), tool, built, out)


def _spec(
    name: str,
    pack: str,
    kind: ToolKind,
    action: str,
    target: str,
    built: ToolInput,
    out: ToolOutput,
) -> ToolSpec:
    """One generated tool, under the name requirement 11.1 gives it.

    The one place a generated tool is assembled, so what every tool carries is decided
    here: the input schema and the parameter paths of :mod:`erp_mock.tools.input_schema`,
    and the output schema and the annotations of :mod:`erp_mock.tools.output_schema`.
    """
    return ToolSpec(
        name=name,
        generated_name=name,
        pack=pack,
        kind=kind,
        action=action,
        target=target,
        input_schema=built.schema,
        output_schema=out.schema,
        annotations=out.annotations,
        param_paths=built.param_paths,
    )


def _object_input(item: EntityModel | BillModel, action: str) -> ToolInput:
    """What the ``action`` tool of an entity or a bill takes (requirement 11.4)."""
    if isinstance(item, BillModel):
        return bill_input(item, action)
    return entity_input(item, action)


def _objects(domain: DomainModel, pack: str) -> tuple[EntityModel | BillModel, ...]:
    """The entities and bills of ``pack``, by the path of the declaring file (req 11.9).

    Entities and bills are written in the same directory of a pack, so one order covers
    both; the name decides between two objects of the same file, which a pack does not
    write but which keeps the order defined anyway.
    """
    found: list[EntityModel | BillModel] = [
        entity for entity in domain.entities.values() if entity.pack == pack
    ]
    found += [bill for bill in domain.bills.values() if bill.pack == pack]
    return tuple(sorted(found, key=lambda item: (item.file, item.name)))


def _conversions(domain: DomainModel, pack: str) -> tuple[ConversionModel, ...]:
    """The conversions of ``pack``, by the path of the document that declares them (req 11.9)."""
    found = [conversion for conversion in domain.conversions.values() if conversion.pack == pack]
    return tuple(sorted(found, key=lambda conversion: (conversion.file, conversion.id)))
