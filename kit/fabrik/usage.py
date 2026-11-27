#!/usr/bin/env python3
# Kit-Datei: Formatierer und Linter des Projekts lassen sie aus.
# fmt: off
# ruff: noqa
"""Protokolliert Kontingent und Nutzung als JSONL."""

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

if __name__ == "__main__":
    sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabrik_lib as lib  # noqa: E402


def repository_name(root):
    code, out, _ = lib.run(["git", "config", "--get", "remote.origin.url"], cwd=root)
    remote = out.strip() if code == 0 else ""
    if "://" in remote:
        path = urlsplit(remote).path
    else:
        path = remote.split(":", 1)[1] if ":" in remote else ""
    if path.strip("/"):
        return path.strip("/").removesuffix(".git")
    code, out, _ = lib.run(["git", "rev-parse", "--git-common-dir"], cwd=root)
    return (root / out.strip()).resolve().parent.name if code == 0 else root.name


def log(root, event, manufacturer, role, quota, model=None, stage=None, issue=None, pr=None,
        decision=None, reason=None):
    source = quota["quelle_stand"]
    reasons = [reason] if reason else []
    if not (quota["fenster_5h"] or quota["woche"]):
        reasons.append("unbekannt")
    if source is not None and time.time() - source > 1800:
        reasons.append("veraltet")
    entry = {
        "zeit": datetime.now().astimezone().isoformat(),
        "ereignis": event, "hersteller": manufacturer, "modell": model, "stufe": stage,
        "rolle": role, "repo": root.name, "issue": issue, "pr": pr,
        "quelle_stand": source, "entscheidung": decision, "grund": ", ".join(reasons) or None,
    }
    for key in ("fenster_5h", "woche"):
        window = quota[key]
        entry[key] = {field: window[field] for field in ("prozent", "reset")} if window else None
    try:
        entry["repo"] = repository_name(root)
        path = Path(os.environ.get("FABRIK_USAGE_LOG") or Path.home() / ".fabrik" / "usage.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Warnung: Nutzungsprotokoll konnte nicht geschrieben werden: {exc}", file=sys.stderr)


def partner_blocked(root, partner, role, quota, stage, issue=None, pr=None, *, hit_limit=False, model=None):
    if not hit_limit and not lib.quota_exhausted(quota):
        return None
    urgent = lib.issue_urgent(root, issue)
    log(root, "bremse", partner["name"], role, quota,
        model or lib.partner_setting(partner, "modelle", stage), stage, issue, pr,
        "claude" if urgent else "warten",
        "Nutzungslimit erreicht" if hit_limit else "5-Stunden-Fenster bei mindestens 90 %")
    print(lib.reset_message(quota))
    if urgent:
        print("Auftrag ist Dringend. Claude darf übernehmen.")
    return 3 if urgent else lib.PARTNER_LIMIT


def latest_codex_quota(sessions):
    files = []
    for path in sessions.glob("*/*/*/*.jsonl"):
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            continue

    latest = None
    for mtime, path in sorted(files, key=lambda entry: entry[0], reverse=True):
        if latest is not None and mtime < latest[0]:
            break
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        limits = event.get("rate_limits")
                        payload = event.get("payload")
                        if limits is None and isinstance(payload, dict):
                            limits = payload.get("rate_limits")
                        if not isinstance(limits, dict) or not any(
                            isinstance(window, dict)
                            and type(window.get("used_percent")) in (int, float)
                            and 0 <= window["used_percent"] <= 100
                            and type(window.get("window_minutes")) in (int, float)
                            and 0 < window["window_minutes"] <= 253402000000
                            for window in limits.values()
                        ):
                            continue
                        stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
                    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
                        continue
                    if latest is None or stamp > latest[0]:
                        latest = stamp, limits
        except OSError:
            continue
    return latest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot")
    quota_parser = commands.add_parser("codex-kontingent")
    quota_parser.add_argument("--sessions", type=Path, default=Path("~/.codex/sessions"))
    args = parser.parse_args(argv)
    try:
        if args.command == "codex-kontingent":
            latest = latest_codex_quota(args.sessions.expanduser())
            if latest is not None:
                stamp, limits = latest
                print('"rate_limits":' + json.dumps(limits, separators=(",", ":")))
                print(f"stand: {stamp}")
            return 0
        root = lib.repo_root()
        partner = lib.load_partner(include_disabled=True) or {}
        for key, name in (("kontingent", partner.get("name")), ("claude_kontingent", "claude")):
            if not name:
                continue
            quota = lib.read_quota(partner.get(key), root)
            if quota["fenster_5h"] or quota["woche"]:
                log(root, "schnappschuss", name, "budget", quota)
        return 0
    except (lib.FabrikError, OSError, RuntimeError, ValueError) as exc:
        print(f"Nutzungsprotokoll: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
