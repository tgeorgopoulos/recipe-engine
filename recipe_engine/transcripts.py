"""One JSONL line per replay, per recipe, plus the plain-English reading of them.

The transcript is the evidence. `recipe_stats` reads it back for a non-engineer
("ran 14 times in the last 30 days: 13 succeeded ..."), the harness reads it for
ranked failure reasons, and `replay_recipe` reads the last failures so a client
model is told what already did not work instead of rediscovering it.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from recipe_engine import config


def path_for(recipe_id: str) -> Path:
    return Path(config.TRANSCRIPTS_DIR) / f"{recipe_id}.jsonl"


def append(recipe_id: str, record: dict[str, Any]) -> Path:
    p = path_for(recipe_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"t": round(time.time(), 3), **record}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def last(recipe_id: str, n: int = 50) -> list[dict[str, Any]]:
    p = path_for(recipe_id)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def failed_reasons(recipe_id: str, n: int = 5) -> list[str]:
    """Distinct recent failure reasons, newest first - what a client model must not retry."""
    seen: list[str] = []
    for r in reversed(last(recipe_id, 50)):
        if r.get("status") in ("failed", "quarantined") and r.get("failure_reason"):
            reason = str(r["failure_reason"])
            step = r.get("failed_step")
            text = f"step {step}: {reason}" if step else reason
            if text not in seen:
                seen.append(text)
            if len(seen) >= n:
                break
    return seen


def _ago(t: float) -> str:
    d = time.time() - t
    if d < 90:
        return "just now"
    if d < 5400:
        return f"{int(d // 60)} minutes ago"
    if d < 172800:
        return f"{int(d // 3600)} hours ago"
    return f"{int(d // 86400)} days ago"


def stats_text(recipe, records: Optional[list[dict[str, Any]]] = None, days: int = 30) -> str:
    """Plain English for a person, not a dashboard."""
    recs = records if records is not None else last(recipe.id, 500)
    cutoff = time.time() - days * 86400
    recent = [r for r in recs if float(r.get("t", 0)) >= cutoff]
    name = getattr(recipe, "name", None) or recipe.label
    if not recent:
        base = f"{name} has not run in the last {days} days."
    else:
        ok = [r for r in recent if r.get("status") == "ok"]
        bad = [r for r in recent if r.get("status") in ("failed", "quarantined")]
        other = len(recent) - len(ok) - len(bad)
        parts = [f"{name} has run {len(recent)} time{'s' if len(recent) != 1 else ''} in the last {days} days: "
                 f"{len(ok)} succeeded, {len(bad)} failed"]
        if other:
            parts.append(f", {other} refused or busy")
        parts.append(".")
        if bad:
            c = Counter((r.get("failed_step"), str(r.get("failure_reason") or "")[:90]) for r in bad)
            (step, reason), count = c.most_common(1)[0]
            where = f"step {step}" if step else "before the first step"
            pct = round(100 * count / len(recent))
            parts.append(f" Most common failure ({pct}% of runs): {where} - {reason}.")
        secs = sorted(float(r.get("seconds", 0)) for r in ok if r.get("seconds"))
        if secs:
            med = secs[len(secs) // 2]
            parts.append(f" Typical successful run: {med:.1f} s.")
        base = "".join(parts)
    q = " QUARANTINED: it will not replay until re-recorded." if getattr(recipe, "quarantined", False) else " Not quarantined."
    ls = getattr(recipe, "last_success", None)
    if ls:
        try:
            from datetime import datetime
            t = datetime.fromisoformat(ls.replace("Z", "+00:00")).timestamp()
            q += f" Last success: {_ago(t)}."
        except Exception:
            q += f" Last success: {ls}."
    return base + q
