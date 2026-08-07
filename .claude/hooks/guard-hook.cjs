#!/usr/bin/env node
// spec-hub scaffold guard-hook v1 (2026-08-06) — installed by spec-hub project registration.
// Managed file: do not edit by hand; upgrades re-apply via POST /api/projects/{id}/scaffold.
/**
 * spec-hub guard hook — PreToolUse enforcement (spec-hub plan §8.6; ported
 * from the illidan2 guard hook with two documented behavior deltas: the
 * URL/token file resolution and the unbound floor-only mode).
 *
 * Self-contained: plain Node, no dependencies, single file. Scaffolded into a
 * target repo's `.claude/settings.json` as:
 *
 *   { "hooks": { "PreToolUse": [ { "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell",
 *       "hooks": [ { "type": "command", "command": "node \"<abs>/guard-hook.cjs\"" } ] } ] } }
 *
 * Contract (verified by the 2026-07-12 spike — hooks fire and block in
 * spawned sessions, `--dangerously-skip-permissions` notwithstanding):
 *   stdin:  one JSON object { session_id, cwd, tool_name, tool_input, ... }
 *   allow:  exit 0, no output — for BOTH a bound-scope in-scope write and
 *           floor-only (unbound) mode. An explicit PreToolUse "allow" would
 *           BYPASS the CLI permission system (auto-approving every non-floor
 *           write without a prompt), so the hook never emits one.
 *   deny:   exit 0, stdout JSON { hookSpecificOutput: { hookEventName:
 *           "PreToolUse", permissionDecision: "deny",
 *           permissionDecisionReason } }
 *
 * Decision order (fail-closed, plan §8.6):
 *   1. HARDCODED safety floor on every write target (.git/, .env*, *secret*,
 *      credentials, .ssh/, .claude/**) — evaluated FIRST, non-overridable.
 *   2. No write targets (plain read command) → allow. Reads are NEVER
 *      restricted and never need the scope service.
 *   3. Scope lookup: GET {server}/api/sessions/by-claude-id/{sid}/scope
 *      (2s timeout). Bound scope → enforce + refresh the disk cache.
 *      `bound:false` (registered session, no scope) → floor-only mode:
 *      SILENT allow (the floor still denies first; the bind-a-scope hint
 *      reaches the agent via the SessionStart orientation channel, not this
 *      hook); NEVER written to the disk cache.
 *   4. Server unreachable → cached BOUND scope younger than 10 minutes →
 *      enforce the cached scope. Cache stale/absent, or ANY internal error →
 *      DENY with a reason that instructs the agent to request a scope
 *      extension.
 *
 * Config: server base URL via `--server <url>` argv → SPEC_HUB_URL env →
 * <homedir>/.spec-hub/server-url (utf8, trimmed) → default
 * http://127.0.0.1:9115. Agent token via SPEC_HUB_AGENT_TOKEN env →
 * <homedir>/.spec-hub/agent.token (trimmed) → none; when present it is sent
 * as `x-spec-hub-agent-token` on both the scope lookup and the denial
 * report. Cache file via GUARD_HOOK_CACHE (default: guard-hook-cache.json
 * next to this script).
 */
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");

const WRITE_TOOLS = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit"]);
const SHELL_TOOLS = new Set(["Bash", "PowerShell"]);

const CACHE_TTL_MS = 10 * 60 * 1000;
const LOOKUP_TIMEOUT_MS = 2000;
const REPORT_TIMEOUT_MS = 800;

// ─────────────────────────────────────────────────────────────────────────
// Safety floor — hardcoded, evaluated before any scope logic, and NOT
// overridable by scopes or extensions (plan §8.6).
// ─────────────────────────────────────────────────────────────────────────

const FLOOR_RULES = [
  { name: ".git/", test: (segs) => segs.includes(".git") },
  {
    name: ".env*",
    test: (segs) => segs.some((s) => s.toLowerCase().startsWith(".env")),
  },
  {
    name: "**/*secret*",
    test: (segs) => segs.some((s) => s.toLowerCase().includes("secret")),
  },
  {
    name: "**/*credential*",
    test: (segs) => segs.some((s) => s.toLowerCase().includes("credential")),
  },
  { name: ".ssh/", test: (segs) => segs.includes(".ssh") },
  { name: ".claude/**", test: (segs) => segs.includes(".claude") },
];

