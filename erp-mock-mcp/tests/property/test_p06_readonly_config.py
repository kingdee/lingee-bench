"""Property 6: the configuration object is read-only.

**Validates: Requirements 2.11**

For any path of the object tree the Loader produces and any modification attempt at it, the
attempt raises and the canonical JSON output of the whole object is the one it had before.
The five attempts of the property are one parametrized run each, so every one of them gets
its own 100 examples:

- ``attribute``: assignment to an attribute of an object node;
- ``item``: assignment at a key of a mapping node or at an index of a sequence node, which
  is the list and mapping assignment requirement 2.11 names;
- ``insert``: a key, an element or an item added - ``node[new] = v``, ``update``,
  ``setdefault``, ``|=`` on a mapping, ``insert`` on a sequence;
- ``delete``: an attribute, a key or an element removed - ``del``, ``pop``, ``popitem``,
  ``remove``, ``clear``;
- ``append``: ``append`` and ``extend`` on a sequence.

:func:`build_tree` walks the object breadth-first and records every path it reaches, so an
example draws a path of the whole tree - 35 000 of them for the engine test fixture, 15
levels deep - and not of a hand-picked part of it. A node is one of three kinds: an object
(a frozen dataclass or a frozen pydantic model), a mapping (a
:class:`~erp_mock.common.frozen.FrozenMap`) or a sequence (a tuple); anything else - a str,
an int, a bool, None, a Path - is a leaf, which holds no content of its own. Each kind of
attempt draws from the nodes it applies to, in the order the walk reached them, so a
counterexample shrinks towards a path near the root.

The attempts are the ones a client can write at a path of the tree: an attribute the object
declares, a key or an index the container holds, and a key, an element or an item an
attempt adds. A name starting with an underscore is not such a path: pydantic keeps those
for the private attributes of a model, which are no fields of it and appear in no output of
it.

:func:`canonical_json` is what "the content is unchanged" is read against: the object as
JSON text, each object node written as its declared attributes, each mapping as its keys in
iteration order, each sequence as its items, and a leaf JSON cannot carry - a Path - as its
repr. It is compared after every single attempt, so a modification that raised halfway
through would still be caught.

One :class:`~erp_mock.loader.instance.LoadedInstance` serves every example of the module:
building it runs the validation steps, and no attempt of this test can change what it
holds, which is what the property asserts.
"""

import operator
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Final, Literal

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue, dumps_compact
from tests.helpers.fixtures import fixture_loaded

# What a read-only object raises: a frozen dataclass FrozenInstanceError (an
# AttributeError), a frozen pydantic model ValidationError (a ValueError), and a FrozenMap
# or a tuple TypeError for item assignment and deletion and AttributeError for a method of
# a mutable container it does not have.
REJECTED: Final = (AttributeError, TypeError, ValidationError)

# What an attempt writes. None of these can be stored, so what they are does not matter: a
# scalar, an empty read-only container and a filled one.
VALUES: Final = st.sampled_from([None, 0, False, "", "x", (), FrozenMap(), FrozenMap({"a": 1})])

# The keys an insertion adds. None of them is a key of the tree, where a key is a
# configuration name (``^[a-z][a-z0-9_]*$``), a rule id, a tool name or a key of the
# protocol, so such an attempt adds an element instead of replacing one.
NEW_KEYS: Final = st.sampled_from(["", "0", "P6", "新增"])

# What a node is: an object with declared attributes (a frozen dataclass or a frozen
# pydantic model), a mapping, or a sequence that is no str. Everything else is a leaf.
type NodeKind = Literal["object", "mapping", "sequence"]

# A node, as an attempt sees it. The attempts are the ones a client would write, which no
# read-only type accepts, so the node is deliberately untyped here.
type Node = Any

# The path of the root in every path of the tree, which is how a counterexample reads:
# ``loaded.domain.bills['supply_order'].lines['qty'].name``.
ROOT_PATH: Final = "loaded"


# --- the object tree ---------------------------------------------------------------------


def node_kind(value: object) -> NodeKind | None:
    """Which kind of node ``value`` is, or None when it is a leaf.

    A pydantic model is no Mapping and a str is no node, so the order of the checks decides
    nothing but the leaf case.
    """
    if isinstance(value, BaseModel):
        return "object"
    if is_dataclass(value) and not isinstance(value, type):
        return "object"
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, str | bytes):
        return None
    if isinstance(value, Sequence):
        return "sequence"
    return None


