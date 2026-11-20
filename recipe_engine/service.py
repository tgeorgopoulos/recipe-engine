"""The host process. Owns input for this Windows user and serves MCP.

Start order matters and is the reason this file exists:
  DPI awareness (before uiautomation is ever imported)
  -> single-instance mutex (a second engine would fight for the mouse)
  -> install token
  -> invariant self-test (a violation means: serve engine_status only, refuse the rest)
  -> desktop-control check (mouse can move, screen can be captured)
  -> uvicorn on 127.0.0.1:PORT in a thread (MCP at /mcp + the panel), behind the token
  -> tray icon on the main thread (or an idle wait with --no-tray)

`--stdio` serves the same MCPServer over stdin/stdout instead - for a Claude Code
plugin running on this machine - and takes the same mutex, so there is still exactly
one engine. The Node shim in the .mcpb is how Claude Desktop reaches the HTTP one.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Optional

from recipe_engine import auth, config, display, invariants
from recipe_engine.config import log
from recipe_engine.engine import Engine

MUTEX_NAME = "Global\\RecipeEngine.v1"
_mutex_handle = None


def take_single_instance() -> bool:
    """True if this process now owns the engine mutex; False if another engine runs."""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    ERROR_ALREADY_EXISTS = 183
    k32 = ctypes.windll.kernel32
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return True                                   # cannot tell; do not block startup
    if k32.GetLastError() == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(h)
        return False
    _mutex_handle = h
    return True


def build_app(eng: Engine):
    """The Starlette ASGI app: MCP at config.MCP_PATH plus the panel, behind the token."""
    from mcp.server.transport_security import TransportSecuritySettings

    from recipe_engine import web
    from recipe_engine.mcp_server import build_server

    server = build_server(eng)
    hosts = [f"{config.BIND_HOST}:{config.PORT}", f"localhost:{config.PORT}", f"127.0.0.1:{config.PORT}"]
    app = server.streamable_http_app(
        streamable_http_path=config.MCP_PATH,
        host=config.BIND_HOST,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                     allowed_origins=[f"http://{h}" for h in hosts]),
    )
    web.attach(app, eng)
    app = auth.BearerAuthMiddleware(app, auth.current_token)
    return app


def prepare(eng: Engine) -> None:
    """Everything that must be true before a single tool is served."""
    display.make_dpi_aware()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    auth.ensure_token()
    eng.invariant_problems = invariants.check()
    if eng.invariant_problems:
        log("INVARIANTS", "!!! PROTECTED SAFETY INVARIANTS ARE BROKEN - only engine_status will answer:")
        for p in eng.invariant_problems:
            log("INVARIANTS", f"    - {p}")
    else:
        log("INVARIANTS", f"ok ({len(invariants.PROTECTED_INVARIANTS)} invariants)")
    try:
        chk = display.check_control_permissions()
        eng.control_ok, eng.control_message = chk.ok, chk.message
    except Exception as e:
        eng.control_ok, eng.control_message = False, f"control check failed: {e!r}"
    log("CHECK", eng.control_message)
    from recipe_engine import uia, vdesk
    ok, why = vdesk.available()
    eng.virtual_desktop = {"available": ok, "reason": why, "used": False,
                           "note": "not used: a window parked on another virtual desktop loses its UI Automation subtree"}
    st = uia.self_test()
    eng.background_available = bool(st.get("uia_ok"))
    eng.background_reason = ("pattern-driven replay in place (no input taken, focus restored) for recipes whose every step "
                             "has a UIA pattern" if eng.background_available else f"UI Automation unavailable: {st.get('error')}")
    log("CHECK", f"background replay {'available' if eng.background_available else 'unavailable'}: {eng.background_reason}")


class _UvicornThread(threading.Thread):
    def __init__(self, app, host: str, port: int) -> None:
        super().__init__(name="uvicorn", daemon=True)
        import uvicorn
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on", access_log=False)
        self.server = uvicorn.Server(self.config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def serve(*, stdio: bool = False, no_tray: bool = False, bind: Optional[str] = None, port: Optional[int] = None) -> int:
    if bind:
        config.BIND_HOST = bind
    if port:
        config.PORT = int(port)
    eng = Engine()
    if not take_single_instance():
        log("SERVICE", f"another Recipe Engine already runs on this machine (use the HTTP one at http://{config.BIND_HOST}:{config.PORT})")
        return config.EXIT_ALREADY_RUNNING
    prepare(eng)

    if stdio:
        from recipe_engine.mcp_server import build_server
        log("SERVICE", "serving MCP over stdio")
        build_server(eng).run(transport="stdio")
        return config.EXIT_OK

    app = build_app(eng)
    srv = _UvicornThread(app, config.BIND_HOST, config.PORT)
    srv.start()
    for _ in range(50):
        if getattr(srv.server, "started", False):
            break
        time.sleep(0.1)
    if not getattr(srv.server, "started", False):
        log("SERVICE", f"could not bind {config.BIND_HOST}:{config.PORT} (port busy?)")
        return config.EXIT_CHECK_FAILED
    url = f"http://{config.BIND_HOST}:{config.PORT}"
    log("SERVICE", f"Recipe Engine ready: MCP at {url}{config.MCP_PATH}, panel at {url}/  (token: {config.TOKEN_FILE})")
    from recipe_engine.scheduler import Scheduler
    sched = Scheduler(eng).start()                    # just another client of eng.replay
    stop = threading.Event()
    try:
        if no_tray:
            stop.wait()
        else:
            from recipe_engine import tray
            tray.run(eng, url, on_quit=stop.set)
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop()
        srv.stop()
        try:
            from recipe_engine import browser
            browser.close_service()
        except Exception:
            pass
        log("SERVICE", "stopped")
    return config.EXIT_OK