/** Returns the violated floor rule name, or null. Input: absolute path. */
function floorViolation(absPath) {
  const segs = String(absPath).replace(/\\/g, "/").split("/").filter(Boolean);
  for (const rule of FLOOR_RULES) {
    if (rule.test(segs)) return rule.name;
  }
  return null;
}

// ─────────────────────────────────────────────────────────────────────────
// Glob matching (no deps). Supports **, *, ? — case-insensitive because the
// host filesystem is: NTFS on Windows and APFS on macOS both default to
// case-insensitive, so a case-sensitive matcher would let `SRC/x.ts` slip
// past a scope of `src/**`. "X/**" also matches "X" itself (mkdir of the scope
// folder must pass).
// ─────────────────────────────────────────────────────────────────────────

function globToRegExp(glob) {
  let g = String(glob).replace(/\\/g, "/").replace(/\/+$/, "");
  if (g === "**" || g === "." || g === "") return /^.*$/i;
  let src = "";
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === "*") {
      if (g[i + 1] === "*") {
        i++;
        if (g[i + 1] === "/") {
          src += "(?:[^/]+/)*"; // "**/" — zero or more directories
          i++;
        } else {
          src += ".*";
        }
      } else {
        src += "[^/]*";
      }
    } else if (c === "?") {
      src += "[^/]";
    } else if (c === "/" && g.slice(i) === "/**") {
      src += "(?:/.*)?"; // trailing "/**" — the prefix itself matches too
      break;
    } else {
      src += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    }
  }
  return new RegExp("^" + src + "$", "i");
}

function matchesAnyGlob(relPath, globs) {
  return globs.some((g) => globToRegExp(g).test(relPath));
}

/** Repo-relative forward-slash path, or null when outside the repo. */
function relIn(repoRoot, absTarget) {
  const rel = path.relative(path.resolve(repoRoot), path.resolve(absTarget));
  if (rel === "") return ".";
  if (rel.startsWith("..") || path.isAbsolute(rel)) return null;
  return rel.split(path.sep).join("/");
}

// ─────────────────────────────────────────────────────────────────────────
// Shell command classification (Bash + PowerShell tools).
//
// The spike proved the shell is an escape route (the model reached for
// `printf > file` the moment Write was removed), so shell segments are
// classified fail-closed:
//   - plain read commands (allowlist, no redirection)      → allow
//   - write commands with extractable targets              → floor+scope check
//   - interpreters/builders/package managers               → treated as a
//     write to the TRACKED working directory (must be in scope); inline
//     code flags (-e/-c/--eval) are denied outright
//   - git: read subcommands allowed; checkout/restore with explicit `--`
//     pathspecs scope-checked; every other mutating subcommand denied
//   - anything unrecognized                                → deny
// ─────────────────────────────────────────────────────────────────────────

/** Split on top-level && || ; | and newlines, respecting quotes. */
function splitSegments(command) {
  const segs = [];
  let cur = "";
  let q = null;
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    const n = command[i + 1];
    if (q) {
      cur += c;
      if (c === q) q = null;
      continue;
    }
    if (c === "'" || c === '"') {
      q = c;
      cur += c;
      continue;
    }
    if ((c === "&" && n === "&") || (c === "|" && n === "|")) {
      segs.push(cur);
      cur = "";
      i++;
      continue;
    }
    if (c === "|" || c === ";" || c === "\n") {
      segs.push(cur);
      cur = "";
      continue;
    }
    cur += c;
  }
  segs.push(cur);
  return segs.map((s) => s.trim()).filter(Boolean);
}

/**
 * Pull `$(...)` / backtick substitution bodies out of a segment (they
 * execute too, so they are classified as extra segments). Single quotes
 * suppress substitution; double quotes do not.
 */
function extractSubstitutions(seg) {
  const inner = [];
  let out = "";
  let inSingle = false;
  for (let i = 0; i < seg.length; i++) {
    const c = seg[i];
    if (inSingle) {
      out += c;
      if (c === "'") inSingle = false;
      continue;
    }
    if (c === "'") {
      inSingle = true;
      out += c;
      continue;
    }
    if (c === "$" && seg[i + 1] === "(") {
      let depth = 1;
      let j = i + 2;
      let buf = "";
      while (j < seg.length && depth > 0) {
        if (seg[j] === "(") depth++;
        else if (seg[j] === ")") depth--;
        if (depth > 0) buf += seg[j];
        j++;
      }
      inner.push(buf);
      out += "__SUBST__";
      i = j - 1;
      continue;
    }
    if (c === "`") {
      let j = i + 1;
      let buf = "";
      while (j < seg.length && seg[j] !== "`") {
        buf += seg[j];
        j++;
      }
      inner.push(buf);
      out += "__SUBST__";
      i = j;
      continue;
    }
    out += c;
  }
  return { outer: out, inner };
}

