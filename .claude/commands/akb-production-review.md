---
description: Review AKB production-governance docs and hooks
allowed-tools: ["Read", "LS", "Grep", "Bash(git status *)", "Bash(git diff *)", "Bash(nl *)"]
---

# AKB Production Governance Review

Review the current AKB production-governance package.

Scope:

- `AGENTS.md`
- `CLAUDE.md`
- `.claude/settings.json`
- `.claude/hooks/akb_pm_hook.py`
- `docs/prd/`
- `docs/design/`
- `docs/_templates/`

Focus:

- PM usability and lifecycle clarity.
- Correct backend/proxy/storage boundaries.
- Hook usefulness and noise risk.
- Missing safety gates, tests, or doc states.
- AKB vault mirroring rule, especially avoiding `akb-test`.

Return findings first, ordered by severity, with file/line references.
