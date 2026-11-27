#!/usr/bin/env python3
# Kit-Datei: Formatierer und Linter des Projekts lassen sie aus.
# fmt: off
# ruff: noqa
"""PreToolUse hook on Bash: decides merge, push to main and ship by risk stage.

  no verdict APPROVE for the PR head  -> deny (every stage)
  gate check not green                -> deny
  T0, T1                              -> pass (normal permission flow)
  T2                                  -> ask once
  T3                                  -> ask, every time

The hook only sees the command text. A merge hidden in a wrapper script passes it; the CI step
after the merge reports such merges (merge_audit.py).
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabrik_lib as lib  # noqa: E402
import risk  # noqa: E402

SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n()]")
MERGE_VALUE_FLAGS = {"-R", "--repo", "-t", "--subject", "-b", "--body", "-F", "--body-file",
                     "-A", "--author-email", "--match-head-commit"}
PASS, ASK, DENY = "pass", "ask", "deny"
ORDER = (PASS, ASK, DENY)


def _tokens(segment):
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _as_list(value):
    if not value:
        return []
    return [value] if isinstance(value, str) else [v for v in value if v]


def implicit_push_targets(root, current_branch):
    """Branches a 'git push' without refspec updates, or None if git config makes it unclear."""
    code, out, _ = lib.run(["git", "config", "--get-regexp", r"^remote\..*\.push$"], cwd=root)
    if code == 0 and out.strip():
        return None  # remote.<name>.push decides: do not guess
    code, out, _ = lib.run(["git", "config", "--get", "push.default"], cwd=root)
    mode = out.strip() if code == 0 else "simple"
    if mode == "matching":
        return None
    if mode in ("upstream", "tracking"):
        code, out, _ = lib.run(["git", "config", "--get", f"branch.{current_branch}.merge"], cwd=root)
        merge = out.strip() if code == 0 else ""
        if not merge.startswith("refs/heads/"):
            return None  # upstream push without a readable target: do not guess
        return {merge[len("refs/heads/"):]}
    return {current_branch}


def _push_destinations(tokens, current_branch, implicit=None):
    after = tokens[tokens.index("push") + 1:]
    if "--all" in after or "--mirror" in after:
        return None  # every branch, the main branch included
    positional = [t for t in after if not t.startswith("-")]
    if len(positional) <= 1:  # 'git push' or 'git push origin': decided by git config
        return implicit if implicit != "unset" else {current_branch}
    dests = set()
    for spec in positional[1:]:
        spec = spec.lstrip("+")
        dest = spec.split(":")[-1] if ":" in spec else spec
        dest = dest.replace("refs/heads/", "")
        dests.add(current_branch if dest == "HEAD" else dest)
    return dests


def find_targets(command, ship_commands=None, main_branches="main", current_branch=None,
                 implicit="unset"):
    """Return [(kind, detail)] for merge, api-merge, push-main and ship steps in a command."""
    ships = _as_list(ship_commands)
    mains = set(_as_list(main_branches))
    targets = []
    for match in re.finditer(r"repos/([^/\s]+/[^/\s]+)/pulls/(\d+)/merge", command):
        targets.append(("merge", {"ref": match.group(2), "repo": match.group(1)}))
    for segment in SEGMENT_SPLIT.split(command):
        tokens = _tokens(segment.strip())
        if not tokens:
            continue
        for i in range(len(tokens) - 2):
            if Path(tokens[i]).name == "gh" and tokens[i + 1] == "pr" and tokens[i + 2] == "merge":
                ref, repo, j = None, None, i + 3
                while j < len(tokens):
                    tok = tokens[j]
                    if tok in MERGE_VALUE_FLAGS:
                        if tok in ("-R", "--repo") and j + 1 < len(tokens):
                            repo = tokens[j + 1]
                        j += 2
                        continue
                    if not tok.startswith("-") and ref is None:
                        ref = tok
                    j += 1
                targets.append(("merge", {"ref": ref, "repo": repo}))
        if "git" in [Path(t).name for t in tokens] and "push" in tokens:
            dests = _push_destinations(tokens, current_branch, implicit)
            if dests is None or dests & mains:
                targets.append(("push-main", {}))
        if any(_is_ship(segment, tokens, ship) for ship in ships):
            targets.append(("ship", {}))
    return targets


def _is_ship(segment, tokens, ship_command):
    if ship_command.strip() in segment:
        return True
    script = _tokens(ship_command)[0] if _tokens(ship_command) else ""
    if "/" in script or "." in Path(script).name:
        return any(Path(t).name == Path(script).name for t in tokens)
    return False


def decide_pr(info, config, action="Merge"):
    """Verdict, gate and stage of one PR. Used for merging it and for shipping it."""
    head = info["head_sha"]
    verdict = lib.final_verdict_for(info["comments"], head)
    if not verdict:
        partial = lib.verdict_for(info["comments"], head)
        if partial:
            return DENY, f"{action} gesperrt: nur Zwischenurteil für Scheibe {partial['slice']}."
        return DENY, (f"{action} gesperrt: kein Prüfurteil für Commit {head[:7]} von PR "
                      f"#{info['number']}. Erst den Skill pruefen ausführen.")
    if verdict["urteil"] != "APPROVE":
        return DENY, f"{action} gesperrt: Prüfurteil {verdict['urteil']} für {head[:7]}."
    gate = lib.gate_state(info["checks"])
    if gate != "success":
        hint = " Warten mit 'gh pr checks --watch'." if gate == "pending" else ""
        return DENY, f"{action} gesperrt: Gate ({lib.GATE_CHECK_NAME}) ist {gate}.{hint}"
    computed = risk.classify(info["files"], config)
    stage = lib.stage_max(lib.label_stage(info["labels"]), computed["stage"])
    reason = f"PR #{info['number']} ist {stage} ({', '.join(computed['categories'])})"
    if stage in ("T2", "T3"):
        return ASK, f"{reason}: {action} braucht deine Bestätigung."
    return PASS, reason


def decide_merge(info, config):
    return decide_pr(info, config, "Merge")


def decide_ship(root, config):
    sha = lib.git(["rev-parse", "HEAD"], cwd=root).strip()
    try:
        pulls = lib.gh_json(["api", f"repos/{{owner}}/{{repo}}/commits/{sha}/pulls"], cwd=root) or []
    except lib.FabrikError:
        pulls = []
    if not pulls:
        return DENY, (f"Ship gesperrt: zu Commit {sha[:7]} gibt es keinen PR und damit kein "
                      "Prüfurteil.")
    return decide_pr(lib.pr_info(pulls[0]["number"], cwd=root), config, "Ship")


def default_branch(root):
    """Main branch name from the remote, not from the (changeable) working config."""
    code, out, _ = lib.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=root)
    if code == 0 and out.strip().startswith("origin/"):
        return out.strip()[len("origin/"):]
    for name in ("main", "master"):
        if lib.run(["git", "rev-parse", "--verify", "--quiet", f"origin/{name}"], cwd=root)[0] == 0:
            return name
    return "main"


def load_configs(root):
    """(trusted, working, error): trusted is the config on the remote main branch."""
    try:
        working = lib.load_config(root)
    except lib.FabrikError:
        working = None
    main_branch = default_branch(root)
    code, text, _ = lib.run(["git", "show", f"origin/{main_branch}:{lib.CONFIG_PATH}"], cwd=root)
    trusted, error = None, None
    if code == 0:
        try:
            trusted = lib.load_config(root, text)
        except lib.FabrikError as exc:
            error = f"Konfiguration auf {main_branch} nicht lesbar ({exc})"
    return trusted, working, main_branch, error


def decide(command, root):
    """Return (decision, reason) for a Bash command."""
    trusted, working, main_branch, error = load_configs(root)
    configs = [c for c in (trusted, working) if c]
    if not configs and not error:
        return PASS, ""
    code, out, _ = lib.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    current = out.strip() if code == 0 else None
    mains = {main_branch} | {c["main_branch"] for c in configs}
    implicit = implicit_push_targets(root, current) if "push" in command else "unset"
    targets = find_targets(command, [c.get("ship") for c in configs], sorted(mains), current,
                           implicit)
    if not targets:
        return PASS, ""
    if error:
        return DENY, f"Stufen-Hook: {error}."
    config = trusted or working
    results = []
    for kind, detail in targets:
        try:
            if kind == "merge":
                info = lib.pr_info(detail["ref"], detail["repo"], cwd=root)
                results.append(decide_merge(info, config))
            elif kind == "push-main":
                results.append((DENY, f"Direkter Push auf {main_branch} ist gesperrt. "
                                      "Weg über PR und Merge."))
            elif kind == "ship":
                results.append(decide_ship(root, config))
        except lib.FabrikError as exc:
            results.append((DENY, f"Stufen-Hook: Stand nicht lesbar ({exc})."))
    worst = max(results, key=lambda r: ORDER.index(r[0]))
    reasons = " ".join(r[1] for r in results if r[0] == worst[0])
    return worst[0], reasons


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command") or ""
    try:
        root = lib.repo_root(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR"))
    except lib.FabrikError:
        return 0
    decision, reason = decide(command, root)
    if decision == PASS:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
