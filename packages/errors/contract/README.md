# Gateway error-envelope contract fixture

**Contract:** error envelope · **Canonical shape:** [Interface Contracts §3](https://oraclous.atlassian.net/wiki/spaces/OP/pages/1277953) · **Owner:** solution-architect (security-architect on the threat model)

This directory is the **single source of truth** for the shape of every 4xx/5xx
response the gateway produces and the frontend api-client consumes. It is the
pre-R6 enforcement mechanism for the cross-repo agreement (Cross-cutting
agreement protocol §2.6): a **copied-with-checksum fixture**. At R6 the shape
migrates into the gateway's OpenAPI spec and this fixture is retired.

> Record once, link many. This is the one place the shape lives. The backend and
> frontend test suites consume *these* files — they never re-declare the shape.

## Artifacts (language-neutral, checksummed)

| File | What it is |
| --- | --- |
| `error-envelope.schema.json` | JSON Schema (draft 2020-12). `additionalProperties: false` at every level; closed 13-entry `code` enum; `message`/`requestId`/`retryable` required; `details[]` present **iff** `code == VALIDATION_FAILED`, items `{ field, issue }` only. |
| `error-code-taxonomy.json` | Machine-readable §3 taxonomy (code → HTTP, retryable guidance, when). **Guidance, not schema-enforced**: `retryable` is server-authoritative and decoupled from `code`. |
| `forbidden-substrings.json` | Negative-test pattern set for the §3 "must NEVER appear" sensitive-data rules. Each pattern carries a deliberately-fake `example` (placeholders, never real secrets). |
| `samples/<CODE>.json` | One valid example per error code (13). |
| `CHECKSUMS.sha256` | sha256 of every artifact above. The drift guard. |

## How each side consumes it

- **Backend (this repo):** the reference consumer lives in `tools/contract/` and is
  exercised by `tests/contract/test_error_envelope_fixture.py`. The gateway's
  error-path tests validate every emitted error body against
  `error-envelope.schema.json` and scan it with `forbidden-substrings.json`.
- **Frontend:** copies this directory verbatim and mirrors the checksum
  guard (a TypeScript equivalent of `tools/contract/verify_checksums`), validating
  the api-client against the **same** `error-envelope.schema.json` (e.g. via ajv).
  CI on either side breaks if a copy drifts from the recorded checksums.

## Changing the fixture

The shape is owned by solution-architect via the error-envelope Contract — do not edit the
schema or taxonomy without going through that Contract. After any *approved*
change, regenerate the manifest:

```sh
uv run python -m tools.contract.verify_checksums --write
```

CI verifies it on every PR:

```sh
uv run python -m tools.contract.verify_checksums
```

## Open follow-up

`details[].issue` is constrained to an uppercase machine token
(`^[A-Z][A-Z0-9_]*$`) — enough to forbid reflected raw values — but the **closed
sub-vocabulary** of issue tokens (beyond the §3 example `INVALID_FORMAT`) is not
yet enumerated. Enumerating it is a solution-architect decision on the
error-envelope Contract; until then any uppercase token validates.
