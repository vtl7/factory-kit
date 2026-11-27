# Kit-Datei: Formatierer und Linter des Projekts lassen sie aus.
# fmt: off
# ruff: noqa
"""Shared helpers for the Fabrik kit scripts: config, path patterns, git and gh calls.

Standard library only, Python 3.9+. Every script in this folder imports this module.
"""

import argparse
import json
import math
import os
import re
import shlex
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

CONFIG_PATH = ".fabrik/fabrik.yml"
STAGES = ("T0", "T1", "T2", "T3")
GATE_CHECK_NAME = "fabrik-gate"
VERDICTS = ("APPROVE", "CHANGES", "ESCALATE")
VERDICT_MARK = "fabrik-urteil"
DEEP_MARK = "fabrik-tief"
LABEL_URGENT = "Dringend"
QUOTA_TIMEOUT_S = 30
PARTNER_LIMIT = 14
LIMIT_MESSAGE = re.compile(
    r"\b(?:usage|rate)[ -]limit\s+(?:reached|exceeded)\b"
    r"|\b(?:hit|reached|exceeded) (?:your |the )?(?:usage|rate)[ -]limit\b"
    r"|(?:^|:)\s*usage[ -]limit\s*(?:$|[.!:])", re.I)
RESET_MARK = "fabrik-reset"
LABEL_CLAUDE_ONLY = "Prüfung: Claude allein"
LABEL_WAITING = "Wartet auf dich"
LABEL_STUCK = "Festgefahren"
LABEL_INCIDENT = "Vorfall"
MAX_ROUNDS = 3

DEFAULTS = {
    "check": None,
    "setup": None,
    "test_cmd": None,
    "test_fail_codes": [1, 2],
    "test_paths": ["tests/", "test/"],
    "protected_paths": [],
    "ship": None,
    "verify": None,
    "main_branch": "main",
    "timeout_s": 1800,
    "risk": {},
    "extra_categories": {},
    "repos": [],
    "commit_trailers": [],
}


class FabrikError(Exception):
    """A config or environment problem the caller should report in plain words."""


# --- Minimal YAML subset -------------------------------------------------------------
# Supports what fabrik.yml and partner.yml need: nested maps, block lists, inline lists,
# quoted and plain scalars, comments. Anything fancier is a FabrikError.

# Kommentare und Inline-Listen liest ein kleiner Zustandsautomat:
#   - Ein Wert beginnt nach "schlüssel:", nach "- " oder nach "[" und "," einer Inline-Liste.
#   - Nur am Beginn eines Werts öffnet ein Anführungszeichen ein Zitat. Mitten im Wert ist es
#     ein gewöhnliches Zeichen (owner's, a 'b).
#   - Ein Kommentar beginnt nur bei " #" (oder "#" am Zeilenanfang) außerhalb eines Zitats.

def _quote_end(text, i):
    """Index after the quote that opens at text[i]; doubled '' and \\x stay inside."""
    quote, j = text[i], i + 1
    while j < len(text):
        if quote == '"' and text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            if quote == "'" and text[j + 1:j + 2] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return len(text)


def _scan_value(text, start):
    """Walk a value from start. Returns (comment_index, item_boundaries) where comment_index is
    where a comment begins (or len(text)) and item_boundaries are the commas of an inline list."""
    # inner zählt Klammern innerhalb eines unzitierten Eintrags (Glob wie a[bc].md): Sie
    # gehören zum Wert, ihr Komma trennt nichts, ihr "]" schließt die Liste nicht.
    value_start, depth, inner, commas, i = True, 0, 0, [], start
    while i < len(text):
        ch = text[i]
        if ch == "#" and (i == 0 or text[i - 1].isspace()):
            return i, commas
        if ch.isspace():
            i += 1
            continue
        if value_start and ch in "\"'":
            i, value_start = _quote_end(text, i), False
            continue
        if value_start and ch == "[":
            depth += 1
            i += 1
            continue
        if depth and ch == "[":
            inner += 1
        elif depth and inner and ch == "]":
            inner -= 1
        elif depth and not inner and ch == ",":
            commas.append(i)
            value_start = True
            i += 1
            continue
        elif depth and ch == "]":
            depth -= 1
        value_start = False
        i += 1
    return len(text), commas


