"""
Failure triage: classify an exception, describe it in plain words, trim it for a
one-line report. Pure functions; the caller decides what to do.

| Class       | Examples                                              |
|-------------|-------------------------------------------------------|
| ENVIRONMENT | ModuleNotFoundError, Playwright/Chrome missing        |
| PERMISSION  | pyautogui blocked, elevation mismatch                 |
| AUTH        | a site's login page during a replay                   |
| TRANSIENT   | network timeout, 429/503                              |
| TASK        | UI element not found, page changed, fail-safe hit     |
| CODE        | traceback originating in our own .py files            |

No repair loop, no model: a CODE failure is reported, never "fixed" by the engine.
"""
from __future__ import annotations

import enum
import re
import sys
import traceback as tb_mod
from dataclasses import dataclass, field
from typing import Any, Optional

from recipe_engine import config
from recipe_engine.config import log


class FailureClass(enum.Enum):
    ENVIRONMENT = "ENVIRONMENT"
    PERMISSION = "PERMISSION"
    AUTH = "AUTH"
    TRANSIENT = "TRANSIENT"
    TASK = "TASK"
    CODE = "CODE"
    UNKNOWN = "UNKNOWN"


class StageError(Exception):
    """A stage reported an error through its result object instead of raising
    (planner/executor `res.error`). `hint` nudges classification."""

    def __init__(self, message: str, hint: Optional[FailureClass] = None) -> None:
        super().__init__(message)
        self.hint = hint


class ControlBlocked(Exception):
    """Desktop control / microphone permission failure detected by a check."""


@dataclass
class Failure:
    cls: FailureClass
    exc: BaseException
    traceback: str
    state: str
    command: str = ""
    plan: str = ""
    origin_file: Optional[str] = None
    origin_line: Optional[int] = None
    verifier: Optional[str] = None
    verifier_args: dict[str, Any] = field(default_factory=dict)

    @property
    def exc_type(self) -> str:
        return type(self.exc).__name__


# ---------------------------------------------------------------- patterns
_PERM_RE = re.compile(r"access (is )?denied|permission denied|blocked|elevat|not permitted|winerror 5\b|uipi", re.I)
_AUTH_RE = re.compile(r"not logged in|log ?in required|unauthori[sz]ed|authentication|invalid api key|\b401\b|"
                      r"usage limit|out of credit|credit(s)? exhausted|billing|subscription|quota", re.I)
_TRANSIENT_RE = re.compile(r"\b(429|502|503|504)\b|overloaded|rate.?limit|timed? ?out|timeout|econnreset|econnrefused|"
                           r"connection (reset|refused|aborted|error)|temporarily unavailable|hf_hub|huggingface|download", re.I)
_CODE_TYPES = (TypeError, AttributeError, NameError, KeyError, IndexError, ValueError, AssertionError,
               UnboundLocalError, ZeroDivisionError, RuntimeError, RecursionError, NotImplementedError)


def _our_frames(exc: BaseException) -> list[tuple[str, int, str]]:
    """(file, line, func) for every traceback frame inside the project (not .venv)."""
    out = []
    proj = str(config.PROJECT_DIR).lower()
    for fr in tb_mod.extract_tb(exc.__traceback__):
        f = fr.filename.lower()
        if f.startswith(proj) and ".venv" not in f:
            out.append((fr.filename, fr.lineno or 0, fr.name))
    return out


def _messages(exc: BaseException) -> str:
    parts = []
    e: Optional[BaseException] = exc
    while e is not None and len(parts) < 5:
        parts.append(f"{type(e).__name__}: {e}")
        e = e.__cause__ or e.__context__
    return " | ".join(parts)


_TASK_NAMES = ("ReplayFailed", "ReplayLoginBlocked", "ReplayCancelled", "TaskCancelled", "BrowserProfileLocked")


def classify(exc: BaseException) -> FailureClass:
    msg = _messages(exc)
    names = {type(e).__name__ for e in _chain(exc)}

    if isinstance(exc, StageError) and exc.hint is not None:
        return exc.hint
    # V3 TASK: a recipe/browser step did not match the page, the profile is held, the user cancelled.
    if names & set(_TASK_NAMES):
        return FailureClass.TASK
    if any(type(e).__module__.startswith("playwright") for e in _chain(exc)):
        return FailureClass.TASK           # Playwright TimeoutError / strict-mode etc: the page, not our code
    if "BrowserUnavailable" in names:
        return FailureClass.ENVIRONMENT
    # PERMISSION --------------------------------------------------------------
    if isinstance(exc, ControlBlocked) or "PortAudioError" in names and re.search(r"input|device|access", msg, re.I):
        return FailureClass.PERMISSION
    if isinstance(exc, PermissionError) or (isinstance(exc, OSError) and getattr(exc, "winerror", None) == 5):
        return FailureClass.PERMISSION
    if _PERM_RE.search(msg) and not _our_code_bug(exc):
        return FailureClass.PERMISSION
    # AUTH --------------------------------------------------------------------
    if _AUTH_RE.search(msg):
        return FailureClass.AUTH
    # ENVIRONMENT -------------------------------------------------------------
    if isinstance(exc, (ModuleNotFoundError, ImportError)) or "CLINotFoundError" in names:
        return FailureClass.ENVIRONMENT
    if isinstance(exc, FileNotFoundError) and ("chrome" in msg.lower() or "playwright" in msg.lower()):
        return FailureClass.ENVIRONMENT
    if str(config.PROJECT_DIR).lower() not in sys.prefix.lower() and isinstance(exc, (ModuleNotFoundError,)):
        return FailureClass.ENVIRONMENT
    # TRANSIENT ---------------------------------------------------------------
    if isinstance(exc, (TimeoutError, ConnectionError)) or any(n in names for n in (
            "ReadTimeout", "ConnectTimeout", "ConnectError", "RemoteProtocolError", "HTTPStatusError",
            "ClientConnectorError", "ServerDisconnectedError", "EndOfStream", "BrokenResourceError")):
        return FailureClass.TRANSIENT
    if _TRANSIENT_RE.search(msg) and not _our_code_bug(exc):
        return FailureClass.TRANSIENT
    # TASK --------------------------------------------------------------------
    if "FailSafeException" in names:
        return FailureClass.TASK
    # CODE --------------------------------------------------------------------
    if _our_code_bug(exc):
        return FailureClass.CODE
    return FailureClass.UNKNOWN


