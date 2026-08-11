# WhisperType

<!-- FIXED FILE — installed by spec-hub registration; never edited.
     Every evolving rule lives in the SPEC documents, not here. -->

This repository is developed through spec-hub scoped sessions.

## SPEC.md files are the law

- Every scope (a folder subtree) carries a `SPEC.md` describing what the
  unit does. Read your scope's SPEC.md — and its ancestors up to the
  root — before changing anything.
- Code that contradicts its SPEC.md is wrong, even if it "works".
- SPEC.md content changes are NEVER written directly with file tools.
  They are proposed through the spec-hub server
  (`POST /api/doc-revisions`) and land only when the user approves the
  diff. See `.claude/skills/integrate-annotations/`.
- How SPEC text must be written is defined in the ROOT SPEC's
  "Dokumentáció-írás" chapter — it arrives in your orientation; follow
  it in every SPEC text you write or propose.

## Scope discipline

- A session bound to a scope may write ONLY inside it. The guard hook
  (`.claude/hooks/guard-hook.cjs`) enforces this; reads are never
  restricted, and what the guard cannot verify it allows with an audit
  event — but real file writes outside the scope are denied.
- A session with NO bound scope runs floor-only: bind a scope first —
  see `.claude/skills/scope-bind/` (`/scope-bind`).
- If your task genuinely needs a write outside the scope, ask the user
  for a scope extension. Never work around a guard denial (no shell
  tricks, no relocated files) — denials are logged.
- Temporary files (probe scripts, one-off dumps, scratch output) go
  ONLY into the repo's `.tmp/` directory. It is gitignored and writable
  from every scope; anything dropped elsewhere is swept into the hub's
  automatic commits and pollutes the codemap.

## Pointers

- Root spec: `SPEC.md` (each scope folder has its own)
- Annotation integration skill: `.claude/skills/integrate-annotations/SKILL.md`
- Scope binding skill: `.claude/skills/scope-bind/SKILL.md`
- Codemap on demand: your orientation carries a map focused on your
  scope; for a map detailed on ANY other area, call
  `GET {hub}/api/projects/{projectId}/codemap?focus=<dir>` with the
  `x-spec-hub-agent-token` header (hub URL and token as in the
  scope-bind skill; projectId via `GET /api/projects`).