def _value_start(line):
    """Where the value of a block line begins: after "- " of a list item or after the key's colon."""
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    if stripped == "-" or stripped.startswith("- "):
        return indent + 1
    if stripped.startswith("#"):
        return indent
    colon = line.find(":", indent)
    return colon + 1 if colon != -1 else indent


def _strip_comment(line):
    return line[:_scan_value(line, _value_start(line))[0]].rstrip()


def _split_inline(inner):
    _, commas = _scan_value("[" + inner + "]", 0)
    bounds = [-1] + [c - 1 for c in commas] + [len(inner)]
    return [inner[a + 1:b] for a, b in zip(bounds, bounds[1:])]


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(part) for part in _split_inline(inner)] if inner else []
    if text in ("", "~", "null"):
        return None
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text == "{}":
        return {}
    if text.startswith("{"):
        raise FabrikError(f"YAML: Inline-Maps werden nicht unterstützt: {text}")
    return text


def load_yaml(text):
    lines = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if line.strip():
            if "\t" in line[: len(line) - len(line.lstrip())]:
                raise FabrikError("YAML: Tabs zur Einrückung sind nicht erlaubt")
            lines.append((len(line) - len(line.lstrip()), line.strip()))
    value, pos = _parse_block(lines, 0, 0)
    if pos != len(lines):
        raise FabrikError(f"YAML: unerwartete Einrückung bei '{lines[pos][1]}'")
    return value if value is not None else {}


def _parse_block(lines, pos, indent):
    if pos >= len(lines) or lines[pos][0] < indent:
        return None, pos
    indent = lines[pos][0]
    if lines[pos][1].startswith("- ") or lines[pos][1] == "-":
        result = []
        while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("-"):
            result.append(_scalar(lines[pos][1][1:]))
            pos += 1
        return result, pos
    result = {}
    while pos < len(lines) and lines[pos][0] == indent:
        key, sep, rest = lines[pos][1].partition(":")
        if not sep:
            raise FabrikError(f"YAML: Zeile ohne Doppelpunkt: '{lines[pos][1]}'")
        pos += 1
        if rest.strip():
            result[key.strip()] = _scalar(rest)
        else:
            child, pos = _parse_block(lines, pos, indent + 1)
            result[key.strip()] = child
    return result, pos


# --- Config --------------------------------------------------------------------------

def partner_file():
    return Path(os.environ.get("FABRIK_PARTNER_FILE")
                or Path.home() / ".config" / "fabrik" / "partner.yml")


def load_partner(include_disabled=False):
    """Liest den Partnereintrag des Rechners."""
    path = partner_file()
    if not path.is_file():
        return None
    data = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise FabrikError(f"{path}: Partnereintrag muss eine Zuordnung sein.")
    name = data.get("name") or ""
    if not isinstance(name, str) or (name and len(name.splitlines()) != 1):
        raise FabrikError(f"{path}: Ungültiger Partnername.")
    data["name"] = name.strip()
    if data["name"].casefold() in ("", "keiner"):
        if not include_disabled:
            return None
        data["name"] = None
    return data


def partner_setting(partner, key, stage):
    values = partner.get(key)
    if isinstance(values, dict):
        value = values.get(stage) or values.get("standard")
        if isinstance(value, str) and value.strip():
            return value
    return None


def partner_command(partner, role, stage):
    command = partner.get(role)
    if not isinstance(command, str) or not command.strip():
        raise FabrikError(f"Für Partner {partner['name']} fehlt ein gültiger Befehl '{role}'.")
    keys = {"modell": "modelle", "aufwand": "aufwand"}

    def replace(match):
        key = keys[match[1]]
        value = partner_setting(partner, key, stage)
        if value is None:
            raise FabrikError(f"Für Partner {partner['name']} fehlt '{key}' für {stage} und standard.")
        return shlex.quote(value)

    return re.sub(r"\{(modell|aufwand)\}", replace, command)


def claude_model(stage):
    return "opus" if stage == "tief" else "sonnet"


def read_quota(command, root):
    if not isinstance(command, str) or not command:
        return parse_quota("")
    try:
        with subprocess.Popen(["sh", "-c", command], cwd=root, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              start_new_session=True) as worker:
            try:
                output, _ = worker.communicate(timeout=QUOTA_TIMEOUT_S)
                if worker.returncode == 0:
                    return parse_quota(output.decode("utf-8"))
            except subprocess.TimeoutExpired:
                # Die Frist gilt auch für Kindprozesse einer Pipeline.
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except UnicodeDecodeError:
                pass
    except (OSError, ValueError):
        pass
    return parse_quota("")


