"""The local control panel: a few Starlette routes attached to the SAME app that
serves MCP, behind the same bearer token. Loopback only.

* Reads: engine status, recipes with stats, transcripts, the log ring, live events (SSE).
* Writes: replay a recipe, abort the running replay, forget a recipe. Every POST must
  be application/json (a form on another site cannot POST JSON cross-origin without a
  preflight we never answer) and must carry the token like every other request.
* The page is static/index.html, read on every request so editing it takes effect on
  refresh. It gets the token from the URL fragment the tray menu opens
  (http://127.0.0.1:47391/#t=...), keeps it in sessionStorage, and strips it from the URL.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import time
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from recipe_engine import config, events, recipes, transcripts
from recipe_engine.config import log

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
_FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#04060c"/>'
            b'<circle cx="16" cy="16" r="9" fill="none" stroke="#19e6ff" stroke-width="2"/>'
            b'<circle cx="16" cy="16" r="3.5" fill="#ff2bd6"/></svg>')


def recipe_rows() -> list[dict]:
    rows = []
    for r in recipes.load_all():
        runs = r.runs or {}
        ok, bad = int(runs.get("success", 0)), int(runs.get("fail", 0))
        total = ok + bad
        rows.append({
            "id": r.id, "name": r.name or r.command_normalized, "description": r.description,
            "surface": r.surface, "steps": len(r.steps), "apps": list(r.app_hints or []),
            "success": ok, "fail": bad, "rate": round(100.0 * ok / total) if total else None,
            "avg_s": round(float(r.avg_duration_s or 0.0), 1),
            "last_used": r.last_used or "", "created": r.created or "",
            "quarantined": bool(r.quarantined), "stale": bool(r.stale),
            "status": "quarantined" if r.quarantined else ("expired" if r.stale else "ready"),
        })
    rows.sort(key=lambda x: x["last_used"], reverse=True)
    return rows


def _local(request: Request) -> bool:
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    return host in _LOCAL_HOSTS or host == config.BIND_HOST


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _json(obj: Any, status: int = 200) -> Response:
    return _no_store(JSONResponse(obj, status_code=status))


async def _body(request: Request) -> dict:
    if (request.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
        raise ValueError("POST must be application/json")
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise ValueError("body too large")
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        raise ValueError("bad JSON body")
    return data if isinstance(data, dict) else {}


def attach(app, eng) -> None:
    """Add the panel routes to a Starlette app (the MCP app, so its lifespan is kept)."""

    def guarded(fn):
        async def inner(request: Request):
            if not _local(request):
                return _json({"error": "local access only"}, 403)
            try:
                return await fn(request)
            except ValueError as e:
                return _json({"error": str(e)}, 400)
            except Exception as e:
                log("WEB", f"{request.method} {request.url.path} failed: {e!r}")
                return _json({"error": repr(e)}, 500)
        return inner

    @guarded
    async def page(request: Request):
        f = config.WEB_DIR / "index.html"
        if not f.exists():
            return PlainTextResponse("static/index.html is missing", status_code=500)
        return _no_store(Response(f.read_bytes(), media_type="text/html; charset=utf-8"))

    async def favicon(request: Request):
        return Response(_FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    async def health(request: Request):
        return _json({"ok": True, "pid": os.getpid(), "t": time.time(), "version": eng.status()["version"]})

    @guarded
    async def state(request: Request):
        return _json(eng.status())

    @guarded
    async def recipes_list(request: Request):
        return _json({"recipes": recipe_rows()})

    @guarded
    async def recipe_stats(request: Request):
        r = recipes.load_by_name(request.path_params["name"])
        if r is None:
            return _json({"error": "no such recipe"}, 404)
        return _json({"name": r.name or r.command_normalized, "text": transcripts.stats_text(r),
                      "steps": recipes.describe_steps(r), "runs": transcripts.last(r.id, 30)})

    @guarded
    async def replay(request: Request):
        name = request.path_params["name"]
        data = await _body(request)
        out = await asyncio.to_thread(eng.replay, name, dict(data.get("inputs") or {}), trigger="panel")
        return _json(out)

    @guarded
    async def abort(request: Request):
        await _body(request)
        return _json({"ok": eng.abort()})

    @guarded
    async def forget(request: Request):
        data = await _body(request)
        r = recipes.load_by_name(str(data.get("name") or data.get("id") or ""))
        if r is None:
            return _json({"ok": False, "error": "no such recipe"}, 404)
        ok = recipes.forget(r.id)
        try:
            transcripts.path_for(r.id).unlink(missing_ok=True)      # same as the MCP tool: history goes with the recipe
        except OSError:
            pass
        log("WEB", f"recipe {r.id} {'forgotten' if ok else 'not found'} (panel)")
        return _json({"ok": ok})

    @guarded
    async def jobs(request: Request):
        try:
            from recipe_engine import scheduler
            return _json({"jobs": [j.to_dict() for j in scheduler.list_jobs()]})
        except Exception:
            return _json({"jobs": []})

    @guarded
    async def log_tail(request: Request):
        n = max(1, min(int(request.query_params.get("n", "300")), config.LOG_RING_SIZE))
        with config._log_lock:
            lines = list(config.LOG_RING)[-n:]
        return _json({"lines": lines})

    @guarded
    async def sse(request: Request):
        since = int(request.query_params.get("since", "0") or 0)

        async def gen():
            q = events.subscribe()
            try:
                for ev in events.history(since):
                    yield _sse_bytes(ev)
                yield _sse_bytes({"kind": "hello", "id": 0, "t": time.time(), "snapshot": eng.status()})
                last_beat = time.time()
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.to_thread(q.get, True, 5.0)
                        yield _sse_bytes(ev)
                    except queue.Empty:
                        pass
                    if time.time() - last_beat > 15:
                        yield b": ping\n\n"
                        last_beat = time.time()
            finally:
                events.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    for path, endpoint, methods in (
        ("/", page, ["GET"]), ("/index.html", page, ["GET"]), ("/favicon.ico", favicon, ["GET"]),
        ("/api/health", health, ["GET"]), ("/api/state", state, ["GET"]), ("/api/events", sse, ["GET"]),
        ("/api/recipes", recipes_list, ["GET"]), ("/api/recipes/{name}/stats", recipe_stats, ["GET"]),
        ("/api/recipes/{name}/replay", replay, ["POST"]), ("/api/replay/abort", abort, ["POST"]),
        ("/api/recipes/forget", forget, ["POST"]), ("/api/jobs", jobs, ["GET"]), ("/api/log", log_tail, ["GET"]),
    ):
        app.router.routes.append(Route(path, endpoint, methods=methods))


def _sse_bytes(ev: dict) -> bytes:
    return b"id: " + str(ev.get("id", 0)).encode() + b"\ndata: " + json.dumps(ev, default=str, ensure_ascii=False).encode("utf-8") + b"\n\n"
