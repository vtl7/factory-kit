#!/usr/bin/env python3
# Kit-Datei: Formatierer und Linter des Projekts lassen sie aus.
# fmt: off
# ruff: noqa
"""CI step after a push to the main branch: a merge without verdict becomes an incident.

Without branch protection the stage hook can be bypassed (wrapper script, web UI, direct push).
This step finds such merges afterwards, opens an issue labelled 'Vorfall' and a revert PR.
It prevents nothing; it makes the bypass visible and easy to undo.

Usage (in CI): merge_audit.py --sha SHA
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabrik_lib as lib  # noqa: E402

REVERT_PREFIX = "fabrik/revert-"
BOT_LOGIN = "github-actions[bot]"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def find_problem(root, sha):
    """Return (problem or None, pr_number or None). None means the merge is fine or exempt."""
    code, _, _ = lib.run(["git", "cat-file", "-e", f"{sha}^:{lib.CONFIG_PATH}"], cwd=root)
    if code != 0:
        return None, None  # the kit arrives with this commit: nothing to audit yet
    pulls = lib.gh_json(["api", f"repos/{{owner}}/{{repo}}/commits/{sha}/pulls"], cwd=root) or []
    merged = [p for p in pulls if p.get("merge_commit_sha") == sha] or pulls
    if not merged:
        return f"Commit {sha[:7]} kam ohne PR auf den Hauptzweig.", None
    number = merged[0]["number"]
    head_ref = str((merged[0].get("head") or {}).get("ref", ""))
    author = str((merged[0].get("user") or {}).get("login", ""))
    if head_ref.startswith(REVERT_PREFIX) and author == BOT_LOGIN:
        return None, number  # revert PR opened by this audit
    info = lib.pr_info(number, cwd=root, fields="number,headRefOid,comments")
    verdict = lib.final_verdict_for(info["comments"], info["head_sha"])
    if not verdict:
        return f"PR #{number} wurde ohne Prüfurteil für den gesamten Stand seines letzten Commits gemergt.", number
    if verdict["urteil"] != "APPROVE":
        return f"PR #{number} wurde mit Prüfurteil {verdict['urteil']} gemergt.", number
    return None, number


def pushed_commits(root, before, sha):
    """(commits, complete): commits that reached the main branch with this push, oldest first.

    complete is False when the state before the push cannot be found (force push, shallow
    clone). Then only the new head is checked and the caller reports the gap.
    """
    if not before or set(before) == {"0"}:
        return [sha], True  # first push of the branch
    if lib.run(["git", "cat-file", "-e", f"{before}^{{commit}}"], cwd=root)[0] != 0:
        lib.run(["git", "fetch", "--quiet", "origin", before], cwd=root)
    if lib.run(["git", "cat-file", "-e", f"{before}^{{commit}}"], cwd=root)[0] != 0:
        return [sha], False
    if lib.run(["git", "merge-base", "--is-ancestor", before, sha], cwd=root)[0] != 0:
        return [sha], False  # history was rewritten
    out = lib.git(["rev-list", "--first-parent", "--reverse", f"{before}..{sha}"], cwd=root)
    return (out.split() or [sha]), True


def report_gap(root, before, sha):
    body = (f"Der Push auf den Hauptzweig ließ sich nicht vollständig prüfen: der Stand davor "
            f"({before[:7]}) ist nicht erreichbar oder kein Vorgänger von {sha[:7]}. Meist ist das "
            "ein Force-Push. Geprüft wurde nur der neue Stand. Bitte den Verlauf ansehen.")
    title = f"Push nicht vollständig prüfbar: {sha[:7]}"
    try:
        url = lib.gh(["issue", "create", "--title", title, "--body", body,
                      "--label", lib.LABEL_INCIDENT], cwd=root).strip()
    except lib.FabrikError:
        url = lib.gh(["issue", "create", "--title", title, "--body", body], cwd=root).strip()
    return url


def open_incident(root, sha, problem, pr):
    subject = lib.git(["log", "-1", "--format=%s", sha], cwd=root).strip()
    body = (f"{problem}\n\nCommit: {sha}\nBetreff: {subject}\n\n"
            "Gefunden vom CI-Schritt nach dem Merge. Ein Revert-PR folgt, sofern er sich anlegen "
            "lässt. Erst prüfen, dann den Revert mergen oder das Prüfurteil nachholen und den "
            "Revert schließen.")
    title = f"Merge ohne Prüfurteil: {subject[:60]}"
    try:
        url = lib.gh(["issue", "create", "--title", title, "--body", body,
                      "--label", lib.LABEL_INCIDENT], cwd=root).strip()
    except lib.FabrikError:
        url = lib.gh(["issue", "create", "--title", title, "--body", body], cwd=root).strip()
    issue = url.rstrip("/").split("/")[-1]
    branch = f"{REVERT_PREFIX}{sha[:7]}"
    parents = lib.git(["rev-list", "--parents", "-n", "1", sha], cwd=root).split()
    revert = ["revert", "--no-edit"] + (["-m", "1"] if len(parents) > 2 else []) + [sha]
    ident = ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]
    try:
        lib.git(["checkout", "-B", branch, sha], cwd=root)
        lib.git(ident + revert, cwd=root)
        lib.git(["push", "--force", "origin", branch], cwd=root)
        pr_url = lib.gh(["pr", "create", "--head", branch, "--title", f"Revert: {subject[:60]}",
                         "--body", f"Macht {sha[:7]} rückgängig. Refs #{issue}"], cwd=root).strip()
        lib.gh(["issue", "comment", issue, "--body", f"Revert-PR: {pr_url}"], cwd=root)
        return issue, pr_url
    except lib.FabrikError as exc:
        lib.gh(["issue", "comment", issue, "--body",
                f"Revert-PR ließ sich nicht automatisch anlegen: {exc}\n"
                f"Von Hand: `git revert {sha}` auf einem Branch und PR öffnen."], cwd=root)
        return issue, None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Merge ohne Prüfurteil als Vorfall melden")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--before", help="Stand vor dem Push; dann wird jeder neue Commit geprüft")
    args = parser.parse_args(argv)
    try:
        root = lib.repo_root(os.environ.get("CLAUDE_PROJECT_DIR"))
        found = 0
        commits, complete = pushed_commits(root, args.before, args.sha)
        if not complete:
            found += 1
            print(f"Merge-Prüfung: Stand vor dem Push nicht prüfbar, Vorfall {report_gap(root, args.before, args.sha)}")
        for sha in commits:
            problem, pr = find_problem(root, sha)
            if not problem:
                print(f"Merge-Prüfung {sha[:7]}: in Ordnung "
                      f"({'PR #' + str(pr) if pr else 'Einbindung des Kits'}).")
                continue
            found += 1
            print(f"Merge-Prüfung {sha[:7]}: {problem}")
            issue, pr_url = open_incident(root, sha, problem, pr)
            print(f"Vorfall #{issue}, Revert-PR: {pr_url or 'nicht angelegt'}")
        return 1 if found else 0
    except lib.FabrikError as exc:
        print(f"Merge-Prüfung nicht ausführbar: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