def field_names(node: Node) -> tuple[str, ...]:
    """The attributes an object node declares, in declaration order.

    The fields of the model or of the dataclass: what the object holds. A property such as
    :attr:`BillModel.line_name <erp_mock.config.model.BillModel.line_name>` is derived from
    them and is no path of the tree.
    """
    if isinstance(node, BaseModel):
        return tuple(type(node).model_fields)
    return tuple(field.name for field in fields(node))


def children(node: Node, kind: NodeKind) -> tuple[tuple[str, object], ...]:
    """The children of ``node``, each with the path step that reaches it."""
    match kind:
        case "object":
            return tuple((f".{name}", getattr(node, name)) for name in field_names(node))
        case "mapping":
            return tuple((f"[{key!r}]", item) for key, item in node.items())
        case "sequence":
            return tuple((f"[{index}]", item) for index, item in enumerate(node))


def keys(node: Node) -> st.SearchStrategy[Any]:
    """The keys of a mapping node or the indices of a sequence node."""
    if isinstance(node, Mapping):
        return st.sampled_from(tuple(node))
    return st.sampled_from(range(len(node)))


def canonical_json(value: Node) -> str:
    """The canonical JSON text of ``value``: everything the object tree holds.

    What an attempt must leave unchanged. An object node is written as its declared
    attributes, a mapping as its keys in iteration order, a sequence as its items, and a
    leaf as itself when JSON can carry it and as its repr when it cannot, which a Path is
    the only case of.
    """
    return dumps_compact(as_json(value))


def as_json(value: Node) -> JsonValue:
    """``value`` as a JSON value, read from the same nodes the tree walk follows."""
    match node_kind(value):
        case "object":
            return {name: as_json(getattr(value, name)) for name in field_names(value)}
        case "mapping":
            return {str(key): as_json(item) for key, item in value.items()}
        case "sequence":
            return [as_json(item) for item in value]
        case None:
            if value is None or isinstance(value, bool | int | float | str):
                return value
            return repr(value)


# --- the attempts ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Attempt:
    """One modification attempt: how it reads, and the call that makes it."""

    label: str
    run: Callable[[], object]


# Draws one attempt of its kind against a node, together with the key, index, field name
# and value it needs.
type Builder = Callable[[st.DataObject, Node], Attempt]


def assign_attribute(data: st.DataObject, node: Node) -> Attempt:
    name = data.draw(st.sampled_from(field_names(node)), label="field")
    value = data.draw(VALUES, label="value")
    return Attempt(f"node.{name} = {value!r}", lambda: setattr(node, name, value))


def delete_attribute(data: st.DataObject, node: Node) -> Attempt:
    name = data.draw(st.sampled_from(field_names(node)), label="field")
    return Attempt(f"del node.{name}", lambda: delattr(node, name))


def assign_item(data: st.DataObject, node: Node) -> Attempt:
    key = data.draw(keys(node), label="key")
    value = data.draw(VALUES, label="value")
    return Attempt(f"node[{key!r}] = {value!r}", lambda: operator.setitem(node, key, value))


def insert_item(data: st.DataObject, node: Node) -> Attempt:
    key = data.draw(NEW_KEYS, label="key")
    value = data.draw(VALUES, label="value")
    return Attempt(f"node[{key!r}] = {value!r}", lambda: operator.setitem(node, key, value))


def update(data: st.DataObject, node: Node) -> Attempt:
    key = data.draw(NEW_KEYS, label="key")
    value = data.draw(VALUES, label="value")
    return Attempt(f"node.update({{{key!r}: {value!r}}})", lambda: node.update({key: value}))


def setdefault(data: st.DataObject, node: Node) -> Attempt:
    key = data.draw(NEW_KEYS, label="key")
    value = data.draw(VALUES, label="value")
    return Attempt(f"node.setdefault({key!r}, {value!r})", lambda: node.setdefault(key, value))


def merge(data: st.DataObject, node: Node) -> Attempt:
    key = data.draw(NEW_KEYS, label="key")
    value = data.draw(VALUES, label="value")
    # ``node |= {...}`` rebinds a local name, so the in-place operator itself is called.
    return Attempt(f"node |= {{{key!r}: {value!r}}}", lambda: operator.ior(node, {key: value}))


def insert_at(data: st.DataObject, node: Node) -> Attempt:
    index = data.draw(st.integers(min_value=0, max_value=len(node)), label="index")
    value = data.draw(VALUES, label="value")
    return Attempt(f"node.insert({index}, {value!r})", lambda: node.insert(index, value))


def delete_item(data: st.DataObject, node: Node) -> Attempt:
    key = data.draw(keys(node), label="key")
    return Attempt(f"del node[{key!r}]", lambda: operator.delitem(node, key))


def pop_item(data: st.DataObject, node: Node) -> Attempt:
    key = data.draw(keys(node), label="key")
    return Attempt(f"node.pop({key!r})", lambda: node.pop(key))


