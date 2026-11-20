"""
Recipes: capture / replay / stats for workflows Claude performed through the engine.

* Capture: a successful run's trace (one dict per real action, from desktop._rec and
  browser.BrowserSession.record) becomes an ordered list of steps, each with the
  assertion (`expect`) that must hold before the next step runs, sized from how long
  the recorded run took. Guarded runs (login page seen, credential refused, destructive,
  cancelled, errored, polling) are never written.
* Replay NEVER blind: preconditions first, then after EVERY step `expect` is asserted
  within its window - Playwright auto-wait / a bounded poll of the foreground window,
  never a fixed sleep. The first failed assertion halts the replay (ReplayFailed) so a
  wrong click never cascades.
* Stats: record_outcome() is the one writer. Two consecutive failures or a success rate
  below RECIPE_MIN_SUCCESS_RATE over the last RECIPE_WINDOW_RUNS quarantines a recipe;
  RECIPE_STALE_DAYS without a success marks it stale.

Zero model calls. The old fuzzy matcher and read-text summariser were the only two
LLM call sites in the engine and are gone: replay returns what it read and the CLIENT
model decides what it means.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from recipe_engine import config, sealed
from recipe_engine.config import log

SCHEMA_VERSION = 2
_PARAM_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_ID_RE = re.compile(r"\b[0-9a-f]{12}\b")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ReplayFailed(Exception):
    """A step's action or `expect` assertion failed. Replay halted at `step`."""

    def __init__(self, step: int, reason: str, steps_done: int = 0) -> None:
        super().__init__(f"step {step}: {reason}")
        self.step = step
        self.reason = reason
        self.steps_done = steps_done


class ReplayLoginBlocked(Exception):
    """A login / 2FA page appeared during replay (not the recipe's fault)."""


class ReplayCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Normalisation / ids
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    words = _NON_WORD.sub(" ", (text or "").lower().replace("'", "")).split()
    kept = [w for w in words if w not in config.RECIPE_FILLER_WORDS]
    return " ".join(kept or words)


def recipe_id(normalized: str) -> str:
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_ts(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The recipe record
# ---------------------------------------------------------------------------
@dataclass
class Recipe:
    id: str
    command_normalized: str
    command_variants: list[str] = field(default_factory=list)
    surface: str = "browser"
    created: str = field(default_factory=_now)
    last_success: str | None = None
    last_used: str | None = None
    runs: dict[str, int] = field(default_factory=lambda: {"success": 0, "fail": 0})
    avg_duration_s: float = 0.0
    preconditions: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    quarantined: bool = False
    consecutive_failures: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)   # last N {ok, t, s}
    display: dict[str, Any] | None = None                           # desktop only: {w, h, scale}
    destructive: bool = False                                       # always False: destructive runs are never saved
    description: str = ""
    name: str = ""                     # kebab-case handle Claude uses (record_recipe/replay_recipe)
    app_hints: list[str] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)   # [{name, example, used_in:[step idx]}]
    background_capable: bool = False                             # every desktop step is pattern-driven
    imported: bool = False
    schema: int = SCHEMA_VERSION

    # ------------------------------------------------------------ derived
    @property
    def total_runs(self) -> int:
        return self.runs.get("success", 0) + self.runs.get("fail", 0)

    @property
    def success_rate(self) -> float:
        return self.runs.get("success", 0) / self.total_runs if self.total_runs else 0.0

    def window_rate(self) -> Optional[float]:
        """Success rate over the last RECIPE_WINDOW_RUNS runs (None if fewer)."""
        h = self.history[-config.RECIPE_WINDOW_RUNS:]
        if len(h) < config.RECIPE_WINDOW_RUNS:
            return None
        return sum(1 for x in h if x.get("ok")) / len(h)

    @property
    def stale(self) -> bool:
        ref = _parse_ts(self.last_success) or _parse_ts(self.created)
        if ref is None:
            return True
        return (datetime.now(timezone.utc) - ref).days >= config.RECIPE_STALE_DAYS

    @property
    def label(self) -> str:
        return self.description or self.command_normalized

    @property
    def short_label(self) -> str:
        """<= 8 words, no trailing period: what is spoken."""
        words = self.label.rstrip(". ").split()
        return " ".join(words[:8]) + ("..." if len(words) > 8 else "")

    def matches_exact(self, normalized: str) -> bool:
        return normalized == self.command_normalized or normalized in self.command_variants

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Recipe":
        d = migrate_v1(d)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
_io_lock = threading.Lock()


