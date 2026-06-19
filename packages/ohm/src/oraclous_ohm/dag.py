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