def parse_quota(output, now=None):
    now = time.time() if now is None else now
    quota = {"fenster_5h": None, "woche": None, "quelle_stand": None}
    lines = output.strip().splitlines()
    if not lines:
        return quota
    decoder = json.JSONDecoder()
    try:
        line = lines[0]
        if re.match(r'^"rate_limits"\s*:', line):
            limits, _ = decoder.raw_decode(line.split(":", 1)[1].lstrip())
        else:
            content = "\n".join(line for line in lines if not line.strip().startswith("stand:"))
            data = json.loads(content)
            limits = data.get("rate_limits", data) if isinstance(data, dict) else None
    except ValueError:
        return quota
    if not isinstance(limits, dict):
        return quota
    for line in lines[1:]:
        match = re.fullmatch(r"stand:\s*(\d+(?:\.\d+)?)", line.strip())
        if match:
            stamp = float(match[1])
            if math.isfinite(stamp):
                quota["quelle_stand"] = stamp

    def number(value):
        return type(value) in (int, float) and abs(value) <= 253402000000

    for name, window in limits.items():
        if not isinstance(window, dict):
            continue
        percent, reset = window.get("used_percent"), window.get("resets_at")
        if not number(percent) or not 0 <= percent <= 100:
            continue
        # Alte Quellen liefern für primary/secondary teils keine Fensterdauer.
        duration = window.get("window_minutes", {"primary": 300, "secondary": 10080}.get(name))
        if not number(duration) or duration <= 0:
            continue
        reset = reset if number(reset) and reset > 0 else None
        elapsed = (max(0, min(100, (1 - (reset - now) / (duration * 60)) * 100))
                   if reset is not None else None)
        key = "fenster_5h" if duration <= 300 else "woche"
        entry = {"prozent": 0 if reset is not None and reset <= now else percent,
                 "reset": reset, "verstrichen": elapsed}
        if quota[key] is None or entry["prozent"] > quota[key]["prozent"]:
            quota[key] = entry
    return quota


def quota_exhausted(quota):
    window = quota["fenster_5h"]
    return window is not None and window["prozent"] >= 90


def claude_budget(quota):
    week = quota["woche"]
    if week is None:
        return "unbekannt", "Wochenverbrauch unbekannt"
    percent, elapsed = week["prozent"], week["verstrichen"]
    if percent >= 70:
        return "gebremst", "Wochenreserve für den Product Owner"
    if percent >= 25:
        if elapsed is None:
            return "unbekannt", "Verstrichener Wochenanteil unbekannt"
        if percent > elapsed + 10 + 1e-9:
            return "gebremst", "Wochenverbrauch mehr als 10 Punkte voraus"
    return "frei", "Wochenkontingent frei"


def issue_urgent(root, issue):
    if not issue:
        return False
    data = gh_json(["issue", "view", str(issue), "--json", "labels"], cwd=root) or {}
    return any(label.get("name") == LABEL_URGENT for label in data.get("labels") or [])


def reset_message(quota):
    reset = (quota["fenster_5h"] or {}).get("reset")
    if reset is None:
        return "Partner am Limit. Reset-Zeit unbekannt. Warten auf den Reset."
    local = datetime.fromtimestamp(reset).astimezone()
    return f"Partner am Limit bis {local:%H:%M}. Warten auf den Reset.\nReset: {local.isoformat()}"


