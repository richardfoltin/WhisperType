#!/usr/bin/env node
// spec-hub scaffold turn-end v3 (2026-08-08) — installed by spec-hub project registration.
// Managed file: do not edit by hand; upgrades re-apply via POST /api/projects/{id}/scaffold.
/**
 * spec-hub Stop hook (kör-lezárás) — pings the hub at every turn end so
 * it can auto-commit the turn's work. The hub does everything (decision,
 * commit title, git); this script is a thin fire-and-forget bridge, so
 * the turn is never blocked (the Illidan lesson: a synchronous judge
 * freezes the streaming indicator for minutes).
 *
 *   stdin:  one JSON object { session_id, transcript_path, ... }
 *   POST {base}/api/hook/turn-end { claudeSessionId }
 *   stdout: nothing, ever.
 *
 * FAIL-OPEN: a down hub must never block a session — every error path
 * exits 0 silently. A missed turn commit self-corrects at the next turn
 * or at the next approval sweep.
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
    serverBaseUrl() + "/api/hook/turn-end",
    { claudeSessionId: input.session_id },
    token ? { "x-spec-hub-agent-token": token } : {},
  );
}

main().then(
  () => process.exit(0),
  () => process.exit(0), // fail-open: any error → silent success
);
