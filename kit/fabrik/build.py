#!/usr/bin/env python3
# Kit-Datei: Formatierer und Linter des Projekts lassen sie aus.
# fmt: off
# ruff: noqa
"""Startet einen Partnerbau und holt sein Ergebnis auch in einem späteren Aufruf ab."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True  # Der Start muss einen sauberen Arbeitsbaum vorfinden.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabrik_lib as lib  # noqa: E402
import usage  # noqa: E402


def budget(root):
    partner = lib.load_partner(include_disabled=True) or {}
    decisions = {}
    for key, name in (("kontingent", partner.get("name")), ("claude_kontingent", "claude")):
        if name is None:
            continue
        quota = lib.read_quota(partner.get(key), root)
        for window_key, title in (("fenster_5h", "5-Stunden-Fenster"), ("woche", "Woche")):
            window = quota[window_key]
            stand = "unbekannt"
            if window:
                reset = datetime.fromtimestamp(window["reset"]).astimezone().isoformat() if window["reset"] else "unbekannt"
                elapsed = window["verstrichen"]
                elapsed_text = f"{elapsed:.1f} %" if elapsed is not None else "unbekannt"
                stand = f"{window['prozent']:g} % verbraucht, Reset {reset}, verstrichen {elapsed_text}"
            print(f"{name}, {title}: {stand}")
        if key == "claude_kontingent":
            decision, reason = lib.claude_budget(quota)
        elif lib.quota_exhausted(quota):
            decision, reason = "gebremst", lib.reset_message(quota)
        else:
            decision = "frei"
            reason = "5-Stunden-Fenster unter 90 %" if quota["fenster_5h"] else "5-Stunden-Fenster unbekannt"
        decisions[key] = decision
        print(f"{name}: {decision}. {reason}")
        event = "bremse" if decision == "gebremst" else "schnappschuss"
        usage.log(root, event, name, "budget", quota, decision=decision, reason=reason)
    if not partner.get("name"):
        print("Kein Partner: Claude baut selbst.")
        return 3
    return lib.PARTNER_LIMIT if decisions["kontingent"] == "gebremst" else 0


def build_prompt(root, args):
    config = lib.load_config(root)
    issue = lib.gh_json(["issue", "view", str(args.issue), "--json", "title,body"], cwd=root)
    if not isinstance(issue, dict) or not isinstance(issue.get("title"), str):
        raise lib.FabrikError(f"Auftragsblatt für Issue #{args.issue} fehlt.")
    current, total = args.slice.split("/")
    return f"""## Auftragsblatt (Issue #{args.issue})
{issue['title']}
{issue.get('body') or ''}

## Scheibe {current} von {total}
{args.task}

