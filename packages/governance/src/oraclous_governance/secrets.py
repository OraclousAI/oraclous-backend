"""Fail-closed secret resolution (WP-1, Structured Threat Catalogue T6 / ADR-008).

Several substrate services historically baked **publicly-known** security defaults straight into
their settings (``INTERNAL_SERVICE_KEY="dev-internal-key"``, ``JWT_SECRET="change-me-…"``, an
in-source OAuth dev key). Those defaults are convenient for the local docker stack but are a
silent production footgun: a deploy that forgets to inject the real secret boots anyway, with a key
an attacker can read off GitHub.

This module makes the failure **loud in production and silent in dev**, gated by a single env var:

``RUN_MODE`` — ``"dev"`` (default) or ``"prod"``.

* ``RUN_MODE`` **unset or ``dev``** → :func:`require_secret` returns the supplied ``dev_default``
  when the env value is missing/empty. This is the critical behaviour for the running local docker
  stack, which may not set ``RUN_MODE`` at all: it must keep booting key-free with the dev defaults.
* ``RUN_MODE=prod`` → a missing/empty secret raises :class:`MissingSecretError` at construction
  (fail closed), exactly like credential-broker's no-default pydantic settings.

An **empty-string** env value is treated as *missing* (not as an explicit override): this closes the
``os.environ.get(name) or _DEV_KEY`` fallback bug, where an operator who set ``OAUTH_ENC_KEY=`` in a
prod manifest silently fell back to the in-source dev key.
"""

from __future__ import annotations

import os

RUN_MODE_ENV = "RUN_MODE"
PROD = "prod"
DEV = "dev"


class MissingSecretError(RuntimeError):
    """A required security secret was missing/empty while ``RUN_MODE=prod`` (fail closed)."""


def run_mode() -> str:
    """The current run mode (lower-cased). Unset/empty → ``"dev"`` (local stack keeps booting)."""
    return (os.environ.get(RUN_MODE_ENV) or DEV).strip().lower()


def is_prod() -> bool:
    """Whether the process is running in the fail-closed production mode."""
    return run_mode() == PROD


def require_secret(name: str, *, dev_default: str) -> str:
    """Resolve a required security secret from the environment, failing closed in prod.

    Returns the value of env var ``name``. An **empty string is treated as missing**. When missing:
    * ``RUN_MODE != prod`` (the default/dev path) → return ``dev_default`` so dev/CI/local-docker
      keep booting key-free.
    * ``RUN_MODE == prod`` → raise :class:`MissingSecretError` (fail closed at construction).
    """
    value = os.environ.get(name)
    if value:  # non-empty → an explicit, operator-supplied value always wins
        return value
    if is_prod():
        raise MissingSecretError(
            f"{name} is required when RUN_MODE=prod but was missing or empty; "
            "inject it from secret management (no publicly-known default is allowed in prod)."
        )
    return dev_default
