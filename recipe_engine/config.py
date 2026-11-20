"""
All tunable constants for Recipe Engine live here. Nothing in the other modules
should hard-code a threshold, path, or pattern.

Data lives under %LOCALAPPDATA%\\RecipeEngine (RECIPE_ENGINE_DATA_DIR overrides it,
read ONCE at import so tests and the reliability harness can isolate a run). A few
values are runtime-adjustable and persist in settings.json inside the data dir (see
load_settings / save_setting at the bottom) so the defaults below stay untouched.
"""
from __future__ import annotations

import collections
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


# ---------------------------------------------------------------------------
# Where everything the engine owns lives (per Windows user, DPAPI-sealed where secret)
# ---------------------------------------------------------------------------
def _default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "RecipeEngine" if base else Path.home() / ".recipe-engine"


DATA_DIR = Path(os.environ["RECIPE_ENGINE_DATA_DIR"]) if os.environ.get("RECIPE_ENGINE_DATA_DIR") else _default_data_dir()
TOKEN_FILE = DATA_DIR / "token"
EXPORT_KEY_FILE = DATA_DIR / "export.key"
RECIPES_DIR = DATA_DIR / "recipes"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
JOBS_FILE = DATA_DIR / "jobs.json"
REPORTS_DIR = DATA_DIR / "reports"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "engine.log"

# ---------------------------------------------------------------------------
# Host service
# ---------------------------------------------------------------------------
BIND_HOST = "127.0.0.1"              # loopback ONLY: anything that can reach this port can drive the desktop
PORT = 47391
MCP_PATH = "/mcp"
BACKGROUND_REPLAY = True             # pattern-driven replay in place (no SendInput, focus restored) when every step has a UIA pattern
ESC_POLL_MS = 50                     # abort watchdog poll; the spec's abort budget is 200 ms
ABORT_DEADLINE_MS = 200
UIA_RESOLVE_TIMEOUT_MS = 5_000       # bounded poll for a recorded UI element to appear on replay
RECIPE_MAX_REPLAY_S = 300            # hard cap on one replay_recipe call


# ---------------------------------------------------------------------------
# Screen / desktop control
# ---------------------------------------------------------------------------
SCREENSHOT_MAX_WIDTH = 1280          # screenshots are downscaled to this width; clicks are rescaled back
SCREENSHOT_JPEG_QUALITY = 70         # JPEG keeps each screenshot ~100-200 KB
SDK_MAX_BUFFER_BYTES = 32 * 1024 * 1024  # Agent SDK JSON message buffer; screenshots ride inside it
ACTION_SETTLE_MS = 600               # wait after each action before the follow-up screenshot
TYPE_INTERVAL_S = 0.02               # per-keystroke delay for pyautogui.write
PYAUTOGUI_PAUSE_S = 0.1              # global pause between pyautogui calls
PYAUTOGUI_FAILSAFE = False           # Esc is the abort (abort.py). PyAutoGUI's corner fail-safe would fail every
                                     # click while the user's mouse merely rests in a screen corner.

# ---------------------------------------------------------------------------
# Browser (persistent profile, logged in manually ONCE)
# ---------------------------------------------------------------------------
CHROME_PATH = next(
    (p for p in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
    ) if os.path.exists(p)),
    "chrome",
)
BROWSER_PROFILE_DIR = DATA_DIR / "browser-profile"