Baue nur diese Scheibe. Bei einer Verhaltensänderung schreibe zuerst einen fehlschlagenden Test.
Es ist kein Netz verfügbar. Installiere nichts. Nutze vorhandene lokale Umgebungen, etwa .venv/bin/.
Überlasse git commit und Push der Bau-Sitzung. Erstelle und wechsle keine Branches.
Ändere keine geschützten Pfade: {', '.join(config['protected_paths']) or '(keine)'}
Prüfbefehl (check): {config['check'] or '(keiner)'}
Testbefehl (test_cmd): {config['test_cmd'] or '(keiner)'}
Fasse am Ende die geänderten Dateien und das Testergebnis kurz zusammen.
"""


@contextmanager
def state_lock(directory):
    with (directory / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def changes(root):
    status = lib.git(["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"], cwd=root)
    lines = []
    for line in status.split("\n"):
        if line.startswith("?? "):
            path = Path(line[3:].strip('"'))
            if (path.parts[:2] == (".claude", "fabrik")
                    and ("__pycache__" in path.parts[2:-1] or path.name.endswith(".pyc"))):
                continue
        lines.append(line)
    return "\n".join(lines)


def require_closed_build(state_path):
    if state_path.exists():
        raise lib.FabrikError("Ein Bau ist offen. Ergebnis mit build.py wait abholen.")


def escalation_lock_dir(root):
    common = lib.git(["rev-parse", "--git-common-dir"], cwd=root).strip()
    directory = (root / common / "fabrik").resolve()
    directory.mkdir(exist_ok=True)
    return directory


def escalation_available(root, issue):
    return not lib._markers(lib.issue_comments(issue, cwd=root), lib.DEEP_MARK)


def claim_escalation(root, issue, slice_spec):
    marker = lib.format_marker(lib.DEEP_MARK, {"issue": issue, "slice": slice_spec})
    lib.gh(["issue", "comment", str(issue), "--body", marker], cwd=root)


def claude_escalation(root, issue, slice_spec):
    with state_lock(escalation_lock_dir(root)):
        if escalation_available(root, issue):
            claim_escalation(root, issue, slice_spec)
            print(lib.claude_model("tief"))
        else:
            print(f"bereits verbraucht, {lib.claude_model('standard')}")
    return 0


def claude_build():
    print(f"Modell für Claudes Bau: {lib.claude_model('standard')}.")
    return 3


def start(root, state_path, args):
    require_closed_build(state_path)
    partner = lib.load_partner()
    quota = lib.read_quota((partner or {}).get("kontingent"), root)
    with ExitStack() as locks:
        locks.enter_context(state_lock(state_path.parent))
        require_closed_build(state_path)
        if args.eskalation:
            lock_dir = escalation_lock_dir(root)
            if lock_dir != state_path.parent.resolve():
                locks.enter_context(state_lock(lock_dir))
        return start_locked(root, state_path, args, partner, quota)


def start_locked(root, state_path, args, partner, quota):
    if not partner:
        print("Kein Partner: Claude baut selbst.")
        return claude_build()
    if not partner.get("bauen"):
        raise lib.FabrikError(f"Für Partner {partner['name']} fehlt der Befehl 'bauen'.")
    stage = "standard"
    if args.eskalation:
        if escalation_available(root, args.issue):
            stage = "tief"
        else:
            print("Eskalation für diesen Auftrag bereits verbraucht. Bau in Stufe standard.")
    blocked = usage.partner_blocked(root, partner, "bauen", quota, stage, issue=args.issue)
    if blocked == 3:
        return claude_build()
    if blocked:
        return blocked
    return launch(root, state_path, args, partner, quota, stage)


def launch(root, state_path, args, partner, quota, stage):
    command = lib.partner_command(partner, "bauen", stage)
    model = lib.partner_setting(partner, "modelle", stage)
    if changes(root):
        raise lib.FabrikError("Das Arbeitsverzeichnis muss vor dem Bau sauber sein.")
    head = lib.git(["rev-parse", "HEAD"], cwd=root).strip()
    prompt = build_prompt(root, args)
    # Die Shell wartet auf die Freigabe über stdin. Bei einem Startabbruch läuft kein Partner los.
    wrapper = ('read -r ready || exit 1\n'
               'sh -c "$1" < "/dev/fd/$2" >> "$4" 2>&1\n'
               'printf "%s\\n" "$?" > "$3.tmp" && mv "$3.tmp" "$3"')
    with tempfile.NamedTemporaryFile(dir=state_path.parent, prefix="build-", suffix=".log",
                                     delete=False) as log:
        log_path = Path(log.name)
        result_path = log_path.with_suffix(".exit")
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as source:
            source.write(prompt)
            source.seek(0)
            worker = subprocess.Popen(["sh", "-c", wrapper, "build", command,
                                       str(source.fileno()), str(result_path), str(log_path)], stdin=subprocess.PIPE,
                                      pass_fds=(source.fileno(),), stdout=log, stderr=subprocess.STDOUT,
                                      cwd=root, start_new_session=True)
            with worker.stdin:
                state = {"partner": partner["name"], "issue": args.issue, "slice": args.slice,
                         "head": head, "pid": worker.pid, "log": str(log_path),
                         "stufe": stage, "modell": model}
                staged = state_path.with_suffix(".tmp")
                staged.write_text(json.dumps(state), encoding="utf-8")
                staged.replace(state_path)
                try:
                    if stage == "tief":
                        claim_escalation(root, args.issue, args.slice)
                    usage.log(root, "lauf_start", partner["name"], "bauen", quota, model, stage,
                              issue=args.issue, decision="starten")
                    worker.stdin.write(b"start\n")
                    worker.stdin.flush()
                except (lib.FabrikError, OSError):
                    state_path.unlink()
                    raise
    print(f"Modell: {model or 'unbekannt'}, Stufe: {stage}.")
    print(f"Bau gestartet: {partner['name']}, Issue #{args.issue}, Scheibe {args.slice}, PID {worker.pid}.")
    return 0


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Der Prozess existiert unter einem anderen Benutzer.
    return True


def running(state):
    # Die negative PID prüft die ganze Gruppe, auch nach einem Wrapper-Abbruch.
    return not Path(state["log"]).with_suffix(".exit").is_file() and process_exists(-state["pid"])


def shell_text(text):
    return '"' + re.sub(r'([\\"$`])', r'\\\1', text) + '"'


def commit_command(trailers, partner=None):
    """Commit command with all trailers in one last paragraph, so Git reads them as trailers."""
    lines = ([f"Gebaut-von: {partner}"] if partner else []) + list(trailers)
    command = 'git commit --cleanup=verbatim -m "<Betreff>"'
    return command + (f" -m {shell_text(chr(10).join(lines))}" if lines else "")


def commit_commands(root, partner):
    trailers = lib.load_config(root)["commit_trailers"]
    text = (f"Partnerstand mit Trailer committen:\n{commit_command(trailers, partner)}\n"
            f"Eigene Korrekturen danach ohne Gebaut-von committen:\n{commit_command(trailers)}")
    missing = [t for t in trailers if t in unrecognized(root, [f"Gebaut-von: {partner}", *trailers])
               or t in unrecognized(root, trailers)]
    if missing:
        text += ("\nHinweis: Mit der Git-Konfiguration dieses Repos (etwa core.commentChar, "
                 "core.commentString oder trailer.separators) erkennt Git diese Trailer nicht: "
                 f"{', '.join(missing)}. Im Commit stehen sie dank --cleanup=verbatim trotzdem.")
    return text


def unrecognized(root, lines):
    """Lines that git interpret-trailers does not report back with the repository's config."""
    message = "Betreff\n\n" + "\n".join(lines) + "\n"
    code, out, _ = lib.run(["git", "interpret-trailers", "--parse"], cwd=root, input_text=message)
    found = set()
    for line in out.splitlines() if code == 0 else []:
        match = re.fullmatch(r"([A-Za-z0-9-]+)\s*[^\sA-Za-z0-9-]\s*(.*)", line.strip())
        if match:
            found.add((match.group(1).casefold(), match.group(2).strip()))
    return [line for line in lines
            if (line.split(":", 1)[0].casefold(), line.split(":", 1)[1].strip()) not in found]


