"""Capability-registry client (services layer).

The runtime composes the capability-registry over HTTP — it never imports it (four-layer contract).
This client resolves an OHM capability reference to a registry descriptor, materialises a registry
*instance* (the unit the registry executes), configures its credential mappings, and dispatches
operations. Identity is propagated per the trusted-gateway model (ADR-018): the caller passes the
already-built downstream headers (gateway headers + the internal key; dev: a bearer), so
the registry sees the same tenant and its org-scoping holds end-to-end.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
from oraclous_ohm._slug import basic_slug

#: The shape a registry ``error_code`` may take. The registry is trusted, but this token is echoed
#: into a string a MODEL reads, so it is accepted only as a bounded lowercase identifier — never as
#: an arbitrary relay channel for whatever happened to land in that field (#483 / ADR-042).
_CODE_TOKEN = re.compile(r"^[a-z0-9_]{1,64}$")

#: What a registry code means, in words the calling member can act on. Owned HERE, not echoed from
#: the upstream body: #692's member was told only "409" for a credential that no longer existed, so
#: it could not tell "reconnect this tool" from "retry later" and simply repeated the call.
_CODE_MEANINGS = {
    "credential_not_found": (
        "the credential connected to this tool no longer exists — it must be reconnected before "
        "this tool can run"
    ),
    "credential_not_mapped": (
        "no credential is connected to this tool — one must be connected before this tool can run"
    ),
    "pending_approval": "this tool is imported but not yet approved by an organisation admin",
    "no_executor": "this tool has no runnable implementation in this deployment",
    # #1004: the registry now checks the requested operation against the operations the instance's
    # DESCRIPTOR declares. A bare token would tell a member the call failed, not that this tool
    # cannot do that thing at all and another one has to be chosen — which is the whole point of
    # #692's lesson: an unactionable error gets repeated.
    "unsupported_operation": (
        "this tool does not offer the operation the call asked for — use one of the operations "
        "the tool declares, or a different tool"
    ),
}


class RegistryError(Exception):
    """A capability-registry call failed (non-2xx or transport error).

    ``error_code`` is the registry's own typed code when it sent one — the single field allowed
    across the leak boundary, because it comes from a closed vocabulary the registry generates and
    never from customer content. #1111: it also carries a tool execution's curated ``error_type``
    (e.g. ``PROVIDER_QUOTA_EXHAUSTED``) when the registry call completed but the tool failed; only
    one of the two is ever present on a given failure.

    ``transient`` marks a failure a bounded retry may recover — a rate-limited provider, or the
    registry call itself failing in transport (5xx, timeout, reset connection). It mirrors
    ``LLMClientError.transient``, so the loop reads both with the same check.

    ``effect_unknown`` (#1111 review round 1, B1) marks a transient failure whose call MAY ALREADY
    HAVE TAKEN EFFECT: the request was on the wire and the answer never came back, so "it failed"
    and "it worked and the receipt was lost" are indistinguishable from here. ``transient`` alone
    says a retry could succeed; this says a retry could also DUPLICATE — a second row appended, a
    second message sent. The two are separate because most transient failures are refusals that
    provably did nothing (a 429 before dispatch), and those stay freely retryable.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        transient: bool = False,
        effect_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.transient = transient
        self.effect_unknown = effect_unknown


#: #1111 (review round 1, B1): the transport failures that PROVE the request never reached the
#: registry — no connection was ever established (``ConnectError``/``ConnectTimeout``) or none was
#: ever taken from the pool (``PoolTimeout``), so not a byte of the call was sent and nothing
#: downstream can have run. Every OTHER transport failure is ambiguous by construction: a read
#: timeout, a reset mid-call or a protocol error all happen AFTER the request went out, and the
#: connector on the far side may have completed its provider call before the answer was lost.
#: Deliberately a small allow-list rather than a deny-list of the ambiguous ones: a transport
#: exception class this code has never seen must land on the ambiguous side, not the safe one.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _error_code(resp: httpx.Response) -> str | None:
    """The registry's typed ``error_code``, if the body carries one in the accepted token shape."""
    try:
        body = resp.json()
    except ValueError:  # a proxy's HTML error page, an empty body — nothing typed to read
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("error_code")
    return code if isinstance(code, str) and _CODE_TOKEN.match(code) else None


_slug = basic_slug


def capability_slug(name: str) -> str:
    """The registry row's OWN name, slugified — the only trustworthy identity of a capability.

    A manifest picks its ``binding`` alias freely (``retriever``, ``Read``, anything), so the alias
    never identifies which capability a binding actually resolved to. ``resolve_capability`` proves
    the row's name matches the ref slug, so slugging that name is what a caller may key trust on.
    """
    return _slug(name)


def _ref_slug(ref: str) -> str:
    """``core/postgresql-reader@1.0.0`` → ``postgresql-reader`` (drop the prefix + @version).

    Deliberately its own reader, not folded into ``basic_slug``: it answers "which registry ROW
    does this ref's TAIL name" (server-side match), a different question from the plain primitive
    or from ``policy._registry_of``'s HEAD match — see ``test_registry_client.py``'s
    ``test_the_deliberately_different_readers_are_declared_not_unified``-adjacent pin.
    """
    tail = ref.split("/")[-1].split("@")[0]  # drop core/ or org:<id>/ prefix and @version
    return _slug(tail)


class RegistryClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str],
        internal_key: str = "",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # #1130: the registry's ``/internal/v1`` plane is gated on the shared key, and the harness
        # reaches it in EVERY auth mode — ``build_downstream_headers`` only carries the key in
        # gateway/jwt mode, where the caller's identity is header-asserted, so dev mode (a bearer)
        # would otherwise 401 on the internal plane. Mirrors ``BrokerClient``, which has always
        # taken the key explicitly rather than inferring it from the auth mode. A caller-supplied
        # header still wins, so nothing already sending its own key changes.
        key_header = {"X-Internal-Key": internal_key} if internal_key else {}
        # #1130 compare-and-set: the configuration document this client last SAW for an instance,
        # keyed by instance id and filled by ``list_instances``. ``update_configuration`` sends it
        # as the precondition for its replace, so a write built on a read another writer has since
        # superseded is refused instead of silently resurrecting the stale document. Scoped to one
        # client, which the runtime builds per request — exactly the read-modify-write window.
        self._seen_configuration: dict[str, dict[str, Any]] = {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Content-Type": "application/json", **key_header, **headers},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _json(self, resp: httpx.Response, *, dispatched: bool = False) -> dict[str, Any]:
        """The response body, or a ``RegistryError`` classified from its status.

        ``dispatched`` marks a request that can cause an effect OUTSIDE the registry (only
        ``execute``). On such a request a 5xx is ambiguous — the connector's own outbound call may
        have completed before the registry's handler failed, so the answer, not the work, is what
        was lost. A 429 stays unambiguous either way: it is a refusal taken before any work.
        """
        if resp.status_code // 100 != 2:
            # leak-safe: surface the method/path + coarse status, never the upstream body (it may
            # echo customer input/output) — CLAUDE.md §11 / the ADR-042 leak class. #692: the
            # registry's typed error_code DOES cross, plus this service's own words for it — a bare
            # status told the member nothing it could act on, so it repeated the failing call.
            message = f"{resp.request.method} {resp.request.url.path} → {resp.status_code}"
            code = _error_code(resp)
            if code is not None:
                meaning = _CODE_MEANINGS.get(code)
                message = f"{message} ({code})" + (f": {meaning}" if meaning else "")
            # #1111: a 5xx or 429 from the registry itself is a failure a retry may clear.
            transient = resp.status_code >= 500 or resp.status_code == 429
            raise RegistryError(
                message,
                error_code=code,
                transient=transient,
                effect_unknown=dispatched and resp.status_code >= 500,
            )
        return resp.json()

    async def list_tools(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/v1/tools")
        body = await self._json(resp)
        return body.get("capabilities") or []

    async def get_capability(self, capability_id: str) -> dict[str, Any]:
        """Fetch one capability descriptor by id (used to resolve a harness ``manifest_ref``)."""
        return await self._json(await self._client.get(f"/api/v1/capabilities/{capability_id}"))

    async def list_instances(self) -> list[dict[str, Any]]:
        """List the caller-org's tool instances (used to find-or-reuse a harness's instances)."""
        body = await self._json(await self._client.get("/api/v1/instances"))
        rows: list[dict[str, Any]] = body.get("instances") or []
        for row in rows:
            instance_id = row.get("id")
            if instance_id is not None:
                # #1130: remember what each configuration looked like at this read, so a replace
                # built on it can name the read it was built on (see ``_seen_configuration``).
                self._seen_configuration[str(instance_id)] = dict(row.get("configuration") or {})
        return rows

    async def resolve_capability(
        self, ref: str, *, explicit_id: str | None = None
    ) -> dict[str, Any]:
        """Resolve an OHM capability ``ref`` to a registry tool item (carries ``id`` + descriptor).
        A ``config.capability_id`` selects the row, but its resolved name MUST still match the ref's
        name slug — else a benign ref could smuggle in a different (forbidden) capability by id.
        Without an id, match the ref's name slug to a tool's ``name``. Fail-closed."""
        tools = await self.list_tools()
        slug = _ref_slug(ref)
        if explicit_id:
            found = next((t for t in tools if str(t.get("id")) == explicit_id), None)
            if found is None:
                raise RegistryError(f"capability_id {explicit_id} not found in the registry")
            if _slug(found.get("name", "")) != slug:
                raise RegistryError(
                    f"capability_id {explicit_id} resolves to {found.get('name')!r}, "
                    f"not matching ref {ref!r} (slug {slug!r})"
                )
            return found
        found = next((t for t in tools if _slug(t.get("name", "")) == slug), None)
        if found is None:
            raise RegistryError(f"no registry capability matches ref {ref!r} (slug {slug!r})")
        return found

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/api/v1/instances",
            json={"capability_id": capability_id, "name": name, "configuration": configuration},
        )
        return await self._json(resp)

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/api/v1/instances/{instance_id}/configure-credentials",
            json={"credential_mappings": mappings},
        )
        return await self._json(resp)

    async def update_configuration(
        self, instance_id: uuid.UUID, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace the instance's stored configuration (#1130).

        Keeps the registry's stored row coherent with the run that is currently set up on a reused
        instance. It is NOT what a dispatch trusts — ``execute`` carries this run's identity with
        the call itself — so a row another run has since rebound cannot misfile this run's output.

        On the ``/internal/v1`` plane (X-Internal-Key), never the member-facing ``/api/v1`` one:
        the document replaced here carries the producer identity, and the gateway never routes
        ``/internal``, so no human caller can reach it to forge one. A full replace: the caller
        merges onto what it read, exactly as ``configure_credentials`` requires for mappings.

        Conditional on that read: the document ``list_instances`` last returned for this instance
        rides along as the compare-and-set precondition, so a replace whose base another writer
        has already superseded comes back ``409 configuration_conflict`` (fail-closed) instead of
        clobbering it. No prior read → nothing to compare → an unconditional write.
        """
        body: dict[str, Any] = {"configuration": configuration}
        expected = self._seen_configuration.get(str(instance_id))
        if expected is not None:
            body["expected_configuration"] = expected
        resp = await self._client.put(
            f"/internal/v1/instances/{instance_id}/configuration", json=body
        )
        result = await self._json(resp)
        # the write landed, so this is now the document a further replace would be built on
        self._seen_configuration[str(instance_id)] = dict(configuration)
        return result

    async def execute(
        self,
        instance_id: uuid.UUID,
        input_data: dict[str, Any],
        *,
        run_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch one operation on a registry instance.

        ``run_context`` (#1130) is THIS RUN's own identity — producer / graph / working tree — and
        it travels WITH the call, so the registry uses it instead of the instance's stored
        configuration. That row is shared by every run of the same seeded app and is re-read on
        every dispatch, so a second run starting mid-flight used to silently take the first run's
        artifacts with it.

        Stating an identity is a privileged act, so a call that states one goes over the internal
        plane (X-Internal-Key; the gateway never edge-routes ``/internal``) — no human caller can
        reach it to claim to be someone else's run. A dispatch that states nothing asserts nothing
        and keeps the ordinary member-facing path, unchanged.
        """
        body: dict[str, Any] = {"input_data": input_data}
        if run_context:
            path = f"/internal/v1/instances/{instance_id}/execute"
            body["run_context"] = run_context
        else:
            path = f"/api/v1/instances/{instance_id}/execute"
        try:
            resp = await self._client.post(path, json=body)
        except httpx.TransportError as exc:
            # #1111: a timeout or a reset connection used to escape as a raw httpx exception. It is
            # transient, and only the exception class crosses — never its text. Whether the call
            # may ALREADY have run is the caller's whole retry decision, so it is classified here,
            # where the exception type still says which half of the exchange failed.
            raise RegistryError(
                f"POST {path} → {type(exc).__name__}",
                transient=True,
                effect_unknown=not isinstance(exc, _NEVER_SENT),
            ) from exc
        return await self._json(resp, dispatched=True)
