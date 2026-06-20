"""Result aggregation / merge reducer — the deterministic fan-in merge (#421; ADR-035 §2/B3).

When ``orchestrate.parallel`` fans a member out (one instance per item), the per-instance outputs
must be MERGED, not discarded by the round-table's last-writer-wins (``final = transcript[-1]``).
This is the deterministic reducer (``aggregate.reduce``): **concat** (collect all items), **dedupe**
(collect + remove duplicates, optionally on a key), and **group_by** (group items by a schema key).
EURail: merge the 14 ``evidence_batch`` outputs into one ledger. The optional LLM
``aggregate.synthesize`` is a dispatch-injected member (the orchestrator's job), not here. Pure.
"""

from __future__ import annotations

from typing import Any

from oraclous_ohm.errors import OHMError


def _collect(outputs: list[Any], field: str | None) -> list[Any]:
    """Flatten the per-instance outputs into one item list (optionally extracting ``field``)."""
    items: list[Any] = []
    for out in outputs:
        value = out.get(field, []) if field is not None and isinstance(out, dict) else out
        if isinstance(value, list):
            items.extend(value)
        else:
            items.append(value)
    return items


def _dedupe(items: list[Any], on: str | None) -> list[Any]:
    """Stable de-dup — by item[``on``] when given (first wins), else by the item value."""
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        marker = item.get(on) if on is not None and isinstance(item, dict) else item
        try:
            key = marker if isinstance(marker, (str, int, float, bool, tuple)) else repr(marker)
        except Exception:  # noqa: BLE001 — an unhashable marker falls back to its repr
            key = repr(marker)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _group_by(items: list[Any], key: str) -> dict[Any, list[Any]]:
    groups: dict[Any, list[Any]] = {}
    for item in items:
        bucket = item.get(key) if isinstance(item, dict) else None
        groups.setdefault(bucket, []).append(item)
    return groups


def aggregate_reduce(
    outputs: list[Any],
    *,
    strategy: str = "concat",
    field: str | None = None,
    on: str | None = None,
    key: str | None = None,
) -> Any:
    """Deterministically merge fanned-out ``outputs`` (replacing last-writer-wins).

    ``strategy``: ``concat`` (collect all items), ``dedupe`` (collect + unique, on ``on`` if given),
    or ``group_by`` (group items by ``key``). ``field`` extracts a list field from each dict output
    before merging (e.g. each batch's ``evidence`` list). Fail-closed on an unknown strategy.
    """
    items = _collect(outputs, field)
    if strategy == "concat":
        return items
    if strategy == "dedupe":
        return _dedupe(items, on)
    if strategy == "group_by":
        if not key:
            raise OHMError("aggregate_reduce 'group_by' requires a 'key'")
        return _group_by(items, key)
    raise OHMError(f"unknown reduce strategy {strategy!r} (concat | dedupe | group_by)")
