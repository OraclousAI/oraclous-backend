"""Sink-member derivation (domain layer; #995).

A team's "sink" members are those no other member ``depends_on`` — the manifest's terminal roles,
in declaration order. This is the SAME structural rule the engine already applied at four sites
(the #602 seeded-refresh cost lever's sink target, the #604 verdict grade target + re_task
retarget, and the #602 5-way delta's record source) before it was ALSO exposed read-side as
``TeamRunOut.answer_roles`` (#995) — this module is the ONE place the rule now lives.

Pure; accepts either already-validated ``OHMMember`` objects (the four existing engine call
sites, which pass ``OHMManifest.members``) or raw manifest dicts (``TeamRunOut``'s read-side
derivation off the stored snapshot, which must never re-validate it). Fail-closed to ``[]`` on
anything that does not look like a valid, acyclic member list — a non-list ``members``, a missing/
non-string ``role``, a non-list (or non-str-elements) ``depends_on``, a member that is neither an
``OHMMember`` nor a ``dict``, or a ``depends_on`` cycle among the given members. Never raises.

#995 point 7: a ``kind: "human"`` member is never a sink — a terminal approval gate's payload is a
decision object, not an answer.
"""

from __future__ import annotations

from oraclous_ohm.manifest import OHMMember

_ParsedMember = tuple[str, list[str], str | None]  # (role, depends_on, kind)


def sink_roles(members: object) -> list[str]:
    """The plan's sink members, in manifest declaration order, or ``[]`` on anything malformed."""
    parsed = _parse(members)
    if parsed is None or _has_cycle(parsed):
        return []
    depended = {dep for _, deps, _ in parsed for dep in deps}
    return [role for role, _, kind in parsed if role not in depended and kind != "human"]


def _parse(members: object) -> list[_ParsedMember] | None:
    if not isinstance(members, list):
        return None
    out: list[_ParsedMember] = []
    for m in members:
        if isinstance(m, OHMMember):
            out.append((m.role, list(m.depends_on), m.kind))
            continue
        if not isinstance(m, dict):
            return None
        role = m.get("role")
        deps = m.get("depends_on")
        if not isinstance(role, str) or not role:
            return None
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            return None
        kind = m.get("kind")
        out.append((role, deps, kind if isinstance(kind, str) else None))
    return out


def _has_cycle(parsed: list[_ParsedMember]) -> bool:
    edges = {role: deps for role, deps, _ in parsed}
    white, gray, black = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(edges, white)

    def visit(role: str) -> bool:
        color[role] = gray
        for dep in edges.get(role, []):
            state = color.get(dep)
            if state == gray:
                return True
            if state == white and visit(dep):
                return True
        color[role] = black
        return False

    return any(color[role] == white and visit(role) for role in edges)