const DEV_SINKS = new Set([
  "/dev/null",
  "/dev/stdout",
  "/dev/stderr",
  "nul",
  "NUL",
]);

/**
 * Strip output redirections (`>`, `>>`, fd variants) from a segment and
 * return their file targets. Input redirection (`<`, heredocs) is a read
 * and is simply dropped. `2>&1`-style fd duplication has no file target.
 */
function extractRedirects(seg) {
  const targets = [];
  let out = "";
  let q = null;
  for (let i = 0; i < seg.length; i++) {
    const c = seg[i];
    if (q) {
      out += c;
      if (c === q) q = null;
      continue;
    }
    if (c === "'" || c === '"') {
      q = c;
      out += c;
      continue;
    }
    if (c === "<") {
      if (seg[i + 1] === "<") i++; // heredoc marker — the body is data
      out += " ";
      continue;
    }
    if (c === ">") {
      // drop an fd/& prefix already copied to `out` (e.g. "2>", "&>")
      let j = out.length;
      while (j > 0 && /[\d&]/.test(out[j - 1])) j--;
      out = out.slice(0, j);
      let k = i + 1;
      if (seg[k] === ">") k++; // ">>"
      if (seg[k] === "&") {
        // fd duplication (>&1) — no file target
        k++;
        while (k < seg.length && /\d/.test(seg[k])) k++;
        i = k - 1;
        continue;
      }
      while (k < seg.length && /\s/.test(seg[k])) k++;
      let fname = "";
      if (seg[k] === "'" || seg[k] === '"') {
        const qq = seg[k];
        k++;
        while (k < seg.length && seg[k] !== qq) {
          fname += seg[k];
          k++;
        }
        k++;
      } else {
        while (k < seg.length && !/[\s;&|<>]/.test(seg[k])) {
          fname += seg[k];
          k++;
        }
      }
      if (fname && !DEV_SINKS.has(fname)) targets.push(fname);
      i = k - 1;
      continue;
    }
    out += c;
  }
  return { rest: out, targets };
}

/** Whitespace tokenizer that groups quoted spans (quotes stripped). */
function tokenize(seg) {
  const tokens = [];
  let cur = "";
  let q = null;
  let had = false;
  for (const c of seg) {
    if (q) {
      if (c === q) q = null;
      else cur += c;
      continue;
    }
    if (c === "'" || c === '"') {
      q = c;
      had = true;
      continue;
    }
    if (/\s/.test(c)) {
      if (cur || had) tokens.push(cur);
      cur = "";
      had = false;
      continue;
    }
    cur += c;
  }
  if (cur || had) tokens.push(cur);
  return tokens;
}

const READ_CMDS = new Set([
  "cat", "less", "more", "head", "tail", "ls", "dir", "tree", "pwd",
  "echo", "printf", "true", "false", "which", "whereis", "type", "file",
  "stat", "du", "df", "wc", "grep", "egrep", "fgrep", "rg", "ag", "fd",
  "sort", "uniq", "cut", "tr", "awk", "gawk", "diff", "cmp", "comm",
  "md5sum", "sha1sum", "sha256sum", "basename", "dirname", "realpath",
  "readlink", "date", "whoami", "id", "uname", "hostname", "printenv",
  "sleep", "test", "[", "expr", "seq", "jq", "xxd", "od",
  "strings", "column", "nl", "tac", "paste", "join", "base64",
  // PowerShell read cmdlets / aliases (lowercased at classification time)
  "get-content", "gc", "get-childitem", "gci", "get-item", "get-location",
  "select-string", "sls", "test-path", "get-command", "write-output",
  "write-host", "measure-object", "select-object", "where-object",
]);

/** All non-flag args are write targets. */
const WRITE_ALL_ARGS = new Set([
  "rm", "rmdir", "unlink", "shred", "touch", "mkdir", "chmod", "chown",
  "chgrp", "ln", "truncate", "mv",
  "remove-item", "ri", "new-item", "ni", "set-content", "sc", "add-content",
  "ac", "out-file", "move-item", "mi", "clear-content",
]);

/** Last non-flag arg is the write target (sources are only read). */
const WRITE_LAST_ARG = new Set(["cp", "install", "rsync", "copy-item", "cpi"]);

