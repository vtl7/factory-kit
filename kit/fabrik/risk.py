#!/usr/bin/env python3
# Kit-Datei: Formatierer und Linter des Projekts lassen sie aus.
# fmt: off
# ruff: noqa
"""Risk stage T0..T3 from changed paths.

The categories live in the kit. A project maps its own paths to them in .fabrik/fabrik.yml
(`risk:`) and may add categories (`extra_categories:`), but cannot remove any.

Usage:
  risk.py [--base REF] [--files PATH ...] [--json]
  risk.py --label PR [--base REF]      # also sets the T label on the PR and its issues
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabrik_lib as lib  # noqa: E402

# Categories that describe configuration of the agent or rights. They win over doc/test.
STRONG = {
    "rechte": ("T3", [".claude/settings.json", ".claude/settings.local.json", "CODEOWNERS",
                      "**/permissions/**"]),
    "kit": ("T2", [".claude/", ".fabrik/", "AGENTS.md", "CLAUDE.md"]),
}
# Documentation; tests come from test_paths in the config.
LOW = {
    "doku": ("T0", ["*.md", "*.rst", "docs/", "LICENSE"]),
}
OTHER = {
    "migration": ("T2", ["**/migrations/**", "**/alembic/**", "*.sql"]),
    "abhaengigkeit": ("T2", [
        "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock",
        "poetry.lock", "uv.lock", "package.json", "package-lock.json", "pnpm-lock.yaml",
        "yarn.lock", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
    ]),
    "ci_deploy": ("T2", [".github/", ".gitlab-ci.yml", "Dockerfile", "docker-compose*.yml",
                         "compose*.yml", "*.tf", "fly.toml", "vercel.json", "netlify.toml",
                         "Procfile"]),
    "auth_secrets": ("T3", [".env", ".env.*", "*.pem", "*.key", "*secret*", "**/auth/**"]),
    "zahlung": ("T3", ["**/payment/**", "**/payments/**", "**/billing/**"]),
    "daten_loeschen": ("T3", []),
}
KIT_CATEGORIES = {**STRONG, **LOW, **OTHER}


def project_categories(config):
    """Project path mappings: {category: (stage, patterns)}. Unknown categories need a stage."""
    extra = config.get("extra_categories") or {}
    for name, stage in extra.items():
        if stage not in lib.STAGES:
            raise lib.FabrikError(f"extra_categories.{name}: Stufe muss T0 bis T3 sein, nicht {stage}")
    mapped = {}
    for name, patterns in (config.get("risk") or {}).items():
        if name in KIT_CATEGORIES:
            stage = KIT_CATEGORIES[name][0]
        elif name in extra:
            stage = extra[name]
        else:
            raise lib.FabrikError(
                f"risk.{name}: unbekannte Kategorie. Erst unter extra_categories mit Stufe anlegen.")
        if isinstance(patterns, str):
            patterns = [patterns]
        mapped[name] = (stage, list(patterns or []))
    return mapped


def _hits(path, table):
    return [(name, stage) for name, (stage, pats) in table.items() if lib.path_matches(path, pats)]


def classify_path(path, config, mapped=None):
    """Return (stage, [categories]) for one path.

    The highest matching stage wins. Kit patterns always apply; project patterns only add.
    Tests and docs are T0 instead of the path heuristics (an auth test is no auth code),
    but never below the kit or project categories.
    """
    mapped = project_categories(config) if mapped is None else mapped
    hits = _hits(path, STRONG) + _hits(path, mapped)
    if lib.path_matches(path, config.get("test_paths") or []):
        hits.append(("tests", "T0"))
    elif lib.path_matches(path, LOW["doku"][1]):
        hits.append(("doku", "T0"))
    else:
        hits += _hits(path, OTHER) or [("code", "T1")]
    stage = lib.stage_max(*[s for _, s in hits])
    return stage, sorted({n for n, s in hits if s == stage})


def classify(paths, config):
    """Return {'stage', 'categories', 'files'} for a set of changed paths."""
    mapped = project_categories(config)
    files = {p: classify_path(p, config, mapped) for p in paths if p}
    stage = lib.stage_max(*[s for s, _ in files.values()]) or "T0"
    categories = sorted({c for s, cats in files.values() if s == stage for c in cats})
    return {"stage": stage, "categories": categories,
            "files": {p: {"stage": s, "categories": c} for p, (s, c) in files.items()}}


def changed_paths(root, base, head="HEAD"):
    out = lib.git(["diff", "--name-only", "--no-renames", f"{base}...{head}"], cwd=root)
    return [line for line in out.splitlines() if line.strip()]


def set_stage_label(number, stage, labels, cwd=None, kind="pr"):
    remove = [s for s in lib.STAGES if s != stage and s in labels]
    args = [kind, "edit", str(number), "--add-label", stage]
    for label in remove:
        args += ["--remove-label", label]
    lib.gh(args, cwd=cwd)


def label_pr(number, config, root, base=None):
    info = lib.pr_info(number, cwd=root, fields="number,labels,files,body")
    paths = changed_paths(root, base) if base else info["files"]
    result = classify(paths, config)
    set_stage_label(number, result["stage"], info["labels"], cwd=root)
    for issue in lib.referenced_issues(info["body"]):
        data = lib.gh_json(["issue", "view", str(issue), "--json", "labels"], cwd=root) or {}
        names = [label.get("name") for label in data.get("labels") or []]
        set_stage_label(issue, result["stage"], names, cwd=root, kind="issue")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Risikostufe T0 bis T3 nach Pfaden")
    parser.add_argument("--base", help="Vergleichsstand, Standard origin/<main_branch>")
    parser.add_argument("--files", nargs="*", help="Pfade statt git diff")
    parser.add_argument("--label", type=int, metavar="PR", help="Stufe als Label an PR und Issues")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = lib.repo_root()
        base = args.base
        if base is None and args.files is None:
            base = f"origin/{lib.load_config(root)['main_branch']}"
        if base is None:
            config = lib.load_config(root)
        else:
            config, _ = lib.base_config(root, base)
        if args.label:
            result = label_pr(args.label, config, root, None if args.files is not None else base)
        else:
            paths = args.files if args.files is not None else changed_paths(root, base)
            result = classify(paths, config)
    except lib.FabrikError as exc:
        print(f"Risiko: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['stage']} ({', '.join(result['categories']) or 'keine Änderung'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
