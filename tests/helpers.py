"""Test helpers: temporary git repos and a fake gh."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
KIT_SCRIPTS = ROOT / "kit" / "fabrik"
sys.path.insert(0, str(KIT_SCRIPTS))

import fabrik_lib as lib  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

CONFIG = """check: "python3 -c 'import sys; sys.exit(0)'"
test_cmd: "python3 {files}"
test_paths:
  - tests/
protected_paths:
  - locked/
main_branch: main
"""


def git(cwd, *args):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout


class RepoCase(unittest.TestCase):
    """A fresh git repo with an origin/main ref, config committed on main."""

    config = CONFIG

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fabrik-test-"))
        self.usage_log = self.tmp / "usage.jsonl"
        patcher = mock.patch.dict(os.environ, FABRIK_USAGE_LOG=str(self.usage_log))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        if self.config is not None:
            self.write(".fabrik/fabrik.yml", self.config)
        self.write("README.md", "start\n")
        self.commit("start")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "work")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def usage(self):
        return [json.loads(line) for line in self.usage_log.read_text().splitlines()]

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD").strip()


class FakeGh:
    """Replaces fabrik_lib._run_impl: answers gh calls, passes everything else through."""

    def __init__(self):
        self.calls = []
        self.handlers = []
        self.real = lib._run_impl

    def on(self, prefix, result):
        """result: str/dict/list (stdout as JSON), or callable(args) -> (code, out, err)."""
        self.handlers.append((list(prefix), result))
        return self

    def __call__(self, args, cwd=None, input_text=None, timeout=None):
        if args[0] != "gh":
            return self.real(args, cwd=cwd, input_text=input_text, timeout=timeout)
        rest = list(args[1:])
        self.calls.append(rest)
        for prefix, result in reversed(self.handlers):
            if rest[: len(prefix)] == prefix:
                if callable(result):
                    return result(rest)
                out = result if isinstance(result, str) else json.dumps(result)
                return 0, out, ""
        return 0, "", ""

    def called(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def pr_json(number=7, sha="a" * 40, labels=(), files=(), comments=(), checks=None, body="Refs #3"):
    if checks is None:
        checks = [{"__typename": "CheckRun", "name": lib.GATE_CHECK_NAME,
                   "status": "COMPLETED", "conclusion": "SUCCESS"}]
    return {"number": number, "headRefOid": sha, "headRefName": "work", "baseRefName": "main",
            "labels": [{"name": n} for n in labels], "files": [{"path": f} for f in files],
            "comments": [{"body": c} for c in comments], "statusCheckRollup": checks,
            "body": body, "state": "OPEN"}


def verdict_comment(verdict, sha="a" * 40, **extra):
    return lib.format_marker(lib.VERDICT_MARK, {"urteil": verdict, "sha": sha, **extra})


def quota_json(short=0, week=0, reset=None, week_reset=None):
    return json.dumps({"rate_limits": {
        "primary": {"used_percent": short, "window_minutes": 300, "resets_at": reset},
        "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": week_reset},
    }})