def result(root, state, log, exit_path, quota):
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 8192))
        tail = stream.read().decode("utf-8", errors="replace").rstrip()
    if tail:
        print(tail)
    if lib.git(["rev-parse", "HEAD"], cwd=root).strip() != state["head"]:
        raise lib.FabrikError(
            f"HEAD weicht vom Start-Commit {state['head']} ab. Der Bau bleibt offen. "
            f"Prüfe die Commits. Für einen Partner-Commit: git reset --soft {state['head']}, "
            f"dann build.py wait erneut am Start-HEAD aufrufen und den geprüften Stand so "
            f"committen:\n{commit_commands(root, state['partner'])}\n"
            "Ist die Urheberschaft bereits geklärt, den Bauzustand mit build.py clear löschen.")
    if not exit_path.is_file():
        print("Der Bau ist ohne Exitcode abgebrochen.")
        if changes(root):
            print(f"Verwertbare Teiländerungen prüfen. {commit_commands(root, state['partner'])}")
        return 2
    code = int(exit_path.read_text())
    if code:
        print(f"Partner mit Exitcode {code} beendet. Log: {log}")
        with log.open(encoding="utf-8", errors="replace") as stream:
            limit = any(lib.LIMIT_MESSAGE.search(line) for line in deque(stream, maxlen=20))
        limit = limit or lib.quota_exhausted(quota)
        if limit:
            print("Nutzungslimit erreicht.")
        if changes(root):
            print(f"Verwertbare Teiländerungen prüfen. {commit_commands(root, state['partner'])}")
        if limit:
            stage = state.get("stufe", "standard")
            blocked = usage.partner_blocked(root, {"name": state["partner"]}, "bauen", quota,
                                            stage, state["issue"], hit_limit=True, model=state.get("modell"))
            return claude_build() if blocked == 3 else blocked
        return 2
    status = changes(root)
    if not status:
        print("Der Partner hat keine Dateien geändert.")
        return 8
    print(status, end="")
    print(commit_commands(root, state["partner"]))
    return 0