/**
 * Interpreters / builders / test runners: can write anywhere in principle,
 * but conventionally write beneath their working directory — enforced as a
 * write to the TRACKED cwd. Inline-code flags are denied outright.
 */
const INTERPRETER_CMDS = new Set([
  "node", "python", "python3", "py", "pip", "pip3", "tsx", "ts-node", "tsc",
  "deno", "vite", "vitest", "jest", "mocha", "pytest", "uv", "uvx", "make",
  "cmake", "cargo", "go", "rustc", "dotnet", "mvn", "gradle", "gradlew",
  "esbuild", "rollup", "webpack", "turbo", "nx", "prettier", "eslint",
  "npx", "perl", "ruby",
]);

/** Inline-code flags are only meaningful for real language interpreters —
 * build tools reuse the same letters for harmless things (`tsc -p`). */
const INLINE_CODE_INTERPRETERS = new Set([
  "node", "python", "python3", "py", "perl", "ruby", "deno",
]);
const INLINE_CODE_FLAGS = new Set(["-e", "--eval", "-c", "-p", "--print"]);

const PKG_MANAGERS = new Set(["npm", "pnpm", "yarn", "bun"]);
const PKG_READ_SUBS = new Set([
  "ls", "list", "ll", "view", "info", "show", "outdated", "why", "ping",
  "root", "bin", "help", "licenses", "audit",
]);
const PKG_DENY_SUBS = new Set(["publish", "login", "adduser", "logout"]);

const GIT_READ_SUBS = new Set([
  "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree",
  "ls-remote", "blame", "shortlog", "describe", "grep", "cat-file",
  "reflog", "fetch", "var", "version", "count-objects",
]);

/** Shells-in-shells cannot be verified — always denied. */
const NESTED_SHELLS = new Set([
  "sh", "bash", "zsh", "dash", "ksh", "cmd", "powershell", "pwsh",
  "xargs", "eval", "source", ".",
]);

const WRAPPER_CMDS = new Set([
  "sudo", "command", "exec", "nohup", "time", "nice", "env",
]);

function isFlag(tok) {
  return tok.startsWith("-");
}

function nonFlagArgs(tokens) {
  return tokens.filter((t) => !isFlag(t) && t !== "--" && t !== "__SUBST__");
}

/**
 * Classify one already-redirect-stripped, tokenized segment.
 * Returns { targets: string[] (raw, unresolved), dir?: newTrackedDir|null,
 *           deny?: string, dirWrite?: boolean }.
 */
