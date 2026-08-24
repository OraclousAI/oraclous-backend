"""Curated library operation registry (#488) — the ONLY functions a library-group tool may run.

A request's ``operation`` selects an :class:`OperationSpec` here; there is no free import or
eval, so no arbitrary-code path. The plugin's CAPABILITIES + the INPUT_SCHEMA operation enum are
GENERATED from this registry (:func:`capabilities`, :func:`operation_names`) so the descriptor can
never drift from the callables. Each op declares its args (name → type) for validation.

Operations are grouped, and the group is the unit a member binds (#822). ``library_group.py`` says
it plainly: "a member binds the group token and gets every operation". One flat set of operations
would therefore hand the member that asked for a word counter a compounding function, and the
member doing unit economics an e-mail extractor. So each curated library is its own group with its
own plugin, and every lookup here is parameterised by :data:`TEXT_TOOLS` / :data:`MATH_TOOLS`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from oraclous_capability_registry_service.domain.libraries import math_tools, text_tools

#: Group keys — the curated library a set of operations belongs to. Each maps 1:1 to a plugin whose
#: NAME slugifies to the same token (``Text Tools`` → ``text-tools``), which is what a ref resolves.
TEXT_TOOLS = "text-tools"
MATH_TOOLS = "math-tools"


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


def _by_name(*specs: OperationSpec) -> dict[str, OperationSpec]:
    return {spec.name: spec for spec in specs}


#: group key → its operations, in declaration order. Nothing outside a group is dispatchable.
_GROUPS: dict[str, dict[str, OperationSpec]] = {
    TEXT_TOOLS: _by_name(
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
    ),
    MATH_TOOLS: _by_name(
        # All five are `status` under the same criterion (#822): a computed figure names nothing
        # that exists independently of the call, so there is no source a reader could be pointed
        # at. The number's warrant is its inputs and the arithmetic, which is the whole reason it
        # is worth computing rather than letting a model write it in prose. (Whether the
        # provenance of the INPUTS carries through to a derived figure is #864, and the values
        # here are correct either way.)
        OperationSpec(
            "percentage_change",
            math_tools.percentage_change,
            "The signed percentage change from a start value to an end value.",
            "status",
            {"start": float, "end": float},
        ),
        OperationSpec(
            "compound_growth",
            math_tools.compound_growth,
            "A start value grown by a rate per period, compounded over a number of periods.",
            "status",
            {"start": float, "rate": float, "periods": int},
        ),
        OperationSpec(
            "break_even_units",
            math_tools.break_even_units,
            "The unit volume at which contribution per unit covers the fixed costs.",
            "status",
            {"fixed_costs": float, "price_per_unit": float, "variable_cost_per_unit": float},
        ),
        OperationSpec(
            "payback_period",
            math_tools.payback_period,
            "How many periods of cash flow it takes to recover an initial investment.",
            "status",
            {"initial_investment": float, "cash_flow_per_period": float},
        ),
        OperationSpec(
            "ratio",
            math_tools.ratio,
            "One quantity divided by another, carrying its units into the result.",
            "status",
            {
                "numerator": float,
                "denominator": float,
                "numerator_unit": str,
                "denominator_unit": str,
            },
        ),
    ),
}


def group_names() -> list[str]:
    """Every curated group key (stable order)."""
    return list(_GROUPS)


def get_operation(name: str, group: str = TEXT_TOOLS) -> OperationSpec | None:
    """The curated operation for ``name`` WITHIN ``group``, or ``None`` if the group does not own
    it. An operation of another group is unknown here, which is what keeps the groups separate at
    dispatch rather than only in the advertised capability list."""
    return _GROUPS.get(group, {}).get(name)


def operation_names(group: str = TEXT_TOOLS) -> list[str]:
    """The group's operation names (stable order) — for the INPUT_SCHEMA enum + diagnostics."""
    return list(_GROUPS.get(group, {}))


def capabilities(group: str = TEXT_TOOLS) -> list[dict]:
    """The descriptor CAPABILITIES — one entry per operation in the group, generated from it."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": s.parameters(),
            "result_kind": s.result_kind,
        }
        for s in _GROUPS.get(group, {}).values()
    ]
