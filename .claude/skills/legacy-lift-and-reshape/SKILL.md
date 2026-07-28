---
name: legacy-lift-and-reshape
description: How to honour a story's lift-tag (Lift, Reshape, Extract, Greenfield) when porting behaviour from the legacy backend at legacy-reference/old-backend. Use when a story brief carries a lift-tag, when deciding whether to start from legacy code or write fresh, or when a brief has no lift-tag but the code has a legacy precursor.
---

# Legacy lift-vs-rewrite

Companion to `CLAUDE.md` §12. The prohibitions in §12.3 (never write to `legacy-reference/`, never switch its branch) stay in `CLAUDE.md` and apply whether or not this skill is loaded.

## This is a migration, not a rewrite

Most existing backend services are production-grade and correctly factored (`auth-service`, `credential-broker-service`) or sprawling-but-salvageable (`knowledge-graph-builder`). The default for backend work is **lift-and-reshape against the four-layer model** — populate the new repo from the legacy service, then refactor under TDD to the target layer and conventions. **Greenfield is the exception, not the default**, applying only to genuinely new surfaces (the application gateway, the metering subsystem) that have no clean legacy precursor.

> The legacy codebase is always at minimum the **behavioural specification** — even when its code is not reusable. New code passes when it does what the legacy did, plus the architectural invariants. "Start from scratch" must be justified, not assumed.

## The lift-vs-rewrite rubric

You do not decide lift-vs-rewrite yourself per file. The verdict is decided once per deliverable in the release page's **Migration source map** (see [09. Releases](https://oraclous.atlassian.net/wiki/spaces/OP/pages/164160) Section 7) and arrives in your story brief as a **lift-tag**: `Lift`, `Reshape`, `Extract`, or `Greenfield`, with the specific legacy source path named. Your job is to honour the tag:

- **Lift** — start from the named legacy code, light refactor only.
- **Reshape** — start from the named legacy logic, refit it to the target layer boundary and conventions (organisation_id, OHM, ReBAC, fail-closed), keep the logic.
- **Extract** — lift the behaviour out of a larger legacy service into its target service.
- **Greenfield** — no usable legacy precursor; write fresh against the architecture. The legacy may still be the spec of what *not* to do.

If a story brief lacks a lift-tag for code that you believe has a legacy precursor, that is a planning gap — flag it to `product-planner` (via the coordinator) rather than silently choosing greenfield.
