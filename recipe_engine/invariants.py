"""
Protected invariants v2 - the safety properties no edit, human or automated, may
silently remove. Approved by the user on 2026-09-20; changing THIS LIST needs the
user's explicit approval again (README: "safety invariants").

Invariants 13 (Esc abort + release keys, abort.py) and 17 (signed export carries
no secrets, export.py) join the list when those modules land; their slots are
reserved below so the numbering matches the approved text.

check() reads the source files from disk (not the imported modules, so it sees
what was actually written) and returns a list of violations. An empty list means
every invariant is intact. The service runs it at start and refuses every acting
tool while it is non-empty (invariant 15).

The self-test (`python -m recipe_engine.invariants --self-test`) copies the
protected files to a temp dir, applies each MUTATION - a verbatim snippet of
current source swapped for a hollowed-out version - and requires check() to flag
it. A SKIP (the snippet is no longer in the source) is a FAILURE on purpose: it
means a refactor drifted a pinned mechanism and this table must be updated
deliberately, not silently.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

PROTECTED_INVARIANTS = (
    "1. One credential regex: safety._SECRET_RE (password, passcode, passwd, one-time, verification code, 2fa, "
    "mfa, otp, pin); every other gate imports it; no other re.compile containing 'password' in the package.",
    "2. Typing is double-gated: desktop.type_text calls guard_text(text) AND guard_focused_element() before any "
    "pyautogui call; browser.op_fill keeps all three layers (text/selector regex, type=='password', login_seen).",
    "3. Login detection after every browser action: _after_action calls detect_login_page and sets login_seen; "
    "every op in OPS (op_download included) ends in _after_action; op_screenshot runs the detector.",
    "4. Nothing unsafe is ever saved: recipes.should_capture refuses destructive / login_seen / login_blocked / "
    "credential_flagged / cancelled / error / empty / polling traces; capture() goes through it; recorder.stop "
    "calls capture().",
    "5. Destructive steps are never recorded: recorder.record drops any step where is_destructive() (selector, "
    "typed value, UIA target name) and sets destructive_hit; recorder.start refuses a destructive name/description.",
    "6. Replay is verified and never sleeps: browser replay asserts _assert_expect_browser per step and raises "
    "ReplayFailed; desktop replay asserts _wait_title; no sleep() in the browser replay path; _wait_title and "
    "uia.resolve poll only inside a deadline-bounded loop.",
    "7. Quarantine is honoured before step 1: recipes.replay raises on quarantined first; engine.replay maps it "
    "to status 'quarantined'.",
    "8. Portable targets first, pixels only on a matching display: resolver.resolve_target tries UIA first, "
    "touches step['pixel'] only inside `if display_matches(`, otherwise raises UnresolvableElement.",
    "9. One owner of the input: engine.desktop_lock is a module-level threading.Lock; engine.replay and the "
    "desktop primitives (mcp_server._act) go through acquire_input(); block=False -> 'busy', never queued.",
    "10. Send-whitelist on replay: engine.replay returns 'refused' on is_destructive() (name, description, step "
    "values/selectors, UIA names) BEFORE taking the input lock.",
    "11. Parameters are explicit: recipes.substitute raises MissingParam; engine.replay calls it before "
    "acquire_input(); a credential-looking input -> 'refused'.",
    "12. Loopback + bearer token: service.build_app wraps the app in BearerAuthMiddleware (hmac.compare_digest), "
    "binds config.BIND_HOST (default 127.0.0.1) and passes TransportSecuritySettings(enable_dns_rebinding_protection=True).",
    "13. (lands with abort.py) Esc aborts within ESC_POLL_MS <= 50 and release_all_keys() emits KEYUP for the "
    "modifier keys and a left-mouse-up.",
    "14. Zero LLM: no module in the package imports claude_agent_sdk / anthropic / openai; no 'claude' string is "
    "passed to subprocess.",
    "15. The gate is wired: service.prepare stores invariants.check() on the engine; mcp_server.guard returns "
    "invariants_failed for every acting tool while it is non-empty.",
    "16. Sealed at rest: recipes.save writes via sealed.write_sealed_json(); session_store writes via seal(); "
    "plaintext only under RECIPE_ENGINE_UNSEALED.",
    "17. (lands with export.py) export_recipe never serialises runs/history/cookies/storage, refuses on "
    "_SECRET_RE, signs with hmac.new(); import_recipe verifies and re-runs the destructive + credential checks.",
)

PROTECTED_FILES = ("safety.py", "desktop.py", "browser.py", "recipes.py", "resolver.py", "uia.py", "recorder.py",
                   "engine.py", "mcp_server.py", "auth.py", "service.py", "sealed.py", "session_store.py")
# Reserved: "abort.py" (13) and "export.py" (17) are appended when they exist.
OPTIONAL_FILES = ("abort.py", "export.py")

_BANNED_IMPORTS = {"claude_agent_sdk", "anthropic", "openai"}


def _src(name: str, root: Path | None = None) -> str:
    return ((root or PACKAGE_DIR) / name).read_text(encoding="utf-8", errors="replace")


def _func_source(tree: ast.AST, source: str, name: str) -> str | None:
    """Source text of the top-level or nested function/async function called `name`."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return None


