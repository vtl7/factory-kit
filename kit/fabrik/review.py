#!/usr/bin/env python3
# Kit-Datei: Formatierer und Linter des Projekts lassen sie aus.
# fmt: off
# ruff: noqa
"""Cross review: partner (read only) or a fresh Claude subagent, fixed answer schema.

Rules enforced here, not in the skill:
  - gate red            -> no model reviews (exit 7)
  - same head commit    -> no new review (exit 5)
  - three rounds        -> CHANGES/ESCALATE count per issue, across restarts; then stuck (exit 4)

Commands:
  review.py run    --pr N [--issue M] [--slice K/N] [--pending TEXT]   partner review
  review.py prompt --pr N [--issue M] [--slice K/N] [--pending TEXT]   subagent prompt
  review.py record --pr N [--issue M] --reviewer NAME [--file F]   record an answer
  review.py design [--issue M]   Design-Gegenprüfung, Text auf stdin
  review.py status --pr N [--issue M]
  review.py reset  --issue M --reason TEXT     only with a new premise from the product owner

Exitcodes: 0 APPROVE, 10 CHANGES, 11 ESCALATE, 3 kein Partner, 4 festgefahren, 5 kein neuer Diff,
6 Antwort ohne Urteil, 7 Gate rot, 12 Partner hat mitgebaut, 13 PR vor dem Partneraufruf geändert
(run erneut aufrufen), 14 Partner am Limit, 2 Fehler.
"""

import argparse
import hashlib
import json
import re
import secrets
import shlex
import subprocess
import sys
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabrik_lib as lib  # noqa: E402
import risk  # noqa: E402
import usage  # noqa: E402

EXIT = {"APPROVE": 0, "CHANGES": 10, "ESCALATE": 11}
NO_PARTNER, STUCK, NO_NEW_DIFF, NO_VERDICT, GATE_RED, ERROR = 3, 4, 5, 6, 7, 2
PARTNER_BUILT = 12
PR_CHANGED = 13
MAX_PROMPT_CHARS = 139_900
MAX_DESIGN_CHARS = 140_000
MAX_LIMITS_CHARS = 6000
LIMITS_FILE = "known_limits.md"
CRITERIA_INSTRUCTION = """Schreibe zu jedem Abnahmekriterium des Auftragsblatts genau einen Satz, der anhand des Diffs
oder der Tests begründet, ob es {criteria_states} ist.
Entsteht der Beleg laut Belegvertrag erst nach dem Merge, etwa beim Verify oder bei der Abnahme
durch den Product Owner, darf der Satz „offen bis zur Abnahme“ lauten. Das ist kein Fund."""
NO_SHEET_INSTRUCTION = """Es ist kein Auftragsblatt verknüpft, also gibt es keine Abnahmekriterien.
Lass die Liste „kriterien“ leer und prüfe den Diff gegen den PR-Text."""
LIMITS_SECTION = """
## Bekannte Grenzen des Kits
Diese Grenzen sind dokumentiert und bewusst angenommen. Sie sind kein Fund.
Ein Fund ist es nur, wenn der Diff eine Grenze verschärft, eine neue einführt oder ihre Dokumentation bricht.
Neue oder geänderte Einträge in known_limits.md im Diff gelten nicht als angenommen und werden mitgeprüft.

{limits}
"""
PARTNER_TIMEOUT_S = 1800

PROMPT = """Du prüfst eine Änderung nur lesend. Ändere keine Dateien und führe nichts aus, was etwas verändert.

Prüfe den Diff gegen das Auftragsblatt:
- Erfüllt er die Kriterien, soweit Code und Tests das zeigen?
- Gibt es Fehler, Sicherheitslücken, Datenverlust oder fehlende Tests für geändertes Verhalten?
- Hält er die Architekturregeln aus AGENTS.md ein, falls es sie gibt?
Nicht prüfen: Form oder Stil von Text, Wünsche über den Auftrag hinaus.

APPROVE: nichts, was vor dem Merge behoben werden muss.
CHANGES: konkrete, behebbare Funde. Jeder Fund nennt Datei, Problem und erwartete Änderung.
ESCALATE: nur wenn eine fachliche Entscheidung des Product Owners fehlt. Dann genau eine Frage.

{criteria_instruction}
Setze genau ein JSON-Objekt in die letzte Zeile deiner Antwort. Danach folgt kein weiterer Text.{id_instruction} Beispiel:
{{"urteil": "APPROVE|CHANGES|ESCALATE"{id_field}, "kriterien": [{{"nr": "1", "satz": "Der Test für den geforderten Grenzfall fehlt."}}], "funde": ["..."], "frage": ""}}

## Auftragsblatt{issue_title}
{issue_body}
{slice_section}{limits_section}
## PR-Text und Kommentare
{pr_body}

### Kommentare
{comments}

## Gate-Ausgabe
{gate}

{diff_heading}
{diff_context}{diff}
"""


