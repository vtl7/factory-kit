#!/usr/bin/env python3
# Kit-Datei: Formatierer und Linter des Projekts lassen sie aus.
# fmt: off
# ruff: noqa
"""Gate: check command, protected paths, red-green probe. Green or red, nothing in between.

The check command and the protected paths come from .fabrik/fabrik.yml as it is on the base
branch, so a change cannot weaken its own gate. Only on the very first PR (no config on base yet)
the working tree config is used.

Usage:
  gate.py [--base REF] [--slice-base REF] [--behavior-change] [--setup] [--json]

Exit code 0 = green, 1 = red, 2 = cannot run (config or git problem).
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabrik_lib as lib  # noqa: E402
import risk  # noqa: E402

TAIL_LINES = 60
SLICE_WARN_LINES = 150
SHARED_DIRS = ("node_modules", ".venv", "venv")


def shell(command, cwd, timeout):
    """Run a project command through the shell. Returns (code or None on timeout, output)."""
    try:
        done = subprocess.run(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"Zeitlimit {timeout} s überschritten"
    return done.returncode, done.stdout + done.stderr


def changes(root, base):
    """[(status, path)] between merge base and HEAD, renames split into delete + add."""
    out = lib.git(["diff", "--name-status", "--no-renames", f"{base}...HEAD"], cwd=root)
    result = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            result.append((parts[0][:1], parts[-1]))
    return result


def changed_lines(root, base):
    out = lib.git(["diff", "--numstat", f"{base}...HEAD"], cwd=root)
    total = 0
    for line in out.splitlines():
        added, removed = line.split("\t")[:2]
        if added.isdigit() and removed.isdigit():
            total += int(added) + int(removed)
    return total


def protected_violations(changed, config):
    patterns = config.get("protected_paths") or []
    return [path for status, path in changed if status != "A" and lib.path_matches(path, patterns)]


def red_green(root, base, config, changed, timeout):
    """New tests must pass on HEAD and fail on the base. Returns (ok, message)."""
    tests = [p for s, p in changed if s in "AM" and lib.path_matches(p, config["test_paths"])]
    if not tests:
        return False, "Rot-Grün-Probe rot: Verhaltensänderung ohne neue oder geänderte Tests"
    if not config.get("test_cmd"):
        return True, "Rot-Grün-Probe nicht geprüft: test_cmd fehlt in .fabrik/fabrik.yml"
    files = " ".join(shlex.quote(p) for p in tests)
    command = config["test_cmd"].replace("{files}", files)
    code, _ = shell(command, root, timeout)
    if code != 0:
        return False, f"Rot-Grün-Probe rot: neue Tests auf neuem Stand nicht grün (Exitcode {code})"
    tmp = Path(tempfile.mkdtemp(prefix="fabrik-gate-"))
    old = tmp / "base"
    try:
        lib.git(["worktree", "add", "--detach", "--quiet", str(old), base], cwd=root)
        for name in SHARED_DIRS:
            if (root / name).is_dir() and not (old / name).exists():
                os.symlink(root / name, old / name)
        for path in tests:
            target = old / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / path, target)
        code, _ = shell(command, old, timeout)
    finally:
        lib.run(["git", "worktree", "remove", "--force", str(old)], cwd=root)
        shutil.rmtree(tmp, ignore_errors=True)
    if code in config["test_fail_codes"]:
        return True, "Rot-Grün-Probe grün: neuer Stand grün, alter Stand rot"
    detail = "Tests auf altem Stand grün" if code == 0 else f"Testlauf nicht auswertbar (Exitcode {code})"
    return False, f"Rot-Grün-Probe rot: {detail}"


def run_gate(root, base, slice_base=None, behavior_change=False, setup=False):
    root = Path(root)
    config, source = lib.base_config(root, base)
    timeout = int(config.get("timeout_s") or 1800)
    lines = []
    green = True
    if setup and config.get("setup"):
        code, out = shell(config["setup"], root, timeout)
        if code != 0:
            tail = "\n".join(out.splitlines()[-TAIL_LINES:])
            failure = f"GATE: ROT\nEinrichtung `{config['setup']}` fehlgeschlagen (Exitcode {code})"
            return {"green": False, "report": f"{failure}\n```\n{tail}\n```\n", "stage": None,
                    "review_report": f"{failure}\n```\n{tail[-2500:]}\n```\n"}
    changed = changes(root, base)
    stage = risk.classify([p for _, p in changed], config)
    violations = protected_violations(changed, config)
    if violations:
        green = False
        lines.append("Geschützte Pfade geändert oder gelöscht:")
        lines += [f"- {p}" for p in violations]
    if behavior_change:
        ok, message = red_green(root, slice_base or base, config, changes(root, slice_base or base),
                                timeout)
        green = green and ok
        lines.append(message)
    review_lines = lines.copy()
    count = changed_lines(root, slice_base or base)
    if count > SLICE_WARN_LINES:
        lines.append(f"Warnung: Scheibe mit {count} geänderten Zeilen (über {SLICE_WARN_LINES}).")
    if config.get("check"):
        code, out = shell(config["check"], root, timeout)
        green = green and code == 0
        tail = "\n".join(out.splitlines()[-TAIL_LINES:])
        summary = (f"Prüfbefehl `{config['check']}`: {'grün' if code == 0 else 'rot'} "
                     f"(Exitcode {code}, Konfiguration aus {source})")
        lines.append(summary)
        lines += [f"Letzte {TAIL_LINES} Zeilen:", "```", tail, "```"]
        if code != 0:
            review_lines.extend([summary, "```", tail[-2500:], "```"])
    else:
        green = False
        lines.append("Prüfbefehl fehlt: 'check' in .fabrik/fabrik.yml eintragen.")
        review_lines.append(lines[-1])
    head = [f"GATE: {'GRÜN' if green else 'ROT'}",
            f"Stufe: {stage['stage']} ({', '.join(stage['categories']) or 'keine Änderung'})"]
    return {"green": green, "report": "\n".join(head + lines) + "\n", "stage": stage["stage"],
            "review_report": "\n".join(head + review_lines) + "\n"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gate: Prüfbefehl, geschützte Pfade, Rot-Grün-Probe")
    parser.add_argument("--base", help="Vergleichsstand, Standard origin/<main_branch>")
    parser.add_argument("--slice-base", help="Beginn der aktuellen Scheibe für Probe und Größe")
    parser.add_argument("--behavior-change", action="store_true",
                        help="Auftrag ändert Verhalten: Rot-Grün-Probe ist Pflicht")
    parser.add_argument("--setup", action="store_true", help="vorher 'setup' ausführen (CI)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = lib.repo_root()
        base = args.base or f"origin/{lib.load_config(root)['main_branch']}"
        result = run_gate(root, base, args.slice_base, args.behavior_change, args.setup)
    except lib.FabrikError as exc:
        print(f"GATE: NICHT AUSFÜHRBAR\n{exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False) if args.json else result["report"], end="")
    return 0 if result["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
