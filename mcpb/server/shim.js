#!/usr/bin/env node
"use strict";
/*
 * Recipe Engine stdio -> streamable-HTTP shim.
 *
 * Claude Desktop launches this with the extension's stdio transport. The engine
 * itself is a per-user host process that must own the desktop, so it runs
 * exactly once, out of process, on http://127.0.0.1:47391/mcp behind a bearer
 * token in %LOCALAPPDATA%\RecipeEngine\token. This file:
 *   1. reads the token (no token -> a clear error to the client, never a guess);
 *   2. if /api/health is down, starts the host (recipe-enginew.exe serve) and waits;
 *   3. forwards every JSON-RPC message from stdin as a POST to /mcp, keeping the
 *      Mcp-Session-Id the server assigns on initialize, and writes every message the
 *      server answers with (plain JSON or an SSE stream) to stdout, one per line.
 * No dependencies: node's http, fs, child_process only. Nothing here decides
 * anything about the desktop - the host does.
 */
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const readline = require("readline");

const PROTOCOL_VERSION = "2025-06-18";
const DEFAULT_URL = "http://127.0.0.1:47391/mcp";
const url = new URL(process.env.RECIPE_ENGINE_URL && process.env.RECIPE_ENGINE_URL.trim() ? process.env.RECIPE_ENGINE_URL.trim() : DEFAULT_URL);
const dataDir = process.env.RECIPE_ENGINE_DATA_DIR || path.join(process.env.LOCALAPPDATA || os.homedir(), "RecipeEngine");
const tokenFile = process.env.RECIPE_ENGINE_TOKEN_FILE || path.join(dataDir, "token");
const hostExe = (process.env.RECIPE_ENGINE_HOST_EXE && process.env.RECIPE_ENGINE_HOST_EXE.trim())
  || path.join(process.env.LOCALAPPDATA || os.homedir(), "Programs", "RecipeEngine", "recipe-enginew.exe");
const START_TIMEOUT_MS = Number(process.env.RECIPE_ENGINE_START_TIMEOUT_MS || 20000);

let sessionId = null;
const log = (m) => process.stderr.write(`[recipe-engine shim] ${m}\n`);
const out = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");

function readToken() {
  try { return fs.readFileSync(tokenFile, "utf8").trim(); } catch (e) { return ""; }
}

function request(method, p, headers, body) {
  return new Promise((resolve, reject) => {
    // agent:false + Connection: close - every request stands alone, and node exits cleanly
    // when stdin closes instead of being kept alive by a pooled socket
    const h = { ...headers, Connection: "close" };
    if (body !== undefined) h["Content-Length"] = Buffer.byteLength(body);   // never chunked: some servers read Content-Length only
    const req = http.request({ host: url.hostname, port: url.port || 80, path: p, method, agent: false, headers: h }, (res) => {
      resolve(res);
    });
    req.on("error", reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

async function healthy() {
  try {
    const res = await request("GET", "/api/health", {});
    res.resume();
    return res.statusCode === 200;
  } catch (e) { return false; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ensureHost() {
  if (await healthy()) return true;
  if (!fs.existsSync(hostExe)) {
    log(`host not running and ${hostExe} not found - install the host first (see README: 90-second install)`);
    return false;
  }
  log(`host not running; starting ${hostExe} serve`);
  try {
    const child = spawn(hostExe, ["serve"], { detached: true, stdio: "ignore", windowsHide: true });
    child.unref();
  } catch (e) {
    log(`could not start the host: ${e.message}`);
    return false;
  }
  const deadline = Date.now() + START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await healthy()) return true;
    await sleep(250);
  }
  log(`host did not answer within ${START_TIMEOUT_MS} ms`);
  return false;
}

function errorFor(msg, code, text) {
  return { jsonrpc: "2.0", id: msg && msg.id !== undefined ? msg.id : null, error: { code, message: text } };
}

/** Parse an SSE body into JSON messages, writing each to stdout as it arrives. */
function pumpSse(res) {
  return new Promise((resolve) => {
    let buf = "";
    res.setEncoding("utf8");
    res.on("data", (chunk) => {
      buf = (buf + chunk).replace(/\r\n/g, "\n");         // sse-starlette emits CRLF; a CR/LF pair may straddle chunks
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const data = block.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
        if (!data) continue;
        try { out(JSON.parse(data)); } catch (e) { log(`bad SSE data: ${data.slice(0, 120)}`); }
      }
    });
    res.on("end", resolve);
    res.on("error", resolve);
  });
}

async function forward(msg) {
  const token = readToken();
  if (!token) {
    if (msg.id !== undefined) out(errorFor(msg, -32000, `no install token at ${tokenFile}; run the host once (recipe-engine install)`));
    return;
  }
  const headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Authorization": `Bearer ${token}`,
    "MCP-Protocol-Version": PROTOCOL_VERSION,
  };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  let res;
  try {
    res = await request("POST", url.pathname, headers, JSON.stringify(msg));
  } catch (e) {
    if (msg.id !== undefined) out(errorFor(msg, -32001, `engine unreachable at ${url.href}: ${e.message}`));
    return;
  }
  const sid = res.headers["mcp-session-id"];
  if (sid) sessionId = sid;
  const ctype = (res.headers["content-type"] || "").toLowerCase();
  if (res.statusCode === 202 || res.statusCode === 204) { res.resume(); return; }
  if (res.statusCode === 401) {
    res.resume();
    if (msg.id !== undefined) out(errorFor(msg, -32002, `engine refused the token in ${tokenFile} (rotate it with recipe-engine token --rotate and restart Claude)`));
    return;
  }
  if (ctype.includes("text/event-stream")) { await pumpSse(res); return; }
  let body = "";
  res.setEncoding("utf8");
  for await (const chunk of res) body += chunk;
  if (!body.trim()) return;
  try {
    const parsed = JSON.parse(body);
    if (Array.isArray(parsed)) parsed.forEach(out); else out(parsed);
  } catch (e) {
    if (msg.id !== undefined) out(errorFor(msg, -32003, `engine answered ${res.statusCode} with non-JSON: ${body.slice(0, 200)}`));
  }
}

async function main() {
  const ready = await ensureHost();
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  let chain = Promise.resolve();
  rl.on("line", (line) => {
    line = line.trim();
    if (!line) return;
    let msg;
    try { msg = JSON.parse(line); } catch (e) { log(`ignoring non-JSON line: ${line.slice(0, 80)}`); return; }
    if (!ready) {
      if (msg.id !== undefined) out(errorFor(msg, -32001, `Recipe Engine host is not running and could not be started (${hostExe})`));
      return;
    }
    chain = chain.then(() => forward(msg)).catch((e) => log(`forward failed: ${e.message}`));
  });
  // Never process.exit(): stdout is an async pipe on Windows and exit() would truncate the
  // last answers. Let the loop drain; with no sockets or timers left node exits by itself.
  rl.on("close", () => { chain.finally(() => { process.exitCode = 0; }); });
}

main().catch((e) => { log(`fatal: ${e.stack || e}`); process.exit(1); });
