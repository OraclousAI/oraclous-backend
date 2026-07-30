---
paths:
  - "services/**"
  - "packages/**"
---

# Tenancy: org-scoping and the RLS backstop (as built)

<!-- #665 stage 4: moved verbatim from root CLAUDE.md §3.3 (the as-built RLS paragraph).
     The invariant itself (organisation_id on every storage operation; app-layer scoping
     primary, RLS backstop second) stays in the root; this file carries the operational
     detail that only matters when editing service or package code. -->

The root invariant (CLAUDE.md §3.3): every Substrate write carries `organisation_id`, every read is parameterised by it, no exceptions.

Tenant-scoped substrate access goes through the `oraclous_substrate.access` seam (the `scoped_*` functions), which sources `organisation_id` from the authenticated org-context and fails closed when none is bound. **App-layer org-scoping (every read/write parameterised by `organisation_id`) is the primary, live tenancy control.** The Postgres **RLS backstop** described in ADR-012 §2 is **now realized across all 7 Postgres-backed services** — every service except `knowledge-retriever-service`, whose only persistence is a Redis query cache (epic `oraclous-backend#353` closed, ADR-030 — 2026-06-17): each connects at runtime as the `NOSUPERUSER`/`NOBYPASSRLS` `oraclous_app` role, with `ENABLE`+`FORCE ROW LEVEL SECURITY` + an org-isolation policy on every org-scoped table (27 forced-RLS tables). The org-GUC (`app.current_organisation_id`) is bound transaction-locally per request by the substrate `install_org_guc_guard`/`org_scope` seam (`oraclous_substrate.access_async`); the dev `oraclous_app` password must be overridden with a managed credential in prod. So RLS is the realized defense-in-depth **second** line — but **app-layer `WHERE organisation_id = …` remains the primary control**: a request-path DB op that runs *without* binding the org (no `org_scope`/`use_organisation_context`) hits an empty GUC and fail-closes (zero rows / 42501) under `oraclous_app` — bind the org on every request-path op (the `check_rls_request_binding` guardrail enforces a service-level presence check; the `check_service_dep_imports` guardrail enforces that a service declares the packages it imports).

Never connect to Postgres as a superuser or `BYPASSRLS` role, and never bind the org-GUC at session scope on a pooled connection — both silently void the RLS backstop (ADR-012).

References: ADR-006 (Organisation as Outermost Tenancy Unit), ADR-012 (Substrate Tenancy Enforcement Seam and RLS Backstop Preconditions — RLS now realized, see its as-built note), ADR-030 (Realize the Postgres RLS Backstop; #353 closed). ADR index: `docs/knowledge-links.md`.
