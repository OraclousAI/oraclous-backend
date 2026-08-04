"""Input validation against a tool descriptor's declared ``input_schema`` (domain layer; #693).

Every builtin tool descriptor already declares an ``input_schema``; nothing enforced it, so a
connector that subscribed a declared-but-unchecked key turned a caller's mistake into a raw Python
``KeyError`` whose message — the quoted key name — became the whole error detail the caller read
(``tool execution failed: 'path'``). A model cannot repair a call from that, so it reissues the same
one until the loop gives up.

This module is the check the schema was always asking for. ``InternalTool.execute`` runs it before
``_execute_internal``, so it binds to EVERY builtin connector at once rather than to the one that
was reported (#693 AC5) — the executors are all ``InternalTool`` subclasses and all carry their
descriptor. The message names the argument path (``files[0].path``) and the expectation, which is
what makes the failure repairable.

Two deliberate limits, both about not breaking calls that work today:

* Only ``required`` and ``type`` are enforced. ``enum`` is not: the connectors already reject an
  unknown operation with their own curated ``INVALID_OPERATION``, and enforcing it here would
  reclassify that established failure for no acceptance-criterion gain (an unknown *tool* name is
  #673, not this).
* A **top-level** ``required`` field may be satisfied by the instance configuration instead of the
  call. Several tools bind an argument on the instance rather than passing it per call — the sink's
  ``repo`` (the "configured, not passed" shape, #542) and recall-memory's ``graph_id``. Nested
  ``required`` (the ``files[]`` items, where the bug lives) is enforced strictly.

An unknown keyword or an unparseable schema fragment is ignored rather than treated as a failure: a
descriptor is data, and an MCP import carries a remote server's schema. Validation may only reject
what it positively understands to be wrong.
"""

from __future__ import annotations

from typing import Any

#: JSON-Schema type name -> the Python type(s) that satisfy it. ``integer``/``number`` exclude
#: ``bool`` explicitly (``bool`` is a subclass of ``int`` in Python, but not a JSON number).
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def _type_name(value: Any) -> str:
    """The JSON-Schema type name of a runtime value, for the "got X" half of a message."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "null" if value is None else type(value).__name__


def _matches(expected: str, value: Any) -> bool:
    allowed = _TYPES.get(expected)
    if allowed is None:  # a type name this validator does not model → nothing to reject
        return True
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def _expected_of(schema: Any) -> str | None:
    """The declared type of a property, used to tell the caller what shape was wanted."""
    if isinstance(schema, dict) and isinstance(schema.get("type"), str):
        return schema["type"]
    return None


def _check(schema: Any, value: Any, path: str, *, bound: set[str] | None = None) -> str | None:
    """The first problem in ``value`` under ``schema``, as a caller-repairable sentence, else None.

    ``bound`` is the set of names supplied elsewhere than the call (the instance configuration); it
    applies to this level's ``required`` only, never to nested objects.
    """
    if not isinstance(schema, dict):
        return None

    expected = _expected_of(schema)
    if expected is not None and not _matches(expected, value):
        where = path or "the input"
        return f"{where} must be a {expected}, got {_type_name(value)}"

    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if not isinstance(name, str) or name in value or (bound and name in bound):
                continue
            field = f"{path}.{name}" if path else name
            declared = _expected_of((schema.get("properties") or {}).get(name))
            suffix = f" ({declared})" if declared else ""
            return f"{field} is required{suffix}"
        for name, sub in (schema.get("properties") or {}).items():
            if name not in value:
                continue
            problem = _check(sub, value[name], f"{path}.{name}" if path else str(name))
            if problem is not None:
                return problem

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                problem = _check(items, item, f"{path}[{index}]")
                if problem is not None:
                    return problem

    return None


def validate_input(
    schema: dict[str, Any], input_data: Any, *, configuration: dict[str, Any] | None = None
) -> str | None:
    """The first way ``input_data`` violates ``schema``, phrased so a caller can repair the call.

    Returns ``None`` when the input is acceptable. Only the FIRST problem is reported: a model
    repairs one argument at a time, and a list of every fault is harder to act on than one sentence.
    """
    return _check(schema, input_data, "", bound=set(configuration or {}))
