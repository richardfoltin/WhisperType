#!/usr/bin/env node
// spec-hub scaffold session-end v1 (2026-08-06) — installed by spec-hub project registration.
// Managed file: do not edit by hand; upgrades re-apply via POST /api/projects/{id}/scaffold.
/**
 * spec-hub SessionEnd hook (plan §8.7) — marks the session `ended` in the
 * hub's registry so the fleet view stops rendering it live.
 *
 *   stdin:  one JSON object { session_id, ... }
 *   POST {base}/api/hook/session-end { claudeSessionId }
 *   stdout: nothing, ever.
 *
 * FAIL-OPEN by design (asymmetric with the fail-closed guard hook): a
 * down hub must never block a session end — every error path exits 0
 * silently. A missed end-mark self-corrects via the staleness rule.
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

const TIMEOUT_MS = 2000;

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

function httpJson(method, urlStr, body, headers) {
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
    req.setTimeout(TIMEOUT_MS, () => req.destroy(new Error("timeout")));
    if (data) req.write(data);
    req.end();
  });
}

async function main() {
  let raw = "";
  for await (const chunk of process.stdin) raw += chunk;
  const input = JSON.parse(raw);
  if (!input || typeof input.session_id !== "string") return;

  const token = agentToken();
  await httpJson(
    "POST",
    serverBaseUrl() + "/api/hook/session-end",
    { claudeSessionId: input.session_id },
    token ? { "x-spec-hub-agent-token": token } : {},
  );
}

main().then(
  () => process.exit(0),
  () => process.exit(0), // fail-open: any error → silent success
);