def _method_source(tree: ast.AST, source: str, cls: str, name: str) -> str | None:
    """Source text of method `name` of class `cls` (not the first `name` in the file)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    return ast.get_source_segment(source, sub) or ""
    return None


def _imports_of(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def _subprocess_calls_with(tree: ast.AST, needle: str) -> bool:
    """True if any subprocess.* / Popen call carries a string constant containing `needle`."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
        if owner != "subprocess" and name not in ("Popen", "run", "check_output", "check_call", "call"):
            continue
        if owner and owner != "subprocess":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and needle in sub.value.lower():
                return True
    return False


def _before(hay: str, first: str, second: str) -> bool:
    """`first` occurs in `hay`, and before the first occurrence of `second` (or `second` is absent)."""
    a, b = hay.find(first), hay.find(second)
    return a >= 0 and (b < 0 or a < b)


def check(root: Path | None = None) -> list[str]:
    v: list[str] = []
    base = root or PACKAGE_DIR
    files = list(PROTECTED_FILES) + [f for f in OPTIONAL_FILES if (base / f).exists()]
    try:
        S = {f: _src(f, root) for f in files}
        T = {f: ast.parse(s) for f, s in S.items()}
    except (OSError, SyntaxError) as e:
        return [f"cannot parse a protected file: {e!r}"]

    def fn(f: str, name: str) -> str | None:
        return _func_source(T[f], S[f], name)

    # 1. one credential regex ------------------------------------------------
    m = re.search(r"^_SECRET_RE\s*=\s*re\.compile\((.+?)\)\s*$", S["safety.py"], re.M | re.S)
    if not m:
        v.append("safety._SECRET_RE missing")
    else:
        for kw in ("password", "passcode", "passwd", "one[- ]time", "verification code", "2fa", "mfa", "otp", "pin"):
            if kw not in m.group(1).lower():
                v.append(f"safety._SECRET_RE no longer matches '{kw}'")
    if not re.search(r"^from recipe_engine\.safety import .*\b_SECRET_RE\b", S["browser.py"], re.M):
        v.append("browser.py no longer imports _SECRET_RE from safety (second gate?)")
    if "_SECRET_RE" not in (fn("recipes.py", "_perform_browser") or ""):
        v.append("recipes._perform_browser no longer checks _SECRET_RE before a fill")
    for f in sorted(base.glob("*.py")):
        if f.name in ("safety.py", "invariants.py"):       # the checker holds the mutation strings
            continue
        for cm in re.finditer(r"re\.compile\(([^\n]*)", f.read_text(encoding="utf-8", errors="replace")):
            if "password" in cm.group(1).lower():
                v.append(f"{f.name} compiles its own password regex - there is one gate, safety._SECRET_RE")

    # 2. typing is double-gated -------------------------------------------------
    tt = fn("desktop.py", "type_text")
    if tt is None:
        v.append("desktop.type_text missing")
    else:
        if not _before(tt, "safety.guard_text(", "_pyautogui()"):
            v.append("desktop.type_text no longer guards the text before touching pyautogui")
        if not _before(tt, "safety.guard_focused_element(", "_pyautogui()"):
            v.append("desktop.type_text no longer checks the focused control (password field) before typing")
    if fn("safety.py", "guard_focused_element") is None or "is_password" not in (fn("safety.py", "element_is_secret") or ""):
        v.append("safety.guard_focused_element / element_is_secret no longer check IsPassword")
    of = fn("browser.py", "op_fill") or ""
    if "_SECRET_RE.search(text)" not in of or "_SECRET_RE.search(sel)" not in of:
        v.append("browser.op_fill no longer refuses credential text / selectors")
    if '"password"' not in of:
        v.append("browser.op_fill no longer refuses password fields")
    if of.count("login_seen = True") < 2:
        v.append("browser.op_fill refusals no longer mark login_seen")

    # 3. login detection after every browser action -------------------------------
    if fn("browser.py", "detect_login_page") is None:
        v.append("browser.detect_login_page missing")
    aa = fn("browser.py", "_after_action") or ""
    if "detect_login_page(" not in aa or "login_seen = True" not in aa:
        v.append("browser._after_action no longer runs the login detector")
    ops_m = re.search(r"^OPS\b[^\n=]*=\s*\{(.+?)\}", S["browser.py"], re.M | re.S)
    op_names = sorted(set(re.findall(r"\bop_\w+", ops_m.group(1)))) if ops_m else []
    if not op_names:
        v.append("browser.OPS table missing")
    if "op_download" not in op_names:
        v.append("browser.OPS no longer routes download through op_download")
    for name in op_names:
        src = fn("browser.py", name)
        if src is None:
            v.append(f"browser.{name} listed in OPS but missing")
            continue
        if name == "op_screenshot":
            if "detect_login_page(" not in src:
                v.append("browser.op_screenshot no longer runs the login detector")
        elif "_after_action(" not in src:
            v.append(f"browser.{name} no longer ends in _after_action (login detection skipped)")

    # 4. nothing unsafe is ever saved ---------------------------------------------
    sc = fn("recipes.py", "should_capture") or ""
    if not re.search(r"if\s+destructive\s*:\s*\n\s*return\s+False", sc):
        v.append("recipes.should_capture no longer guards on destructive")
    for guard in ("login_seen", "login_blocked", "credential_flagged", "cancelled"):
        if not re.search(r"if[^\n]*getattr\(result,\s*[\"']" + guard + r"[\"'][^\n]*:\s*\n\s*return\s+False", sc):
            v.append(f"recipes.should_capture no longer guards on {guard}")
    for word in ("error", "polling"):
        if word not in sc:
            v.append(f"recipes.should_capture no longer guards on {word}")
    if "should_capture(" not in (fn("recipes.py", "capture") or ""):
        v.append("recipes.capture no longer goes through should_capture")
    if "recipes.capture(" not in (fn("recorder.py", "stop") or ""):
        v.append("recorder.stop no longer saves through recipes.capture")

    # 5. destructive steps are never recorded ------------------------------------
    rr = fn("recorder.py", "record") or ""
    if "safety.is_destructive(" not in rr or "self.destructive_hit =" not in rr or not re.search(r"is_destructive\(text\):[^\n]*\n(?:[^\n]*\n){0,3}\s*return\b", rr):
        v.append("recorder.record no longer drops destructive steps")
    rs = fn("recorder.py", "start") or ""
    if "safety.is_destructive(" not in rs or "return False" not in rs:
        v.append("recorder.start no longer refuses a destructive name/description")

    # 6. verified replay, no sleeps -----------------------------------------------
    rb = fn("recipes.py", "replay_browser_steps") or ""
    if "_assert_expect_browser(" not in rb or "raise ReplayFailed" not in rb:
        v.append("recipes.replay_browser_steps no longer asserts every step's expect / raises ReplayFailed")
    rd = fn("recipes.py", "_replay_desktop") or ""
    if "_wait_title(" not in rd or "raise ReplayFailed" not in rd:
        v.append("recipes._replay_desktop no longer asserts the window after every step / raises ReplayFailed")
    for name in ("replay_browser_steps", "_perform_browser", "_assert_expect_browser"):
        if re.search(r"\bsleep\(", fn("recipes.py", name) or ""):
            v.append(f"recipes.{name} uses a fixed sleep() (replay must wait on conditions, never on time)")
    wt = fn("recipes.py", "_wait_title") or ""
    if "deadline" not in wt or "while" not in wt or not re.search(r"if now >= deadline:\s*\n\s*return", wt):
        v.append("recipes._wait_title is no longer a deadline-bounded poll")
    ur = fn("uia.py", "resolve") or ""
    if "deadline" not in ur or re.search(r"\bsleep\(", ur):
        v.append("uia.resolve is no longer deadline-bounded / sleeps")

    # 7. quarantine before step 1 --------------------------------------------------
    rp = fn("recipes.py", "replay") or ""
    q = re.search(r"if recipe\.quarantined:\s*\n\s*raise ReplayFailed", rp)
    if not q or rp.find("_replay_") < q.start():
        v.append("recipes.replay no longer refuses quarantined recipes before the first step")
    er = fn("engine.py", "replay") or ""
    if not re.search(r"if r\.quarantined:\s*\n\s*return[^\n]*\"quarantined\"", er):
        v.append("engine.replay no longer maps a quarantined recipe to status 'quarantined'")

    # 8. UIA first, pixels only on a matching display ------------------------------
    rt = fn("resolver.py", "resolve_target") or ""
    if "uia.resolve(" not in rt or not re.search(r"raise UnresolvableElement\(detail", rt):   # the final, unconditional refusal
        v.append("resolver.resolve_target no longer resolves by UIA / raises UnresolvableElement")
    elif rt.find("uia.resolve(") > rt.find("rel_to_logical("):
        v.append("resolver.resolve_target tries pixels before UIA")
    for pm in re.finditer(r"rel_to_logical\(", rt):
        line_start = rt.rfind("\n", 0, pm.start())
        guard_line = rt[rt.rfind("\n", 0, line_start) + 1:line_start]
        if "if " not in guard_line or "display_matches(" not in guard_line:
            v.append("resolver.resolve_target uses pixel coordinates without a display_matches guard")
    if "resolve_target(" not in rd:
        v.append("recipes._replay_desktop no longer resolves targets through resolver.resolve_target")

    # 9. one owner of the input ------------------------------------------------------
    if not re.search(r"^desktop_lock\s*=\s*threading\.Lock\(\)", S["engine.py"], re.M):
        v.append("engine.desktop_lock (module-level threading.Lock) missing")
    acq = fn("engine.py", "acquire_input") or ""
    if "desktop_lock.acquire(" not in acq or "desktop_lock.release()" not in acq or "raise InputBusy" not in acq:
        v.append("engine.acquire_input no longer owns/releases desktop_lock or raises InputBusy")
    if "acquire_input(" not in er or not re.search(r"except InputBusy[^\n]*:\s*\n\s*return[^\n]*\"busy\"", er):
        v.append("engine.replay no longer takes the input lock / answers 'busy'")
    act = fn("mcp_server.py", "_act") or ""
    if "acquire_input(" not in act or "block=False" not in act or not re.search(r"except InputBusy[^\n]*:\s*\n\s*return[^\n]*busy", act):
        v.append("mcp_server._act no longer takes the input lock non-blocking / answers 'busy'")

    # 10. send-whitelist on replay ----------------------------------------------------
    d = re.search(r"if safety\.is_destructive\(text\):\s*\n\s*return[^\n]*\"refused\"", er)
    if not d:
        v.append("engine.replay no longer refuses destructive recipes")
    elif d.start() > er.find("acquire_input("):
        v.append("engine.replay checks is_destructive only after taking the input lock")
    if "uia" not in er[:d.start()] if d else True:
        v.append("engine.replay's destructive check no longer reads the UIA target names")

    # 11. parameters are explicit -----------------------------------------------------
    if fn("recipes.py", "substitute") is None or "raise MissingParam" not in (fn("recipes.py", "substitute") or ""):
        v.append("recipes.substitute no longer raises MissingParam")
    s_pos = er.find("recipes.substitute(")
    if s_pos < 0 or s_pos > er.find("acquire_input(") or not re.search(r"except recipes\.MissingParam[^\n]*:\s*\n\s*return[^\n]*\"refused\"", er):
        v.append("engine.replay no longer substitutes inputs (MissingParam -> refused) before the input lock")
    if not re.search(r"except safety\.CredentialRefused[^\n]*:\s*\n\s*return[^\n]*\"refused\"", er):
        v.append("engine.replay no longer refuses credential-looking inputs")

    # 12. loopback + bearer token ------------------------------------------------------
    ba = fn("service.py", "build_app") or ""
    if "auth.BearerAuthMiddleware(" not in ba:
        v.append("service.build_app no longer wraps the app in BearerAuthMiddleware")
    if "host=config.BIND_HOST" not in ba:
        v.append("service.build_app no longer binds config.BIND_HOST")
    if "TransportSecuritySettings(enable_dns_rebinding_protection=True" not in ba:
        v.append("service.build_app no longer enables DNS-rebinding protection")
    if not re.search(r'^BIND_HOST\s*=\s*"127\.0\.0\.1"', _src("config.py", root) if (base / "config.py").exists() else "", re.M):
        v.append("config.BIND_HOST default is no longer 127.0.0.1")
    bm = _method_source(T["auth.py"], S["auth.py"], "BearerAuthMiddleware", "__call__") or ""
    if "hmac.compare_digest(" not in bm:
        v.append("auth.BearerAuthMiddleware no longer compares tokens with hmac.compare_digest")

    # 13. Esc abort + release keys (when abort.py exists) --------------------------------
    if "abort.py" in S:
        ab = S["abort.py"]
        if "GetAsyncKeyState" not in ab or "release_all_keys" not in ab:
            v.append("abort.py no longer polls GetAsyncKeyState / releases keys")
        fire = fn("abort.py", "fire") or ""
        if "release_all_keys(" not in fire:
            v.append("abort.AbortWatch.fire no longer releases keys")
        rk = fn("abort.py", "release_all_keys") or ""
        if not re.search(r"for vk in MODIFIER_VKS:\s*\n\s*batch\.append\(_keyup\(vk, KEYEVENTF_KEYUP\)\)", rk) or "MOUSEEVENTF_LEFTUP" not in rk:
            v.append("abort.release_all_keys no longer emits KEYUP / left-mouse-up")
        cm = re.search(r"^ESC_POLL_MS\s*=\s*(\d+)", _src("config.py", root), re.M)
        if not cm or int(cm.group(1)) > 50:
            v.append("config.ESC_POLL_MS is no longer <= 50")

    # 14. zero LLM ------------------------------------------------------------------------
    for f in sorted(base.glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        hit = _BANNED_IMPORTS & _imports_of(tree)
        if hit:
            v.append(f"{f.name} imports {sorted(hit)} - the engine must never call a model")
        if _subprocess_calls_with(tree, "claude"):
            v.append(f"{f.name} launches 'claude' through subprocess - the engine must never call a model")

    # 15. the gate is wired -----------------------------------------------------------------
    if "eng.invariant_problems = invariants.check()" not in (fn("service.py", "prepare") or ""):
        v.append("service.prepare no longer stores invariants.check() on the engine")
    g = fn("mcp_server.py", "guard") or ""
    if "eng.invariant_problems" not in g or "_REFUSED_INVARIANTS" not in g:
        v.append("mcp_server.guard no longer refuses on invariant problems")
    if "invariants_failed" not in S["mcp_server.py"]:
        v.append("mcp_server no longer names the refusal 'invariants_failed'")
    if "guard()" not in act or "guard()" not in (fn("mcp_server.py", "replay_recipe") or "") \
            or "guard()" not in (fn("mcp_server.py", "record_recipe") or "") or "guard()" not in (fn("mcp_server.py", "bop") or ""):
        v.append("an acting tool path (desktop, replay, record, browser) no longer calls guard()")

    # 16. sealed at rest ----------------------------------------------------------------------
    if "sealed.write_sealed_json(" not in (fn("recipes.py", "save") or ""):
        v.append("recipes.save no longer writes through sealed.write_sealed_json")
    if "seal(" not in (fn("session_store.py", "_write") or ""):
        v.append("session_store._write no longer seals the snapshot")
    sb = fn("sealed.py", "seal_bytes") or ""
    if "MAGIC_DPAPI" not in sb or "_dpapi(" not in sb or "if sealed():" not in sb:
        v.append("sealed.seal_bytes no longer uses DPAPI when sealing is on")
    if "RECIPE_ENGINE_UNSEALED" not in S["sealed.py"]:
        v.append("sealed.py plaintext switch is no longer RECIPE_ENGINE_UNSEALED")

    # 17. signed export carries no secrets (when export.py exists) -------------------------------
    if "export.py" in S:
        helper = fn("export.py", "_safety_problems") or ""      # the shared refusal: destructive, _SECRET_RE, forbidden keys
        ex = fn("export.py", "export_recipe") or ""
        for word in ("runs", "history"):
            if word not in ex:
                v.append(f"export.export_recipe no longer strips '{word}'")
        if "cookie" not in helper or "_FORBIDDEN_KEY_RE" not in helper:
            v.append("export.export_recipe no longer strips 'cookie' keys")
        if "_safety_problems(" not in ex or "_SECRET_RE.search(" not in helper or "is_destructive(" not in helper                 or "hmac.new(" not in S["export.py"]:
            v.append("export.export_recipe no longer refuses secrets / signs with hmac")
        im = fn("export.py", "import_recipe") or ""
        if "_safety_problems(" not in im or "compare_digest(" not in im or "_FORBIDDEN_KEY_RE" not in im:
            v.append("export.import_recipe no longer verifies the signature / re-runs the safety checks")
    return v


def prompt_text() -> str:
    return "\n".join(PROTECTED_INVARIANTS)


# ---------------------------------------------------------------------------
# Self-test mutations: (file, verbatim old, new, substring the violation must contain)
# ---------------------------------------------------------------------------
MUTATIONS: list[tuple[str, str, str, str]] = [
    # 1
    ("safety.py", "password|passcode", "passcode", "_SECRET_RE no longer matches 'password'"),
    ("browser.py", "from recipe_engine.safety import _SECRET_RE",
     "import re as _r\n_SECRET_RE = _r.compile('zzz')", "no longer imports _SECRET_RE from safety"),
    ("desktop.py", "import time\n", "import re\nimport time\n_PW = re.compile('password')\n", "compiles its own password regex"),
    # 2
    ("desktop.py", "    safety.guard_text(text)\n", "", "no longer guards the text"),
    ("desktop.py", "        safety.guard_focused_element()", "        pass", "no longer checks the focused control"),
    ("browser.py",
     "    if _SECRET_RE.search(text) or _SECRET_RE.search(sel):\n        session.login_seen = True\n"
     "        return _err(\"Filling credentials or codes is forbidden. Reply LOGIN_REQUIRED.\")\n",
     "", "op_fill no longer refuses credential text"),
    ("browser.py", '    if (kind or "").lower() == "password":', "    if False:", "op_fill no longer refuses password fields"),
    # 3
    ("browser.py",
     "    login = await detect_login_page(page)\n    if login:\n        session.login_seen = True\n"
     "        session.login_reason = login",
     "    login = None\n    if login:\n        pass", "_after_action no longer runs the login detector"),
    ("browser.py", 'return await _after_action(session, "click", args, f"clicked {sel}", selector=sel)',
     'return _text("clicked")', "op_click no longer ends in _after_action"),
    ("browser.py",
     'return await _after_action(session, "download", args, f"downloaded to {dest}", selector=sel, value=str(dest))',
     'return _text("downloaded")', "op_download no longer ends in _after_action"),
    # 4
    ("recipes.py", '    if destructive:\n        return False, "destructive task (always re-plans and confirms)"\n',
     "", "no longer guards on destructive"),
    ("recipes.py", '    if getattr(result, "credential_flagged", False):\n        return False, "credential-blocking hook fired"\n',
     "", "no longer guards on credential_flagged"),
    ("recorder.py", "        r = recipes.capture(name, result, surface, dur, destructive=False, display=display,",
     "        r = recipes.build(name, trace, surface, dur, display=display,", "recorder.stop no longer saves through recipes.capture"),
    # 5
    ("recorder.py", "                if text and safety.is_destructive(text):", "                if False:",
     "recorder.record no longer drops destructive steps"),
    ("recorder.py", "        if safety.is_destructive(f\"{name} {description or ''}\"):", "        if False:",
     "recorder.start no longer refuses"),
    # 6
    ("recipes.py",
     'await _assert_expect_browser(page, step.get("expect") or "", int(step.get("expect_timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS))',
     "pass", "no longer asserts every step's expect"),
    ("recipes.py",
     "        except Exception as e:\n            raise ReplayFailed(i, f\"expect {step.get('expect')!r} not met: {type(e).__name__}: {str(e)[:200]}\", done) from None",
     "        except Exception as e:\n            await asyncio.sleep(1)", "uses a fixed sleep()"),
    ("recipes.py", '        cur = _wait_title(expect[6:], int(step.get("timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS), cancel)',
     "        cur = expect[6:]", "no longer asserts the window"),
    ("recipes.py", "        if now >= deadline:\n            return cur\n", "", "_wait_title is no longer a deadline-bounded poll"),
    # 7
    ("recipes.py", '    if recipe.quarantined:\n        raise ReplayFailed(0, "recipe is quarantined")\n', "",
     "no longer refuses quarantined"),
    ("engine.py", "        if r.quarantined:\n            return finish(_outcome(\"quarantined\"", "        if False:\n            return finish(_outcome(\"quarantined\"",
     "no longer maps a quarantined recipe"),
    # 8
    ("resolver.py", '    if pixel_ok and step.get("pixel") and display_matches(step["pixel"].get("display"), display_now):\n        xy = rel_to_logical(',
     '    if pixel_ok and step.get("pixel"):\n        xy = rel_to_logical(', "uses pixel coordinates without a display_matches guard"),
    ("resolver.py", "    raise UnresolvableElement(detail.strip(\"; \"))", "    return Resolved(\"pixel\", xy=(0, 0), how=\"guess\")",
     "no longer resolves by UIA / raises UnresolvableElement"),
    # 9
    ("engine.py", "desktop_lock = threading.Lock()", "desktop_lock = threading.Semaphore(4)",
     "desktop_lock (module-level threading.Lock) missing"),
    ("engine.py", "        except InputBusy as e:\n            return finish(_outcome(\"busy\", failure_reason=str(e)))",
     "        except InputBusy as e:\n            pass", "engine.replay no longer takes the input lock / answers 'busy'"),
    ("mcp_server.py", "            with engine_mod.acquire_input(what, block=False):\n                return _shot(fn(*args))",
     "            return _shot(fn(*args))", "mcp_server._act no longer takes the input lock"),
    # 10
    ("engine.py", "        if safety.is_destructive(text):\n            return finish(_outcome(\"refused\"",
     "        if False:\n            return finish(_outcome(\"refused\"", "no longer refuses destructive recipes"),
    # 11
    ("engine.py", "            steps = recipes.substitute(r, inputs)", "            steps = r.steps",
     "no longer substitutes inputs"),
    # 12
    ("service.py", "    app = auth.BearerAuthMiddleware(app, auth.current_token)\n", "",
     "no longer wraps the app in BearerAuthMiddleware"),
    ("service.py", "TransportSecuritySettings(enable_dns_rebinding_protection=True",
     "TransportSecuritySettings(enable_dns_rebinding_protection=False", "no longer enables DNS-rebinding protection"),
    ("auth.py", "hmac.compare_digest(given.encode(), expected.encode())", "(lambda a, b: True)(given, expected)",
     "no longer compares tokens with hmac.compare_digest"),
    # 13
    ("abort.py", "        release_all_keys()\n        try:\n            self.on_abort()", "        try:\n            self.on_abort()",
     "fire no longer releases keys"),
    ("abort.py", "        batch.append(_keyup(vk, KEYEVENTF_KEYUP))\n        released.append(f\"vk{vk:02X}\")\n    for vk in range(0x08, 0xFF):",
     "        pass\n    for vk in range(0x08, 0xFF):", "no longer emits KEYUP"),
    ("config.py", "ESC_POLL_MS = 50", "ESC_POLL_MS = 500", "ESC_POLL_MS is no longer <= 50"),
    # 14
    ("engine.py", "import threading\n", "import threading\nimport claude_agent_sdk  # noqa\n", "imports ['claude_agent_sdk']"),
    # 17
    ("export.py", '    for k in ("runs", "history", "last_success", "last_used", "avg_duration_s", "quarantined", "consecutive_failures",\n              "imported", "destructive", "id"):\n        d.pop(k, None)                                    # nothing about THIS machine\'s runs travels\n', "", "export_recipe no longer strips 'runs'"),
    ("export.py", '    why = _safety_problems(portable)\n    if why:\n        return {"status": "refused", "failure_reason": why}\n    key = export_key()', "    key = export_key()", "export_recipe no longer refuses secrets"),
    ("export.py", "hmac.compare_digest(given, _sign(payload))", "(lambda a, b: True)(given, _sign(payload))",
     "import_recipe no longer verifies the signature"),
    ("engine.py", "import threading\n", "import subprocess\nimport threading\nsubprocess.run(['claude', '-p', 'x'])\n",
     "launches 'claude' through subprocess"),
    # 15
    ("service.py", "    eng.invariant_problems = invariants.check()", "    eng.invariant_problems = []",
     "no longer stores invariants.check()"),
    ("mcp_server.py", "        return _REFUSED_INVARIANTS if eng.invariant_problems else None", "        return None",
     "no longer refuses on invariant problems"),
    # 16
    ("recipes.py", "        sealed.write_sealed_json(p, recipe.to_dict())", "        p.write_text(json.dumps(recipe.to_dict()))",
     "no longer writes through sealed.write_sealed_json"),
    ("sealed.py", "    if sealed():\n", "    if False:\n", "no longer uses DPAPI when sealing is on"),
]


def _selftest() -> int:
    import shutil
    import tempfile

    base = check()
    if base:
        print("FAIL: the real tree has violations:", base)
        return 1
    print(f"real tree: ok ({len(PROTECTED_INVARIANTS)} invariants)")
    tmp = Path(tempfile.mkdtemp(prefix="invariants-"))
    files = list(PROTECTED_FILES) + [f for f in OPTIONAL_FILES if (PACKAGE_DIR / f).exists()] + ["config.py"]
    bad = 0
    for i, (fname, old, new, expect) in enumerate(MUTATIONS, 1):
        for f in files:
            shutil.copy(PACKAGE_DIR / f, tmp / f)
        src = (tmp / fname).read_text(encoding="utf-8")
        if old not in src:
            print(f"{i:2d}. SKIP (mutation text not found in {fname}): {old[:60]!r}")
            bad += 1
            continue
        (tmp / fname).write_text(src.replace(old, new, 1), encoding="utf-8")
        problems = check(root=tmp)
        hit = any(expect in p for p in problems)
        print(f"{i:2d}. {'ok  ' if hit else 'MISS'} {fname}: {expect}" + ("" if hit else f"   got {problems}"))
        bad += not hit
    shutil.rmtree(tmp, ignore_errors=True)
    print("self-test", "FAILED" if bad else f"ok: all {len(MUTATIONS)} mutations detected")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_selftest())
    problems = check()
    if problems:
        print("INVARIANT VIOLATIONS:")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print("all protected invariants intact")
