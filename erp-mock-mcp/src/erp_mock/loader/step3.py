"""Loading step 3: the dependencies between the business packs of the instance.

The third step of design.md 2.3. A manifest declares the packs its configuration builds on
and the versions it accepts of them (``depends_on``, business-pack.md section 4), and
step 5 resolves a name of another pack only when ``depends_on`` lists that pack directly
(requirement 2.9). This step settles that those declarations hold for the packs this
instance loads (requirement 2.5):

- every range is a version range (requirement 2.4, the syntax of
  :class:`~erp_mock.common.versions.VersionRange`, as described in
  :mod:`erp_mock.loader.step2`);
- every pack depended on is referenced by instance.yaml, since a pack outside the instance
  provides nothing to resolve against;
- the version instance.yaml pins that pack to satisfies the range, so the pack is combined
  only with a version it was written against;
- the dependencies have no cycle, a pack depending on itself included. Without that, no
  order of the packs would let each read what it depends on.

Each of the first three is reported at ``depends_on.<pack>`` in the pack.yaml of the pack
that declares it; an entry that is wrong in two ways (an unknown pack and a range that is
not a range) gives both errors, as step 1 does for a manifest that matches neither the
referenced name nor the referenced version.

Cycles come from the dependency graph as a whole, not from one entry, so they are found by
Tarjan's algorithm (:func:`_components`): a strongly connected component of more than one
pack, or a single pack with a dependency on itself, is one cycle and one error. That error
names every pack of the cycle in its reason (requirement 2.5) and locates the entry of each
of them that leads into the cycle (requirement 2.2); since a
:class:`~erp_mock.loader.errors.Location` holds a pack-relative file, the reason is where
the packs are read, and the error is sorted and reported with the first of them in
instance.yaml order. A dependency takes part in the graph whatever its range says: a cycle
is about which packs need which, so an entry already reported for its range or its version
is still an edge.

Every pack and every entry is checked, so one run reports them all (requirement 2.2). The
step leaves nothing behind: step 5 reads ``depends_on`` from the manifests, which this step
has shown to name packs of the instance.
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Final

from erp_mock.common.versions import SemVer, VersionError, VersionRange
from erp_mock.loader.errors import Location, StepErrors
from erp_mock.loader.step1 import PACK_FILE, PackSource

__all__ = ["CONFIG_KEY", "check"]

# The manifest item this step checks; one entry of it is reported at ``depends_on.<pack>``.
CONFIG_KEY: Final = "depends_on"


def check(packs: Sequence[PackSource], errors: StepErrors) -> None:
    """Run step 3 on ``packs``, recording what it finds.

    ``packs`` are the packs instance.yaml references, in its order, each with the version
    it is pinned to. The step passed when ``errors`` stayed empty.
    """
    versions = {pack.name: pack.version for pack in packs}
    edges = {pack.name: _dependencies(pack, versions, errors) for pack in packs}
    for component in _components([pack.name for pack in packs], edges):
        if len(component) > 1 or component[0] in edges[component[0]]:
            _report_cycle(component, edges, errors)


def _dependencies(
    pack: PackSource, versions: Mapping[str, str], errors: StepErrors
) -> tuple[str, ...]:
    """Check the ``depends_on`` entries of ``pack``; return the packs it depends on.

    The returned names are the edges of the dependency graph: the packs of the instance
    (``versions``) this pack depends on, in the order its manifest writes them. A pack that
    is not one of them is reported here and is no edge, there being no node for it.
    """
    found: list[str] = []
    for name, text in pack.manifest.depends_on.items():
        config_path = f"{CONFIG_KEY}.{name}"
        required = _parse_range(text, pack.name, config_path, errors)
        version = versions.get(name)
        if version is None:
            reason = f"所依赖的业务包 {name} 不在 instance.yaml 的 packs 中"
            errors.add_at(pack.name, PACK_FILE, config_path, reason)
            continue
        found.append(name)
        if required is not None and not required.matches(SemVer.parse(version)):
            # The range as the manifest writes it, not its canonical text (step 2).
            reason = f"instance.yaml 引用的 {name} 版本 {version} 不满足范围 {text}"
            errors.add_at(pack.name, PACK_FILE, config_path, reason)
    return tuple(found)


def _parse_range(text: str, pack: str, config_path: str, errors: StepErrors) -> VersionRange | None:
    """Parse the range ``text`` of one entry; record the error and return None if it fails."""
    try:
        return VersionRange.parse(text)
    except VersionError as error:
        errors.add_at(pack, PACK_FILE, config_path, f"不是合法的版本范围：{error.reason}")
        return None


def _report_cycle(
    component: tuple[str, ...], edges: Mapping[str, Sequence[str]], errors: StepErrors
) -> None:
    """Record the cycle formed by ``component``, one error for the whole cycle.

    Each pack of the cycle is located at the first entry of its ``depends_on`` that names
    another pack of the cycle, or itself for a pack that depends on itself; strong
    connectivity means every member has such an entry.
    """
    members = frozenset(component)
    locations: list[Location] = []
    for member in component:
        inside = next(name for name in edges[member] if name in members)
        locations.append(Location(PACK_FILE, f"{CONFIG_KEY}.{inside}"))
    errors.add(component[0], locations, f"依赖关系存在循环，涉及业务包 {'、'.join(component)}")


def _components(nodes: Sequence[str], edges: Mapping[str, Sequence[str]]) -> list[tuple[str, ...]]:
    """The strongly connected components of a directed graph, by Tarjan's algorithm.

    ``nodes`` are the vertices and ``edges`` maps each of them to its successors, which are
    vertices too. Every vertex belongs to exactly one component, and two vertices share a
    component exactly when each can be reached from the other; a component of more than one
    vertex therefore means a cycle through all of them, and so does a single vertex with an
    edge to itself. The members of a component come in ``nodes`` order and the components in
    the order the walk closes them, so the result depends on the graph alone.

    The walk is iterative, carrying its own path instead of recursing, so a long dependency
    chain cannot exhaust the interpreter's stack.
    """
    position = {node: index for index, node in enumerate(nodes)}
    visited: dict[str, int] = {}  # vertex -> its number, in the order the walk reached it
    lowest: dict[str, int] = {}  # vertex -> the lowest number its subtree reaches back to
    pending: list[str] = []  # the vertices of the components that are not closed yet
    open_nodes: set[str] = set()  # the vertices on ``pending``
    # The path of the walk: each vertex on it with the successors it has left to visit.
    path: list[tuple[str, Iterator[str]]] = []
    components: list[tuple[str, ...]] = []

    def enter(node: str) -> None:
        """Number ``node``, put it on the path and make it pending."""
        visited[node] = lowest[node] = len(visited)
        pending.append(node)
        open_nodes.add(node)
        path.append((node, iter(edges.get(node, ()))))

    for root in nodes:
        if root in visited:
            continue
        enter(root)
        while path:
            node, successors = path[-1]
            descended = False
            for successor in successors:
                if successor not in visited:
                    enter(successor)
                    descended = True
                    break
                if successor in open_nodes:
                    # The successor is on the path or in a component that is still open, so
                    # it reaches back to this vertex and both share a component.
                    lowest[node] = min(lowest[node], visited[successor])
            if descended:
                continue
            path.pop()
            if lowest[node] == visited[node]:
                # Nothing above ``node`` reaches past it: it is the root of its component,
                # which holds it and every vertex pending above it.
                members: list[str] = []
                while True:
                    member = pending.pop()
                    open_nodes.discard(member)
                    members.append(member)
                    if member == node:
                        break
                components.append(tuple(sorted(members, key=position.__getitem__)))
            if path:
                parent = path[-1][0]
                lowest[parent] = min(lowest[parent], lowest[node])
    return components