function classifyTokens(tokens, dir) {
  // strip leading VAR=value assignments and wrapper commands (repeat until
  // stable: `env VAR=x sudo cmd …`)
  let t = tokens.slice();
  for (;;) {
    if (t.length > 0 && /^[A-Za-z_][A-Za-z0-9_]*=/.test(t[0])) {
      t = t.slice(1);
    } else if (t.length > 0 && WRAPPER_CMDS.has(t[0].toLowerCase())) {
      t = t.slice(1);
    } else {
      break;
    }
  }
  if (t.length === 0) return { targets: [] }; // bare `env` / assignment only

  const rawCmd = t[0];
  const cmd = path
    .basename(rawCmd.replace(/\\/g, "/"))
    .toLowerCase()
    .replace(/\.(exe|cmd|bat|ps1)$/, "");
  const args = t.slice(1);

  if (cmd === "cd" || cmd === "pushd") {
    const dest = nonFlagArgs(args)[0];
    if (
      !dest ||
      dest === "~" ||
      dest === "-" ||
      dest.includes("__SUBST__") ||
      dest.includes("$")
    ) {
      return { targets: [], dir: null }; // untrackable working directory
    }
    return { targets: [], dir: dir === null ? null : path.resolve(dir, dest) };
  }
  if (cmd === "popd") return { targets: [], dir: null };

  if (NESTED_SHELLS.has(cmd)) {
    return {
      targets: [],
      deny: `nested shell/eval command "${cmd}" cannot be verified`,
    };
  }

  if (READ_CMDS.has(cmd)) {
    // `find` grows write powers with -delete/-exec; sed/awk are read-only
    // WITHOUT -i (in-place) — both handled below, everything else is a read.
    return { targets: [] };
  }

  if (cmd === "find") {
    if (args.some((a) => a === "-delete" || a === "-exec" || a === "-execdir" || a === "-ok")) {
      return { targets: [], deny: "find with -delete/-exec cannot be verified" };
    }
    return { targets: [] };
  }

  if (cmd === "sed") {
    const inPlace = args.some((a) => a === "-i" || a.startsWith("-i.") || a === "--in-place");
    if (!inPlace) return { targets: [] };
    return { targets: nonFlagArgs(args).filter((a) => !a.startsWith("s/")) };
  }

  if (cmd === "tee") {
    return { targets: nonFlagArgs(args) };
  }

  if (cmd === "dd") {
    const of = args.find((a) => a.startsWith("of="));
    return { targets: of ? [of.slice(3)] : [] };
  }

  if (cmd === "curl") {
    const targets = [];
    for (let i = 0; i < args.length; i++) {
      if (args[i] === "-o" || args[i] === "--output") {
        if (args[i + 1]) targets.push(args[i + 1]);
      }
    }
    if (args.some((a) => a === "-O" || a === "--remote-name" || a === "-J")) {
      return { targets, dirWrite: true };
    }
    return { targets };
  }
  if (cmd === "wget") {
    const oIdx = args.findIndex((a) => a === "-O" || a === "--output-document");
    if (oIdx >= 0 && args[oIdx + 1]) return { targets: [args[oIdx + 1]] };
    return { targets: [], dirWrite: true };
  }

  if (cmd === "git") {
    return classifyGit(args);
  }

  if (PKG_MANAGERS.has(cmd)) {
    // skip value-taking selector flags to find the subcommand
    let rest = args.slice();
    while (rest.length > 0 && isFlag(rest[0])) {
      const f = rest[0];
      rest = rest.slice(
        f === "--filter" || f === "-F" || f === "--dir" || f === "-C" || f === "--prefix" ? 2 : 1,
      );
    }
    const sub = rest[0] ? rest[0].toLowerCase() : "";
    if (PKG_READ_SUBS.has(sub) && !rest.includes("fix")) return { targets: [] };
    if (PKG_DENY_SUBS.has(sub)) {
      return { targets: [], deny: `package-registry command "${cmd} ${sub}" is not allowed` };
    }
    // install/add/remove/run/test/exec/… — mutates beneath the tracked cwd
    return { targets: [], dirWrite: true };
  }

  if (INTERPRETER_CMDS.has(cmd)) {
    if (
      INLINE_CODE_INTERPRETERS.has(cmd) &&
      args.some((a) => INLINE_CODE_FLAGS.has(a))
    ) {
      return {
        targets: [],
        deny: `inline code (${cmd} with -e/-c/-p) cannot be verified`,
      };
    }
    return { targets: [], dirWrite: true };
  }

  if (WRITE_ALL_ARGS.has(cmd)) {
    const targets = nonFlagArgs(args);
    if (targets.length === 0) {
      return { targets: [], deny: `write command "${cmd}" with no identifiable target` };
    }
    return { targets };
  }

  if (WRITE_LAST_ARG.has(cmd)) {
    const nf = nonFlagArgs(args);
    if (nf.length === 0) {
      return { targets: [], deny: `write command "${cmd}" with no identifiable target` };
    }
    return { targets: [nf[nf.length - 1]] };
  }

  return {
    targets: [],
    deny: `unrecognized command "${cmd}" cannot be verified as read-only`,
  };
}

/** git subcommand policy. Args exclude the leading "git". */
function classifyGit(args) {
  let rest = args.slice();
  // global flags: -C <p> / -c k=v / --git-dir=… / -P
  while (rest.length > 0 && isFlag(rest[0])) {
    const f = rest[0];
    rest = rest.slice(f === "-C" || f === "-c" ? 2 : 1);
  }
  const sub = rest[0] ? rest[0].toLowerCase() : "";
  const subArgs = rest.slice(1);

  if (GIT_READ_SUBS.has(sub)) return { targets: [] };
  if (sub === "branch" || sub === "tag") {
    // read-only when there is no positional arg and no destructive flag
    const destructive = subArgs.some((a) => /^-(d|D|m|M|c|C|f)$/.test(a));
    if (!destructive && nonFlagArgs(subArgs).length === 0) return { targets: [] };
    return { targets: [], deny: `git ${sub} (mutation) is managed by the host's auto-commit system` };
  }
  if (sub === "stash") {
    // bare `git stash` PUSHES a stash — only list/show are reads
    const mode = subArgs.find((a) => !isFlag(a));
    if (mode === "list" || mode === "show") return { targets: [] };
    return { targets: [], deny: "git stash mutates the working tree and is not allowed (never stash in spec-hub-managed repos)" };
  }
  if (sub === "remote" || sub === "worktree" || sub === "config") {
    const mode = subArgs.find((a) => !isFlag(a));
    const readModes = new Set(["show", "list", "get-url", "get", "-l"]);
    if (!mode || readModes.has(mode)) return { targets: [] };
    return { targets: [], deny: `git ${sub} ${mode} (mutation) is not allowed` };
  }
  if (sub === "checkout" || sub === "restore") {
    const sep = subArgs.indexOf("--");
    const pathspecs = sep >= 0 ? subArgs.slice(sep + 1) : [];
    if (pathspecs.length > 0) return { targets: pathspecs };
    return {
      targets: [],
      deny: `git ${sub} without an explicit "--" pathspec rewrites the whole tree`,
    };
  }
  return {
    targets: [],
    deny: `git ${sub || "(bare)"} mutates repo state — git state changes are managed by the host's auto-commit system`,
  };
}

