# WhisperType

This repository is developed through spec-hub scoped sessions.

## SPEC.md files are the law

- Every scope (a folder subtree) carries a `SPEC.md` describing what the
  unit IS and how it MUST behave. Read your scope's SPEC.md — and its
  ancestors up to the root — before changing anything.
- Code that contradicts its SPEC.md is wrong, even if it "works".
- SPEC.md content changes are NEVER written directly with file tools.
  They are proposed through the spec-hub server
  (`POST /api/doc-revisions`) and land only when the user approves the
  diff. See `.claude/skills/integrate-annotations/`.

## Scope discipline

- A session bound to a scope may write ONLY inside it. The guard hook
  (`.claude/hooks/guard-hook.cjs`) enforces this fail-closed; reads are
  never restricted.
- A session with NO bound scope runs floor-only: writes are allowed
  outside the safety floor, but you should bind a scope first — see
  `.claude/skills/scope-bind/` (`/scope-bind`).
- If your task genuinely needs a write outside the scope, ask the user
  for a scope extension. Never work around a guard denial (no shell
  tricks, no relocated files) — denials are logged.

## Pointers

- Root spec: `SPEC.md` (each scope folder has its own)
- Annotation integration skill: `.claude/skills/integrate-annotations/SKILL.md`
- Scope binding skill: `.claude/skills/scope-bind/SKILL.md`