# ---------------------------------------------------------------------------
# V3: browser surface (Playwright on the SAME persistent Chrome profile)
# ---------------------------------------------------------------------------
BROWSER_CHANNEL = "chrome"           # real Chrome, so the manually-logged-in profile's cookies decrypt
BROWSER_HEADLESS = False             # headed: the user must be able to sign in manually on a login page
BROWSER_TAKE_OVER_PROFILE = True     # a stray chrome.exe holding the profile is closed (only that one) before launch
BROWSER_GRACEFUL_CLOSE_S = 5.0       # V4.2: window to close cleanly (cookie flush) before a stray Chrome is force-killed
BROWSER_LAUNCH_TIMEOUT_S = 60
BROWSER_ACTION_TIMEOUT_MS = 10_000   # click/fill/press auto-wait cap
BROWSER_NAV_TIMEOUT_MS = 20_000      # goto cap
BROWSER_SNAPSHOT_MAX_CHARS = 12_000  # aria snapshot returned after every action (truncated beyond this)
BROWSER_READ_MAX_CHARS = 8_000       # page text returned by `read`
BROWSER_KEEP_PAGES = 3               # finished task tabs kept open for the user; older ones are closed
BROWSER_SCREENSHOT_MAX_WIDTH = 1280
# V4.3: where a [browser] `download` step saves files, so a following [desktop] step can
# attach them by path. A fixed, known directory is the point - Playwright otherwise parks
# a download in a temp path that dies with the context, and the desktop phase would have
# nothing to attach. NOT the user's own Downloads folder: the agent's files stay separable.
BROWSER_DOWNLOAD_DIR = DATA_DIR / "downloads"
BROWSER_DOWNLOAD_TIMEOUT_MS = 15_000   # fail fast: a download that has not started in 15 s is not coming
# V4.3: what one surface phase hands to the next. A mixed task ("read an invoice off the portal,
# then type it into the Claude app") is split into a browser phase and a desktop phase that are
# separate executor calls; without this the second phase never learns what the first one found.
# ExecResult.summary is one line by design, so the phases carry ExecResult.detail (the executor's
# full final message) instead, truncated to this cap.
PHASE_CARRY_MAX_CHARS = 8_000
# Login / 2FA page heuristics (the model is ALSO told to stop; this is the code-level belt and braces).
LOGIN_URL_RE = (r"accounts\.google\.com|login\.microsoftonline\.com|login\.live\.com|appleid\.apple\.com|"
                r"auth0\.com|okta\.com|/login\b|/signin\b|/sign-in\b|/sign_in\b|/sso\b|/auth\b|/oauth|/2fa\b|/mfa\b")
LOGIN_TEXT_RE = (r"verify it'?s you|2-step verification|two-factor|enter the (verification )?code|"
                 r"sign in to continue|enter your password|one-time (pass)?code|authenticator app")
# V4.1: a login-looking URL is NOT a verdict on its own. SSO (portal -> Microsoft ->
# portal) bounces through login.microsoftonline.com and /login/... and, with a
# "keep me signed in" cookie, lands on the dashboard by itself in a second or two.
# The detector waits up to LOGIN_SETTLE_MS for the URL to stop changing before it
# decides, and a URL match then only counts together with a visible password /
# code field or login text. A visible password field is always a login page.
LOGIN_SETTLE_MS = 10_000
LOGIN_SETTLE_POLL_MS = 400
LOGIN_QUIET_MS = 3_500               # a login URL that has not changed for this long, with no form, is a real stop

# V4.1: saved login. Session cookies (SSO portals) live in memory and Chrome drops
# them on the next launch, so the live cookie jar is snapshotted every
# SESSION_SNAPSHOT_S while the browser is up (sealed with Windows DPAPI) and put
# back right after the next launch. The profile is also set to "continue where you
# left off" so Chrome itself keeps session cookies when it exits cleanly.
SESSION_STATE_FILE = DATA_DIR / "browser-session.bin"
SESSION_SNAPSHOT_S = 5
SESSION_RESTORE = True

# --- URL normalisation -----------------------------------------------------
# ONE source of truth. browser.open_url, the browser `goto` tool and the desktop
# `open_browser` tool all funnel through normalize_url, so a bare hostname, a
# typed "localhost:8765" and "about:blank" behave the same on every path in.
BLANK_URL = "about:blank"
# Schemes a browser can be handed as-is. Anything else that looks like a scheme
# is only believed when what follows the colon is not a port ("localhost:8765").
URL_SCHEMES = ("http", "https", "file", "about", "data", "blob", "mailto", "ftp", "ftps",
               "chrome", "chrome-extension", "edge", "view-source", "devtools")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PORT_RE = re.compile(r":\d+$")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def normalize_url(url: str | None) -> str:
    """Turn whatever was typed, spoken or replayed from a recipe into something
    a browser can actually open.

    Empty -> about:blank, NEVER "https://about:blank" -- that is a navigation
    error, and it is what --login-browser used to hand Playwright. A URL that
    already names a scheme (http, https, file, about, chrome, data, mailto ...)
    keeps it, lowercased. "//host", a bare host and "host:port" get https,
    except loopback which gets http: nothing serves TLS on localhost. A Windows
    or UNC path becomes a file:// URL. Idempotent, so a caller that normalises
    and then hands the result to another one that does too is fine.

    The ambiguous case is "localhost:8000", which parses as the scheme
    "localhost". A colon followed only by digits is read as a PORT, not a
    scheme, so it is treated as a bare host -> "http://localhost:8000".
    """
    u = (url or "").strip().strip("<>\"'")
    if not u:
        return BLANK_URL
    if u.startswith("\\\\"):                                  # UNC: \\server\share
        return "file://" + quote(u[2:].replace("\\", "/"), safe="/:")
    if _WIN_PATH_RE.match(u):                                 # C:\dir\page.html or C:/dir
        return "file:///" + quote(u.replace("\\", "/"), safe="/:")
    m = _SCHEME_RE.match(u)
    if m and (m.group(1).lower() in URL_SCHEMES
              or not _PORT_RE.match(":" + u[m.end():].split("/")[0])):
        return m.group(1).lower() + ":" + u[m.end():]
    if u.startswith("//"):                                    # protocol-relative
        u = u[2:]
    hostport = u.split("/")[0].split("@")[-1].lower()
    host = hostport.rsplit(":", 1)[0] if _PORT_RE.search(hostport) else hostport
    scheme = "http" if host.strip("[]") in LOCAL_HOSTS else "https"
    return f"{scheme}://{u}"