/**
 * Classify a full shell command. Returns
 *   { targets: string[] (ABSOLUTE), deny?: string }.
 * `cwd` is the session working directory from the hook input; `cd` is
 * tracked across segments so relative targets resolve correctly.
 */
function classifyCommand(command, cwd) {
  const targets = [];
  let dir = cwd;

  const queue = splitSegments(command);
  for (let qi = 0; qi < queue.length; qi++) {
    let seg = queue[qi];
    // strip subshell/grouping wrappers
    seg = seg.replace(/^[({\s]+/, "").replace(/[)}\s]+$/, "");
    if (!seg) continue;

    const { outer, inner } = extractSubstitutions(seg);
    // substitution bodies execute — classify them as additional segments
    for (const body of inner) queue.push(...splitSegments(body));

    const { rest, targets: redirTargets } = extractRedirects(outer);
    const cls = classifyTokens(tokenize(rest), dir);
    if (cls.deny) return { targets: [], deny: cls.deny };
    if (cls.dir !== undefined) dir = cls.dir;

    const rawTargets = [...redirTargets, ...cls.targets];
    if (cls.dirWrite) {
      if (dir === null) {
        return { targets: [], deny: "working directory untrackable after cd — cannot verify write location" };
      }
      targets.push(path.resolve(dir));
    }
    for (const raw of rawTargets) {
      if (DEV_SINKS.has(raw)) continue;
      if (raw.includes("__SUBST__") || raw.includes("$")) {
        return { targets: [], deny: `write target "${raw}" contains a substitution and cannot be verified` };
      }
      if (dir === null && !path.isAbsolute(raw)) {
        return { targets: [], deny: "working directory untrackable after cd — cannot verify write location" };
      }
      targets.push(path.resolve(dir === null ? cwd : dir, raw));
    }
  }
  return { targets };
}

// ─────────────────────────────────────────────────────────────────────────
// Scope resolution: server → disk cache → deny (plan §8.6 precedence).
// ─────────────────────────────────────────────────────────────────────────

