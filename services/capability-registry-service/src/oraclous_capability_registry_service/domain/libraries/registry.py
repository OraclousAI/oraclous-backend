"""Curated library operation registry (#488) — the ONLY functions a library-group tool may run.

A request's ``operation`` selects an :class:`OperationSpec` here; there is no free import or
eval, so no arbitrary-code path. The plugin's CAPABILITIES + the INPUT_SCHEMA operation enum are
GENERATED from this registry (:func:`capabilities`, :func:`operation_names`) so the descriptor can
never drift from the callables. Each op declares its args (name → type) for validation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from oraclous_capability_registry_service.domain.libraries import text_tools


@dataclass(frozen=True)
class OperationSpec:
    """A curated library operation: its name, the callable, a description, and its typed args.

    ``result_kind`` (#804, §CITE rev6) is REQUIRED and has no default. A default is how the next
    curated operation would ship undeclared, and an undeclared operation read as ``status`` is the
    fail-open direction rev6 decision 16 exists to close: an ungraded content-returning capability
    would declare itself an action tool, skip §CITE-QUAL grading and be asserted from uncited. So
    adding an operation without a value fails here, at construction, rather than in the catalogue.
    """

    name: str
    func: Callable[..., dict]
    description: str
    result_kind: str  # "status" | "single" | "collection" (§CITE-QUAL, rev6)
    args: dict[str, type] = field(default_factory=dict)

    def parameters(self) -> dict[str, str]:
        """The descriptor ``parameters`` shape (arg-name → type-name)."""
        return {name: typ.__name__ for name, typ in self.args.items()}


_OPERATIONS: dict[str, OperationSpec] = {
    spec.name: spec
    for spec in (
        # All three are `status`: a transform of input the caller already holds names nothing that
        # exists independently of the call, so there is no source to point a reader at (#804).
        OperationSpec(
            "word_count",
            text_tools.word_count,
            "Count the words in a text.",
            "status",
            {"text": str},
        ),
        OperationSpec(
            "to_upper", text_tools.to_upper, "Upper-case a text.", "status", {"text": str}
        ),
        OperationSpec(
            "extract_emails",
            text_tools.extract_emails,
            "Extract the distinct e-mail addresses in a text.",
            "status",
            {"text": str},
        ),
    )
}


def get_operation(name: str) -> OperationSpec | None:
    """The curated operation for ``name``, or ``None`` if it is not a known operation."""
    return _OPERATIONS.get(name)


def operation_names() -> list[str]:
    """The curated operation names (stable order) — for the INPUT_SCHEMA enum + diagnostics."""
    return list(_OPERATIONS)


def capabilities() -> list[dict]:
    """The descriptor CAPABILITIES — one entry per operation, generated from the registry."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": s.parameters(),
            "result_kind": s.result_kind,
        }
        for s in _OPERATIONS.values()
    ]