# ---------------------------------------------------------------------------
# V3: recipe cache (recipes/<id>.json, one per task)
# ---------------------------------------------------------------------------
RECIPE_STEP_TIMEOUT_MS = 5_000       # minimum per-step `expect` window in a replay
RECIPE_EXPECT_TIMEOUT_MAX_MS = 30_000  # ... sized up from how long the step took on the recorded run, capped here
RECIPE_MIN_SUCCESS_RATE = 0.7        # over the last RECIPE_WINDOW_RUNS runs, else quarantine
RECIPE_WINDOW_RUNS = 5
RECIPE_MAX_CONSECUTIVE_FAILURES = 2  # -> quarantine
RECIPE_STALE_DAYS = 60               # no success this long -> reported stale (client confirms before replaying)
RECIPE_HISTORY_KEEP = 10
RECIPE_SUMMARY_MAX_RAW_CHARS = BROWSER_READ_MAX_CHARS   # a replay returns what it read, trimmed here; the
                                                        # engine never summarises (that is the client model's job)
RECIPE_DESKTOP_STABLE_MS = 500       # a desktop step's window-title assertion must hold this long (a window that
                                     # flashes its final title while still opening does not count as ready)
# Words dropped when normalising a spoken command before matching.
RECIPE_FILLER_WORDS = ("please", "can", "could", "would", "you", "hey", "um", "uh", "just", "for", "me", "my",
                       "the", "a", "an", "now", "go", "ahead", "and", "to", "i", "want", "need", "like", "kindly")
# The send-whitelist. A recipe whose name/description/steps match any of these is irreversible: it is never
# recorded and replay_recipe refuses it ("refused"). Sending is Claude's job, in the client, with the user.
DESTRUCTIVE_WORDS = ("send", "sent", "delete", "remove", "erase", "purchase", "buy", "order", "pay", "payment",
                     "checkout", "check out", "submit", "post", "publish", "transfer", "wire", "unsubscribe",
                     "reply", "respond", "book", "reserve", "cancel my", "cancel the", "confirm the order")

# ---------------------------------------------------------------------------
# V3: parallel tasks
# ---------------------------------------------------------------------------
# Desktop tasks are ALWAYS serial (one OS cursor). Browser tasks may run in parallel,
# each in its own tab of the one persistent context. More than 4 is slower, not faster.
MAX_PARALLEL_BROWSER_TASKS = 2
MAX_PARALLEL_BROWSER_TASKS_HARD_CAP = 4
TASK_JOIN_TIMEOUT_S = 90             # how long a cancel waits for a task thread's current step
STATUS_PHRASES = ("status", "what's running", "whats running", "what are you doing", "what is running")

# ---------------------------------------------------------------------------
# V4: input modes. Both are always available; this is only what is ARMED.
#   voice - wake word + mic, AND typed lines / the web panel still work
#   text  - mic parked (no wake word, no false triggers); typed lines / web only
# Switch at runtime: type /voice or /text, say "<wake> text mode", or click the
# toggle on the web panel. --text starts in text mode with the voice models
# unloaded; the first switch to voice loads them (a few seconds).
# ---------------------------------------------------------------------------
INPUT_MODES = ("voice", "text")
VOICE_MODE_PHRASES = ("voice mode", "switch to voice", "use voice", "use the microphone", "listen to me")
TEXT_MODE_PHRASES = ("text mode", "typing mode", "switch to text", "use text", "stop listening")

