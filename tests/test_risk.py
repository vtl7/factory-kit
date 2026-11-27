import contextlib
import io
import unittest

from unittest import mock

from helpers import lib, FakeGh, RepoCase, git, pr_json
from helpers import CONFIG as REPO_CONFIG
import risk

CONFIG = lib.load_config(".", "test_paths:\n  - tests/\n")


def stage(*paths, config=CONFIG):
    return risk.classify(list(paths), config)["stage"]


class ClassifyTest(unittest.TestCase):
    def test_docs_and_tests_are_t0(self):
        self.assertEqual(stage("README.md", "docs/guide.md", "tests/test_a.py"), "T0")

    def test_code_is_t1(self):
        self.assertEqual(stage("src/app.py", "README.md"), "T1")

    def test_t2_categories(self):
        self.assertEqual(stage("app/migrations/0002_add.py"), "T2")
        self.assertEqual(stage("requirements.txt"), "T2")
        self.assertEqual(stage(".github/workflows/ci.yml"), "T2")
        self.assertEqual(stage(".fabrik/fabrik.yml"), "T2")
        self.assertEqual(stage(".claude/skills/bauen/SKILL.md"), "T2")  # kit wins over doku
        self.assertEqual(stage("AGENTS.md"), "T2")

    def test_t3_categories(self):
        self.assertEqual(stage("src/auth/login.py"), "T3")
        self.assertEqual(stage(".env.production"), "T3")
        self.assertEqual(stage("config/client_secret.json"), "T3")
        self.assertEqual(stage("app/billing/charge.py"), "T3")
        self.assertEqual(stage(".claude/settings.json"), "T3")

    def test_tests_win_over_path_heuristics(self):
        self.assertEqual(stage("tests/auth/test_login.py"), "T0")

    def test_empty_change_is_t0(self):
        self.assertEqual(stage(), "T0")

    def test_categories_of_highest_stage(self):
        result = risk.classify(["src/a.py", "requirements.txt"], CONFIG)
        self.assertEqual(result["categories"], ["abhaengigkeit"])

    def test_project_maps_paths_to_kit_category(self):
        config = lib.load_config(".", "risk:\n  daten_loeschen:\n    - src/cleanup/\n")
        self.assertEqual(stage("src/cleanup/purge.py", config=config), "T3")

    def test_project_mapping_wins_over_docs(self):
        config = lib.load_config(".", "risk:\n  auth_secrets: [docs/keys/]\n")
        self.assertEqual(stage("docs/keys/rotate.md", config=config), "T3")

    def test_extra_category_needs_stage(self):
        config = lib.load_config(".", "risk:\n  export: [src/export/]\n")
        with self.assertRaises(lib.FabrikError):
            risk.classify(["src/export/a.py"], config)
        config = lib.load_config(".", "risk:\n  export: [src/export/]\nextra_categories:\n  export: T2\n")
        self.assertEqual(stage("src/export/a.py", config=config), "T2")

    def test_kit_categories_cannot_be_removed(self):
        config = lib.load_config(".", "risk:\n  auth_secrets: []\n")
        self.assertEqual(stage("src/auth/x.py", config=config), "T3")
        config = lib.load_config(".", "risk:\n  kit: []\n  rechte: []\n")
        self.assertEqual(stage(".claude/fabrik/gate.py", config=config), "T2")
        self.assertEqual(stage(".claude/settings.json", config=config), "T3")

    def test_low_project_mapping_cannot_lower_stage(self):
        config = lib.load_config(".", "risk:\n  doku: [src/auth/, requirements.txt]\n")
        self.assertEqual(stage("src/auth/login.py", config=config), "T3")
        self.assertEqual(stage("requirements.txt", config=config), "T2")


class CliTest(RepoCase):
    def cli_stage(self, *args):
        with mock.patch.object(lib, "repo_root", return_value=self.repo):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(risk.main(list(args)), 0)
        return output.getvalue().split()[0]

    def test_pr_cannot_remove_its_own_t3_mapping(self):
        path = "src/cleanup/purge.py"
        self.write(lib.CONFIG_PATH, REPO_CONFIG + "risk:\n  daten_loeschen: [src/cleanup/]\n")
        self.write(path, "limit = 1\n")
        base = self.commit("map cleanup to T3")
        git(self.repo, "update-ref", "refs/remotes/origin/main", base)
        self.write(lib.CONFIG_PATH, REPO_CONFIG)
        self.write(path, "limit = 2\n")
        self.commit("change cleanup and remove mapping")

        for args in ((), ("--base", base), ("--base", base, "--files", path)):
            with self.subTest(args=args):
                self.assertEqual(self.cli_stage(*args), "T3")
        self.assertEqual(self.cli_stage("--files", path), "T1")

    def test_first_integration_uses_working_tree_config(self):
        (self.repo / lib.CONFIG_PATH).unlink()
        base = self.commit("before integration")
        self.write(lib.CONFIG_PATH, REPO_CONFIG + "risk:\n  daten_loeschen: [src/cleanup/]\n")
        self.write("src/cleanup/purge.py", "limit = 1\n")
        self.commit("introduce risk mapping")
        self.assertEqual(self.cli_stage("--base", base), "T3")

    def test_changed_paths_against_base(self):
        self.write("src/auth/x.py", "x = 1\n")
        self.commit("auth")
        self.assertEqual(risk.changed_paths(self.repo, "origin/main"), ["src/auth/x.py"])
        result = risk.classify(risk.changed_paths(self.repo, "origin/main"), lib.load_config(self.repo))
        self.assertEqual(result["stage"], "T3")


class LabelTest(RepoCase):
    config = REPO_CONFIG + "risk:\n  daten_loeschen: [src/cleanup/]\n"

    def test_label_pr_and_issue_without_reading_checks(self):
        base_sha = git(self.repo, "rev-parse", "origin/main").strip()
        self.write(lib.CONFIG_PATH, REPO_CONFIG)
        self.write("src/cleanup/purge.py", "limit = 1\n")
        self.commit("change cleanup and remove mapping")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        fake = (FakeGh().on(["pr", "view"], pr_json(labels=["T0", "bug"], body="Refs #3"))
                .on(["issue", "view"], {"labels": [{"name": "T1"}]}))
        with mock.patch.object(lib, "_run_impl", fake), mock.patch.object(lib, "repo_root", return_value=self.repo):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(risk.main(["--base", base_sha, "--label", "7"]), 0)
        view = fake.called("pr", "view")[0]
        self.assertNotIn("statusCheckRollup", view[view.index("--json") + 1])
        self.assertEqual(fake.called("pr", "edit")[0],
                         ["pr", "edit", "7", "--add-label", "T3", "--remove-label", "T0"])
        self.assertEqual(fake.called("issue", "edit")[0],
                         ["issue", "edit", "3", "--add-label", "T3", "--remove-label", "T1"])


if __name__ == "__main__":
    unittest.main()
