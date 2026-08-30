---
name: resume-after-context-reset
description: Recovery procedure for picking up mid-task work in oraclous-backend after losing prior session context. Use when resuming an in-progress issue with no memory of what was already done, or when the handoff trail on an issue looks broken or contradictory.
---

# Resuming after a context reset

If you are resuming work mid-task and have lost prior session context:

1. Read `CLAUDE.md`.
2. Read your own skill page from [Agent Skills Catalogue](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753852) *(read-only mirror)*.
3. Read **`FUCK_CLAUDE_FUCK_PAPERCLIP.md`** — the canonical rules; where it and `CLAUDE.md` diverge, that file wins.
4. Look at GitHub: the issue assigned to you that is in progress is yours.
5. Read that issue's comments; the last `[agent:NAME]` comment with an action trailer tells you where you are.
6. Read the linked tests PR (if at Implementation stage) or the brief (if at Tests Authoring).
7. Before any push, run the mandatory local pre-push gate (`CLAUDE.md` §4.7).
8. Continue.

If the trail is broken or contradictory, escalate to the human via the `escalate_to_human` operation in `CLAUDE.md` §5.
