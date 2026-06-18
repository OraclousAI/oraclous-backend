"""Cross-service JWT issuer/audience contract (#356).

The single source of truth for the ``iss`` and ``aud`` claims every platform JWT carries. The
auth-service (the primary issuer) STAMPS these on every token it mints; every verifier (gateway,
credential-broker, capability-registry, knowledge-graph, knowledge-retriever, harness-runtime,
execution-engine) REQUIRES them and rejects a token that is missing them or carries a wrong value.

Why a shared module rather than per-service constants: the issuer and all verifiers must agree on
the exact same strings or a live flow breaks (the gateway mints with ``aud=X`` and a downstream
service that expected ``aud=Y`` would 401 every request). Sourcing both sides from one place — keyed
on shared env vars with sensible dev defaults — makes drift impossible. Every service already
depends on ``oraclous-governance``, so this needs no new dependency.

Unlike ``JWT_SECRET`` these are NOT secrets — they are public, well-known identifiers — so they use
a plain env read with a dev default (always present), not the fail-closed :func:`require_secret`.

Enforcement (the point of #356, not merely emitting the claims): :data:`JWT_REQUIRED_OPTIONS` is the
python-jose ``options`` dict that makes ``aud``/``iss``/``exp`` REQUIRED and verified. python-jose's
options API uses individual ``require_<claim>``/``verify_<claim>`` flags (NOT PyJWT's
``{"require": [...]}`` list); passing ``audience=`` alone does NOT reject a token that omits the
``aud`` claim entirely — ``require_aud`` is what forces presence. Every verifier decodes with::

    jwt.decode(
        token,
        secret,
        algorithms=[alg],
        audience=jwt_audience(),
        issuer=jwt_issuer(),
        options=JWT_REQUIRED_OPTIONS,
    )

so a token lacking ``aud``/``iss``/``exp`` → ``JWTError`` (→ 401); a wrong ``aud``/``iss`` →
``JWTClaimsError`` (→ 401); only a correctly-stamped token is accepted.
"""

from __future__ import annotations

import os

# Public, well-known identifiers (not secrets). Shared env vars consumed by mint + every verify so
# the issuer and all verifiers agree without drift. Dev defaults are always applied when unset.
JWT_ISSUER_ENV = "JWT_ISSUER"
JWT_AUDIENCE_ENV = "JWT_AUDIENCE"

# The issuer identity stamped by (and trusted from) the auth-service — the platform's primary
# token issuer.
DEFAULT_JWT_ISSUER = "oraclous-auth"
# The audience every platform service shares; a token minted for the platform names this so any
# verifier accepts it.
DEFAULT_JWT_AUDIENCE = "oraclous-platform"

# python-jose decode options that turn aud/iss/exp from "verified IF present" into REQUIRED.
# Combined with ``audience=``/``issuer=`` on the decode call this REJECTS a token that is missing or
# carries a wrong aud/iss (and a token with no expiry). See the module docstring for the
# PyJWT-vs-jose options difference.
JWT_REQUIRED_OPTIONS: dict[str, bool] = {
    "require_aud": True,
    "require_iss": True,
    "require_exp": True,
    "verify_aud": True,
    "verify_iss": True,
}


def jwt_issuer() -> str:
    """The trusted token issuer (``iss``). Env ``JWT_ISSUER`` → default ``oraclous-auth``.

    Used by the auth-service mint (stamps it) and by every verifier (``issuer=`` on decode). An
    empty env value is treated as unset so a blank override never silently produces an empty
    ``iss``."""
    return os.environ.get(JWT_ISSUER_ENV) or DEFAULT_JWT_ISSUER


def jwt_audience() -> str:
    """The intended token audience (``aud``) every platform service shares. Env ``JWT_AUDIENCE`` →
    default ``oraclous-platform``.

    Used by the auth-service mint (stamps it) and by every verifier (``audience=`` on decode). An
    empty env value is treated as unset so a blank override never silently produces an empty
    ``aud``."""
    return os.environ.get(JWT_AUDIENCE_ENV) or DEFAULT_JWT_AUDIENCE