def wait(root, state_path, state, seconds):
    log = Path(state["log"])
    deadline = time.monotonic() + seconds
    while running(state) and process_exists(state["pid"]):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("Bau läuft noch, erneut aufrufen: build.py wait")
            return 9
        time.sleep(min(0.25, remaining))
    if running(state):
        print("Partnerprozesse laufen noch. Erneut aufrufen: build.py wait")
        return 9
    try:
        partner = lib.load_partner() or {}
    except (lib.FabrikError, OSError, ValueError):
        partner = {}
    command = partner.get("kontingent") if partner.get("name") == state.get("partner") else None
    quota = lib.read_quota(command, root)
    with state_lock(state_path.parent):
        if not state_path.exists() or json.loads(state_path.read_text()) != state:
            raise lib.FabrikError("Das Ergebnis dieses Baus wurde bereits abgeholt.")
        exit_path = log.with_suffix(".exit")
        code = result(root, state, log, exit_path, quota)
        usage.log(root, "lauf_ende", state.get("partner"), "bauen", quota,
                  state.get("modell"), state.get("stufe", "standard"), issue=state.get("issue"),
                  decision=f"Exitcode {code}")
        state_path.unlink()
        exit_path.unlink(missing_ok=True)
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Eine Scheibe vom Partner bauen lassen")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--slice", type=lib.parse_slice, required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--eskalation", action="store_true")
    sub.add_parser("budget")
    p = sub.add_parser("eskalation", help="Einmaligen Tiefbau für Claude beanspruchen")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--slice", type=lib.parse_slice)
    p = sub.add_parser("wait")
    p.add_argument("--max-wait", type=int, default=540, metavar="SEKUNDEN")
    sub.add_parser("status")
    sub.add_parser("clear", help="Zustand eines beendeten Baus bewusst löschen")
    args = parser.parse_args(argv)
    if args.cmd == "wait" and args.max_wait < 0:
        parser.error("--max-wait muss mindestens 0 sein.")
    try:
        root = lib.repo_root()
        if args.cmd == "budget":
            return budget(root)
        if args.cmd == "eskalation":
            return claude_escalation(root, args.issue, args.slice)
        directory = Path(lib.git(["rev-parse", "--absolute-git-dir"], cwd=root).strip()) / "fabrik"
        directory.mkdir(exist_ok=True)
        state_path = directory / "build.json"
        if args.cmd == "start":
            return start(root, state_path, args)
        with state_lock(directory):
            if not state_path.is_file():
                print("Kein Bau offen.")
                return 0 if args.cmd == "status" else 2
            state = json.loads(state_path.read_text())
            if args.cmd == "clear":
                if running(state):
                    raise lib.FabrikError("Der Bau läuft noch. Ergebnis mit build.py wait abholen.")
                state_path.unlink()
                Path(state["log"]).with_suffix(".exit").unlink(missing_ok=True)
                print("Bauzustand gelöscht.")
                return 0
        if args.cmd == "status":
            phase = "läuft" if running(state) else "beendet"
            print(f"Modell: {state.get('modell') or 'unbekannt'}, Stufe: {state.get('stufe', 'standard')}.")
            print(f"Bau {phase}: {state['partner']}, Issue #{state['issue']}, Scheibe {state['slice']}, "
                  f"PID {state['pid']}, Log: {state['log']}")
            return 0
        return wait(root, state_path, state, args.max_wait)
    except (lib.FabrikError, OSError, ValueError) as exc:
        print(f"Bau: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
