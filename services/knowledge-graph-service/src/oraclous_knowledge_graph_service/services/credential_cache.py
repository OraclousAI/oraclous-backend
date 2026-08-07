"""Short-lived, in-process cache for resolved model credentials (#724).

Named on purpose. Resolution costs two broker round-trips on a fresh connection, and the call sites
are not rare: a single agent turn does a memory ``context`` read plus two ``search`` calls plus a
``store``, which is eight round-trips before any vector is computed. A cache is therefore
load-bearing rather than an optimisation, and a load-bearing cache that lives inside a helper where
nobody can see its TTL or flush it is worse than no cache at all.

What it holds and for how long:

* ``(organisation_id, credential_id) -> ModelCredential`` for :data:`TTL_SECONDS`.
* ``(organisation_id, purpose) -> credential_id`` for the same, so the org-default lookup is not
  repeated either.

Deliberate constraints, each one a security property rather than a tuning knob:

* **Process-local.** Never Redis, never disk. A decrypted BYOM key must not outlive the process or
  become visible to another tenant's process (ADR-008 §3.6 operator separation).
* **Short and fixed.** Sixty seconds bounds the window in which a REVOKED credential still works.
  The broker deletes a credential row on revoke and stored credentials carry no status column, so
  there is no cheap validity probe: the TTL *is* the revocation window. It is deliberately not a
  setting, because the failure mode of someone raising it to an hour is a revoked key that keeps
  reading customer data for an hour.
* **Bounded.** An LRU cap, so a many-tenant process cannot grow this without limit.
* **Flushed on rejection.** :func:`invalidate` is called when a provider answers 401/403, which
  makes rotation self-healing within one call rather than one TTL.

Values are stored, never clients. Caching a built client would extend a key's lifetime for no
benefit: ``OpenAIEmbedder.embed`` constructs its own client per call anyway, so the round-trips are
the cost worth removing, not the construction.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only — importing at runtime would be circular, since the resolver
    # imports this module. The cache stores values it is handed; it never constructs one.
    from oraclous_knowledge_graph_service.services.model_credential import ModelCredential

#: The revocation window. See the module docstring for why this is a constant and not config.
TTL_SECONDS = 60.0

#: Cap on distinct cached entries, so a busy multi-tenant process stays bounded.
MAX_ENTRIES = 512

_credentials: OrderedDict[tuple[str, str], tuple[float, ModelCredential]] = OrderedDict()
_defaults: OrderedDict[tuple[str, str], tuple[float, str | None]] = OrderedDict()


def _get(store: OrderedDict, key: tuple[str, str], now: float):  # noqa: ANN001, ANN202
    entry = store.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if now >= expires_at:
        store.pop(key, None)
        return None
    store.move_to_end(key)
    return (value,)  # wrapped so a cached None is distinguishable from a miss


def _put(store: OrderedDict, key: tuple[str, str], value: object, now: float) -> None:
    store[key] = (now + TTL_SECONDS, value)
    store.move_to_end(key)
    while len(store) > MAX_ENTRIES:
        store.popitem(last=False)


def get_credential(organisation_id: uuid.UUID, credential_id: str) -> ModelCredential | None:
    """The cached credential, or None on a miss or an expired entry."""
    hit = _get(_credentials, (str(organisation_id), credential_id), time.monotonic())
    return hit[0] if hit else None


def put_credential(organisation_id: uuid.UUID, credential: ModelCredential) -> None:
    _put(
        _credentials,
        (str(organisation_id), credential.credential_id),
        credential,
        time.monotonic(),
    )


def get_default_id(organisation_id: uuid.UUID, purpose: str) -> tuple[str | None] | None:
    """``(credential_id_or_None,)`` on a hit, or None on a miss.

    Wrapped because "this org has designated nothing" is a real answer worth caching: without it,
    every call by an unconfigured org pays a round-trip to be told the same thing.
    """
    return _get(_defaults, (str(organisation_id), purpose), time.monotonic())


def put_default_id(organisation_id: uuid.UUID, purpose: str, credential_id: str | None) -> None:
    _put(_defaults, (str(organisation_id), purpose), credential_id, time.monotonic())


def invalidate(organisation_id: uuid.UUID, credential_id: str | None = None) -> None:
    """Drop cached entries for an org, or for one credential of that org.

    Called when a provider rejects a key (401/403) so a rotation heals within one call, and by any
    caller that has reason to believe a designation changed.
    """
    org = str(organisation_id)
    if credential_id is not None:
        _credentials.pop((org, credential_id), None)
    else:
        for key in [k for k in _credentials if k[0] == org]:
            _credentials.pop(key, None)
    for key in [k for k in _defaults if k[0] == org]:
        _defaults.pop(key, None)


def clear() -> None:
    """Drop everything. For tests and for a process that wants a clean slate."""
    _credentials.clear()
    _defaults.clear()
