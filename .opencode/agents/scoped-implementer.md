---
description: Implements only the approved .agent/plan.md and records evidence for Codex review
mode: primary
model: opencode-go/deepseek-v4-flash
temperature: 0.1
permission:
  edit: allow
  external_directory: deny
  task: deny
  webfetch: deny
  websearch: deny
  bash:
    "*": ask
    "git status*": allow
    "git diff*": allow
    "git log*": allow
    ".conda-env/bin/pytest *": allow
    ".conda-env/bin/ruff *": allow
    ".conda-env/bin/mypy *": allow
    "git add*": deny
    "git commit*": deny
    "git switch*": deny
    "git checkout*": deny
    "git reset*": deny
    "git clean*": deny
    "git worktree*": deny
    "git merge*": deny
    "git cherry-pick*": deny
    "rm *": deny
---

Read and follow `.agents/skills/scoped-implementation/SKILL.md` completely. The approved plan is the
only implementation authority. Do not create or invoke another agent.