def _dir() -> Path:
    d = Path(config.RECIPES_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_for(rid: str) -> Path:
    return _dir() / f"{rid}.json"


def save(recipe: Recipe) -> Path:
    """Recipes describe what the user does on their screen: sealed at rest (DPAPI,
    this user only). Plaintext only under RECIPE_ENGINE_UNSEALED (the test-suite)."""
    p = path_for(recipe.id)
    with _io_lock:
        sealed.write_sealed_json(p, recipe.to_dict())
    return p


def load(rid: str) -> Optional[Recipe]:
    p = path_for(rid)
    if not p.exists():
        return None
    try:
        return Recipe.from_dict(sealed.read_sealed_json(p))
    except Exception as e:
        log("RECIPE", f"unreadable recipe {p.name}: {e!r}")
        return None


def load_by_name(name: str) -> Optional[Recipe]:
    """Find a recipe by its kebab-case name, by id, or by normalised command text."""
    key = (name or "").strip().lower()
    if not key:
        return None
    rows = load_all()
    for r in rows:
        if r.name and r.name.lower() == key:
            return r
    for r in rows:
        if r.id == key:
            return r
    norm = normalize(key)
    for r in rows:
        if r.matches_exact(norm):
            return r
    return None


def load_all() -> list[Recipe]:
    out = []
    for p in sorted(_dir().glob("*.json")):
        r = load(p.stem)
        if r is not None:
            out.append(r)
    return out


def forget(rid: str) -> bool:
    p = path_for(rid)
    if p.exists():
        p.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
@dataclass
class Match:
    kind: str                       # exact | stale | quarantined | none
    recipe: Optional[Recipe] = None
    normalized: str = ""

    @property
    def replayable(self) -> bool:
        return self.recipe is not None and self.kind in ("exact", "stale")

    @property
    def needs_confirmation(self) -> bool:
        return self.kind == "stale"


def lookup(command: str) -> Match:
    """Exact (cheap, offline) match on the normalised command or a learned variant.
    Quarantined recipes are never returned as replayable. There is no fuzzy path: a
    near-miss is the client model's call to make, not this engine's."""
    norm = normalize(command)
    for r in load_all():
        if r.matches_exact(norm):
            if r.quarantined:
                log("RECIPE", f"exact hit {r.id} is quarantined")
                return Match("quarantined", r, norm)
            kind = "stale" if r.stale else "exact"
            log("RECIPE", f"{kind} hit {r.id} ({r.label!r}) for {norm!r}")
            return Match(kind, r, norm)
    return Match("none", normalized=norm)


def add_variant(recipe: Recipe, normalized: str) -> None:
    if normalized and not recipe.matches_exact(normalized):
        recipe.command_variants.append(normalized)
        save(recipe)
        log("RECIPE", f"{recipe.id}: added variant {normalized!r}")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
_SKIP_ACTIONS = {"screenshot", "snapshot", "move", "wait"}


def should_capture(result: Any, destructive: bool) -> tuple[bool, str]:
    """`result` is an executor.ExecResult (or anything with the same attributes)."""
    if destructive:
        return False, "destructive task (always re-plans and confirms)"
    if getattr(result, "login_blocked", False) or getattr(result, "login_seen", False):
        return False, "login page seen during the run"
    if getattr(result, "credential_flagged", False):
        return False, "credential-blocking hook fired"
    if getattr(result, "cancelled", False):
        return False, "run was cancelled"
    if getattr(result, "error", None):
        return False, f"run errored: {getattr(result, 'error')}"
    if getattr(result, "hit_turn_limit", False):
        return False, "run hit the turn limit"
    trace = getattr(result, "trace", None) or []
    if not [s for s in trace if s.get("action") not in _SKIP_ACTIONS]:
        return False, "no recordable actions"
    polled = polling_pattern(trace)
    if polled:
        return False, f"the run polled the page ({polled}); a replay would read too early - the model should wait_for()"
    return True, "ok"


_OBSERVE = {"snapshot", "screenshot", "wait", "read"}


def polling_pattern(trace: list[dict]) -> str:
    """'' if the trace has no polling; else a description. Polling = two or more
    consecutive observation-only calls (snapshot/screenshot/wait, or read of the
    SAME selector). One snapshot to look at a page is normal; repeating it means
    the run waited for something a replay cannot know about."""
    prev: Optional[dict] = None
    for s in trace:
        a = s.get("action")
        if prev is not None and a in _OBSERVE and prev.get("action") in _OBSERVE:
            if a == "read" or prev.get("action") == "read":
                if a == prev.get("action") == "read" and s.get("selector") == prev.get("selector"):
                    return f"read {s.get('selector') or 'page'} twice in a row"
                if a != "read" and prev.get("action") != "read":
                    return f"{prev.get('action')} then {a}"
            else:
                return f"{prev.get('action')} then {a}"
        prev = s
    return ""


def expect_window_ms(step: dict, nxt: Optional[dict]) -> int:
    """How long the replay may wait for this step's `expect`: 1.5x the time the
    recorded run took to get from this action to the next one (a slow page
    earns a longer window), floored at RECIPE_STEP_TIMEOUT_MS and capped."""
    lo, hi = config.RECIPE_STEP_TIMEOUT_MS, config.RECIPE_EXPECT_TIMEOUT_MAX_MS
    try:
        if nxt is not None and step.get("t") is not None and nxt.get("t") is not None:
            return int(max(lo, min(hi, (float(nxt["t"]) - float(step["t"])) * 1500)))
    except (TypeError, ValueError):
        pass
    return lo


def _url_prefix(url: str) -> str:
    m = re.match(r"^([a-z]+://[^/]+)", url or "")
    return m.group(1) if m else ""


def migrate_v1(d: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2 in memory. A v1 desktop step had only `rel`; it becomes a pixel-only step
    locked to the recipe's display stamp. That is the honest outcome: re-record to get a
    UIA target and portability."""
    if int(d.get("schema") or 1) >= 2:
        return d
    d = dict(d)
    display = d.get("display")
    steps = []
    for st in d.get("steps") or []:
        st = dict(st)
        st.setdefault("params", _params_in(st.get("value")))
        if d.get("surface") == "desktop":
            st.setdefault("input_kind", "input")
            st.setdefault("uia", None)
            rel = st.pop("rel", None)
            st.setdefault("pixel", {"rel": list(rel), "display": display} if rel else None)
        st.setdefault("expect_timeout_ms", st.get("timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS)
        steps.append(st)
    d["steps"] = steps
    d.setdefault("params", _collect_params(steps))
    d.setdefault("background_capable", False)
    d.setdefault("imported", False)
    if not d.get("name"):
        d["name"] = re.sub(r"[^a-z0-9]+", "-", (d.get("command_normalized") or "")).strip("-")[:64]
    d["schema"] = 2
    return d


def _params_in(value: Any) -> list[str]:
    return sorted(set(_PARAM_RE.findall(str(value)))) if isinstance(value, str) else []


def _collect_params(steps: list[dict]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i, st in enumerate(steps, 1):
        for name in st.get("params") or _params_in(st.get("value")):
            out.setdefault(name, {"name": name, "example": "", "used_in": []})["used_in"].append(i)
    return list(out.values())


class MissingParam(Exception):
    def __init__(self, names: list[str]) -> None:
        super().__init__(f"missing inputs: {', '.join(names)}")
        self.names = names


def substitute(recipe: "Recipe", inputs: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """The recipe's steps with {{params}} filled in. Raises MissingParam BEFORE any
    action, and safety.CredentialRefused if a substituted value looks like a secret.
    Unknown inputs are ignored."""
    from recipe_engine import safety
    inputs = {str(k): str(v) for k, v in (inputs or {}).items()}
    need = sorted({n for st in recipe.steps for n in (st.get("params") or _params_in(st.get("value")))})
    missing = [n for n in need if n not in inputs]
    if missing:
        raise MissingParam(missing)
    out = []
    for st in recipe.steps:
        st = dict(st)
        v = st.get("value")
        if isinstance(v, str) and _PARAM_RE.search(v):
            v = _PARAM_RE.sub(lambda m: inputs[m.group(1)], v)
            safety.guard_text(v)
            st["value"] = v
        out.append(st)
    return out


def _pattern_kind(action: str, u: Optional[dict]) -> str:
    """'pattern' when the step can be driven by a UIA pattern without real input."""
    if not u:
        return "input"
    pat = u.get("pattern") or "none"
    if action in ("click", "double_click") and pat in ("invoke", "toggle", "select"):
        return "pattern"
    if action == "type" and pat == "set_value" and not u.get("is_password"):
        return "pattern"
    return "input"


def build(command: str, trace: list[dict], surface: str, duration_s: float,
          display: Optional[dict] = None, description: str = "", name: str = "",
          app_hints: Optional[list[str]] = None, params: Optional[list[dict]] = None) -> Recipe:
    """Turn a tool-call trace into a recipe. Every step gets an `expect`:
    browser -> the NEXT step's selector (auto-waits for the page it needs) or
    `url=<post url>`; desktop -> `title=<foreground window after the step>`."""
    norm = normalize(command)
    kept = [dict(s) for s in trace if s.get("action") not in _SKIP_ACTIONS and s.get("surface", surface) == surface]
    steps: list[dict[str, Any]] = list(kept)
    out: list[dict[str, Any]] = []
    for i, s in enumerate(steps):
        post = s.get("post") or {}
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        step: dict[str, Any] = {"action": s["action"], "selector": s.get("selector"), "value": s.get("value"),
                                "expect": None, "timeout_ms": config.RECIPE_STEP_TIMEOUT_MS,
                                "expect_timeout_ms": expect_window_ms(s, nxt)}
        if surface == "browser":
            if s["action"] == "goto":
                step["timeout_ms"] = config.BROWSER_NAV_TIMEOUT_MS
            if nxt is not None and nxt.get("selector"):
                step["expect"] = nxt["selector"]
            else:
                step["expect"] = f"url={post.get('url', '')}"
        else:
            u = s.get("uia") if isinstance(s.get("uia"), dict) else None
            step["uia"] = u
            step["pixel"] = {"rel": list(s["rel"]), "display": display} if s.get("rel") is not None else None
            step["input_kind"] = _pattern_kind(s["action"], u)
            # The window to assert after the step: the foreground title, or - when the foreground
            # is locked by another process and reads empty - the UIA window the step targeted.
            win_title = post.get("window", "") or ((u or {}).get("ancestors") or [{}])[0].get("name", "")
            step["expect"] = f"title={win_title}"
        step["params"] = _params_in(step.get("value"))
        out.append(step)
    pre: dict[str, Any] = {}
    if surface == "browser":
        first = next((s for s in steps if s["action"] == "goto"), None)
        pre["url_prefix"] = _url_prefix(((first or {}).get("post") or {}).get("url", "") or (first or {}).get("value", ""))
        host = pre["url_prefix"].split("://")[-1]
        pre["requires_login"] = not host.startswith(("127.0.0.1", "localhost"))
    else:
        pre["window"] = ((steps[0].get("post") or {}).get("window", "") if steps else "")
    declared = {pd["name"]: pd for pd in (params or []) if pd.get("name")}
    merged = _collect_params(out)
    for pd in merged:
        if pd["name"] in declared:
            pd["example"] = declared[pd["name"]].get("example", "")
    r = Recipe(id=recipe_id(norm), command_normalized=norm, surface=surface, preconditions=pre, steps=out,
               display=display if surface == "desktop" else None, description=description or command.strip(),
               name=name or "", app_hints=list(app_hints or []), params=merged,
               background_capable=bool(out) and surface == "desktop" and all(st.get("input_kind") == "pattern" for st in out),
               last_success=_now(), last_used=_now(), runs={"success": 1, "fail": 0},
               avg_duration_s=round(float(duration_s), 2), history=[{"ok": True, "t": _now(), "s": round(duration_s, 1)}])
    return r


def capture(command: str, result: Any, surface: str, duration_s: float, destructive: bool,
            display: Optional[dict] = None, description: str = "", name: str = "",
            app_hints: Optional[list[str]] = None, params: Optional[list[dict]] = None) -> Optional[Recipe]:
    """Write a recipe for a successful run, or explain (in the log) why not."""
    ok, why = should_capture(result, destructive)
    if not ok:
        log("RECIPE", f"skipped: {why}")
        return None
    r = build(command, result.trace, surface, duration_s, display=display, description=description,
              name=name, app_hints=app_hints, params=params)
    if not r.steps:
        log("RECIPE", "skipped: no steps after stripping")
        return None
    prev = load(r.id)
    if prev is not None:
        r.command_variants = list(prev.command_variants)     # keep learned phrasings; stats restart (new steps)
        r.created = prev.created
    save(r)
    log("RECIPE", f"{'overwrote' if prev else 'wrote'} {r.id} ({surface}, {len(r.steps)} steps, {duration_s:.0f}s): {path_for(r.id).name}")
    return r


# ---------------------------------------------------------------------------
# Reliability
# ---------------------------------------------------------------------------
def record_outcome(recipe: Recipe, ok: bool, duration_s: float) -> Recipe:
    key = "success" if ok else "fail"
    recipe.runs[key] = recipe.runs.get(key, 0) + 1
    n = recipe.total_runs
    recipe.avg_duration_s = round(((recipe.avg_duration_s * (n - 1)) + duration_s) / n, 2) if n else round(duration_s, 2)
    recipe.last_used = _now()
    recipe.history = (recipe.history + [{"ok": ok, "t": _now(), "s": round(duration_s, 1)}])[-config.RECIPE_HISTORY_KEEP:]
    if ok:
        recipe.last_success = _now()
        recipe.consecutive_failures = 0
    else:
        recipe.consecutive_failures += 1
    rate = recipe.window_rate()
    if recipe.consecutive_failures >= config.RECIPE_MAX_CONSECUTIVE_FAILURES:
        recipe.quarantined = True
        log("RECIPE", f"{recipe.id} QUARANTINED: {recipe.consecutive_failures} consecutive failures")
    elif rate is not None and rate < config.RECIPE_MIN_SUCCESS_RATE:
        recipe.quarantined = True
        log("RECIPE", f"{recipe.id} QUARANTINED: success rate {rate:.0%} over the last {config.RECIPE_WINDOW_RUNS} runs")
    save(recipe)
    return recipe


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
@dataclass
class ReplayResult:
    summary: str
    steps_done: int
    duration_s: float
    read_texts: list[str] = field(default_factory=list)


from recipe_engine.display import current_display, display_matches  # noqa: E402  (re-export)


def describe_steps(recipe: Recipe) -> str:
    lines = []
    for i, s in enumerate(recipe.steps, 1):
        tgt = s.get("selector") or (f"@({s['rel'][0]:.3f},{s['rel'][1]:.3f})" if s.get("rel") else "")
        val = f" {str(s.get('value'))[:60]!r}" if s.get("value") not in (None, "") else ""
        lines.append(f"  {i}. {s['action']} {tgt}{val}  -> expect {s.get('expect')} within {s.get('expect_timeout_ms') or config.RECIPE_STEP_TIMEOUT_MS}ms")
    return "\n".join(lines)


def selector_label(sel: str | None) -> str:
    """role=link[name='Invoices'] -> 'the Invoices link'; text=Save -> 'Save'; label=Search -> 'the Search field'."""
    s = (sel or "").strip()
    if not s:
        return ""
    m = re.match(r"role=(\w+)\[name=['\"](.+?)['\"]\]", s)
    if m:
        return f"the {m.group(2)} {m.group(1)}"
    for prefix, fmt in (("text=", "{}"), ("label=", "the {} field"), ("placeholder=", "the {} field"),
                        ("testid=", "{}"), ("css=", "{}"), ("title=", "{}"), ("alt=", "{}")):
        if s.lower().startswith(prefix):
            return fmt.format(s[len(prefix):].strip("'\""))
    return s


def _title_matches(expected: str, current: str) -> bool:
    e = (expected or "").strip().lstrip("*").strip().lower()
    c = (current or "").strip().lstrip("*").strip().lower()
    if not e:
        return True
    tail = e.rsplit(" - ", 1)[-1] if " - " in e else e
    return tail in c


async def _assert_expect_browser(page, expect: str, timeout_ms: int) -> None:
    from recipe_engine.browser import resolve_locator
    if not expect:
        raise ReplayFailed(0, "step has no expect assertion")
    if expect.startswith("url="):
        prefix = expect[4:]
        await page.wait_for_url(lambda u: u.startswith(prefix), timeout=timeout_ms)
        return
    await resolve_locator(page, expect).first.wait_for(state="visible", timeout=timeout_ms)


async def _perform_browser(page, step: dict, read_texts: list[str]) -> None:
    from recipe_engine.browser import resolve_locator
    from recipe_engine.safety import _SECRET_RE
    a, sel, val = step["action"], step.get("selector") or "", step.get("value")
    t = int(step.get("timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS)
    if a == "goto":
        await page.goto(str(val), wait_until="domcontentloaded", timeout=t)
    elif a == "click":
        await resolve_locator(page, sel).click(timeout=t)
    elif a == "fill":
        if _SECRET_RE.search(str(val)) or _SECRET_RE.search(sel):
            raise ReplayFailed(0, "recipe step would fill a credential; refusing")
        await resolve_locator(page, sel).fill(str(val), timeout=t)
    elif a == "press":
        if sel:
            await resolve_locator(page, sel).press(str(val), timeout=t)
        else:
            await page.keyboard.press(str(val))
    elif a == "select":
        try:
            await resolve_locator(page, sel).select_option(label=str(val), timeout=t)
        except Exception:
            await resolve_locator(page, sel).select_option(value=str(val), timeout=t)
    elif a == "check":
        await resolve_locator(page, sel).check(timeout=t)
    elif a == "wait_for":
        await resolve_locator(page, sel).first.wait_for(state="visible", timeout=t)
    elif a == "read":
        loc = resolve_locator(page, sel).first if sel else page.locator("body")
        # A `read` step's own assertion is whatever the capture recorded - often
        # "url=...", which is satisfied the instant the URL matches, i.e. BEFORE the
        # page's JS has filled the element. Reading then returns "" and the whole
        # replay speaks "Done." instead of the answer (seen 2026-09-15 on a vendor portal
        # homepage). Wait on the CONDITION "this element has text" - never on a
        # fixed sleep, which invariant 7 forbids and which would be wrong anyway (too
        # short on a slow page, wasted time on a fast one).
        try:
            handle = await loc.element_handle(timeout=t)
            if handle is not None:
                await page.wait_for_function(
                    "el => el && el.innerText && el.innerText.trim().length > 0",
                    arg=handle, timeout=t)
        except Exception:
            pass          # a genuinely empty element is still readable; don't fail the step over it
        text = await loc.inner_text(timeout=t)
        read_texts.append(text[:config.BROWSER_READ_MAX_CHARS])
    elif a == "download":
        # Re-download on every replay rather than trusting the captured path: the file
        # the original run saved may be gone, and the document can change between runs.
        # The fresh path is appended to read_texts so the desktop phase gets it.
        from recipe_engine.browser import DownloadFailed, save_download
        try:
            dest = await save_download(page, lambda: resolve_locator(page, sel).click(timeout=t))   # 15 s download budget
        except DownloadFailed as e:
            raise ReplayFailed(0, f"download failed ({e.reason}): {e.detail}")
        read_texts.append(f"downloaded file saved at: {dest}")
    elif a == "scroll":
        await page.mouse.wheel(0, -600 if str(val) == "up" else 600)
    elif a == "back":
        await page.go_back(wait_until="domcontentloaded")
    else:
        raise ReplayFailed(0, f"unknown browser action {a!r}")


async def replay_browser_steps(page, recipe: Recipe, cancel: Optional[threading.Event] = None,
                               login_check: Optional[Callable] = None,
                               steps: Optional[list[dict[str, Any]]] = None) -> list[str]:
    """Runs ON the browser loop against `page`. Returns the texts of `read` steps.
    Halts with ReplayFailed on the first failed action/assertion."""
    read_texts: list[str] = []
    done = 0
    for i, step in enumerate(steps if steps is not None else recipe.steps, 1):
        if cancel is not None and cancel.is_set():
            raise ReplayCancelled()
        try:
            await _perform_browser(page, step, read_texts)
        except ReplayFailed as e:
            raise ReplayFailed(i, e.reason, done) from None
        except Exception as e:
            raise ReplayFailed(i, f"action {step['action']} failed: {type(e).__name__}: {str(e)[:200]}", done) from None
        if login_check is not None:
            reason = await login_check(page)
            if reason:
                raise ReplayLoginBlocked(reason)
        try:
            # The action's own timeout may be long (page load); the assertion window is the short one.
            await _assert_expect_browser(page, step.get("expect") or "", int(step.get("expect_timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS))
        except ReplayFailed as e:
            raise ReplayFailed(i, e.reason, done) from None
        except Exception as e:
            raise ReplayFailed(i, f"expect {step.get('expect')!r} not met: {type(e).__name__}: {str(e)[:200]}", done) from None
        done = i
        log("REPLAY", f"step {i}/{len(recipe.steps)} ok: {step['action']} {step.get('selector') or step.get('value') or ''}")
    return read_texts


def _replay_browser(recipe: Recipe, cancel: Optional[threading.Event],
                    steps: Optional[list[dict[str, Any]]] = None) -> tuple[list[str], int]:
    from recipe_engine import browser
    svc = browser.service()

    async def _go():
        page = await svc.new_page()
        try:
            texts = await replay_browser_steps(page, recipe, cancel, login_check=browser.detect_login_page, steps=steps)
            return texts, len(recipe.steps)
        except (ReplayFailed, ReplayLoginBlocked):
            svc.keep_page(page)          # leave it for the user / the vision fallback to look at
            raise

    return svc.run(_go(), timeout=None)


def _wait_title(expected: str, timeout_ms: int, cancel: Optional[threading.Event] = None) -> str:
    """Bounded poll of the foreground window title (100 ms) - a wait-until, not a
    sleep. The match must hold for RECIPE_DESKTOP_STABLE_MS: a window that shows
    its final title while it is still opening/animating is not ready for the
    next action (that is exactly how a replay used to click into thin air)."""
    from recipe_engine.display import foreground_window_title
    deadline = time.time() + timeout_ms / 1000.0
    stable_needed = config.RECIPE_DESKTOP_STABLE_MS / 1000.0
    matched_since: Optional[float] = None
    cur = foreground_window_title()
    while True:
        now = time.time()
        if not cur and expected:
            # The foreground is held by a titleless window (a service holding the foreground lock):
            # the app cannot be "in front" here, so accept it being present with the expected title.
            for t in _window_titles():
                if _title_matches(expected, t):
                    cur = t
                    break
        if _title_matches(expected, cur):
            if matched_since is None:
                matched_since = now
            if now - matched_since >= stable_needed:
                return cur
        else:
            matched_since = None
        if now >= deadline:
            return cur
        if cancel is not None and cancel.is_set():
            raise ReplayCancelled()
        time.sleep(0.1)
        cur = foreground_window_title()


def _window_titles() -> list[str]:
    """Titles of every top-level window - on ANY virtual desktop (background replay
    asserts the window exists and carries the expected title, not that it is in front)."""
    import ctypes
    import ctypes.wintypes as W
    out: list[str] = []
    u = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            out.append(buf.value)
        return True
    u.EnumWindows(cb, 0)
    return out


def _wait_window(expected: str, timeout_ms: int, cancel: Optional[threading.Event] = None) -> Optional[str]:
    """Background-mode assertion: a top-level window whose title matches `expected`
    exists (foreground not required). Deadline-bounded poll; None on timeout."""
    deadline = time.time() + timeout_ms / 1000.0
    while True:
        for t in _window_titles():
            if _title_matches(expected, t):
                return t
        if time.time() >= deadline:
            return None
        if cancel is not None and cancel.is_set():
            raise ReplayCancelled()
        time.sleep(0.1)


def _has_keyboard_focus(element) -> bool:
    """True when the resolved element already owns keyboard focus. The focus click exists
    to recover focus that something else took; clicking a field that is ALREADY focused
    drops its selection (a dialog's pre-filled file name, the Run box's last command), so
    the typing that replaced the text when recorded would append to it at replay."""
    try:
        if element is None:
            return False
        import uiautomation as auto
        f = auto.GetFocusedControl()
        return f is not None and list(f.GetRuntimeId() or []) == list(element.GetRuntimeId() or [])
    except Exception:
        return False


def _type_chunked(pyautogui, text: str, cancel: Optional[threading.Event]) -> None:
    """The foreground typing loop: shift+enter for newlines (a raw Enter would SEND),
    written in short chunks so an Esc abort stops the keystrokes within the budget."""
    segments = text.split("\n")
    for j, seg in enumerate(segments):
        for k in range(0, len(seg), 16):
            if cancel is not None and cancel.is_set():
                raise ReplayCancelled()
            pyautogui.write(seg[k:k + 16], interval=config.TYPE_INTERVAL_S)
        if j < len(segments) - 1:
            pyautogui.hotkey("shift", "enter")


def _replay_desktop_background(recipe: Recipe, cancel: Optional[threading.Event],
                               steps: Optional[list[dict[str, Any]]] = None) -> tuple[list[str], int]:
    """Pattern-only replay: every step drives its UIA element through a provider
    pattern (Invoke / SetValue / Toggle / Select). No pixel fallback, no real input,
    no foreground - and no silent switch to the foreground path mid-run: a step that
    cannot be driven this way FAILS with pattern_unavailable."""
    import ctypes
    from recipe_engine import safety, uia, vdesk
    from recipe_engine.display import current_display, make_dpi_aware
    from recipe_engine.resolver import UnresolvableElement, resolve_target
    make_dpi_aware()
    now = current_display()
    fg_user = int(ctypes.windll.user32.GetForegroundWindow() or 0)     # what the user is looking at
    done = 0
    for i, step in enumerate(steps if steps is not None else recipe.steps, 1):
        if cancel is not None and cancel.is_set():
            raise ReplayCancelled()
        a, val = step["action"], step.get("value")
        u = step.get("uia") or {}
        if step.get("input_kind") != "pattern" or not u:
            raise ReplayFailed(i, f"pattern_unavailable: step {a!r} needs real input; this recipe cannot run in the background", done)
        try:
            r = resolve_target(step, now, int(step.get("timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS), pixel_ok=False, cancel=cancel)
        except UnresolvableElement as e:
            raise ReplayFailed(i, str(e), done) from None
        if r.kind != "uia":
            raise ReplayFailed(i, "pattern_unavailable: element not resolved by identity", done)
        pattern = u.get("pattern") or "none"
        try:
            if a in ("click", "double_click"):
                uia.perform_pattern(r.element, pattern)
            elif a == "type":
                try:
                    safety.guard_text(str(val))
                except safety.CredentialRefused:
                    raise ReplayFailed(i, "recipe step would type a credential; refusing", done)
                if uia._is_password(r.element):
                    raise ReplayFailed(i, "refused: the target is a password field", done)
                uia.perform_pattern(r.element, "set_value", str(val))
            else:
                raise ReplayFailed(i, f"pattern_unavailable: {a!r} has no UIA pattern", done)
        except uia.PatternUnavailable as e:
            raise ReplayFailed(i, f"pattern_unavailable: {e}", done) from None
        vdesk.restore_foreground(fg_user)           # a provider may have pulled focus to its own control
        expect = step.get("expect") or ""
        if not expect.startswith("title="):
            raise ReplayFailed(i, f"desktop step has no window assertion ({expect!r})", done)
        cur = _wait_window(expect[6:], int(step.get("timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS), cancel)
        if cur is None:
            raise ReplayFailed(i, f"expected a window titled {expect[6:]!r}; none found", done)
        done = i
        log("REPLAY", f"[background] step {i}/{len(recipe.steps)} ok: {a} via {pattern} -> {cur!r}")
    return [], done


def _replay_desktop(recipe: Recipe, cancel: Optional[threading.Event],
                    steps: Optional[list[dict[str, Any]]] = None, background: bool = False) -> tuple[list[str], int]:
    if background:
        return _replay_desktop_background(recipe, cancel, steps)
    import pyautogui
    from recipe_engine import safety
    from recipe_engine.display import current_display, make_dpi_aware
    from recipe_engine.resolver import UnresolvableElement, resolve_target
    make_dpi_aware()
    now = current_display()

    def target(step, i, done, timeout_ms=None):
        try:
            return resolve_target(step, now, int(timeout_ms or step.get("timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS), cancel=cancel)
        except UnresolvableElement as e:
            raise ReplayFailed(i, str(e), done) from None

    done = 0
    for i, step in enumerate(steps if steps is not None else recipe.steps, 1):
        if cancel is not None and cancel.is_set():
            raise ReplayCancelled()
        a, val = step["action"], step.get("value")
        try:
            if a in ("click", "double_click", "right_click"):
                r = target(step, i, done)
                x, y = r.xy
                {"click": pyautogui.click, "double_click": pyautogui.doubleClick, "right_click": pyautogui.rightClick}[a](x, y)
            elif a == "type":
                try:
                    safety.guard_text(str(val))
                except safety.CredentialRefused:
                    raise ReplayFailed(i, "recipe step would type a credential; refusing", done)
                if step.get("uia"):
                    try:
                        r = target(step, i, done, timeout_ms=1500)   # opportunistic: a short look, not a step budget
                        if not _has_keyboard_focus(r.element):
                            pyautogui.click(*r.xy)      # put focus where it was when recorded
                    except ReplayFailed:
                        pass                            # typing into whatever has focus is how v1 behaved
                _type_chunked(pyautogui, str(val), cancel)
            elif a == "key":
                parts = [p for p in str(val).replace(" ", "").split("+") if p]
                pyautogui.press(parts[0]) if len(parts) == 1 else pyautogui.hotkey(*parts)
            elif a == "scroll":
                r = target(step, i, done)
                x, y = r.xy
                pyautogui.scroll(int(val), x=x, y=y)
            elif a == "open_browser":
                from recipe_engine import browser
                browser.open_url(str(val))
            else:
                raise ReplayFailed(i, f"unknown desktop action {a!r}", done)
        except (ReplayFailed, ReplayCancelled):
            raise
        except Exception as e:
            raise ReplayFailed(i, f"action {a} failed: {type(e).__name__}: {str(e)[:200]}", done) from None
        expect = step.get("expect") or ""
        if not expect.startswith("title="):
            raise ReplayFailed(i, f"desktop step has no window assertion ({expect!r})", done)
        cur = _wait_title(expect[6:], int(step.get("timeout_ms") or config.RECIPE_STEP_TIMEOUT_MS), cancel)
        if not _title_matches(expect[6:], cur):
            raise ReplayFailed(i, f"expected window {expect[6:]!r}, foreground is {cur!r}", done)
        done = i
        log("REPLAY", f"step {i}/{len(recipe.steps)} ok: {a} -> {cur!r}")
    return [], done


def replay(recipe: Recipe, cancel: Optional[threading.Event] = None,
           steps: Optional[list[dict[str, Any]]] = None, background: bool = False) -> ReplayResult:
    """Verified replay. Raises ReplayFailed / ReplayLoginBlocked / ReplayCancelled;
    the CALLER records the outcome (record_outcome) so stats stay in one place.
    `summary` is the raw text the recipe read, trimmed - the engine never summarises,
    the client model does."""
    if recipe.quarantined:
        raise ReplayFailed(0, "recipe is quarantined")
    log("REPLAY", f"recipe {recipe.id} ({recipe.surface}): {recipe.label}\n{describe_steps(recipe)}")
    t0 = time.time()
    if recipe.surface == "desktop":
        # No whole-recipe display gate any more: each step's target is resolved by UIA
        # identity first, and pixel coordinates are used only when THAT step's recorded
        # display matches (resolver.resolve_target) - so a recipe recorded at 1080p
        # still runs at 1440p wherever the element can be found by what it is.
        texts, n = _replay_desktop(recipe, cancel, steps, background=background)
    else:
        texts, n = _replay_browser(recipe, cancel, steps)
    dur = time.time() - t0
    raw = " ".join(" ".join(t.split()) for t in texts).strip() if texts else ""
    summary = raw[:config.RECIPE_SUMMARY_MAX_RAW_CHARS] or "Done."
    return ReplayResult(summary=summary, steps_done=n, duration_s=dur, read_texts=texts)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
def list_table() -> str:
    rows = load_all()
    if not rows:
        return "(no recipes yet)"
    head = f"{'id':<13}{'surface':<9}{'rate':>6}{'runs':>6}{'avg s':>7}  {'last used':<20} {'flags':<14} description"
    lines = [head, "-" * len(head)]
    for r in rows:
        flags = ",".join(f for f, on in (("quarantined", r.quarantined), ("stale", r.stale)) if on) or "-"
        used = (r.last_used or r.created or "")[:19].replace("T", " ")
        lines.append(f"{r.id:<13}{r.surface:<9}{r.success_rate:>6.0%}{r.total_runs:>6}{r.avg_duration_s:>7.1f}  {used:<20} {flags:<14} {r.label[:50]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verifier (self-repair) and self-test: no model calls, no browser, temp dir
# ---------------------------------------------------------------------------
def verify() -> dict:
    n = normalize("Please check my vendor invoices!")
    assert n == "check vendor invoices", n
    assert recipe_id(n) == recipe_id("check vendor invoices")
    r = build("check vendor invoices",
              [{"surface": "browser", "action": "goto", "value": "https://x.example/", "post": {"url": "https://x.example/", "title": "X"}},
               {"surface": "browser", "action": "snapshot", "post": {"url": "https://x.example/"}},
               {"surface": "browser", "action": "click", "selector": "role=link[name='Invoices']", "post": {"url": "https://x.example/invoices"}},
               {"surface": "browser", "action": "read", "selector": "role=table", "post": {"url": "https://x.example/invoices"}}],
              "browser", 42.0)
    assert [s["action"] for s in r.steps] == ["goto", "click", "read"], r.steps
    assert r.steps[0]["expect"] == "role=link[name='Invoices']" and r.steps[2]["expect"] == "url=https://x.example/invoices"
    assert r.steps[0]["expect_timeout_ms"] == config.RECIPE_STEP_TIMEOUT_MS      # no timestamps -> the floor
    slow = build("slow", [{"surface": "browser", "action": "goto", "value": "http://s/", "t": 100.0, "post": {"url": "http://s/"}},
                          {"surface": "browser", "action": "read", "selector": "css=p", "t": 117.0, "post": {"url": "http://s/"}}],
                 "browser", 20.0)
    assert slow.steps[0]["expect_timeout_ms"] == 25500, slow.steps[0]
    assert expect_window_ms({"t": 0.0}, {"t": 999.0}) == config.RECIPE_EXPECT_TIMEOUT_MAX_MS
    assert r.preconditions["url_prefix"] == "https://x.example"
    return {"normalized": n, "id": r.id, "steps": len(r.steps), "recipes_on_disk": len(load_all())}


def _selftest() -> None:
    import tempfile
    from types import SimpleNamespace

    tmp = tempfile.mkdtemp(prefix="recipes-test-")
    config.RECIPES_DIR = Path(tmp)
    print("recipes self-test in", tmp)

    # 1. normalisation + ids
    assert normalize("Hey, could you check my invoices please?") == "check invoices"
    assert normalize("what are my invoices") == "what are invoices"
    assert normalize("please") == "please"                  # never empty

    # 2. capture guards
    good = SimpleNamespace(login_blocked=False, login_seen=False, credential_flagged=False, cancelled=False,
                           error=None, hit_turn_limit=False,
                           trace=[{"surface": "browser", "action": "goto", "value": "http://127.0.0.1:8765/", "post": {"url": "http://127.0.0.1:8765/"}},
                                  {"surface": "browser", "action": "click", "selector": "role=link[name='Invoices']", "post": {"url": "http://127.0.0.1:8765/invoices.html"}},
                                  {"surface": "browser", "action": "read", "selector": "role=table", "post": {"url": "http://127.0.0.1:8765/invoices.html"}}])
    assert should_capture(good, destructive=False)[0]
    assert not should_capture(good, destructive=True)[0]
    for bad in ({"login_seen": True}, {"login_blocked": True}, {"credential_flagged": True}, {"cancelled": True},
                {"error": "x"}, {"hit_turn_limit": True}, {"trace": [{"action": "screenshot"}]}):
        ns = SimpleNamespace(**{**good.__dict__, **bad})
        ok, why = should_capture(ns, destructive=False)
        assert not ok, bad
    assert capture("send an email", good, "browser", 5, destructive=True) is None
    polled = SimpleNamespace(**{**good.__dict__, "trace": good.trace + [good.trace[-1]]})   # read css=table twice
    ok, why = should_capture(polled, destructive=False)
    assert not ok and "polled" in why, why
    polled2 = SimpleNamespace(**{**good.__dict__, "trace": [good.trace[0], {"action": "snapshot"}, {"action": "snapshot"}, good.trace[1]]})
    assert not should_capture(polled2, destructive=False)[0]
    once = SimpleNamespace(**{**good.__dict__, "trace": [{"action": "snapshot"}] + good.trace})       # one look is fine
    assert should_capture(once, destructive=False)[0]
    assert capture("x", SimpleNamespace(**{**good.__dict__, "login_seen": True}), "browser", 5, destructive=False) is None
    assert not list(Path(tmp).glob("*.json")), "guarded runs must write nothing"

    # 3. capture + exact/variant matching + quarantine exclusion
    r = capture("check my invoices", good, "browser", 31.0, destructive=False)
    assert r is not None and path_for(r.id).exists()
    assert r.preconditions == {"url_prefix": "http://127.0.0.1:8765", "requires_login": False}
    m = lookup("please check my invoices")
    assert m.kind == "exact" and m.recipe.id == r.id and not m.needs_confirmation
    assert lookup("what are my invoices").kind == "none"
    add_variant(r, normalize("what are my invoices"))
    assert lookup("what are my invoices").kind == "exact"

    # 4. stats + quarantine (2 consecutive failures)
    r = load(r.id)
    record_outcome(r, True, 3.0)
    record_outcome(r, False, 2.0)
    assert not load(r.id).quarantined
    record_outcome(r, False, 2.0)
    r = load(r.id)
    assert r.quarantined and r.consecutive_failures == 2 and r.runs == {"success": 2, "fail": 2}
    assert lookup("check my invoices").kind == "quarantined"
    # a successful re-plan overwrites and clears quarantine, keeps variants
    r2 = capture("check my invoices", good, "browser", 28.0, destructive=False)
    assert not r2.quarantined and r2.consecutive_failures == 0 and "what are invoices" in r2.command_variants

    # 5. window-rate quarantine (< 70 % over 5)
    r3 = capture("open the news", good, "browser", 10.0, destructive=False)
    for ok in (True, False, True, False):
        record_outcome(load(r3.id), ok, 1.0)
    r3 = load(r3.id)
    assert r3.window_rate() == 0.6 and r3.quarantined, (r3.window_rate(), r3.quarantined)

    # 6. stale
    r4 = capture("read the weather", good, "browser", 10.0, destructive=False)
    r4.last_success = "2020-01-01T00:00:00+00:00"
    save(r4)
    m = lookup("read the weather")
    assert m.kind == "stale" and m.needs_confirmation

    # 7. desktop stamp invalidation: no step executed
    d = capture("open notepad", SimpleNamespace(**{**good.__dict__, "trace": [
        {"surface": "desktop", "action": "key", "value": "win", "post": {"window": "Start"}},
        {"surface": "desktop", "action": "type", "value": "notepad", "post": {"window": "Start"}},
        {"surface": "desktop", "action": "key", "value": "enter", "post": {"window": "Untitled - Notepad"}}]}),
        "desktop", 20.0, destructive=False, display={"w": 1920, "h": 1080, "scale": 1.0})
    assert d.display == {"w": 1920, "h": 1080, "scale": 1.0} and d.steps[2]["expect"] == "title=Untitled - Notepad"
    assert display_matches(d.display, {"w": 1920, "h": 1080, "scale": 1.0})
    assert not display_matches(d.display, {"w": 2560, "h": 1440, "scale": 1.0})
    assert not display_matches(d.display, {"w": 1920, "h": 1080, "scale": 1.25})
    assert _title_matches("Untitled - Notepad", "*hello.txt - Notepad") and not _title_matches("Untitled - Notepad", "Calculator")

    # 8. replay halts at the first failed expect (fake page, no browser)
    class FakeLoc:
        def __init__(self, page, sel): self.page, self.sel = page, sel
        @property
        def first(self): return self
        async def click(self, timeout=None):
            self.page.log.append(("click", self.sel)); self.page.url = "http://127.0.0.1:8765/invoices.html"
        async def fill(self, v, timeout=None): self.page.log.append(("fill", self.sel))
        async def inner_text(self, timeout=None): self.page.log.append(("read", self.sel)); return "Acme 91.50"
        async def wait_for(self, state="visible", timeout=None):
            if self.sel in self.page.missing:
                raise TimeoutError(f"{self.sel} not visible")

    class FakePage:
        def __init__(self, missing=()): self.log, self.missing, self.url = [], set(missing), "http://127.0.0.1:8765/"
        def locator(self, sel): return FakeLoc(self, sel)
        async def goto(self, url, wait_until=None, timeout=None): self.log.append(("goto", url)); self.url = url
        async def wait_for_url(self, pred, timeout=None):
            if not pred(self.url): raise TimeoutError("url")

    rec = load(r2.id)
    p = FakePage()
    texts = asyncio.run(replay_browser_steps(p, rec))
    assert [x[0] for x in p.log] == ["goto", "click", "read"] and texts == ["Acme 91.50"]
    p = FakePage(missing={"role=link[name='Invoices']"})       # the button was renamed
    try:
        asyncio.run(replay_browser_steps(p, rec))
        raise AssertionError("must fail")
    except ReplayFailed as e:
        assert e.step == 1 and e.steps_done == 0, (e.step, e.steps_done)
        assert [x[0] for x in p.log] == ["goto"], p.log   # nothing after the failed assertion ran
    p = FakePage(missing={"role=table"})
    try:
        asyncio.run(replay_browser_steps(p, rec))
        raise AssertionError("must fail")
    except ReplayFailed as e:
        assert e.step == 2 and e.steps_done == 1 and [x[0] for x in p.log] == ["goto", "click"]
    # quarantined recipes never replay
    q = load(r3.id)
    try:
        replay(q)
        raise AssertionError("quarantined must not replay")
    except ReplayFailed as e:
        assert "quarantined" in e.reason
    # login during replay is not a recipe failure
    async def login_yes(page): return "visible password field"
    try:
        asyncio.run(replay_browser_steps(FakePage(), rec, login_check=login_yes))
        raise AssertionError("must raise")
    except ReplayLoginBlocked:
        pass
    # 9. listing + forget
    assert r2.id in list_table() and "quarantined" in list_table()
    assert forget(r4.id) and not forget(r4.id)
    print(list_table())
    print("recipes self-test ok")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        print(json.dumps(verify(), default=str))
        sys.exit(0)
    if "--test" in sys.argv:
        _selftest()
        sys.exit(0)
    if "--forget" in sys.argv:
        rid = sys.argv[sys.argv.index("--forget") + 1]
        print("forgotten" if forget(rid) else "no such recipe", rid)
        sys.exit(0)
    print(list_table())
