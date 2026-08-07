#!/usr/bin/env node
// spec-hub scaffold prompt-submit v1 (2026-08-06) — installed by spec-hub project registration.
// Managed file: do not edit by hand; upgrades re-apply via POST /api/projects/{id}/scaffold.
/**
 * spec-hub UserPromptSubmit hook (plan §1.2 / §8.7) — the EXTERNAL
 * sessions' delivery window: sent annotations, queued feedback and
 * agent-apply instructions ride in as additionalContext with each user
 * message. The same call stamps session activity for follow mode (§8.8).
 *
 *   stdin:  one JSON object { session_id, cwd, ... }
 *   GET  {base}/api/deliveries/pending?claudeSessionId=…
 *   404 → the hub missed SessionStart (it was down/restarted): self-heal
 *         by POSTing /api/hook/session-start, then retry ONCE.
 *   → { message } — when non-empty:
 *   stdout: { hookSpecificOutput: { hookEventName: "UserPromptSubmit",
 *             additionalContext: message } }
 *
 * FAIL-OPEN by design (asymmetric with the fail-closed guard hook): a
 * down hub must never block a prompt — every error path exits 0 silently.
 *
 * Config resolution, exactly like the guard hook: `--server <url>` argv →
 * SPEC_HUB_URL / SPEC_HUB_AGENT_TOKEN env → the ~/.spec-hub/server-url +
 * agent.token files (written by the hub at boot) → http://127.0.0.1:9115.
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");

// A SINGLE wall-clock budget for the whole hook (plan §8.7): the up-to-three
// sequential requests (pending → self-heal POST → retry) share one deadline,
// so even the heal path stays within ~2s rather than 3× a per-request timeout.
const DEADLINE_MS = 2000;

/** <homedir>/.spec-hub/<name>, utf8-trimmed; unreadable/empty → null. */
function readSpecHubFile(name) {
  try {
    const v = fs.readFileSync(path.join(os.homedir(), ".spec-hub", name), "utf8").trim();
    return v || null;
  } catch {
    return null;
  }
}

function serverBaseUrl() {
  const i = process.argv.indexOf("--server");
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  if (process.env.SPEC_HUB_URL) return process.env.SPEC_HUB_URL;
  return readSpecHubFile("server-url") || "http://127.0.0.1:9115";
}

function agentToken() {
  const env = process.env.SPEC_HUB_AGENT_TOKEN;
  if (env && env.trim()) return env.trim();
  return readSpecHubFile("agent.token");
}

function httpJson(method, urlStr, body, headers, timeoutMs) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr);
    const data = body ? Buffer.from(JSON.stringify(body)) : null;
    const req = http.request(
      {
        hostname: u.hostname,
        port: u.port,
        path: u.pathname + u.search,
        method,
        headers: {
          ...(headers || {}),
          ...(data
            ? { "content-type": "application/json", "content-length": data.length }
            : {}),
        },
      },
      (res) => {
        let buf = "";
        res.on("data", (d) => (buf += d));
        res.on("end", () => resolve({ status: res.statusCode, body: buf }));
      },
    );
    req.on("error", reject);
    req.setTimeout(timeoutMs, () => req.destroy(new Error("timeout")));
    if (data) req.write(data);
    req.end();
  });
}

async function main() {
  let raw = "";
  for await (const chunk of process.stdin) raw += chunk;
  const input = JSON.parse(raw);
  if (!input || typeof input.session_id !== "string" || typeof input.cwd !== "string") {
    return;
  }

  const base = serverBaseUrl();
  const token = agentToken();
  const headers = token ? { "x-spec-hub-agent-token": token } : {};
  const pendingUrl =
    base +
    "/api/deliveries/pending?claudeSessionId=" +
    encodeURIComponent(input.session_id);

  // One shared wall-clock deadline for all requests below: the heal path
  // (pending → session-start → retry) must still fit in ~DEADLINE_MS total,
  // so each request gets only the remaining budget (min 1ms).
  const deadline = Date.now() + DEADLINE_MS;
  const remaining = () => Math.max(1, deadline - Date.now());

  let res = await httpJson("GET", pendingUrl, null, headers, remaining());
  if (res.status === 404) {
    // Unknown session — re-register (self-healing upsert, plan §8.7) and
    // retry exactly once. The upsert preserves an existing scope binding.
    await httpJson(
      "POST",
      base + "/api/hook/session-start",
      {
        claudeSessionId: input.session_id,
        cwd: input.cwd,
        configDir:
          process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"),
      },
      headers,
      remaining(),
    );
    res = await httpJson("GET", pendingUrl, null, headers, remaining());
  }
  if (res.status !== 200) return;
  const j = JSON.parse(res.body);
  if (j && typeof j.message === "string" && j.message.trim() !== "") {
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "UserPromptSubmit",
          additionalContext: j.message,
        },
      }),
    );
  }
}

main().then(
  () => process.exit(0),
  () => process.exit(0), // fail-open: any error → silent success
);
