"""The kit stays general and the skills stay short."""

import os
import re
import subprocess
import unittest

from helpers import ROOT, lib

KIT = ROOT / "kit"
SKILLS = ("auftrag", "bauen", "pruefen", "abnahme")
PROJECT_WORDS = ("/Users/", "/home/")
# Private Begriffe (Namen, eigene Repos, Produkte) stehen nicht im Repo, sondern lokal in
# .fabrik-private-words (ein Begriff je Zeile, # für Kommentare) oder in FABRIK_PRIVATE_WORDS
# (durch Komma getrennt). Der Test prüft dann jede eingecheckte Datei außer LICENSE.
PRIVATE_WORDS_FILE = ROOT / ".fabrik-private-words"
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "anthropic.com")


class SkillTest(unittest.TestCase):
    def test_skills_have_frontmatter_and_at_most_about_40_instructions(self):
        for name in SKILLS:
            text = (KIT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith(f"---\nname: {name}\ndescription: "), name)
            steps = re.findall(r"^\s*(?:\d+\.|-) ", text.split("---", 2)[2], re.M)
            self.assertLessEqual(len(steps), 42, f"{name}: {len(steps)} Anweisungen")

    def test_models_budget_and_handover_in_skills(self):
        for name in ("auftrag", "bauen", "pruefen"):
            with self.subTest(name=name):
                text = (KIT / "skills" / name / "SKILL.md").read_text()
                for wording in ("70 %", "25 %", "10 Prozentpunkte", "auch nachts", "Exitcode 14", "Reset"):
                    self.assertIn(wording, text)
                self.assertNotIn("gpt-", text)
                if name != "pruefen":
                    self.assertIn("build.py budget", text)
                if name != "auftrag":
                    self.assertIn("`Stapel`", text)
                else:
                    self.assertIn("review.py design", text)
                    self.assertIn("`opus`", text)
        self.assertIn("--eskalation", (KIT / "skills/bauen/SKILL.md").read_text())
        self.assertIn("Modell für den Subagenten", (KIT / "skills/pruefen/SKILL.md").read_text())

    def test_skills_never_close_issues_on_merge(self):
        bauen = (KIT / "skills" / "bauen" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`Refs #N`, nie `Closes`", bauen)
        abnahme = (KIT / "skills" / "abnahme" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("gh issue close", abnahme)

    def test_review_without_partner_keeps_t2_t3_with_partner(self):
        text = (KIT / "skills" / "pruefen" / "SKILL.md").read_text(encoding="utf-8")
        step = next(line for line in text.splitlines() if line.startswith("3. Exitcode 3"))
        self.assertIn("Bei T2 oder T3 nicht ohne Partner prüfen", step)
        self.assertIn("Exitcode 14", step)

    def test_second_missing_verdict_follows_exitcode_three(self):
        text = (KIT / "skills/pruefen/SKILL.md").read_text(encoding="utf-8")
        step = next(line for line in text.splitlines() if line.startswith("5. Exitcode 6"))
        self.assertIn("Bei erneutem Exitcode 6 gelten die Regeln von Exitcode 3", step)
        self.assertIn("ausgegebene Risikostufe", step)
        self.assertNotIn("`prompt`", step)

    def test_claude_claims_escalation_after_budget_clearance(self):
        text = (KIT / "skills/bauen/SKILL.md").read_text(encoding="utf-8")
        step = next(line for line in text.splitlines() if line.startswith("11. Bei Exitcode 3"))
        self.assertLess(step.index("Sobald es frei ist"), step.index("build.py eskalation --issue N"))

    def test_build_commits_partner_work_before_claude_corrections(self):
        text = (KIT / "skills" / "bauen" / "SKILL.md").read_text(encoding="utf-8")
        for code in (0, 3):
            with self.subTest(code=code):
                step = next(line for line in text.splitlines() if f"Bei Exitcode {code} " in line)
                self.assertRegex(step, r"committe .*Gebaut-von.*[Dd]anach .*eigene.* ohne `Gebaut-von`")
                self.assertIn("commit_trailers", step)
                self.assertNotIn('-m "Gebaut-von', step)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = (KIT / "templates" / "fabrik-ci.yml").read_text(encoding="utf-8")
        sections = re.split(r"^([\w-]+):", self.workflow, flags=re.M)
        self.sections = dict(zip(sections[1::2], sections[2::2]))
        jobs = self.sections["jobs"]
        blocks = re.split(r"^  ([\w-]+):\n", jobs, flags=re.M)
        self.jobs = dict(zip(blocks[1::2], blocks[2::2]))

    def job_settings(self, job):
        return lib.load_yaml(job.split("    steps:\n", 1)[0])

    def assert_checkout_without_credentials(self, job):
        steps = job.split("\n      - ")[1:]
        checkouts = [step for step in steps if step.startswith("uses: actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        inputs = lib.load_yaml(checkouts[0].split("\n", 1)[1])["with"]
        self.assertIs(inputs["persist-credentials"], False)

    def test_workflow_permissions(self):
        permissions = lib.load_yaml("permissions:" + self.sections["permissions"])
        self.assertIn(permissions["permissions"], ({}, {"contents": "read"}))

    def test_jobs_running_gate_or_fabrik_bin_have_only_contents_read(self):
        for name, job in self.jobs.items():
            if re.search(r"\bgate\.py\b|\$FABRIK_BIN\b|\$\{FABRIK_BIN\}", job):
                with self.subTest(job=name):
                    self.assertEqual(self.job_settings(job)["permissions"], {"contents": "read"})

    def test_pr_jobs_use_event_base_commit(self):
        for name in ("fabrik-gate", "fabrik-risk-label"):
            with self.subTest(job=name):
                job = self.jobs[name]
                self.assertEqual(self.job_settings(job)["env"]["BASE_SHA"],
                                 "${{ github.event.pull_request.base.sha }}")
                self.assertNotIn("origin/$BASE_REF", job)
                self.assertIn('git archive "$BASE_SHA" .claude/fabrik | tar -x -C "$RUNNER_TEMP/fabrik"', job)

    def test_gate_has_read_only_checkout_and_no_token(self):
        gate = self.jobs["fabrik-gate"]
        self.assertEqual(self.job_settings(gate)["permissions"], {"contents": "read"})
        self.assert_checkout_without_credentials(gate)
        for reference in ("GH_TOKEN", "GH_REPO", "github.token", "secrets."):
            self.assertNotIn(reference.lower(), gate.lower())
            self.assertNotIn(reference.lower(), self.sections.get("env", "").lower())
        self.assertIn('git cat-file -e "$BASE_SHA:.claude/fabrik/gate.py"', gate)
        self.assertIn('python3 "$FABRIK_BIN/gate.py" --base "$BASE_SHA"', gate)
        self.assertNotIn("python3 .claude/fabrik/gate.py", gate)

    def test_label_runs_only_base_scripts_after_gate(self):
        job = self.jobs["fabrik-risk-label"]
        settings = self.job_settings(job)
        self.assertEqual(settings["needs"], "fabrik-gate")
        self.assertEqual(settings["if"], "always() && github.event_name == 'pull_request'")
        self.assertEqual(settings["permissions"],
                         {"contents": "read", "pull-requests": "write", "issues": "write"})
        self.assert_checkout_without_credentials(job)
        before, label = job.split("      - name: Risikostufe als Label\n")
        self.assertNotIn("github.token", before)
        self.assertIn("          GH_TOKEN: ${{ github.token }}", label)
        self.assertNotIn("\n      - ", label)
        self.assertNotRegex(job, r"gate\.py|\b(?:setup|check)\b")
        self.assertIn('git ls-tree --name-only "$BASE_SHA" -- .claude/fabrik/risk.py', label)
        self.assertIn('"$RUNNER_TEMP/fabrik/.claude/fabrik/risk.py" --base "$BASE_SHA" --label', label)
        self.assertNotIn("GITHUB_WORKSPACE", label)

    def test_merge_audit_keeps_write_permissions_on_push(self):
        job = self.jobs["fabrik-merge-audit"]
        settings = self.job_settings(job)
        self.assertEqual(settings["if"], "github.event_name == 'push'")
        self.assertEqual(settings["permissions"],
                         {"contents": "write", "pull-requests": "write", "issues": "write"})
        self.assertIn("python3 .claude/fabrik/merge_audit.py", job)

    def test_untrusted_text_only_via_env(self):
        commands = re.findall(r"^        run:.*(?:\n(?: {10}.*|[ \t]*$))*", self.workflow, re.M)
        self.assertTrue(commands)
        for command in commands:
            self.assertNotIn("${{", command)


class GeneralprobeBlockerTest(unittest.TestCase):
    """Befunde der Generalprobe vom 18. November, die Lieferungen blockierten."""

    def read(self, name):
        return (KIT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")

    def test_long_runs_are_awaited_in_the_same_turn(self):
        for name in ("bauen", "pruefen"):
            self.assertIn("derselben Runde", self.read(name), name)
            self.assertIn("zehn Minuten", self.read(name), name)

    def test_draft_pr_comes_with_the_first_slice(self):
        bauen = self.read("bauen")
        # Der PR muss stehen, bevor die erste Prüfung läuft: review.py braucht --pr.
        self.assertLess(bauen.index("gh pr create --draft"),
                        bauen.index("Dann prüfen mit dem Skill `pruefen`"))
        self.assertEqual(bauen.count("Skill `pruefen`"), 1)
        self.assertIn("gh pr ready", bauen)

    def test_issue_is_read_with_json(self):
        self.assertIn("gh issue view N --json title,body,comments", self.read("bauen"))


class ForeignToolsTest(unittest.TestCase):
    def test_kit_scripts_opt_out_of_project_formatters_and_linters(self):
        # Einkopierte Skripte dürfen den Prüfbefehl des Projekts nicht rot machen.
        for path in (KIT / "fabrik").glob("*.py"):
            head = path.read_text(encoding="utf-8").split('"""')[0]
            self.assertIn("# fmt: off", head, path.name)
            self.assertIn("# ruff: noqa", head, path.name)


def private_words():
    words = [w.strip() for w in os.environ.get("FABRIK_PRIVATE_WORDS", "").split(",")]
    if PRIVATE_WORDS_FILE.is_file():
        words += [line.split("#", 1)[0].strip()
                  for line in PRIVATE_WORDS_FILE.read_text(encoding="utf-8").splitlines()]
    return [w for w in words if w]


def tracked_files():
    done = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True)
    if done.returncode != 0:
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [ROOT / name for name in done.stdout.split("\0") if name and (ROOT / name).is_file()]


class PrivacyTest(unittest.TestCase):
    def test_private_words_file_is_ignored_by_git(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".fabrik-private-words", ignored)

    def test_no_private_words_anywhere(self):
        words = private_words()
        if not words:
            self.skipTest("keine private Wortliste (.fabrik-private-words oder FABRIK_PRIVATE_WORDS)")
        for path in tracked_files():
            if path.name == "LICENSE":
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            for word in words:
                line = text[:text.find(word.lower())].count("\n") + 1 if word.lower() in text else 0
                self.assertFalse(line, f"{path.relative_to(ROOT)}:{line}: privater Begriff")

    def test_no_personal_email_addresses(self):
        for path in tracked_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in EMAIL.finditer(text):
                domain = match.group(1).lower()
                self.assertTrue(domain in ALLOWED_EMAIL_DOMAINS or domain == "users.noreply.github.com"
                                and "github-actions" in text[max(0, match.start() - 20):match.end()],
                                f"{path.relative_to(ROOT)}: {match.group(0)}")


class VersionTest(unittest.TestCase):
    def test_version_is_semver(self):
        version = (KIT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_changelog_lists_current_version_first(self):
        version = (KIT / "VERSION").read_text(encoding="utf-8").strip()
        headings = re.findall(r"^## (\S+)", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
        self.assertEqual(headings[0], "Unveröffentlicht")
        self.assertEqual(headings[1], version, "kit/VERSION und neuester Eintrag in CHANGELOG.md")

    def test_license_is_mit(self):
        self.assertTrue((ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"))


class GeneralTest(unittest.TestCase):
    def test_no_project_specific_words_in_kit(self):
        for path in KIT.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                text = path.read_text(encoding="utf-8").lower()
                for word in PROJECT_WORDS:
                    self.assertNotIn(word.lower(), text, f"{path}: {word}")


if __name__ == "__main__":
    unittest.main()