def _chain(exc: BaseException):
    e: Optional[BaseException] = exc
    seen = 0
    while e is not None and seen < 6:
        yield e
        e = e.__cause__ or e.__context__
        seen += 1


def _our_code_bug(exc: BaseException) -> bool:
    if isinstance(exc, SyntaxError) and exc.filename and str(config.PROJECT_DIR).lower() in exc.filename.lower():
        return True
    return isinstance(exc, _CODE_TYPES) and bool(_our_frames(exc))


def build(exc: BaseException, *, state: str, command: str = "", plan: str = "",
          verifier: Optional[str] = None, verifier_args: Optional[dict] = None) -> Failure:
    frames = _our_frames(exc)
    origin = frames[-1] if frames else None
    if isinstance(exc, SyntaxError) and exc.filename:
        origin = (exc.filename, exc.lineno or 0, "<module>")
    return Failure(
        cls=classify(exc), exc=exc,
        traceback="".join(tb_mod.format_exception(type(exc), exc, exc.__traceback__)),
        state=state, command=command, plan=plan,
        origin_file=origin[0] if origin else None, origin_line=origin[1] if origin else None,
        verifier=verifier, verifier_args=dict(verifier_args or {}),
    )


# ---------------------------------------------------------------- plain words
# V4: what the agent SAYS when a task fails. The technical reason stays in the
# log and the report; the spoken line says what happened and what the user can
# do about it, in words that do not assume they read the terminal.
_ADVICE: tuple[tuple[str, str, str], ...] = (
    # Order matters: the first pattern that matches wins. Sign-in must be an EXPLICIT marker
    # (a reason that merely mentions "login.html" in a list of links is not a sign-in page).
    (r"LOGIN_REQUIRED|LOGIN_PAGE_DETECTED|\b(login|log-in|sign.?in) (page|form|screen|prompt|required)|\b2fa\b|"
     r"verification code|enter your password|asked for a password",
     "It ran into a sign-in page.",
     "Run the login browser, sign in there once, then ask me again."),
    (r"not found|no such|could ?n.t find|couldn't find|found no|there is no|there was no|no (link|button|element|"
     r"field|page|item|option|menu)\b|not visible|no element|does ?n.t exist|doesn't exist|\b404\b|strict mode|"
     r"not attached|detached|not on (this|the) page|only (contains|offers|has)",
     "It couldn't find what it was looking for on the screen.",
     "The page or app may look different from what I expected. Try describing the button or link by its exact name."),
    (r"timed? ?out|timeout",
     "Something took too long to load.",
     "Check the connection or the page, then try again."),
    (r"max_turns|ran out of steps|turn limit",
     "It needed more steps than I'm allowed to take.",
     "Try splitting the task into two smaller requests."),
    (r"profile.?lock|held by|another chrome|take over",
     "Another Chrome window is holding my browser profile.",
     "Close that Chrome window and try again."),
    (r"denied|blocked|permission|elevat",
     "I wasn't allowed to control that window.",
     "It may be running as administrator; close it or run me from an elevated terminal."),
    (r"cancel",
     "It was cancelled.",
     "Say it again whenever you're ready."),
)


def explain(reason: str) -> tuple[str, str]:
    """(what happened, what you can do) for a failure reason, in plain words."""
    r = reason or ""
    for pattern, what, advice in _ADVICE:
        if re.search(pattern, r, re.I):
            return what, advice
    return "It didn't finish.", "The full error is in the terminal and on the web panel."


def short_reason(reason: str, limit: int = 140) -> str:
    """First sentence of a technical error, trimmed for speech."""
    r = re.sub(r"\s+", " ", (reason or "")).strip()
    r = re.sub(r"^(execution error:|error:|failed:)\s*", "", r, flags=re.I)
    r = r.split(". ")[0]
    return (r[:limit] + "...") if len(r) > limit else r
