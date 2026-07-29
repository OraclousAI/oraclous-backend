---
paths:
  - "tests/**"
  - "services/**/tests/**"
---

# Importing not-yet-built intra-repo seams in tests

<!-- #665 stage 4: moved verbatim from root CLAUDE.md §4.1. The rule is mechanically
     enforced (check_test_imports TST001/TST002 in CI + pre-push, and the pre-push
     `pytest --collect-only` check), so the root keeps a one-line pointer; this file
     carries the full rule + rationale for anyone editing tests. -->

**Import not-yet-built intra-repo seams function-locally.** A `[tests]` PR lands tests for a seam (`oraclous_*`) before its `[impl]` exists. If those tests import the not-yet-built seam at *module level*, `pytest` aborts collection (exit 2) for the **whole** run — reddening every open PR's unit/integration/security gate until the `[impl]` lands. Instead, import the seam **inside the test or fixture** (function-locally): the module collects cleanly and the test fails at *runtime* with `ModuleNotFoundError` — RED-by-design, on its own marker only, never masking other suites. Never convert a missing intra-repo seam into a *skip* (`pytest.importorskip("oraclous_…")` or `try/except ImportError → pytest.skip`): a skip turns missing coverage green, and for a `security`-marked test that hides an unverified threat behind a green gate. A missing intra-repo seam must hard-fail, never skip. Enforced by the `check_test_imports` guardrail (TST001/TST002) in CI; the rule self-clears once the `[impl]` lands. The pre-push hook's `pytest --collect-only` check (CLAUDE.md §4.7) catches collection breakage before it ever reaches CI. (security-architect coverage-safety concurrence.)