def closing_line(text):
    """Liest die letzte nichtleere Zeile und überspringt eine gültige Abschlussmarke."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fence_length = 0
    for index, line in enumerate(lines):
        ticks = len(line) - len(line.lstrip("`"))
        if fence_length:
            if ticks == len(line) and ticks >= fence_length:
                if index == len(lines) - 1:
                    return lines[index - 1]
                fence_length = 0
        elif ticks >= 3:
            fence_length = ticks
    return lines[-1] if lines else ""


def parse_answer(text, expected_id=None, require_criteria=True):
    """Liest Urteile in Reihenfolge. APPROVE gilt nur als JSON-Objekt in der Schlusszeile.
    Mit Auftragsblatt braucht APPROVE außerdem mindestens einen Kriteriumssatz."""
    text = text or ""
    decoder = json.JSONDecoder()
    last = last_json = None
    plain_start = 0
    plain_verdict = re.compile(r'\b(?:prüf)?urteil"?[*\s]*[:=][*\s]*"?'
                               r'(APPROVE|CHANGES|ESCALATE)\b(?!\|)', re.I)
    pos = text.find("{")
    while pos != -1:
        try:
            data, end = decoder.raw_decode(text, pos)
        except ValueError:
            end = pos + 1
        else:
            for match in plain_verdict.finditer(text, plain_start, pos):
                last = {"urteil": match.group(1), "plain": True}
            plain_start = max(plain_start, end)
            if str(data.get("urteil", "")).upper() in lib.VERDICTS:
                final = text[pos:end] == closing_line(text)
                last = last_json = dict(data, _final=final)
            elif expected_id is not None and data.get("pruef_id") == expected_id:
                last = None
            else:
                end = pos + 1
        pos = text.find("{", end)
    for match in plain_verdict.finditer(text, plain_start):
        last = {"urteil": match.group(1), "plain": True}
    if last is None:
        return None
    if expected_id is not None and (last_json or {}).get("pruef_id") != expected_id:
        raise lib.FabrikError("Die Prüf-ID der Antwort fehlt oder passt nicht zum aktuellen Prompt.")
    verdict = last["urteil"].upper()
    if verdict == "APPROVE":
        if last.get("plain"):
            if expected_id is None or last_json["urteil"].upper() != "APPROVE":
                return None
            last = last_json
        # Ein zitiertes Urteil kann im folgenden Text zurückgenommen werden.
        if not last["_final"]:
            return {"urteil": "CHANGES", "kriterien": [],
                    "funde": ["Das APPROVE steht nicht in der Schlusszeile der Antwort und gilt deshalb nicht."],
                    "frage": ""}
    criteria = []
    entries = last.get("kriterien")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            number, sentence = entry.get("nr"), entry.get("satz")
            if (type(number) in (str, int, float) and str(number).strip()
                    and isinstance(sentence, str) and sentence.strip()):
                criteria.append({"nr": str(number).strip(), "satz": sentence.strip()})
    if verdict == "APPROVE" and require_criteria and not criteria:
        return None
    findings = last.get("funde") or []
    return {"urteil": verdict, "kriterien": criteria,
            "funde": [str(f) for f in findings] if isinstance(findings, list) else [str(findings)],
            "frage": str(last.get("frage") or "")}


def resolve_issue(explicit_issue, body):
    if explicit_issue:
        return explicit_issue
    refs = lib.referenced_issues(body)
    return refs[0] if refs else None


def diff_base(info):
    return info["base_sha"] or info["base"]


def pr_revision(root, pr):
    data = lib.gh_json(["pr", "view", str(pr), "--json", "headRefOid,commits"], cwd=root)
    if not isinstance(data, dict) or not isinstance(data.get("commits"), list):
        raise lib.FabrikError("Commitliste des PR fehlt.")
    head = data["headRefOid"]
    commits = data["commits"]
    if len(commits) == 100 and commits[-1].get("oid") != head:
        # gh pr view liefert nur die ersten 100 Commits.
        query = """query($owner: String!, $repo: String!, $pr: Int!, $endCursor: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $pr) { commits(first: 100, after: $endCursor) {
              nodes { commit { oid messageHeadline messageBody } }
              pageInfo { hasNextPage endCursor }
            } }
          }
        }"""
        pages = lib.gh_json(["api", "graphql", "--paginate", "--slurp", "-f", f"query={query}",
                             "-F", "owner={owner}", "-F", "repo={repo}", "-F", f"pr={pr}"], cwd=root)
        commits = [node["commit"] for page in pages
                   for node in page["data"]["repository"]["pullRequest"]["commits"]["nodes"]]
    return head, commits


class Context:
    """Everything known about one PR and its order before a review."""

    def __init__(self, root, pr, issue=None):
        self.root = root
        self.info = lib.pr_info(pr, cwd=root, fields=lib.PR_FIELDS + ",baseRefOid")
        self.pr = self.info["number"]
        self.explicit_issue = issue
        self.issue = resolve_issue(issue, self.info["body"])

    @cached_property
    def revision(self):
        return pr_revision(self.root, self.pr)

    @cached_property
    def merge_base(self):
        base = self.info["base_sha"]
        if not base:
            return None
        code, out, _ = lib.run(["git", "merge-base", base, self.info["head_sha"]], cwd=self.root)
        return (out.strip() or None) if code == 0 else None

    @cached_property
    def risk_stage(self):
        base = diff_base(self.info) if self.info["base_sha"] else f"origin/{self.info['base']}"
        config, _ = lib.base_config(self.root, base)
        tier = risk.classify(risk.changed_paths(self.root, base, self.info["head_sha"]), config)
        return lib.stage_max(tier["stage"], lib.label_stage(self.info["labels"]))

    @property
    def stage(self):
        return "tief" if self.risk_stage in ("T2", "T3") else "standard"

    @cached_property
    def builders(self):
        builders = set()
        _, commits = self.revision
        for commit in commits:
            names = []
            for line in commit["messageBody"].splitlines():
                match = re.fullmatch(r"gebaut-von\s*[:=](.*)", line.strip().casefold())
                if match:
                    names.append(match.group(1).strip() or "(unbekannt)")
            builders.update(names or [None])
        return builders

    @cached_property
    def issue_info(self):
        if not self.issue:
            return {}
        return lib.gh_json(["issue", "view", str(self.issue), "--json", "comments,labels"],
                           cwd=self.root) or {}

    @cached_property
    def order_sheet(self):
        if not self.issue:
            return "", ""
        data = lib.gh_json(["issue", "view", str(self.issue), "--json", "title,body"],
                           cwd=self.root) or {}
        return data.get("title") or "", data.get("body") or ""

    @cached_property
    def sheet_hash(self):
        if not self.issue:
            return None
        content = json.dumps(self.order_sheet, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(content).hexdigest()[:16]

    @cached_property
    def rounds(self):
        bodies = ([c["body"] for c in self.issue_info.get("comments", [])]
                  if self.issue else self.info["comments"])
        return lib.rounds_used(bodies)

    def update_review_label(self, reviewer):
        label = lib.LABEL_CLAUDE_ONLY
        if reviewer == "claude" and None in self.builders:
            self.label(label)
            return
        issue_labels = [entry["name"] for entry in self.issue_info.get("labels", [])]
        for kind, number, labels in (("pr", self.pr, self.info["labels"]),
                                     ("issue", self.issue, issue_labels)):
            if label in labels:
                lib.gh([kind, "edit", str(number), "--remove-label", label], cwd=self.root)

    def already_reviewed(self):
        return lib.verdict_for(self.info["comments"], self.info["head_sha"])

    def label(self, *labels):
        for kind, number in (("pr", self.pr), ("issue", self.issue)):
            if number:
                args = [kind, "edit", str(number)]
                for label in labels:
                    args += ["--add-label", label]
                lib.gh(args, cwd=self.root)


def check_preconditions(ctx):
    """Return (exit_code, message) if no review may happen, else None."""
    previous = ctx.already_reviewed()
    if previous:
        return NO_NEW_DIFF, (f"Kein neuer Diff seit dem letzten Urteil {previous['urteil']} für "
                             f"{ctx.info['head_sha'][:7]}. Keine neue Prüfung.")
    if ctx.rounds >= lib.MAX_ROUNDS:
        mark_stuck(ctx)
        return STUCK, stuck_message(ctx)
    local = lib.git(["rev-parse", "HEAD"], cwd=ctx.root).strip()
    if local != ctx.info["head_sha"]:
        return ERROR, (f"Lokaler Stand {local[:7]} weicht vom PR ({ctx.info['head_sha'][:7]}) ab. "
                       "Erst pushen oder den PR-Branch auschecken.")
    return None


def stuck_message(ctx):
    target = f"Issue #{ctx.issue}" if ctx.issue else f"PR #{ctx.pr}"
    return (f"FESTGEFAHREN: {lib.MAX_ROUNDS} Runden für {target} verbraucht. Genau eine fachliche "
            "Frage an den Product Owner stellen, Diagnose in höchstens 30 Zeilen. Zurücksetzen "
            "nur mit neuer Voraussetzung: review.py reset.")


def mark_stuck(ctx):
    ctx.label(lib.LABEL_STUCK, lib.LABEL_WAITING)


def gate_output(ctx):
    base = ctx.info["base_sha"] or f"origin/{ctx.info['base']}"
    args = [sys.executable, str(Path(__file__).resolve().parent / "gate.py"), "--base", base, "--json"]
    if lib.behavior_change(ctx.info["body"]):
        args.append("--behavior-change")
    code, out, err = lib.run(args, cwd=ctx.root)
    try:
        report = json.loads(out)
    except ValueError:
        return code == 0, (out + err)[-4000:]
    return code == 0, report["review_report"]


def truncate(text, limit):
    suffix = "[gekürzt]"
    return text if len(text) <= limit else text[:limit - len(suffix)] + suffix


def prompt_comments(bodies, limit):
    comments = [truncate(body, 2000) for body in bodies
                if body and not lib._markers([body], lib.VERDICT_MARK)]
    separator = "\n\n"
    size = sum(len(comment) + len(separator) for comment in comments) - len(separator)
    if size > limit:
        notice = "[ältere Kommentare ausgelassen]"
        size += len(notice) + len(separator)
        for index, comment in enumerate(comments):
            size -= len(comment) + len(separator)
            if size <= limit:
                return separator.join([notice, *comments[index + 1:]])
    return separator.join(comments) or "(keine Kommentare)"


def known_limits(ctx):
    """Liest bekannte Grenzen ausschließlich aus einer regulären Datei in der Merge-Base."""
    root = ctx.root.resolve()
    script = Path(__file__).absolute()
    if script.is_relative_to(ctx.root.absolute()):
        script = root / script.relative_to(ctx.root.absolute())
    elif not script.is_relative_to(root):
        script = script.resolve()
    path = script.with_name(LIMITS_FILE)
    if not path.is_relative_to(root):
        return ""
    try:
        base = ctx.merge_base
        if not base:
            return ""
        relative = path.relative_to(root).as_posix()
        entry = lib.git(["ls-tree", base, "--", f":(literal){relative}"], cwd=ctx.root)
        if entry.split(" ", 1)[0] not in ("100644", "100755"):
            return ""
        text = lib.git(["show", f"{base}:{relative}"], cwd=ctx.root)
    except (OSError, lib.FabrikError):
        return ""
    text = text.strip()
    return LIMITS_SECTION.format(limits=truncate(text, MAX_LIMITS_CHARS)) if text else ""


def pending_text(slice_spec, pending):
    if slice_spec:
        current, total = slice_spec.split("/")
        if current != total:
            return truncate(pending or "", 8000)
    return ""


def pending_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else None


def prompt_diff(ctx, slice_spec=None, pending_digest=None):
    head = ctx.info["head_sha"]
    base = ctx.merge_base
    for body in reversed(ctx.info["comments"]):
        verdicts = lib.verdicts([body])
        if not verdicts:
            continue
        previous = verdicts[-1]
        sha = previous.get("sha")
        if (previous["urteil"] in ("CHANGES", "ESCALATE") and isinstance(sha, str)
                and base and previous.get("merge_base") == base
                and previous.get("slice") == slice_spec and previous.get("stufe") == ctx.stage
                and previous.get("issue") == ctx.issue
                and "blatt" in previous and previous["blatt"] == ctx.sheet_hash
                and "ausstehend" in previous and previous["ausstehend"] == pending_digest
                and re.fullmatch(r"[0-9a-f]{7,64}", sha) and sha != head
                and lib.run(["git", "merge-base", "--is-ancestor", sha, head], cwd=ctx.root)[0] == 0
                and lib.git(["rev-parse", sha], cwd=ctx.root).strip() != head):
            stat = lib.git(["diff", "--stat", f"{base}..{head}"], cwd=ctx.root)
            findings = re.sub(r"^\s*<!--.*?-->", "", body, count=1, flags=re.S).strip()
            context = ("Die Vorrunde hat den übrigen Diff geprüft. Prüfe, ob ihre Funde behoben sind.\n\n"
                       f"### Dateiliste des Gesamtdiffs\n{truncate(stat, 8000)}\n"
                       f"### Funde und Urteil der Vorrunde\n{truncate(findings, 8000)}\n\n")
            change = lib.git(["diff", f"{sha}..{head}"], cwd=ctx.root)
            return f"## Änderung seit der Vorrunde (Commit {sha[:7]})", context, change
        break
    return f"## Diff (PR #{ctx.pr}, Commit {head[:7]})", "", lib.gh(["pr", "diff", str(ctx.pr)], cwd=ctx.root)


def build_prompt(ctx, gate_text, slice_spec=None, pending=None, review_id=None):
    title, body = ctx.order_sheet
    issue_title = f" (Issue #{ctx.issue}: {title})" if ctx.issue else ""
    issue_body = (body or "(leer)") if ctx.issue else "(kein Issue verknüpft)"
    pending = pending_text(slice_spec, pending)
    heading, context, diff = prompt_diff(ctx, slice_spec, pending_hash(pending))
    slice_section, criteria_states = "", "erfüllt oder verletzt"
    if slice_spec:
        current, total = slice_spec.split("/")
        if current != total:
            slice_section = (f"\n## Zwischenprüfung\nScheibe {current} von {total}. "
                             f"Noch ausstehend: {pending}\n"
                             "Was laut Auftragsblatt erst in späteren Scheiben kommt, ist kein Fund. "
                             "Prüfe, ob das Gebaute korrekt ist und zum Auftrag passt.\n")
            criteria_states = "erfüllt, verletzt oder noch ausstehend"
        else:
            slice_section = (f"\n## Schlussprüfung\nScheibe {current} von {total}. "
                             "Keine spätere Scheibe folgt: Alles, was bis zum Merge belegbar ist, "
                             "muss jetzt erfüllt sein. Belege, die laut Belegvertrag erst nach dem Merge "
                             "entstehen, bleiben „offen bis zur Abnahme“.\n")
    criteria_instruction = (CRITERIA_INSTRUCTION.format(criteria_states=criteria_states)
                            if ctx.issue else NO_SHEET_INSTRUCTION)
    pr_body = truncate(ctx.info["body"], 3000)
    prompt = PROMPT.format(issue_title=truncate(issue_title, 1000), issue_body=truncate(issue_body, 70_000),
                         gate=truncate(gate_text.strip(), 4000),
                         id_instruction=" Übernimm die Prüf-ID unverändert in das Feld pruef_id." if review_id else "",
                         id_field=f', "pruef_id": "{review_id}"' if review_id else "",
                         slice_section=slice_section, criteria_instruction=criteria_instruction,
                         limits_section=known_limits(ctx),
                         pr_body=pr_body, comments=prompt_comments(ctx.info["comments"], 8000 - len(pr_body)),
                         diff_heading=heading, diff_context=context, diff="")
    remaining = MAX_PROMPT_CHARS - len(prompt)
    if len(diff) > remaining:
        notice = "\n[Diff gekürzt. Den Rest lokal mit git diff lesen.]"
        diff = diff[:remaining - len(notice)] + notice
    return prompt + diff


def record(ctx, answer, reviewer, slice_spec, stage, sheet_hash, pending_digest):
    """Set labels before posting the verdict to the PR and issue. Returns the exit code."""
    current = lib.pr_info(ctx.pr, cwd=ctx.root, fields="headRefOid,baseRefOid,baseRefName,body")
    if current["head_sha"] != ctx.info["head_sha"]:
        raise lib.FabrikError("Der PR-Commit hat sich während der Prüfung geändert. Bitte erneut prüfen.")
    if diff_base(current) != diff_base(ctx.info):
        raise lib.FabrikError("Die Basis des PR hat sich während der Prüfung geändert. Bitte erneut prüfen.")
    check_stamp_issue(issue_label(ctx.issue), resolve_issue(ctx.explicit_issue, current["body"]))
    rounds = ctx.rounds + (0 if answer["urteil"] == "APPROVE" else 1)
    marker = {"urteil": answer["urteil"], "sha": ctx.info["head_sha"], "pruefer": reviewer,
              "pr": ctx.pr, "issue": ctx.issue, "runde": rounds, "slice": slice_spec, "stufe": stage,
              "merge_base": ctx.merge_base, "blatt": sheet_hash, "ausstehend": pending_digest}
    slice_label = f"Scheibe {slice_spec}, " if slice_spec else ""
    lines = [f"**Prüfurteil: {answer['urteil']}** ({reviewer}, Commit {ctx.info['head_sha'][:7]}, "
             f"{slice_label}Runde {rounds} von {lib.MAX_ROUNDS})"]
    lines += [f"- Kriterium {entry['nr']}: {entry['satz']}" for entry in answer["kriterien"]]
    lines += [f"- {f}" for f in answer["funde"]]
    if answer["frage"]:
        lines.append(f"\nFrage: {answer['frage']}")
    marker_line = lib.format_marker(lib.VERDICT_MARK, marker)
    body = "\n".join(lines).replace("<!--", "&lt;!--")
    ctx.update_review_label(reviewer)
    stuck = answer["urteil"] != "APPROVE" and rounds >= lib.MAX_ROUNDS
    if stuck:
        mark_stuck(ctx)
    elif answer["urteil"] == "ESCALATE":
        ctx.label(lib.LABEL_WAITING)
    lib.gh(["pr", "comment", str(ctx.pr), "--body", f"{marker_line}\n{body}"], cwd=ctx.root)
    if ctx.issue:
        lib.gh(["issue", "comment", str(ctx.issue), "--body",
                f"{marker_line}\nPrüfurteil {answer['urteil']} "
                f"zu PR #{ctx.pr} ({reviewer}, Runde {rounds})."], cwd=ctx.root)
    if stuck:
        print(stuck_message(ctx))
        return STUCK
    return EXIT[answer["urteil"]]


def print_answer(answer):
    print(f"URTEIL: {answer['urteil']}")
    for f in answer["funde"]:
        print(f"- {f}")
    if answer["frage"]:
        print(f"Frage: {answer['frage']}")


def cmd_prepare(ctx):
    """Shared start of run and prompt: preconditions and gate. Returns (code, text)."""
    blocked = check_preconditions(ctx)
    if blocked:
        return blocked
    green, text = gate_output(ctx)
    if not green:
        return GATE_RED, "Gate rot, kein Modell prüft:\n" + text
    return None, text


def print_claude_model(stage):
    print(f"Modell für den Subagenten: {lib.claude_model(stage)}")
    print(f"Stufe: {stage}")


def subagent(ctx, reason, slice_spec, pending):
    target = f"--pr {ctx.pr}"
    if ctx.explicit_issue is not None:
        target += f" --issue {ctx.explicit_issue}"
    command = f"python3 .claude/fabrik/review.py prompt {target}"
    if slice_spec:
        command += f" --slice {shlex.quote(slice_spec)}"
    if pending is not None:
        command += f" --pending {shlex.quote(pending)}"
    print(f"Risikostufe: {ctx.risk_stage}.")
    print(f"{reason}\n{command}\nAntwort dann mit review.py record {target} --reviewer claude eintragen.")
    print_claude_model(ctx.stage)


def run_partner(root, partner, role, stage, prompt, quota, issue, pr=None):
    command = lib.partner_command(partner, "pruefen", stage)
    model = lib.partner_setting(partner, "modelle", stage)
    print(f"Modell: {model or 'unbekannt'}, Stufe: {stage}.")
    usage.log(root, "lauf_start", partner["name"], role, quota, model, stage, issue, pr, "starten")
    outcome = "abgebrochen"
    try:
        result = lib.run(["sh", "-c", command], cwd=root, input_text=prompt, timeout=PARTNER_TIMEOUT_S)
        outcome = f"Exitcode {result[0]}"
    except subprocess.TimeoutExpired as exc:
        outcome = "Zeitlimit erreicht"
        stdout = (exc.stdout or b"").decode(errors="replace")
        stderr = (exc.stderr or b"").decode(errors="replace")
        result = (ERROR, stdout, f"{stderr}\nZeitlimit von {exc.timeout} Sekunden erreicht.")
    finally:
        final_quota = lib.read_quota(partner.get("kontingent"), root)
        usage.log(root, "lauf_ende", partner["name"], role, final_quota, model, stage, issue, pr, outcome)
    return (*result, final_quota)


def review_limit(root, partner, role, quota, stage, issue, pr, out, err):
    hit_limit = any(lib.LIMIT_MESSAGE.search(line) for line in (out + "\n" + err).splitlines()[-20:])
    return usage.partner_blocked(root, partner, role, quota, stage, issue, pr, hit_limit=hit_limit)


def cmd_design(root, issue):
    partner = lib.load_partner()
    if not partner or not partner.get("pruefen"):
        print_claude_model("tief")
        return NO_PARTNER
    quota = lib.read_quota(partner.get("kontingent"), root)
    blocked = usage.partner_blocked(root, partner, "design", quota, "tief", issue)
    if blocked:
        if blocked == NO_PARTNER:
            print_claude_model("tief")
        return blocked
    prompt = sys.stdin.read(MAX_DESIGN_CHARS)
    if len(prompt) >= MAX_DESIGN_CHARS:
        print("Der Prompt für die Design-Gegenprüfung muss kürzer als 140 000 Zeichen sein.")
        return ERROR
    code, out, err, final_quota = run_partner(root, partner, "design", "tief", prompt, quota, issue)
    if code:
        blocked = review_limit(root, partner, "design", final_quota, "tief", issue, None, out, err)
        if blocked:
            if blocked == NO_PARTNER:
                print_claude_model("tief")
            return blocked
        print(f"Design-Gegenprüfung fehlgeschlagen (Exitcode {code}): {err.strip()[-500:]}")
        return ERROR
    print(out, end="")
    return 0


def cmd_run(ctx, slice_spec=None, pending=None):
    partner_names = sorted(ctx.builders - {None})
    partner = None if partner_names else lib.load_partner()
    if not partner or not partner.get("pruefen"):
        reason = "Kein Partner eingerichtet. Prompt für einen frischen Claude-Subagenten erzeugen"
        if partner_names:
            reason = (f"Partner {', '.join(partner_names)} hat gebaut. "
                      "Prüfung durch einen frischen Claude-Subagenten")
        subagent(ctx, reason, slice_spec, pending)
        return PARTNER_BUILT if partner_names else NO_PARTNER
    code, text = cmd_prepare(ctx)
    if code is not None:
        print(text)
        return code
    quota = lib.read_quota(partner.get("kontingent"), ctx.root)
    blocked = usage.partner_blocked(ctx.root, partner, "pruefen", quota, ctx.stage, ctx.issue, ctx.pr)
    if blocked:
        if blocked == NO_PARTNER:
            subagent(ctx, "Prüfung durch einen frischen Claude-Subagenten", slice_spec, pending)
        return blocked
    prompt = build_prompt(ctx, text, slice_spec, pending)
    previous_head, previous_commits = ctx.revision
    head, commits = pr_revision(ctx.root, ctx.pr)
    if (head != ctx.info["head_sha"] or head != previous_head
            or [c["oid"] for c in commits] != [c["oid"] for c in previous_commits]):
        print("Der PR hat sich während der Prüfung geändert. Bitte erneut prüfen.")
        return PR_CHANGED
    try:
        rc, out, err, final_quota = run_partner(ctx.root, partner, "pruefen", ctx.stage, prompt, quota, ctx.issue, ctx.pr)
    except OSError as exc:
        print(f"Partner {partner['name']} nicht verfügbar: {exc}")
        subagent(ctx, "Prüfung durch einen frischen Claude-Subagenten", slice_spec, pending)
        return NO_PARTNER
    if rc != 0:
        blocked = review_limit(ctx.root, partner, "pruefen", final_quota, ctx.stage, ctx.issue, ctx.pr, out, err)
        if blocked:
            if blocked == NO_PARTNER:
                subagent(ctx, "Prüfung durch einen frischen Claude-Subagenten", slice_spec, pending)
            return blocked
        print(f"Partner {partner['name']} nicht verfügbar (Exitcode {rc}): {err.strip()[-500:]}")
        subagent(ctx, "Prüfung durch einen frischen Claude-Subagenten", slice_spec, pending)
        return NO_PARTNER
    answer = parse_answer(out, require_criteria=bool(ctx.issue))
    if not answer:
        print(f"Risikostufe: {ctx.risk_stage}.")
        print(f"Antwort von {partner.get('name')} ohne gültiges Urteil. "
              f"{criteria_rule(ctx)} Letzte Zeilen:\n{out[-1500:]}")
        return NO_VERDICT
    print_answer(answer)
    return record(ctx, answer, str(partner.get("name", "partner")),
                  slice_spec, ctx.stage, ctx.sheet_hash, pending_hash(pending_text(slice_spec, pending)))


def criteria_rule(ctx):
    if ctx.issue:
        return "APPROVE erfordert mindestens einen Kriteriumssatz."
    return ("Ohne Auftragsblatt braucht APPROVE keine Kriteriensätze, aber ein einzeiliges JSON-Objekt "
            "als Schlusszeile.")


def prompt_stamp(ctx):
    """File that binds a recorded answer to the commit the prompt was built for."""
    git_dir = Path(lib.git(["rev-parse", "--absolute-git-dir"], cwd=ctx.root).strip())
    return git_dir / "fabrik" / f"prompt-{ctx.pr}.sha"


def read_stamp(stamp):
    try:
        content = stamp.read_text(encoding="utf-8").rstrip("\n")
    except FileNotFoundError:
        return None, None, None, None, None, None, None, None
    sha, _, rest = content.partition("\n")
    scope, _, rest = rest.partition("\n")
    review_id, _, rest = rest.partition("\n")
    issue, _, rest = rest.partition("\n")
    base, _, rest = rest.partition("\n")
    sheet_hash, _, rest = rest.partition("\n")
    stage, _, pending_digest = rest.partition("\n")
    return sha, scope or None, review_id, issue, base, sheet_hash or None, stage, pending_digest or None


def issue_label(issue):
    return f"#{issue}" if issue is not None else "kein Issue"


def check_stamp_issue(stamped_issue, current_issue):
    current = issue_label(current_issue)
    if current != stamped_issue:
        raise lib.FabrikError(f"Abweichendes Issue: Beim Prompt war es {stamped_issue}, aktuell ist es {current}.")


def review_id_prefix(sha, scope):
    return f"{sha[:12]}-{scope or 'gesamt'}-"


@contextmanager
def locked_stamp(ctx):
    stamp = prompt_stamp(ctx)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    lock = stamp.with_suffix(".lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise lib.FabrikError(f"Eine andere Prüfung belegt {lock}. Nach deren Abschluss erneut versuchen. "
                              "Ist der Prozess abgebrochen, das Sperrverzeichnis löschen.")
    try:
        yield stamp
    finally:
        lock.rmdir()


def cmd_prompt(ctx, stamp, slice_spec=None, pending=None):
    sha, scope, _, _, base, _, _, _ = read_stamp(stamp)
    head = ctx.info["head_sha"]
    if sha == head and base == diff_base(ctx.info) and scope != slice_spec:
        print(f"Für PR #{ctx.pr} und Commit {sha[:7]} ist ein Prompt mit anderem Prüfumfang offen. "
              f"Erst die offene Antwort mit review.py record --pr {ctx.pr} eintragen "
              f"oder den Stempel bewusst löschen: {stamp}")
        return ERROR
    code, text = cmd_prepare(ctx)
    if code is not None:
        print(text)
        return code
    review_id = review_id_prefix(head, slice_spec) + secrets.token_hex(4)
    prompt = build_prompt(ctx, text, slice_spec, pending, review_id)
    pending_digest = pending_hash(pending_text(slice_spec, pending))
    staged = stamp.with_suffix(".tmp")
    try:
        staged.write_text(f"{head}\n{slice_spec or ''}\n{review_id}\n{issue_label(ctx.issue)}\n"
                          f"{diff_base(ctx.info)}\n{ctx.sheet_hash or ''}\n{ctx.stage}\n"
                          f"{pending_digest or ''}\n", encoding="utf-8")
        staged.replace(stamp)
    finally:
        staged.unlink(missing_ok=True)
    print_claude_model(ctx.stage)
    print(prompt)
    return 0


def cmd_record(ctx, stamp, reviewer, path):
    reviewer = reviewer.strip()
    if reviewer.casefold() in ("claude", "claude-allein"):
        reviewer = "claude"
    if ctx.builders - {None} and reviewer != "claude":
        print("Am PR hat ein Partner gebaut. Nur ein frischer Claude-Subagent darf das Urteil abgeben.")
        return ERROR
    sha, slice_spec, review_id, stamped_issue, base, sheet_hash, stage, pending_digest = read_stamp(stamp)
    if sha == ctx.info["head_sha"] and stamped_issue:
        check_stamp_issue(stamped_issue, ctx.issue)
        if base != diff_base(ctx.info):
            print("Die Basis im Stempel fehlt oder weicht vom PR ab. Bitte den Prompt neu erzeugen.")
            return ERROR
    code, text = cmd_prepare(ctx)
    if code is not None:
        print(text)
        return code
    if sha != ctx.info["head_sha"]:
        print(f"Kein Prompt für Commit {ctx.info['head_sha'][:7]} vorbereitet. Erst "
              f"review.py prompt --pr {ctx.pr}, dann prüfen lassen, dann eintragen.")
        return ERROR
    prefix = review_id_prefix(sha, slice_spec)
    if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{8}", review_id or ""):
        print("Im Stempel fehlt eine passende Prüf-ID für Commit und Scheibe. Bitte den Prompt neu erzeugen.")
        return ERROR
    if not stamped_issue:
        print("Im Stempel fehlt die Issue-Angabe. Bitte den Prompt neu erzeugen.")
        return ERROR
    if ctx.issue and not re.fullmatch(r"[0-9a-f]{16}", sheet_hash or ""):
        print("Im Stempel fehlt der Prüfwert des Auftragsblatts. Bitte den Prompt neu erzeugen.")
        return ERROR
    if stage not in ("standard", "tief"):
        print("Im Stempel fehlt die Prüfstufe. Bitte den Prompt neu erzeugen.")
        return ERROR
    text = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    answer = parse_answer(text, expected_id=review_id, require_criteria=bool(ctx.issue))
    if not answer:
        print("Antwort ohne gültiges Urteil. Erwartet wird APPROVE, CHANGES oder ESCALATE. "
              f"{criteria_rule(ctx)}")
        return NO_VERDICT
    print_answer(answer)
    result = record(ctx, answer, reviewer, slice_spec, stage, sheet_hash, pending_digest)
    stamp.unlink()
    return result


def cmd_status(ctx):
    last = lib.verdicts(ctx.info["comments"])
    print(f"PR #{ctx.pr}, Issue {('#' + str(ctx.issue)) if ctx.issue else '-'}, "
          f"Runden {ctx.rounds} von {lib.MAX_ROUNDS}")
    if last:
        print(f"Letztes Urteil: {last[-1]['urteil']} für {last[-1].get('sha', '')[:7]} "
              f"(aktueller Commit {ctx.info['head_sha'][:7]})")
    return 0


def cmd_reset(root, issue, reason):
    body = (f"{lib.format_marker(lib.RESET_MARK, {'grund': reason})}\n"
            f"Runden zurückgesetzt. Neue Voraussetzung: {reason}")
    lib.gh(["issue", "comment", str(issue), "--body", body], cwd=root)
    lib.run(["gh", "issue", "edit", str(issue), "--remove-label", lib.LABEL_STUCK], cwd=root)
    print(f"Runden für Issue #{issue} zurückgesetzt.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prüfung über Kreuz mit festem Antwortschema")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "prompt", "record", "status"):
        p = sub.add_parser(name)
        p.add_argument("--pr", required=True)
        p.add_argument("--issue", type=int)
        if name in ("run", "prompt"):
            p.add_argument("--slice", dest="slice_spec", type=lib.parse_slice, metavar="K/N",
                           help="Nummer der Scheibe und Gesamtzahl")
            p.add_argument("--pending", metavar="TEXT", help="Für spätere Scheiben geplante Arbeit")
        if name == "record":
            p.add_argument("--reviewer", required=True, help="z. B. codex oder claude")
            p.add_argument("--file", help="Antwort aus Datei statt stdin")
    p = sub.add_parser("design")
    p.add_argument("--issue", type=int)
    p = sub.add_parser("reset")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.cmd in ("run", "prompt") and args.slice_spec:
        current, total = args.slice_spec.split("/")
        if current != total and not (args.pending or "").strip():
            parser.error("Bei einer Zwischenprüfung mit --pending die noch geplante Arbeit angeben.")
    try:
        root = lib.repo_root()
        if args.cmd == "design":
            return cmd_design(root, args.issue)
        if args.cmd == "reset":
            return cmd_reset(root, args.issue, args.reason)
        ctx = Context(root, args.pr, args.issue)
        if args.cmd == "run":
            return cmd_run(ctx, args.slice_spec, args.pending)
        if args.cmd in ("prompt", "record"):
            with locked_stamp(ctx) as stamp:
                if args.cmd == "prompt":
                    return cmd_prompt(ctx, stamp, args.slice_spec, args.pending)
                return cmd_record(ctx, stamp, args.reviewer, args.file)
        return cmd_status(ctx)
    except (lib.FabrikError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Prüfung: {exc}")
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
