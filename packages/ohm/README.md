# oraclous-ohm

Shared OHM (Oraclous Harness Manifest) schema library: manifest types, parsing, atomic reference resolution, signature verification, and canonicalization. A pure pydantic/validator package with no internal coupling — consumed by `harness-runtime-service` (runs harnesses), and (R7) the Importer, the Compiler harness, and execution-engine orchestration. Promoted out of `harness-runtime-service/domain/ohm` (was deleted as dead in PR #299 when it had 0 importers; re-shared for the R7 team-of-agents program).
