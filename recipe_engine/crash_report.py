"""
Crash reports under <data dir>/reports/<timestamp>/.

    report.md          human summary: what was asked, state, classification, likely cause
    traceback.txt      full traceback
    session.log        last 200 log lines (state transitions included)
    screenshot.png     screen at failure (if permissions allow)
    plan.txt           transcribed command + generated plan
    env.json           python, venv, packages, OS, resolution/DPI, data dir, settings
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from recipe_engine import config
from recipe_engine.config import log


def new_report_dir() -> Path:
    d = config.REPORTS_DIR / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ env.json
def _pkg_versions() -> dict[str, str]:
    from importlib import metadata
    names = []
    try:
        for line in (config.PROJECT_DIR / "requirements.txt").read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                names.append(line.split(">=")[0].split("==")[0].strip())
    except OSError:
        pass
    out = {}
    for n in names:
        try:
            out[n] = metadata.version(n)
        except metadata.PackageNotFoundError:
            out[n] = "MISSING"
    return out




def env_info() -> dict[str, Any]:
    env: dict[str, Any] = {
        "python": sys.version,
        "executable": sys.executable,
        "venv": sys.prefix,
        "venv_is_project": str(config.PROJECT_DIR) in sys.prefix,
        "os": platform.platform(),
        "packages": _pkg_versions(),
        "data_dir": str(config.DATA_DIR),
        "settings": {"BIND_HOST": config.BIND_HOST, "PORT": config.PORT, "BACKGROUND_REPLAY": config.BACKGROUND_REPLAY},
        "browser": {"channel": config.BROWSER_CHANNEL, "profile": str(config.BROWSER_PROFILE_DIR),
                    "profile_exists": config.BROWSER_PROFILE_DIR.exists(), "slots": config.browser_slots()},
        "recipes": len(list(config.RECIPES_DIR.glob("*.json"))) if config.RECIPES_DIR.exists() else 0,
    }
    try:
        from recipe_engine.display import dpi_scale_factor, physical_screen_size
        env["screen_physical"] = physical_screen_size()
        env["dpi_scale"] = dpi_scale_factor()
    except Exception as e:
        env["screen_error"] = repr(e)
    return env


# ------------------------------------------------------------- likely cause
def likely_cause(failure) -> str:
    """One plain-language sentence. `failure` is failures.Failure."""
    cls = failure.cls.name
    exc = failure.exc
    where = f"{failure.origin_file} line {failure.origin_line}" if failure.origin_file else "outside our code"
    if cls == "ENVIRONMENT":
        return f"A dependency or model file is missing ({type(exc).__name__}: {exc}). Usually fixed by `pip install -r requirements.txt` or re-downloading the model."
    if cls == "PERMISSION":
        return f"The OS blocked desktop or microphone access ({exc}). A human has to grant the permission or run from a matching (elevated / non-elevated) terminal."
    if cls == "AUTH":
        return f"A site asked for a sign-in during the replay ({exc}). Sign in once in the engine's browser, then replay again."
    if cls == "TRANSIENT":
        return f"A network or service hiccup ({type(exc).__name__}: {exc}). It was retried; if it persists check connectivity / Anthropic status."
    if cls == "TASK":
        return f"The task itself could not be completed on screen ({exc}). Not a code bug: the UI did not match the plan."
    if cls == "CODE":
        return f"Our own code raised {type(exc).__name__} at {where}: {exc}. This is a bug in the file named above."
    return f"Unclassified error {type(exc).__name__} at {where}: {exc}."


# ------------------------------------------------------------------- write
def write(failure, *, report_dir: Optional[Path] = None, extra: Optional[dict] = None) -> Path:
    """Write the full report set. Never raises: a broken report writer must not
    mask the original failure."""
    d = report_dir or new_report_dir()
    try:
        (d / "traceback.txt").write_text(failure.traceback, encoding="utf-8")
        (d / "session.log").write_text("\n".join(list(config.LOG_RING)[-200:]), encoding="utf-8")
        (d / "plan.txt").write_text(f"COMMAND:\n{failure.command or '(none)'}\n\nPLAN:\n{failure.plan or '(none)'}\n", encoding="utf-8")
        (d / "env.json").write_text(json.dumps(env_info(), indent=2, default=str), encoding="utf-8")
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(d / "screenshot.png")
        except Exception as e:
            (d / "screenshot.txt").write_text(f"screenshot unavailable: {e!r}", encoding="utf-8")
        cause = likely_cause(failure)
        md = [
            f"# Crash report {d.name}",
            "",
            f"**What the user asked for:** {failure.command or '(nothing transcribed yet)'}",
            "",
            f"**State it failed in:** {failure.state}",
            "",
            f"**Failure classification:** {failure.cls.name}",
            "",
            f"**Most likely cause:** {cause}",
            "",
            f"**Exception:** `{type(failure.exc).__name__}: {failure.exc}`",
            "",
            f"**Origin:** {failure.origin_file or '-'}:{failure.origin_line or '-'}",
            "",
            f"**Verifier:** `{failure.verifier or '-'}` {json.dumps(failure.verifier_args) if failure.verifier_args else ''}",
            "",
            "## Files",
            "- traceback.txt, session.log, plan.txt, env.json, screenshot.png",
        ]
        if extra:
            md += ["", "## Notes"] + [f"- **{k}:** {v}" for k, v in extra.items()]
        (d / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        log("REPORT", f"written to {d}")
    except Exception as e:
        log("REPORT", f"could not write full report to {d}: {e!r}")
    return d


def append_note(report_dir: Path, text: str) -> None:
    try:
        with (report_dir / "report.md").open("a", encoding="utf-8") as f:
            f.write(f"\n{text}\n")
    except Exception:
        pass


if __name__ == "__main__":
    print(json.dumps(env_info(), indent=2, default=str))