def parse_slice(value):
    if not re.fullmatch(r"[0-9]+/[0-9]+", value):
        raise argparse.ArgumentTypeError("Scheibe als K/N angeben, z. B. 1/2.")
    try:
        current, total = map(int, value.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError("Die Scheibenangabe ist zu lang.")
    if not 1 <= current <= total:
        raise argparse.ArgumentTypeError("Für die Scheibe K/N muss 1 ≤ K ≤ N gelten.")
    return f"{current}/{total}"


def load_config(root, text=None):
    """Read .fabrik/fabrik.yml (or the given text) and fill in defaults."""
    if text is None:
        path = Path(root) / CONFIG_PATH
        if not path.is_file():
            raise FabrikError(f"{CONFIG_PATH} fehlt. Erst 'fabrik init' ausführen.")
        text = path.read_text(encoding="utf-8")
    data = load_yaml(text)
    if not isinstance(data, dict):
        raise FabrikError(f"{CONFIG_PATH}: oberste Ebene muss eine Zuordnung sein")
    config = {key: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
              for key, v in DEFAULTS.items()}
    for key, value in data.items():
        if value is not None:
            config[key] = value
    for key in ("test_paths", "protected_paths", "repos", "test_fail_codes", "commit_trailers"):
        if not isinstance(config[key], list):
            raise FabrikError(f"{CONFIG_PATH}: '{key}' muss eine Liste sein")
    for trailer in config["commit_trailers"]:
        valid = (isinstance(trailer, str) and trailer.isprintable()
                 and re.fullmatch(r"([A-Za-z0-9-]+): *\S.*", trailer))
        if not valid or valid.group(1).casefold() == "gebaut-von":
            raise FabrikError(f"{CONFIG_PATH}: Jeder Eintrag in 'commit_trailers' muss eine Zeile der Form "
                              f"'Schlüssel: Wert' sein, Gebaut-von setzt das Kit selbst, nicht {trailer!r}")
    for key in ("risk", "extra_categories"):
        if not isinstance(config[key], dict):
            raise FabrikError(f"{CONFIG_PATH}: '{key}' muss eine Zuordnung sein")
    return config


def base_config(root, base):
    if git(["ls-tree", "--name-only", base, "--", CONFIG_PATH], cwd=root).strip():
        text = git(["show", f"{base}:{CONFIG_PATH}"], cwd=root)
        return load_config(root, text), "Basis"
    return load_config(root), "Arbeitsstand (Basis hat noch keine Konfiguration)"


# --- Path patterns (gitignore-like) --------------------------------------------------

def _pattern_regex(pattern):
    pattern = pattern.strip()
    is_dir = pattern.endswith("/")
    body = pattern.strip("/")
    anchored = "/" in body
    out, i = "", 0
    while i < len(body):
        if body.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif body.startswith("**", i):
            out, i = out + ".*", i + 2
        elif body[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif body[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(body[i]), i + 1
    if is_dir:
        out += "(?:/.*)?"
    if not anchored:
        out = "(?:.*/)?" + out
    return re.compile("^" + out + "$")


def path_matches(path, patterns):
    path = path[2:] if path.startswith("./") else path
    return any(_pattern_regex(p).match(path) for p in patterns if p)


# --- Processes -----------------------------------------------------------------------

def _run_impl(args, cwd=None, input_text=None, timeout=None):
    done = subprocess.run(args, cwd=cwd, input=input_text, capture_output=True, text=True,
                          timeout=timeout)
    return done.returncode, done.stdout, done.stderr


def run(args, cwd=None, input_text=None, timeout=None):
    """Run a command and return (returncode, stdout, stderr). Tests replace _run_impl."""
    return _run_impl(args, cwd=cwd, input_text=input_text, timeout=timeout)


def gh(args, cwd=None, input_text=None):
    code, out, err = run(["gh", *args], cwd=cwd, input_text=input_text)
    if code != 0:
        raise FabrikError(f"gh {' '.join(args[:3])} fehlgeschlagen: {err.strip() or out.strip()}")
    return out


def gh_json(args, cwd=None):
    out = gh(args, cwd=cwd)
    return json.loads(out) if out.strip() else None


def git(args, cwd=None, input_text=None):
    code, out, err = run(["git", *args], cwd=cwd, input_text=input_text)
    if code != 0:
        raise FabrikError(f"git {' '.join(args[:2])} fehlgeschlagen: {err.strip()}")
    return out


def repo_root(start=None):
    args = ["rev-parse", "--show-toplevel"]
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if start or not project_dir:
        return Path(git(args, cwd=start).strip())
    # Git verwendet denselben Exitcode für fehlende Repos und andere Fehler.
    code, out, err = run(["env", "LC_ALL=C", "git", *args])
    if code == 0:
        return Path(out.strip())
    if err.startswith("fatal: not a git repository ("):
        return Path(git(args, cwd=project_dir).strip())
    raise FabrikError(f"git rev-parse --show-toplevel fehlgeschlagen: {err.strip()}")


# --- Pull requests, verdicts, gate state --------------------------------------------

PR_FIELDS = "number,headRefOid,headRefName,baseRefName,labels,files,comments,statusCheckRollup,body,state"


def pr_info(ref=None, repo=None, cwd=None, fields=PR_FIELDS):
    """PR state. Ask only for the fields you need: in CI the token cannot read check runs."""
    args = ["pr", "view"] + ([str(ref)] if ref else []) + ["--json", fields]
    if repo:
        args += ["-R", repo]
    data = gh_json(args, cwd=cwd) or {}
    return {
        "number": data.get("number"),
        "head_sha": data.get("headRefOid", ""),
        "head_ref": data.get("headRefName", ""),
        "base": data.get("baseRefName", "main"),
        "base_sha": data.get("baseRefOid"),
        "labels": [label.get("name", "") for label in data.get("labels") or []],
        "files": [f.get("path", "") for f in data.get("files") or []],
        "comments": [c.get("body", "") for c in data.get("comments") or []],
        "checks": data.get("statusCheckRollup") or [],
        "body": data.get("body") or "",
        "state": data.get("state", ""),
    }


def issue_comments(number, cwd=None):
    data = gh_json(["issue", "view", str(number), "--json", "comments"], cwd=cwd) or {}
    return [c.get("body", "") for c in data.get("comments") or []]


def _markers(bodies, mark):
    found = []
    for body in bodies:
        match = re.match(r"\s*<!--\s*" + mark + r"\s+(\{.*?\})\s*-->", body, re.S)
        if match:
            try:
                found.append(json.loads(match.group(1)))
            except ValueError:
                continue
    return found


def format_marker(mark, data):
    return f"<!-- {mark} {json.dumps(data, ensure_ascii=False, sort_keys=True)} -->"


def verdicts(bodies):
    return [v for v in _markers(bodies, VERDICT_MARK) if v.get("urteil") in VERDICTS]


def verdict_for(bodies, sha):
    """Latest verdict recorded for exactly this head commit, or None."""
    matching = [v for v in verdicts(bodies) if v.get("sha") and v.get("sha") == sha]
    return matching[-1] if matching else None


def final_verdict_for(bodies, sha):
    """Return this commit's latest verdict only if it covers the whole PR."""
    verdict = verdict_for(bodies, sha)
    if verdict is None or verdict.get("slice") is None:
        return verdict
    scope = verdict["slice"]
    if isinstance(scope, str) and re.fullmatch(r"0*([1-9][0-9]*)/0*\1", scope):
        return verdict
    return None


def rounds_used(bodies):
    """Non-approving verdicts since the last reset marker, in comment order."""
    count = 0
    for body in bodies:
        if _markers([body], RESET_MARK):
            count = 0
        count += sum(1 for v in verdicts([body]) if v.get("urteil") != "APPROVE")
    return count


def gate_state(checks):
    """State of the fabrik-gate check: success, failure, pending or missing."""
    state = "missing"
    for check in checks:
        name = check.get("name") or check.get("context") or ""
        if name != GATE_CHECK_NAME:
            continue
        if check.get("__typename") == "StatusContext" or "state" in check and "status" not in check:
            raw = (check.get("state") or "").upper()
            state = {"SUCCESS": "success", "PENDING": "pending", "EXPECTED": "pending"}.get(raw, "failure")
        elif (check.get("status") or "").upper() != "COMPLETED":
            state = "pending"
        else:
            state = "success" if (check.get("conclusion") or "").upper() == "SUCCESS" else "failure"
    return state


def stage_max(*stages):
    valid = [s for s in stages if s in STAGES]
    return max(valid, key=STAGES.index) if valid else None


def label_stage(labels):
    return stage_max(*[label for label in labels if label in STAGES])


def referenced_issues(body):
    """Issue numbers named with 'Refs #N' (also Closes/Fixes, which the skills avoid)."""
    pattern = r"(?:refs|closes|fixes|resolves|teil von)\s*:?\s*#(\d+)"
    return sorted({int(n) for n in re.findall(pattern, body or "", re.I)})


def behavior_change(body):
    return bool(re.search(r"verhaltens(?:ä|ae)nderung\s*:\s*\**\s*ja", body or "", re.I))
