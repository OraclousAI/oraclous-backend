"""Unit: split a REFUSED embedding credential from an EXHAUSTED one (#1109 ruling 3).

`is_credential_failure` (existing, this package) treats 401/403 (bad key) and 429/quota (exhausted
key) the SAME — both are "the credential is the problem". #1109 needs the retriever to answer these
two differently: a REJECTED key needs a new key (curated ``MODEL_CREDENTIAL_REJECTED``, 422); an
EXHAUSTED one needs the caller to wait or upgrade, not replace anything (the existing
``MODEL_CREDENTIAL_REQUIRED``, 422) — so a NARROWER classifier is needed alongside the existing one,
never replacing it. `knowledge-graph-service`'s write-side refusal still calls only
`is_credential_failure` and must keep collapsing 429 into "credential fault" exactly as before —
pinned here too, so a future edit cannot narrow that shared function by mistake.

`is_credential_rejection` / `CREDENTIAL_REJECTION_MARKERS` do not exist yet — every reference to
them is FUNCTION-LOCAL (`.claude/rules/tests-seam-imports.md`) so this module collects cleanly and
hard-fails RED (ImportError) until the `[impl]` lands.
"""

from __future__ import annotations

from oraclous_embedding import CREDENTIAL_FAULT_MARKERS, is_credential_failure

# Realistic wrapped-exception text — the marker survives only in the stringified message (both the
# provider SDKs and the extraction library wrap the original exception in their own type), matching
# the shape `is_credential_failure`'s own docstring assumes.
_REJECTED_CASES = [
    "AuthenticationError: 401 Unauthorized - invalid API key provided",
    "PermissionDeniedError: 403 Forbidden - you do not have access to this resource",
    "Error code: 401 - invalid_api_key",
]

_EXHAUSTED_CASES = [
    "RateLimitError: 429 Too Many Requests - insufficient_quota",
    "Error code: 429 - you have exceeded your current quota",
    "RateLimitError: key limit exceeded for this organisation",
]

_UNRELATED_CASES = [
    "ValueError: the input batch was empty",
    "ConnectionError: connection refused",
]


def _is_credential_rejection(error: Exception) -> bool:
    from oraclous_embedding.embedder import is_credential_rejection  # noqa: PLC0415

    return is_credential_rejection(error)


def _markers() -> tuple[str, ...]:
    from oraclous_embedding.embedder import CREDENTIAL_REJECTION_MARKERS  # noqa: PLC0415

    return CREDENTIAL_REJECTION_MARKERS


# ── is_credential_rejection: True only for a REFUSED key (401/403) ──────────────────────────────


def test_a_401_is_a_rejection() -> None:
    assert _is_credential_rejection(RuntimeError(_REJECTED_CASES[0])) is True


def test_a_403_is_a_rejection() -> None:
    assert _is_credential_rejection(RuntimeError(_REJECTED_CASES[1])) is True


def test_an_invalid_api_key_message_is_a_rejection() -> None:
    assert _is_credential_rejection(RuntimeError(_REJECTED_CASES[2])) is True


def test_a_429_is_not_a_rejection() -> None:
    assert _is_credential_rejection(RuntimeError(_EXHAUSTED_CASES[0])) is False


def test_insufficient_quota_is_not_a_rejection() -> None:
    assert _is_credential_rejection(RuntimeError(_EXHAUSTED_CASES[1])) is False


def test_key_limit_exceeded_is_not_a_rejection() -> None:
    assert _is_credential_rejection(RuntimeError(_EXHAUSTED_CASES[2])) is False


def test_an_unrelated_error_is_not_a_rejection() -> None:
    for text in _UNRELATED_CASES:
        assert _is_credential_rejection(ValueError(text)) is False


def test_the_marker_list_is_exactly_the_401_403_shaped_markers() -> None:
    """Pinned exactly (#1109 ruling 3) — a marker added here without also widening/narrowing
    `CREDENTIAL_FAULT_MARKERS` in lockstep is precisely the drift #643 exists to prevent."""
    assert set(_markers()) == {
        "401",
        "403",
        "permissiondenied",
        "authenticationerror",
        "invalid_api_key",
    }


def test_every_rejection_marker_is_also_a_credential_fault_marker() -> None:
    """A rejected key is a NARROWER case of a credential fault, never a disjoint one — anything
    `is_credential_rejection` calls True, `is_credential_failure` must also call True."""
    assert set(_markers()) <= set(CREDENTIAL_FAULT_MARKERS)


def test_quota_markers_are_never_rejection_markers() -> None:
    assert not set(_markers()) & {"429", "quota", "insufficient_quota", "key limit exceeded"}


# ── is_credential_failure is UNCHANGED: both rejection and exhaustion still count ───────────────


def test_is_credential_failure_still_true_for_a_401() -> None:
    assert is_credential_failure(RuntimeError(_REJECTED_CASES[0])) is True


def test_is_credential_failure_still_true_for_a_403() -> None:
    assert is_credential_failure(RuntimeError(_REJECTED_CASES[1])) is True


def test_is_credential_failure_still_true_for_a_429() -> None:
    assert is_credential_failure(RuntimeError(_EXHAUSTED_CASES[0])) is True


def test_is_credential_failure_still_true_for_insufficient_quota() -> None:
    assert is_credential_failure(RuntimeError(_EXHAUSTED_CASES[1])) is True


def test_is_credential_failure_still_true_for_key_limit_exceeded() -> None:
    assert is_credential_failure(RuntimeError(_EXHAUSTED_CASES[2])) is True


def test_is_credential_failure_still_false_for_unrelated_errors() -> None:
    for text in _UNRELATED_CASES:
        assert is_credential_failure(ValueError(text)) is False
