# Citation contract fixture

**Contract:** resolvable citation on a tool result · **Canonical shape:** `oraclous-knowledge`
[`flows/interface-contracts.md` §CITE](https://github.com/OraclousAI/oraclous-knowledge/blob/main/flows/interface-contracts.md)
(rev3) · **Tracking Contract:** [oraclous-backend#735](https://github.com/OraclousAI/oraclous-backend/issues/735)
· **Owner:** solution-architect

This directory is the **single source of truth** for the shape of the `citation` the platform mints
for a knowledge record and returns on every retrieval hit, and that the console renders. It is the
enforcement mechanism for a cross-repo agreement (Cross-cutting agreement protocol §2.6): a
**copied-with-checksum fixture**, the same pattern as `packages/errors/contract/`.

> Record once, link many. This is the one place the shape lives. The backend and frontend test
> suites consume *these* files — they never re-declare the shape.

## The rule this fixture exists to enforce

**The platform mints every citation in code. A model can reference one; it can never author one.**

`citation_id` is deterministic over exactly three fields:

```
citation_id = "cit_" + sha256(source_system ‖ 0x00 ‖ source_id ‖ 0x00 ‖ revision)[:32]
```

`‖ 0x00 ‖` is a **NUL byte**, not a literal string. Determinism buys three things at once:
re-reading the same revision yields the same id (an idempotent refresh); a new revision yields a
different id, which makes supersession computable without a supersession table; and an id a model
invents is not in the run's served set, so it fails the answer-time gate.

## Artifacts (language-neutral, checksummed)

| File | What it is |
| --- | --- |
| `citation.schema.json` | JSON Schema (draft 2020-12). Root is `Citation`; `SourceRef` and `Author` are in `$defs`. `additionalProperties: false` at every level. |
| `samples/*.json` | One valid `Citation` per rendering case the console must handle (4). |
| `CHECKSUMS.sha256` | sha256 of every artifact above. The drift guard. |

### The four samples

| Sample | The case it pins |
| --- | --- |
| `github.json` | Full identity — document id, a source-native version (`blob_sha`), an openable link, an author. |
| `web.json` | A live web / MCP read — a link and a read time, but no source-native version, so `revision` is a **content hash**. Not a null. |
| `web-no-author.json` | The same shape with `author: null`. Many sources expose no author, and the console must render that case without degrading the rest. |
| `upload.json` | A direct file upload — `source_id` is the ingest job id, `revision` is the content hash, and `url` is **null** because no route serves an uploaded document back yet. |

## Two things about the shape that surprise people

**There is no `locator`.** A citation resolves to a **document version** and nothing finer — no
chunk index, no line range, no page, no heading anchor (rev3). One consequence follows directly:
every chunk of one ingested document carries the **same** `citation_id`. That is intended for v1,
not a collision to design around. A citation answers "which document, at which version", and the
reader opens it. Sub-document precision, when it arrives, is an additional field and never a change
to identity — so re-chunking a document never invalidates a citation already stored in a published
answer.

**`revision` is never null, on any path.** The platform always holds the content at the moment it
mints, so a source that exposes no version of its own still gets one: the SHA-256 of that content,
with `revision_kind: "content_hash"`. `revision_kind` is what tells a source-native version from
our fallback. The accepted cost is that a trivial change to a web page counts as a new revision.

## How each side consumes it

- **Backend (this repo):** `oraclous_citation` models validate against `citation.schema.json`, and
  `tests/contract/test_citation_fixture.py` verifies these bytes are internally consistent —
  including that every sample's `citation_id` is the real digest of its own three identity fields.
- **Frontend:** copies this directory verbatim and mirrors the checksum guard, validating the
  console's citation rendering against the **same** `citation.schema.json` (e.g. via ajv). CI on
  either side breaks if a copy drifts from the recorded checksums.

## Changing the fixture

The shape is owned by `solution-architect` via Contract #735 — do not edit the schema or the samples
without going through that Contract. After any *approved* change, regenerate the manifest:

```sh
uv run python - <<'PY'
import hashlib, pathlib
d = pathlib.Path("packages/citation/contract")
paths = sorted([d / "citation.schema.json", *(d / "samples").glob("*.json")],
               key=lambda p: p.relative_to(d).as_posix())
(d / "CHECKSUMS.sha256").write_text(
    "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(d).as_posix()}\n"
            for p in paths), encoding="utf-8")
PY
```

## Two values in the samples that are placeholders, not decisions

- **`source_system: "web-research"`** names the shipped `core/web-research` capability. Minting at
  the live-web / MCP tool boundary is [#746](https://github.com/OraclousAI/oraclous-backend/issues/746);
  if that issue registers a different slug, the two `web` samples follow it. Nothing else in the
  fixture depends on the value — `source_system` is deliberately not a closed enum.
- **`url: null` on `upload.json`** holds only until
  [#745](https://github.com/OraclousAI/oraclous-backend/issues/745) serves an uploaded document
  back. It is the one source the platform itself cannot currently resolve.
