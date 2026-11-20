"""The .mcpb shim (mcpb/server/shim.js): stdio JSON-RPC in, streamable-HTTP out.
Driven with a real `node` against a small fake engine that speaks the wire format
(initialize -> JSON + Mcp-Session-Id; tools/list -> an SSE stream; notification ->
202; wrong token -> 401). The last test talks to the REAL engine over HTTP."""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parent.parent / "mcpb" / "server" / "shim.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeEngine(http.server.BaseHTTPRequestHandler):
    token = "t0k3n"
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/health":
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        msg = json.loads(self.rfile.read(n) or b"{}")
        type(self).seen.append({"msg": msg, "auth": self.headers.get("Authorization"), "sid": self.headers.get("Mcp-Session-Id"),
                                "accept": self.headers.get("Accept"), "proto": self.headers.get("MCP-Protocol-Version")})
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
        if "id" not in msg:
            self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
        if msg["method"] == "initialize":
            body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Mcp-Session-Id", "sess-42")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        # everything else answers as an SSE stream: a progress notification then the result
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        note = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1, "total": 1}})
        res = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "replay_recipe"}]}})
        self.wfile.write(f"event: message\ndata: {note}\n\n".encode()); self.wfile.flush()
        self.wfile.write(f"event: message\ndata: {res}\n\n".encode()); self.wfile.flush()


@pytest.fixture
def fake_engine(tmp_path):
    port = _free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _FakeEngine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _FakeEngine.seen = []
    token_file = tmp_path / "token"
    token_file.write_text(_FakeEngine.token)
    yield {"url": f"http://127.0.0.1:{port}/mcp", "token_file": token_file}
    srv.shutdown()


def _run_shim(env_extra: dict, lines: list[dict], timeout=15) -> tuple[list[dict], str]:
    env = {**os.environ, **env_extra}
    p = subprocess.Popen([NODE, str(SHIM)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8")
    inp = "".join(json.dumps(m) + "\n" for m in lines)
    out, err = p.communicate(inp, timeout=timeout)
    msgs = [json.loads(l) for l in out.splitlines() if l.strip()]
    return msgs, err


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
INITD = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def test_shim_forwards_json_rpc_keeps_the_session_and_streams_sse(fake_engine):
    msgs, err = _run_shim({"RECIPE_ENGINE_URL": fake_engine["url"], "RECIPE_ENGINE_TOKEN_FILE": str(fake_engine["token_file"]),
                           "RECIPE_ENGINE_HOST_EXE": "C:/nope/none.exe"}, [INIT, INITD, LIST])
    assert [m.get("id") for m in msgs if "id" in m] == [1, 2], (msgs, err)
    assert msgs[0]["result"]["serverInfo"]["name"] == "fake"
    assert any(m.get("method") == "notifications/progress" for m in msgs), "the SSE notification was not relayed"
    assert msgs[-1]["result"]["tools"][0]["name"] == "replay_recipe"
    seen = _FakeEngine.seen
    assert [s["msg"]["method"] for s in seen] == ["initialize", "notifications/initialized", "tools/list"]
    assert seen[0]["sid"] is None and seen[1]["sid"] == "sess-42" and seen[2]["sid"] == "sess-42", "Mcp-Session-Id not carried"
    assert all(s["auth"] == "Bearer t0k3n" for s in seen)
    assert all("text/event-stream" in s["accept"] and s["proto"] for s in seen)


def test_shim_reports_a_missing_token_and_a_refused_token_as_json_rpc_errors(fake_engine, tmp_path):
    msgs, err = _run_shim({"RECIPE_ENGINE_URL": fake_engine["url"], "RECIPE_ENGINE_TOKEN_FILE": str(tmp_path / "missing"),
                           "RECIPE_ENGINE_HOST_EXE": "C:/nope/none.exe"}, [INIT])
    assert msgs and msgs[0]["error"]["code"] == -32000 and "token" in msgs[0]["error"]["message"]
    bad = tmp_path / "bad"; bad.write_text("wrong")
    msgs, err = _run_shim({"RECIPE_ENGINE_URL": fake_engine["url"], "RECIPE_ENGINE_TOKEN_FILE": str(bad),
                           "RECIPE_ENGINE_HOST_EXE": "C:/nope/none.exe"}, [INIT])
    assert msgs[0]["error"]["code"] == -32002 and "refused the token" in msgs[0]["error"]["message"]


def test_shim_says_so_when_the_host_is_down_and_cannot_be_started(tmp_path):
    port = _free_port()
    msgs, err = _run_shim({"RECIPE_ENGINE_URL": f"http://127.0.0.1:{port}/mcp", "RECIPE_ENGINE_TOKEN_FILE": str(tmp_path / "t"),
                           "RECIPE_ENGINE_HOST_EXE": str(tmp_path / "missing.exe"), "RECIPE_ENGINE_START_TIMEOUT_MS": "500"}, [INIT])
    assert msgs and msgs[0]["error"]["code"] == -32001 and "not running" in msgs[0]["error"]["message"]
    assert "install the host first" in err


@pytest.mark.slow
def test_shim_against_the_real_engine_lists_34_tools(tmp_path):
    """Real host (python -m recipe_engine serve --no-tray) on a free port, real shim."""
    port = _free_port()
    data = tmp_path / "data"
    env = {**os.environ, "RECIPE_ENGINE_DATA_DIR": str(data), "RECIPE_ENGINE_UNSEALED": "1"}
    host = subprocess.Popen([sys.executable, "-m", "recipe_engine", "serve", "--no-tray", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import urllib.request
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            pytest.fail("the host did not come up")
        msgs, err = _run_shim({"RECIPE_ENGINE_URL": f"http://127.0.0.1:{port}/mcp", "RECIPE_ENGINE_TOKEN_FILE": str(data / "token"),
                               "RECIPE_ENGINE_HOST_EXE": "C:/nope/none.exe"}, [INIT, INITD, LIST], timeout=40)
        results = {m["id"]: m for m in msgs if "id" in m}
        assert results[1]["result"]["serverInfo"]["name"] == "recipe-engine", (msgs, err)
        assert len(results[2]["result"]["tools"]) == 34, err
    finally:
        host.terminate()
        try:
            host.wait(timeout=5)
        except Exception:
            host.kill()