def pop_last(_data: st.DataObject, node: Node) -> Attempt:
    return Attempt("node.popitem()", lambda: node.popitem())


def remove_item(data: st.DataObject, node: Node) -> Attempt:
    index = data.draw(keys(node), label="index")
    item = node[index]
    return Attempt(f"node.remove(node[{index}])", lambda: node.remove(item))


def clear(_data: st.DataObject, node: Node) -> Attempt:
    return Attempt("node.clear()", lambda: node.clear())


def append(data: st.DataObject, node: Node) -> Attempt:
    value = data.draw(VALUES, label="value")
    return Attempt(f"node.append({value!r})", lambda: node.append(value))


def extend(data: st.DataObject, node: Node) -> Attempt:
    value = data.draw(VALUES, label="value")
    return Attempt(f"node.extend(({value!r},))", lambda: node.extend((value,)))


@dataclass(frozen=True, slots=True)
class AttemptKind:
    """One of the five modification attempts of property 6: where it applies, and how.

    ``builders`` holds the attempts of this kind per kind of node, which is also what nodes
    an example may draw. ``named`` says whether an attempt names an attribute, a key or an
    index the node already has, which only a node with at least one child can provide; an
    empty mapping and an empty tuple are drawn by the insertion and the append runs.
    """

    builders: Mapping[NodeKind, tuple[Builder, ...]]
    named: bool = False


# The five attempts of property 6, each its own parametrized run (module docstring).
KINDS: Final[Mapping[str, AttemptKind]] = {
    "attribute": AttemptKind({"object": (assign_attribute,)}, named=True),
    "item": AttemptKind({"mapping": (assign_item,), "sequence": (assign_item,)}, named=True),
    "insert": AttemptKind(
        {"mapping": (insert_item, update, setdefault, merge), "sequence": (insert_at,)}
    ),
    "delete": AttemptKind(
        {
            "object": (delete_attribute,),
            "mapping": (delete_item, pop_item, pop_last, clear),
            "sequence": (delete_item, pop_item, remove_item, clear),
        },
        named=True,
    ),
    "append": AttemptKind({"sequence": (append, extend)}),
}


# --- the tree of one loaded instance -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tree:
    """Every path of one object tree, with the canonical output the root starts from.

    ``sites`` maps each path to the node it reaches and ``pools`` holds, per attempt kind,
    the paths an example of that kind may draw, in the order the walk reached them.
    """

    root: object
    sites: Mapping[str, object]
    pools: Mapping[str, tuple[str, ...]]
    canonical: str


def build_tree(root: object) -> Tree:
    """Walk ``root`` breadth-first and record every path, its node and its kind."""
    sites: dict[str, object] = {}
    kinds: dict[str, NodeKind] = {}
    filled: set[str] = set()
    queue = deque([(ROOT_PATH, root)])
    while queue:
        path, value = queue.popleft()
        sites[path] = value
        kind = node_kind(value)
        if kind is None:
            continue
        kinds[path] = kind
        found = children(value, kind)
        if found:
            filled.add(path)
        queue.extend((path + step, child) for step, child in found)
    pools = {
        name: tuple(
            path
            for path, kind in kinds.items()
            if kind in attempt.builders and (path in filled or not attempt.named)
        )
        for name, attempt in KINDS.items()
    }
    return Tree(root=root, sites=sites, pools=pools, canonical=canonical_json(root))


@pytest.fixture(scope="module")
def tree() -> Tree:
    """The object tree of the engine test fixture, loaded and walked once.

    One instance serves every example: no attempt of this test can change what it holds,
    which is the property itself.
    """
    return build_tree(fixture_loaded())


# --- the property ------------------------------------------------------------------------


# Feature: erp-mock-mcp-m0, Property 6: 配置对象只读
@pytest.mark.req("2.11")
@pytest.mark.parametrize("kind", list(KINDS))
@settings(max_examples=100)
@given(data=st.data())
def test_every_modification_attempt_raises_and_keeps_the_content(
    tree: Tree, kind: str, data: st.DataObject
) -> None:
    attempt_kind = KINDS[kind]
    path = data.draw(st.sampled_from(tree.pools[kind]), label="path")
    node = tree.sites[path]
    at = node_kind(node)
    assert at is not None, path
    builder = data.draw(st.sampled_from(attempt_kind.builders[at]), label="attempt")
    attempt = builder(data, node)
    note(f"{path}: {attempt.label}")

    with pytest.raises(REJECTED):
        attempt.run()

    # Nothing of the object changed, at this path or anywhere else.
    assert canonical_json(tree.root) == tree.canonical
