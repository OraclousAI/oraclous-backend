# oraclous-errors

Shared error taxonomy and the **gateway error-envelope emitter**.

Two parts live here:

- **`contract/`** — the cross-repo source of truth for the error envelope: the JSON
  Schema (`error-envelope.schema.json`), the code taxonomy (`error-code-taxonomy.json`),
  the sensitive-data negative-test set (`forbidden-substrings.json`), one valid sample
  per code (`samples/`), and a `CHECKSUMS.sha256` drift guard. The frontend copies this
  directory verbatim. Edits go through the error-envelope Contract (solution-architect owns the shape);
  regenerate checksums with `uv run python -m tools.contract.verify_checksums --write`.
- **`src/oraclous_errors/`** — the Python emitter every backend service uses to produce a
  conformant envelope without re-declaring the shape: `ErrorCode` (the closed 13-value
  taxonomy), `build_envelope(...)`, `new_request_id()`, `status_to_code(...)` (upstream
  normalisation), and the per-code policy (HTTP status, default `retryable`, curated
  message). Pure stdlib — no web-framework or pydantic dependency; it returns plain dicts.

The emitter values are duplicated in Python (the `contract/` dir is not shipped in the
wheel); `tests/contract/test_error_emitter.py` asserts the Python never drifts from the
contract and that every built envelope is schema-valid and leak-free.
