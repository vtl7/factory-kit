import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

from helpers import ROOT, RepoCase, git, lib

FABRIK = ROOT / "bin" / "fabrik"
FAKE_GH = """#!/bin/sh
echo "gh $*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "--version ") echo "gh version 9.9.9";;
  "auth status") echo "  - Token scopes: 'gist', 'repo', 'workflow'" >&2;;
  "repo view") echo "own/probe";;
  "api repos/{owner}/{repo}/actions/permissions/workflow")
    [ "$3" = "-X" ] || echo '{"default_workflow_permissions":"write","can_approve_pull_request_reviews":false}';;
esac
exit 0
"""


class InitCase(RepoCase):
    config = None

    def setUp(self):
        super().setUp()
        self.write("src/calc.py", "def add(a, b):\n    return a + b\n")
        self.write("tests/test_calc.py", "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
        self.write("ship.sh", "#!/bin/sh\necho shipped\n")
        self.write(".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                   "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                       {"type": "command", "command": "python3 other_guard.py"}]}]}}))
        self.write("AGENTS.md", "# Projektregeln\n\nEigener Text.\n")
        self.commit("project")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(FAKE_GH)
        (bindir / "gh").chmod(0o755)
        self.log = self.tmp / "gh.log"
        self.env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
                    "FAKE_GH_LOG": str(self.log),
                    "FABRIK_PARTNER_FILE": str(self.tmp / "partner.yml")}

    def fabrik(self, *args):
        done = subprocess.run([sys.executable, str(FABRIK), *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout + done.stderr

    def read(self, path):
        return (self.repo / path).read_text(encoding="utf-8")


class InitTest(InitCase):
    def test_init_creates_everything(self):
        code, out = self.fabrik("init", "--yes")
        self.assertEqual(code, 0, out)
        config = lib.load_config(self.repo)
        self.assertEqual(config["check"], "python3 -m pytest -q")
        self.assertEqual(config["test_cmd"], "python3 -m pytest -q {files}")
        self.assertEqual(config["ship"], "./ship.sh")
        self.assertEqual(config["test_paths"], ["tests/"])
        self.assertEqual(config["repos"], ["own/probe"])
        for name in ("auftrag", "bauen", "pruefen", "abnahme"):
            self.assertTrue((self.repo / f".claude/skills/{name}/SKILL.md").is_file())
        for script in ("build.py", "gate.py", "risk.py", "stage_hook.py", "review.py", "merge_audit.py",
                       "fabrik_lib.py", "usage.py", "VERSION", "known_limits.md"):
            self.assertTrue((self.repo / ".claude/fabrik" / script).is_file(), script)
        self.assertEqual(config["commit_trailers"], [])
        self.assertIn("Head-Commit", self.read(".claude/fabrik/known_limits.md"))
        workflow = self.read(".github/workflows/fabrik.yml")
        self.assertIn("branches: [main]", workflow)
        self.assertIn("fabrik-gate:", workflow)

    def test_version_shows_kit_and_installed_version(self):
        version = (ROOT / "kit" / "VERSION").read_text().strip()
        code, out = self.fabrik("version")
        self.assertEqual(code, 0, out)
        self.assertIn(f"Fabrik-Kit {version}", out)
        self.assertNotIn("Im Projekt eingebunden", out)
        self.fabrik("init", "--yes")
        code, out = self.fabrik("version")
        self.assertIn(f"Im Projekt eingebunden: {version}", out)
        self.assertNotIn("Abweichende Version", out)
        self.write(".claude/fabrik/VERSION", "0.0.1 (abc1234)\n")
        code, out = self.fabrik("version")
        self.assertIn("Abweichende Version", out)
        code, out = self.fabrik("--version")
        self.assertEqual(code, 0, out)
        self.assertIn(f"Fabrik-Kit {version}", out)

    def test_settings_are_merged_not_replaced(self):
        self.fabrik("init", "--yes")
        settings = json.loads(self.read(".claude/settings.json"))
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls)"]})
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertIn("python3 other_guard.py", commands)
        self.assertEqual(sum("stage_hook.py" in c for c in commands), 1)

    def test_agents_md_keeps_own_text(self):
        self.fabrik("init", "--yes")
        agents = self.read("AGENTS.md")
        self.assertTrue(agents.startswith("# Projektregeln\n\nEigener Text."))
        self.assertEqual(agents.count("<!-- fabrik:start -->"), 1)
        self.assertIn("## Architekturregeln", agents)
        self.assertIn("@AGENTS.md", self.read("CLAUDE.md"))

    def test_second_run_is_idempotent_and_keeps_config(self):
        self.fabrik("init", "--yes")
        self.write(".fabrik/fabrik.yml", self.read(".fabrik/fabrik.yml").replace(
            "protected_paths: []", "protected_paths:\n  - locked/"))
        self.write("AGENTS.md", self.read("AGENTS.md").replace("- (noch keine)", "- Schicht A vor B"))
        before = {p: self.read(p) for p in (".claude/settings.json", "AGENTS.md", ".fabrik/fabrik.yml")}
        code, out = self.fabrik("init", "--yes")
        self.assertEqual(code, 0, out)
        for path, text in before.items():
            self.assertEqual(self.read(path), text, path)
        self.assertIn("behalten", out)

    def test_github_labels_and_actions_permission(self):
        code, out = self.fabrik("init", "--yes")
        self.assertEqual(code, 0, out)
        log = self.log.read_text()
        for label in ("Wartet auf dich", "T3", "Vorfall", lib.LABEL_CLAUDE_ONLY, "nachts", "Dringend"):
            self.assertIn(f"gh label create {label} ", log)
        self.assertIn("default_workflow_permissions=write", log)
        self.assertIn("can_approve_pull_request_reviews=true", log)

    def test_no_github_skips_labels(self):
        self.fabrik("init", "--yes", "--no-github")
        self.assertNotIn("label create", self.log.read_text())

    def test_installed_hook_and_risk_run_in_project(self):
        self.fabrik("init", "--yes", "--no-github")
        hook = subprocess.run([sys.executable, ".claude/fabrik/stage_hook.py"], cwd=self.repo,
                              input=json.dumps({"tool_name": "Bash", "cwd": str(self.repo),
                                                "tool_input": {"command": "git push origin main"}}),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(json.loads(hook.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        risk = subprocess.run([sys.executable, ".claude/fabrik/risk.py", "--files", "src/auth/x.py"],
                              cwd=self.repo, capture_output=True, text=True, env=self.env)
        self.assertTrue(risk.stdout.startswith("T3"), risk.stdout + risk.stderr)

    def test_unknown_project_asks_nothing_with_yes_and_warns(self):
        (self.repo / "tests/test_calc.py").unlink()
        (self.repo / "tests").rmdir()
        code, out = self.fabrik("init", "--yes", "--no-github")
        self.assertEqual(code, 0, out)
        self.assertIn("'check' fehlt", out)


class SetupTest(InitCase):
    def test_setup_with_codex_partner(self):
        codex = self.tmp / "bin" / "codex"
        codex.write_text("#!/bin/sh\nexit 0\n")
        codex.chmod(0o755)
        code, out = self.fabrik("init", "--yes", "--no-github")
        self.assertEqual(code, 0, out)
        code, out = self.fabrik("setup", "--partner", "codex")
        self.assertEqual(code, 0, out)
        partner = lib.load_yaml((self.tmp / "partner.yml").read_text())
        self.assertEqual(partner["name"], "codex")
        self.assertTrue(all(partner.get(k) for k in ("bauen", "pruefen", "kontingent")))
        self.assertIn("gh angemeldet: ja", out)
        self.assertIn("Partner: codex", out)
        self.assertEqual(set(partner["modelle"]), {"standard", "tief"})
        self.assertEqual(partner["aufwand"], {"standard": "low", "tief": "medium"})
        self.assertNotIn("--ephemeral", partner["pruefen"])
        session = self.tmp / ".codex/sessions/2026/11/22/session.jsonl"
        session.parent.mkdir(parents=True)
        def measurement(timestamp, percent):
            return json.dumps({"timestamp": timestamp, "rate_limits": {
                "primary": {"used_percent": percent, "window_minutes": 300}
            }})

        stamp = "2026-11-22T16:24:34.642Z"
        session.write_text(measurement(stamp, 89) + "\n")
        older_measurement = session.with_name("older-measurement.jsonl")
        older_measurement.write_text(measurement("2026-11-22T16:20:00.000Z", 12))
        empty_session = session.with_name("no-measurement.jsonl")
        empty_session.write_text('{"timestamp":"2026-11-22T16:25:00.000Z"}\n')
        epoch = datetime(2026, 11, 22, 16, 24, 34, 642000, tzinfo=timezone.utc).timestamp()
        for offset, path in enumerate((session, older_measurement, empty_session), start=1):
            mtime = epoch + offset
            os.utime(path, (mtime, mtime))

        with mock.patch.dict(os.environ, HOME=str(self.tmp)):
            quota = lib.read_quota(partner["kontingent"], self.repo)
        self.assertEqual(quota["fenster_5h"]["prozent"], 89)
        self.assertEqual(quota["quelle_stand"], epoch)

    def test_setup_recommends_stages_without_overwriting_old_config_and_prints_cron(self):
        config = self.tmp / "partner.yml"
        old = "name: codex\nbauen: true-command\npruefen: sh\nkontingent: sh\n"
        config.write_text(old)
        self.assertEqual(self.fabrik("init", "--yes", "--no-github")[0], 0)
        code, out = self.fabrik("setup", "--partner", "codex")
        self.assertEqual(code, 0, out)
        for fragment in (str(config), "modelle:", "aufwand:", "standard:", "tief:", "{modell}", "{aufwand}"):
            self.assertIn(fragment, out)
        self.assertEqual(config.read_text(), old)
        self.assertIn("*/30 * * * *", out)
        self.assertIn(str(self.repo / ".claude/fabrik/usage.py") + " snapshot", out)

    def test_setup_without_partner(self):
        code, out = self.fabrik("setup", "--partner", "keiner")
        self.assertEqual(code, 0, out)
        self.assertIn("Partner: keiner", out)
        self.assertFalse((self.tmp / "partner.yml").exists())


if __name__ == "__main__":
    unittest.main()
