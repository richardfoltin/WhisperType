---
name: integrate-annotations
description: Use when a chat message starts with "[SPEC annotations]" or otherwise lists SPEC margin annotations with ids and intents. Resolves the annotations — code fixes for enforce, chat answers for questions, and a turn summary to the hub's doc-writer for rule changes.
---

# Integrate SPEC annotations

The user annotated a SPEC document in the margin; the spec-hub server
composed those annotations into the message you just received. Resolve
every annotation this turn and submit the result. Unresolved sent
annotations are re-delivered with the next user message until integrated
or dismissed.

Server base URL: `$SPEC_HUB_URL` if set, else the content of
`~/.spec-hub/server-url`, else `http://127.0.0.1:9115`.
Call the API with `curl` via Bash. EVERY call must carry the agent token
header `x-spec-hub-agent-token`: use `$SPEC_HUB_AGENT_TOKEN` if set, else
the content of `~/.spec-hub/agent.token`.

## 1. Read the batch

The message header is `[SPEC annotations] <docPath> @ <baseSha>`.
Each annotation block gives `id`, `intent`, the anchored quote, and the
user's note.

Resolve the scope id once:
1. `GET {base}/api/projects` — pick the project whose `repoPath` is your
   working directory.
2. `GET {base}/api/projects/{projectId}/scopes` — pick the scope whose
   `specPath` equals the header's `<docPath>`. That is `scopeId`.

## 2. Handle each annotation by intent

- **rule-change** — the user is changing the spec. Do NOT compose
  document text; instead collect WHAT must change as facts for the
  brief (see step 3). Collect these ids for the submission.
- **enforce** — the spec is right, the code is wrong. Do NOT touch the
  document. Fix the code so it complies (stay inside your write scope),
  then mark the annotation resolved:
  `POST {base}/api/scopes/{scopeId}/annotations/{id}/status` with body
  `{"status":"integrated"}`.
- **question** — answer it in chat. No document edit, no status change;
  the user resolves the annotation after reading your answer. When the
  answer establishes a rule worth recording, add it to the brief as a
  fact.

## 3. Submit the turn summary (rule-change ids only)

NEVER write the SPEC file with Write/Edit and NEVER compose the full
document text — the hub's doc-writer writes the prose, and the file
changes only when the user approves the resulting revision.

Build the turn summary: the turn's doc-relevant facts in Hungarian, one per line —

- the exact behavior that changes (what the program does, not how)
- UI labels verbatim, in quotes
- error cases and edge cases spelled out
- only facts you know from the annotations, the chat, or the program's
  observed behavior — what you cannot know, write as an open question
  line (`KÉRDÉS: …`); the doc-writer surfaces it instead of guessing

Submit:

```
POST {base}/api/doc-drafts
{
  "scopeId":      "<scopeId>",
  "docPath":      "<docPath from the header>",
  "baseSha":      "<baseSha from the header>",
  "brief":        "<the turn summary>",
  "sessionId":    "<your claude session id>",
  "annotationIds": ["<rule-change ids you briefed>"]
}
```

On **409** (stale baseSha): `GET {base}/api/scopes/{scopeId}/spec`, take
the fresh `sha256`, resubmit with it as `baseSha`.

The response carries `draft.id`; you may poll
`GET {base}/api/doc-drafts/{id}` — `done` links the created revision
(pending the user's approval), `failed` carries the error. Do not wait
for the approval itself.

If the batch contains no rule-change annotation, skip the brief — there
is nothing to submit.

## 4. Report

End your reply with one line per annotation id:
`<id> → briefed to the doc-writer (revision pending approval)` /
`→ code fixed + integrated` / `→ answered in chat`.