function httpJson(method, urlStr, body, timeoutMs, headers) {
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

function readCacheEntry(cacheFile, sessionId) {
  try {
    const all = JSON.parse(fs.readFileSync(cacheFile, "utf8"));
    const e = all[sessionId];
    if (
      e &&
      typeof e.ts === "number" &&
      Array.isArray(e.writeGlobs) &&
      typeof e.repoPath === "string"
    ) {
      return e;
    }
  } catch {
    /* absent/corrupt cache == no cache */
  }
  return null;
}

function writeCacheEntry(cacheFile, sessionId, entry) {
  try {
    let all = {};
    try {
      all = JSON.parse(fs.readFileSync(cacheFile, "utf8"));
    } catch {
      /* start fresh */
    }
    all[sessionId] = entry;
    const tmp = cacheFile + ".tmp." + process.pid;
    fs.writeFileSync(tmp, JSON.stringify(all), "utf8");
    fs.renameSync(tmp, cacheFile);
  } catch {
    /* cache write failure must never break the decision */
  }
}

async function resolveScope(sessionId, ctx) {
  try {
    const res = await httpJson(
      "GET",
      `${ctx.baseUrl}/api/sessions/by-claude-id/${encodeURIComponent(sessionId)}/scope`,
      null,
      LOOKUP_TIMEOUT_MS,
      ctx.headers,
    );
    if (res.status === 200) {
      const j = JSON.parse(res.body);
      // bound:false MUST be checked before the bound-scope shape (the
      // unbound response also carries repoPath + writeGlobs:[]) and is
      // NEVER cached — a later bind must take effect at the next
      // server-served lookup.
      if (j && j.bound === false) {
        return { ok: true, bound: false, source: "server" };
      }
      if (j && Array.isArray(j.writeGlobs) && typeof j.repoPath === "string") {
        writeCacheEntry(ctx.cacheFile, sessionId, {
          writeGlobs: j.writeGlobs,
          repoPath: j.repoPath,
          ts: ctx.now,
        });
        return { ok: true, bound: true, writeGlobs: j.writeGlobs, repoPath: j.repoPath, source: "server" };
      }
      return { ok: false, reason: "the scope service returned a malformed scope" };
    }
    if (res.status === 404) {
      return {
        ok: false,
        reason:
          "unknown session — spec-hub has not registered this session " +
          "(the SessionStart hook registers sessions; bind a scope via the /scope skill)",
      };
    }
    // 5xx and friends fall through to the cache like a network failure
  } catch {
    /* unreachable — try the cache */
  }
  const cached = readCacheEntry(ctx.cacheFile, sessionId);
  if (cached && ctx.now - cached.ts < CACHE_TTL_MS) {
    return { ok: true, bound: true, writeGlobs: cached.writeGlobs, repoPath: cached.repoPath, source: "cache" };
  }
  return {
    ok: false,
    reason: cached
      ? "the scope service is unreachable and the cached scope is stale (>10 min)"
      : "the scope service is unreachable and no cached scope exists",
  };
}

// ─────────────────────────────────────────────────────────────────────────
// Decision core.
// ─────────────────────────────────────────────────────────────────────────

const EXTENSION_HINT =
  "Reads are never restricted. If this write is genuinely needed for your task, " +
  "ask the user to extend the scope (a scope-extension request the user can " +
  "approve); otherwise stay within your write scope.";

function suggestGlob(rel) {
  if (!rel || rel === ".") return null;
  const slash = rel.lastIndexOf("/");
  return slash > 0 ? rel.slice(0, slash) + "/**" : rel;
}

/**
 * Pure-ish decision function (network/cache via ctx). Returns
 *   { action: "allow" } |
 *   { action: "deny", kind, reason, targets, suggestedGlob }
 * EVERY allow is silent: main() emits nothing and exits 0. The hook never
 * emits a PreToolUse "allow" — an explicit allow would bypass the CLI
 * permission system, auto-approving all non-floor writes without a prompt.
 * The unbound floor-only case is just another silent allow (the floor is
 * still enforced first; the bind-a-scope hint is delivered out-of-band via
 * the SessionStart orientation channel).
 */
async function decide(input, ctx) {
  const tool = input.tool_name;
  if (!WRITE_TOOLS.has(tool) && !SHELL_TOOLS.has(tool)) {
    return { action: "allow" }; // matcher artifact — not a write tool
  }
  const cwd = input.cwd;
  if (typeof cwd !== "string" || cwd.length === 0) {
    return deny("error", "malformed hook input: missing cwd", [], null);
  }
  const toolInput = input.tool_input || {};

  let targets;
  if (WRITE_TOOLS.has(tool)) {
    const p = toolInput.file_path ?? toolInput.notebook_path;
    if (typeof p !== "string" || p.length === 0) {
      return deny("error", `malformed ${tool} input: missing file path`, [], null);
    }
    targets = [path.resolve(cwd, p)];
  } else {
    if (typeof toolInput.command !== "string") {
      return deny("error", `malformed ${tool} input: missing command`, [], null);
    }
    const cls = classifyCommand(toolInput.command, cwd);
    if (cls.deny) {
      return deny("shell", `shell command blocked: ${cls.deny}.`, [], null);
    }
    targets = cls.targets;
  }

  // 1) Safety floor — FIRST, before any scope lookup, non-overridable.
  for (const t of targets) {
    const rule = floorViolation(t);
    if (rule) {
      return {
        action: "deny",
        kind: "floor",
        targets,
        suggestedGlob: null,
        reason:
          `Scope guard: write to "${t}" is blocked by the non-overridable safety floor (${rule}). ` +
          "Scope extensions CANNOT enable this — do not retry. If you believe this " +
          "file must change, ask the user to change it manually.",
      };
    }
  }

  // 2) Plain reads never need the scope service.
  if (targets.length === 0) return { action: "allow" };

  // 3+4) Scope: server → fresh cache → deny.
  const scope = await resolveScope(input.session_id, ctx);
  if (!scope.ok) {
    return deny(
      "unavailable",
      `cannot verify the write scope: ${scope.reason}.`,
      targets,
      null,
    );
  }

  // Registered-but-unbound session: the floor already passed above, so the
  // write is allowed SILENTLY (floor-only mode, plan §8.6). The hook must
  // NOT emit a PreToolUse "allow" — that would bypass the CLI permission
  // system and auto-approve every non-floor write anywhere on disk. The
  // bind-a-scope hint is delivered via the SessionStart orientation channel.
  if (scope.bound === false) {
    return { action: "allow" };
  }

  for (const t of targets) {
    const rel = relIn(scope.repoPath, t);
    if (rel === null) {
      return deny(
        "scope",
        `write to "${t}" is outside the project repository (${scope.repoPath}).`,
        targets,
        null,
        scope,
      );
    }
    if (!matchesAnyGlob(rel, scope.writeGlobs)) {
      return deny(
        "scope",
        `write to "${rel}" is outside this session's write scope.`,
        targets,
        suggestGlob(rel),
        scope,
      );
    }
  }
  return { action: "allow" };

  function deny(kind, detail, denyTargets, suggestedGlob, scopeInfo) {
    const scopeLine = scopeInfo
      ? ` Allowed write globs: ${scopeInfo.writeGlobs.join(", ") || "(none)"}.`
      : "";
    return {
      action: "deny",
      kind,
      targets: denyTargets,
      suggestedGlob: suggestedGlob ?? null,
      reason: `Scope guard: ${detail}${scopeLine} ${EXTENSION_HINT}`,
    };
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Denial reporting (best-effort, never blocks the decision).
// ─────────────────────────────────────────────────────────────────────────

async function reportDenial(input, decision, ctx) {
  try {
    await httpJson(
      "POST",
      `${ctx.baseUrl}/api/events/ingest`,
      {
        level: "warn",
        source: "hook",
        message: `guard denial (${decision.kind}): ${input.tool_name}`,
        payload: {
          claudeSessionId: input.session_id,
          cwd: input.cwd,
          toolName: input.tool_name,
          kind: decision.kind,
          targets: decision.targets,
          suggestedGlob: decision.suggestedGlob,
          reason: decision.reason,
        },
      },
      REPORT_TIMEOUT_MS,
      ctx.headers,
    );
  } catch {
    /* server down — the deny still stands */
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Entry point.
// ─────────────────────────────────────────────────────────────────────────

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

function cachePath() {
  return process.env.GUARD_HOOK_CACHE || path.join(__dirname, "guard-hook-cache.json");
}

async function main() {
  let raw = "";
  for await (const chunk of process.stdin) raw += chunk;

  const token = agentToken();
  const ctx = {
    baseUrl: serverBaseUrl(),
    cacheFile: cachePath(),
    now: Date.now(),
    headers: token ? { "x-spec-hub-agent-token": token } : {},
  };

  let input = null;
  let decision;
  try {
    input = JSON.parse(raw);
    decision = await decide(input, ctx);
  } catch (err) {
    // ANY internal error → deny with reason (fail closed, plan §8.6)
    decision = {
      action: "deny",
      kind: "error",
      targets: [],
      suggestedGlob: null,
      reason:
        `Scope guard: internal error (${err && err.message ? err.message : String(err)}) — ` +
        `failing closed. ${EXTENSION_HINT}`,
    };
  }

  if (decision.action === "allow") {
    // Every allow is silent — bound in-scope AND unbound floor-only. Emitting
    // an explicit PreToolUse "allow" would bypass the CLI permission system,
    // auto-approving all non-floor writes without a prompt.
    process.exit(0);
  }

  if (input) await reportDenial(input, decision, ctx);

  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: decision.reason,
      },
    }),
  );
  process.exit(0);
}

if (require.main === module) {
  main().catch((err) => {
    // Even the entry point fails closed.
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "deny",
          permissionDecisionReason: `Scope guard: fatal error (${String(err)}) — failing closed. ${EXTENSION_HINT}`,
        },
      }),
    );
    process.exit(0);
  });
}

module.exports = {
  floorViolation,
  globToRegExp,
  matchesAnyGlob,
  relIn,
  splitSegments,
  extractSubstitutions,
  extractRedirects,
  tokenize,
  classifyCommand,
  decide,
  CACHE_TTL_MS,
};
