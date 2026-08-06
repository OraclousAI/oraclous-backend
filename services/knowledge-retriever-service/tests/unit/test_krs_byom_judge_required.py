"""#724 seventh site — the KRS evaluation judge must require BYOM, not fall back to a platform key.

KRS already has the correct mechanism: ``resolve_byom_judge`` resolves a per-request credential
from the broker, org-scoped, and fails closed (ADR-037). But it is **opt-in**.
``POST /internal/evaluate`` uses it only when the caller supplies ``judge_credential_id``;
otherwise it falls through to what ``routes/internal_routes.py:86`` calls "the operator-key
fallback: the lifespan singleton", built by ``make_judge(settings)`` from ``KRS_OPENAI_API_KEY``.

That judge then reads the customer's ``target_output`` and grades it. Same class as the six KGS
sites: a model call over customer data on a key the customer does not own and did not choose.

Compose makes the two one key. ``deploy/docker-compose.yml:421`` resolves
``KRS_OPENAI_API_KEY`` to ``${OPENROUTER_API_KEY}``, the same value line 63 gives the KGS extractor,
and the comment says so: "reuses the OpenRouter key (same provider as the KGS extractor)".

Decision on #724: the platform key is removed rather than kept as a fallback, so the judge resolves
BYOM or refuses. KRS's absence posture is already right (a typed 422, never fabricated scores); what
changes is that the platform key stops being an alternative to BYOM.

RED until the [impl] drops the platform-key judge factory and the config field.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]


def test_settings_carries_no_platform_model_key() -> None:
    """#724 AC1, KRS half. While the field exists the evaluate route has something to fall back
    to, and BYOM stays optional."""
    from oraclous_knowledge_retriever_service.core.config import Settings  # noqa: PLC0415

    assert not hasattr(Settings(), "openai_api_key"), (
        "KRS_OPENAI_API_KEY must be removed outright: the evaluate route falls back to a judge "
        "built from it, which then reads the caller's target_output on the platform's key (#724)"
    )


def test_no_platform_key_judge_factory_remains() -> None:
    """#724 AC4, KRS half. ``make_judge(settings)`` is the constructor of the fallback judge. Its
    absence is what makes BYOM the only path, rather than the preferred one."""
    from oraclous_knowledge_retriever_service.services import eval_judge  # noqa: PLC0415

    assert not hasattr(eval_judge, "make_judge"), (
        "the platform-key judge factory must go: while it exists, an evaluate call with no "
        "judge_credential_id grades customer content on the operator's key (#724)"
    )


def test_the_byom_resolver_is_still_the_supported_path() -> None:
    """Guard against over-deletion. The [impl] removes the fallback, not the mechanism: the
    broker-resolved, org-scoped, fail-closed resolver (ADR-037) is what everything routes through
    afterwards, so it must survive."""
    from oraclous_knowledge_retriever_service.services import eval_judge  # noqa: PLC0415

    assert hasattr(eval_judge, "resolve_byom_judge")
