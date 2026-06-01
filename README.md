# oraclous-backend

The Python monorepo for the Oraclous Platform: the substrate, capability registry, harness runtime, execution engine, application gateway, and the supporting services that back them.

> The working contract for any agent (or human) in this repo is [`CLAUDE.md`](./CLAUDE.md) — read it first. Architecture and releases are canonical in Confluence (space `OP`).

## Layout

A [uv](https://docs.astral.sh/uv/)-managed workspace. Shared libraries live in `packages/`, one directory per service lives in `services/`, and cross-service tests live in `tests/`. The full target layout is defined in `CLAUDE.md` §6; the `packages/`, `services/`, and `tests/` trees are created by R0.5 story 0e.

    packages/   # shared libraries (ohm, substrate, governance, provenance, rebac, telemetry, errors)
    services/   # one directory per service (auth, credential-broker, knowledge-graph, ...)
    tests/      # cross-service integration / security / isolation suites
    deploy/     # docker-compose, helm, observability (R0.5 story 0c)

## Development

Requires Python 3.12 (see `.python-version`) and uv.

```bash
uv sync --all-packages         # venv + all workspace members (editable) + dev tooling
uv run ruff check .            # lint
uv run ruff format --check .   # formatting check
uv run pytest                  # run the test suite
```

Test markers (`unit`, `integration`, `security`, `isolation`, `byom`, `organization_isolation`) are declared in `pytest.ini`; select with `uv run pytest -m <marker>`.

## Git hooks setup

Commit-message policy is enforced by a hook in `.githooks/`. Activate once after cloning:

```bash
git config core.hooksPath .githooks
```

The hook rejects forbidden attribution trailers (`Co-Authored-By`, `Generated with/by`, `claude.ai`, `anthropic`, `paperclip.ing`, `🤖`). See `CLAUDE.md` §4.5 for the expected commit format.

## Contributing

All work is test-driven and flows through PRs against protected `main` per `CLAUDE.md` §4. PR prefixes: `[tests]`, `[impl]`, `[impl-infra]`, `[regression]`, `[docs]`, `[chore]`.
