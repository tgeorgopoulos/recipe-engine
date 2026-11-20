"""`recipe-engine` command line.

  recipe-engine serve [--stdio] [--no-tray] [--bind HOST] [--port N]
  recipe-engine token [--show | --rotate]
  recipe-engine check          desktop-control + invariants + browser, no serving
  recipe-engine selftest       every module's self-test (no model, no real desktop actions)
  recipe-engine migrate [--dry-run]   copy the old voice-agent data into the engine's data dir
  recipe-engine recipes list | forget NAME | replay NAME | stats NAME
  recipe-engine export NAME [FILE] | import FILE [--replace] | jobs list|add|remove
  recipe-engine install [--no-autostart] | uninstall [--purge]
  recipe-engine --version
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def _print(obj) -> None:
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print(json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj)


def cmd_serve(a) -> int:
    from recipe_engine import service
    return service.serve(stdio=a.stdio, no_tray=a.no_tray, bind=a.bind, port=a.port)


def cmd_token(a) -> int:
    from recipe_engine import auth
    if a.rotate:
        print(auth.rotate_token())
    else:
        print(auth.ensure_token())
    return 0


def cmd_check(a) -> int:
    from recipe_engine import display, invariants
    problems = invariants.check()
    print("invariants:", "ok" if not problems else "BROKEN")
    for p in problems:
        print("  -", p)
    chk = display.check_control_permissions()
    print("desktop control:", chk.message)
    try:
        from recipe_engine import browser
        print("browser:", browser.verify())
    except Exception as e:
        print("browser: unavailable -", repr(e)[:200])
    return 0 if not problems and chk.ok else 1


def cmd_selftest(a) -> int:
    from recipe_engine import engine, invariants, recipes, safety
    safety._selftest()
    engine._selftest()
    recipes._selftest()
    return invariants._selftest()


def cmd_migrate(a) -> int:
    """Copy the voice-agent era data (recipes, browser profile, sealed session) into the engine's data dir."""
    from recipe_engine import config
    old_home = Path.home() / "voice-agent"
    moves = [
        (config.PROJECT_DIR / "recipes", config.RECIPES_DIR, "recipes"),
        (old_home / "browser-profile", config.BROWSER_PROFILE_DIR, "browser profile"),
        (old_home / "browser-session.bin", config.SESSION_STATE_FILE, "sealed session"),
    ]
    for src, dst, what in moves:
        if not src.exists():
            print(f"skip {what}: {src} not found")
            continue
        if dst.exists() and any(dst.iterdir()) if dst.is_dir() else dst.exists():
            print(f"skip {what}: {dst} already has data")
            continue
        print(f"{'would copy' if a.dry_run else 'copying'} {what}: {src} -> {dst}")
        if not a.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    if not a.dry_run:
        # the copied recipes are plaintext from the voice-agent era: seal them now
        from recipe_engine import recipes, sealed
        n = 0
        for r in recipes.load_all():
            if sealed.is_plaintext_on_disk(recipes.path_for(r.id)):
                recipes.save(r)
                n += 1
        if n:
            print(f"sealed {n} recipe file(s) ({'DPAPI' if sealed.sealed() else 'plaintext mode'})")
    return 0


def cmd_export(a) -> int:
    from recipe_engine import export
    out = export.export_recipe(a.name)
    if out.get("status") != "ok":
        print(out.get("failure_reason"))
        return 1
    if a.file:
        Path(a.file).write_text(out["json"], encoding="utf-8")
        print(f"wrote {a.file} ({out['steps']} steps)")
    else:
        print(out["json"])
    return 0


def cmd_import(a) -> int:
    from recipe_engine import export
    out = export.import_recipe(Path(a.file).read_text(encoding="utf-8"), replace=a.replace)
    print(out)
    return 0 if out.get("status") == "ok" else 1


def cmd_install(a) -> int:
    from recipe_engine import installer
    out = installer.install(exe=a.exe, autostart=not a.no_autostart)
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_uninstall(a) -> int:
    from recipe_engine import installer
    out = installer.uninstall(purge=a.purge)
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_jobs(a) -> int:
    from recipe_engine import scheduler
    if a.action == "list":
        jobs = scheduler.list_jobs()
        if not jobs:
            print("no scheduled jobs")
        for j in jobs:
            last = f"  last {j.last.get('at')} {j.last.get('status')}" if j.last else ""
            print(f"{j.recipe:30s} {j.cron:16s} next {j.next_run}{last}")
        return 0
    if a.action == "remove":
        out = scheduler.remove_job(a.name or "")
        print(out)
        return 0 if out.get("status") == "ok" else 1
    if a.action == "add":
        out = scheduler.add_job(a.name or "", a.cron or "")
        print(out)
        return 0 if out.get("status") == "ok" else 1
    return 2


def cmd_recipes(a) -> int:
    from recipe_engine import engine, recipes, transcripts
    if a.action == "list":
        print(recipes.list_table())
        return 0
    r = recipes.load_by_name(a.name or "")
    if r is None:
        print(f"no recipe named {a.name!r}")
        return 1
    if a.action == "forget":
        ok = recipes.forget(r.id)
        try:
            transcripts.path_for(r.id).unlink(missing_ok=True)      # history goes with the recipe
        except OSError:
            pass
        print("forgotten" if ok else "not found")
        return 0
    if a.action == "stats":
        print(transcripts.stats_text(r))
        return 0
    if a.action == "replay":
        eng = engine.Engine()
        _print(eng.replay(r.name or r.command_normalized, trigger="cli"))
        return 0
    return 2


def main(argv=None) -> int:
    from recipe_engine import __version__
    ap = argparse.ArgumentParser(prog="recipe-engine", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--stdio", action="store_true"); s.add_argument("--no-tray", action="store_true")
    s.add_argument("--bind"); s.add_argument("--port", type=int); s.set_defaults(fn=cmd_serve)
    t = sub.add_parser("token"); g = t.add_mutually_exclusive_group(); g.add_argument("--show", action="store_true"); g.add_argument("--rotate", action="store_true"); t.set_defaults(fn=cmd_token)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    m = sub.add_parser("migrate"); m.add_argument("--dry-run", action="store_true"); m.set_defaults(fn=cmd_migrate)
    r = sub.add_parser("recipes"); r.add_argument("action", choices=["list", "forget", "replay", "stats"]); r.add_argument("name", nargs="?"); r.set_defaults(fn=cmd_recipes)
    e = sub.add_parser("export"); e.add_argument("name"); e.add_argument("file", nargs="?"); e.set_defaults(fn=cmd_export)
    i = sub.add_parser("import"); i.add_argument("file"); i.add_argument("--replace", action="store_true"); i.set_defaults(fn=cmd_import)
    ins = sub.add_parser("install"); ins.add_argument("--exe"); ins.add_argument("--no-autostart", action="store_true"); ins.set_defaults(fn=cmd_install)
    un = sub.add_parser("uninstall"); un.add_argument("--purge", action="store_true"); un.set_defaults(fn=cmd_uninstall)
    j = sub.add_parser("jobs"); j.add_argument("action", choices=["list", "add", "remove"]); j.add_argument("name", nargs="?"); j.add_argument("cron", nargs="?"); j.set_defaults(fn=cmd_jobs)
    a = ap.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
