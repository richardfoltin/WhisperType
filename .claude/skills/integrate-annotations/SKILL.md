---
name: integrate-annotations
description: Use when a chat message starts with "[SPEC annotations]" or otherwise lists SPEC margin annotations with ids and intents. Integrates the annotations into the scope's SPEC document and submits a doc revision to the spec-hub server for user approval.
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

- **rule-change** — the user is changing the spec. Rewrite the anchored
  rule text according to the note. Collect these ids for the revision.
- **enforce** — the spec is right, the code is wrong. Do NOT edit the
  document. Fix the code so it complies (stay inside your write scope),
  then mark the annotation resolved:
  `POST {base}/api/scopes/{scopeId}/annotations/{id}/status` with body
  `{"status":"integrated"}`.
- **question** — answer it in chat. No document edit, no status change;
  the user resolves the annotation after reading your answer.

## 3. Submit the revision (rule-change ids only)

NEVER write the SPEC file with Write/Edit — the file changes only when
the user approves the revision; a direct edit desyncs the CAS baseline.

Build the FULL updated document text (current content with your
rule-change edits applied) and submit:

```
POST {base}/api/doc-revisions
{
  "scopeId":      "<scopeId>",
  "docPath":      "<docPath from the header>",
  "baseSha":      "<baseSha from the header>",
  "proposedText": "<full updated document>",
  "sessionId":    "<your claude session id>",
  "annotationIds": ["<rule-change ids you integrated>"]
}
```

On **409** (stale baseSha): `GET {base}/api/scopes/{scopeId}/spec`, take
the fresh `content` + `sha256`, re-apply your edits, resubmit with the
new sha as `baseSha`.

If the batch contains no rule-change annotation, skip the revision —
there is nothing to submit.

## 4. Report

End your reply with one line per annotation id:
`<id> → submitted in revision (pending approval)` / `→ code fixed +
integrated` / `→ answered in chat`.
