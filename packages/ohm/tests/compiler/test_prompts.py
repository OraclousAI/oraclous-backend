"""#750 — the op-drafter prompt must name EVERY typed refine op, mechanically, not by hand-editing
the prose each time an op is added. Derives the op names straight from the ``RefineOp``
discriminated union (the ``op`` Literal on each member) rather than hard-coding the
four-then-six names here, so a SEVENTH op added later fails this test automatically if
``OP_DRAFTER_PROMPT`` is not updated too.
"""

from __future__ import annotations

import typing

import pytest
from oraclous_ohm.compiler.prompts import OP_DRAFTER_PROMPT
from oraclous_ohm.compiler.refine import RefineOp

pytestmark = pytest.mark.unit


def _op_names() -> set[str]:
    # RefineOp = Annotated[Union[...], Field(discriminator="op")] — unwrap the Annotated, then the
    # Union, to reach each typed op model; its "op" field default IS the discriminator literal.
    (union,) = (typing.get_args(RefineOp)[:1]) or (None,)
    assert union is not None, "RefineOp is not an Annotated[...] discriminated union"
    op_models = typing.get_args(union)
    assert op_models, "RefineOp's Union carries no typed op models"
    names = {m.model_fields["op"].default for m in op_models}
    assert all(isinstance(n, str) and n for n in names)
    return names


def test_the_op_drafter_prompt_names_every_typed_op() -> None:
    for name in _op_names():
        assert name in OP_DRAFTER_PROMPT, (
            f"OP_DRAFTER_PROMPT does not name the typed op {name!r} — the drafter cannot emit an"
            " op it is never shown"
        )