# ---------------------------------------------------------------------------
# Local control panel (served by the same loopback process, behind the same token)
# ---------------------------------------------------------------------------
WEB_DIR = PACKAGE_DIR / "static"
EVENT_RING_SIZE = 600                # events replayed to a panel that connects late


def browser_slots() -> int:
    """MAX_PARALLEL_BROWSER_TASKS clamped to [1, hard cap]."""
    return max(1, min(int(MAX_PARALLEL_BROWSER_TASKS), MAX_PARALLEL_BROWSER_TASKS_HARD_CAP))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_TIMESTAMPS = True
# How much of a spoken line is logged. The log is also the study transcript and the
# panel's feed, so this is the only durable record of what the agent actually ANSWERED;
# it was 120, which truncated every real answer mid-sentence.
LOG_SAY_MAX_CHARS = 2_000
LOG_RING_SIZE = 1000                 # in-memory tail kept for crash reports (session.log = last 200)
LOG_RING: "collections.deque[str]" = collections.deque(maxlen=LOG_RING_SIZE)
_log_lock = threading.Lock()
# V4: callables(tag, msg, line) told about every log line (events.py registers the
# web panel's bus). A subscriber must never raise and must never call log().
LOG_SUBSCRIBERS: list = []

# Process exit codes
EXIT_OK = 0
EXIT_CHECK_FAILED = 1                # invariants or desktop-control check failed
EXIT_ALREADY_RUNNING = 3             # another engine owns the port / mutex


def log(tag: str, msg: str) -> None:
    """Tiny stdout logger shared by every module (keeps the file list flat).
    Every line is also kept in LOG_RING so crash reports can include the tail."""
    import time
    stamp = time.strftime("[%H:%M:%S] ") if LOG_TIMESTAMPS else ""
    line = f"{stamp}{tag:<10} {msg}"
    with _log_lock:
        LOG_RING.append(line)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass                                 # a full disk must never stop the engine
    for fn in list(LOG_SUBSCRIBERS):
        try:
            fn(tag, msg, line)
        except Exception:
            pass
    import sys
    for attempt in (line, line.encode("ascii", "replace").decode("ascii")):
        try:
            sys.stdout.write(attempt + "\n")     # ONE write: multi-line messages never interleave with other threads
            sys.stdout.flush()
            break
        except UnicodeEncodeError:              # cp1252 console: never let a log line take the agent down
            continue


# ---------------------------------------------------------------------------
# Runtime-adjustable settings (persisted in settings.json)
# ---------------------------------------------------------------------------
_PERSISTED_KEYS = ("BIND_HOST", "PORT", "BACKGROUND_REPLAY")


def _validate(name: str, value):
    if name == "BIND_HOST":
        v = str(value).strip()
        if v not in ("127.0.0.1", "localhost", "::1") and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", v):
            raise ValueError("BIND_HOST must be loopback or a dotted IPv4 address of this machine")
        return v
    if name == "PORT":
        pv = int(value)
        if not 1024 <= pv <= 65535:
            raise ValueError("PORT must be 1024-65535")
        return pv
    if name == "BACKGROUND_REPLAY":
        return bool(value)
    raise KeyError(name)


def load_settings() -> dict:
    """Apply settings.json on top of the defaults above. Unknown keys ignored."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # a corrupt file must never take the engine down
        log("CONFIG", f"settings.json unreadable ({e!r}); using defaults")
        return {}
    applied = {}
    for k in _PERSISTED_KEYS:
        if k in data:
            globals()[k] = _validate(k, data[k])
            applied[k] = globals()[k]
    if applied:
        log("CONFIG", f"settings.json overrides: {applied}")
    return applied


def save_setting(name: str, value) -> object:
    """Set a runtime-adjustable value, persist it, return the validated value."""
    value = _validate(name, value)
    globals()[name] = value
    data = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[name] = value
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log("CONFIG", f"{name} = {value} (saved to {SETTINGS_FILE.name})")
    return value


load_settings()
