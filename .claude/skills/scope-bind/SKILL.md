---
name: scope-bind
description: Use when the user says "/scope-bind", "/scope", "bind a scope", or when the guard hook hints that no scope is bound (floor-only mode). Discovers this project's scopes on the spec-hub server and binds the current session to one, enabling scoped writes and annotation delivery.
---

# Bind this session to a scope

Your session is registered with the spec-hub server but may have no bound
scope (floor-only mode: the guard hook only enforces the safety floor and
hints "bind a scope"). Binding a scope declares your write area, turns on
scope enforcement and routes SPEC annotation deliveries to you.

Server base URL: `$SPEC_HUB_URL` if set, else the content of
`~/.spec-hub/server-url`, else `http://127.0.0.1:9115`.
Call the API with `curl` via Bash. EVERY call must carry the agent token
header `x-spec-hub-agent-token`: use `$SPEC_HUB_AGENT_TOKEN` if set, else
the content of `~/.spec-hub/agent.token`.

Your claude session id is the `session_id` your hooks receive; if you do
not know it, ask the user or check the hub's Sessions tab.

## 1. Discover the project and its scopes

1. `GET {base}/api/projects` — pick the project whose `repoPath` is your
   working directory (or an ancestor of it).
2. `GET {base}/api/projects/{projectId}/scopes` — list its scopes. Each
   scope row has a repo-relative `path` (`.` is the root scope).

If the user named a scope, pick that one. Otherwise pick the deepest
scope whose `path` contains the files your task will change, and confirm
the choice with the user when it is ambiguous.

## 2. Bind

```
POST {base}/api/sessions/{claudeSessionId}/scope
{ "path": "<scope path, e.g. \"src/billing\" or \".\">" }
```

The response returns the bound `scope` and your `writeGlobs`. A 404 with
"unknown session" means the session is not registered yet — start it via
the SessionStart hook (or send any user message so the prompt-submit hook
self-heals the registration), then retry. A 404 with "no such scope"
means the `path` does not exist on this project — re-check step 1.

## 3. Confirm

Tell the user which scope you bound and what your write globs are. The
binding takes effect at your next guard-served lookup — from now on
writes outside the scope are denied (ask for a scope extension when you
genuinely need one; never work around a denial).
