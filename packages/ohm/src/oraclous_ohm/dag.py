"""Team DAG resolution over OHM v1.1 ``members[]`` (issue #395, ADR-031).

Pure and I/O-free. ``topological_stages`` turns the members' ``depends_on`` edges into ordered
execution STAGES: every member in a stage has all of its dependencies satisfied by earlier stages,
so the stage may run in parallel (fan-out); stages run in sequence (the fan-in barrier). It fails
CLOSED — a duplicate role, a ``depends_on`` edge to an unknown member, or a dependency cycle raises
``OHMDagError`` rather than executing a malformed topology.
"""

from __future__ import annotations

from oraclous_ohm.errors import OHMDagError
from oraclous_ohm.manifest import OHMMember


def topological_stages(members: list[OHMMember]) -> list[list[str]]:
    """Return the topologically-ordered execution stages for ``members``.

    Each stage is a sorted list of member roles whose dependencies are all satisfied by earlier
    stages. Raises ``OHMDagError`` on a duplicate role, a ``depends_on`` to an unknown member, or a
    dependency cycle.
    """
    if not members:
        return []

    roles = [m.role for m in members]
    role_set = set(roles)
    if len(role_set) != len(roles):
        dupes = sorted({r for r in roles if roles.count(r) > 1})
        raise OHMDagError(f"duplicate member role(s): {', '.join(dupes)}")

    deps: dict[str, set[str]] = {}
    for member in members:
        unknown = sorted(d for d in member.depends_on if d not in role_set)
        if unknown:
            raise OHMDagError(
                f"member '{member.role}' depends on unknown member(s): {', '.join(unknown)}"
            )
        deps[member.role] = set(member.depends_on)

    stages: list[list[str]] = []
    done: set[str] = set()
    remaining = set(role_set)
    while remaining:
        ready = sorted(r for r in remaining if deps[r] <= done)
        if not ready:
            raise OHMDagError(f"dependency cycle among members: {', '.join(sorted(remaining))}")
        stages.append(ready)
        done |= set(ready)
        remaining -= set(ready)
    return stages


def strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly-connected components of a directed graph (node -> successor nodes).

    Used by the importer (ADR-043 #552) to isolate each GENUINE loop in the handoff graph: an SCC of
    >=2 nodes — or a single node with a self-edge — is a real cycle the conductor runs as a bounded
    coordinator seam; every other node is its own singleton SCC (the acyclic skeleton on run_team).
    ``graph`` must include EVERY node as a key (leaf nodes map to an empty set). Each returned SCC
    is sorted; successors are visited in sorted order, so the output is deterministic. Pure + I/O-
    free.
    Teams are small (tens of members), so a recursive walk is well within the recursion limit."""
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    sccs: list[list[str]] = []

    def _strongconnect(node: str) -> None:
        nonlocal counter
        index[node] = lowlink[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for succ in sorted(graph.get(node, ())):
            if succ not in graph:
                continue  # an edge to an unknown node is ignored (the caller validates membership)
            if succ not in index:
                _strongconnect(succ)
                lowlink[node] = min(lowlink[node], lowlink[succ])
            elif succ in on_stack:
                lowlink[node] = min(lowlink[node], index[succ])
        if lowlink[node] == index[node]:  # node is an SCC root — pop the component
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == node:
                    break
            sccs.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            _strongconnect(node)
    return sccs
