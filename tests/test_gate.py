import subprocess
import sys
import unittest

from helpers import RepoCase, CONFIG, KIT_SCRIPTS, git
import gate

FAILING_TEST = "import sys\nsys.path.insert(0, '.')\nfrom src.calc import double\nassert double(2) == 4\n"


class GateTest(RepoCase):
    def test_green_with_passing_check(self):
        self.write("src/a.py", "x = 1\n")
        self.commit("code")
        result = gate.run_gate(self.repo, "origin/main")
        self.assertTrue(result["green"], result["report"])
        self.assertIn("GATE: GRÜN", result["report"])
        self.assertIn("Stufe: T1", result["report"])

    def test_red_when_check_fails(self):
        git(self.repo, "checkout", "-q", "main")
        self.write(".fabrik/fabrik.yml", CONFIG.replace("sys.exit(0)", "sys.exit(3)"))
        self.commit("strict check")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "work2")
        self.write("src/a.py", "x = 1\n")
        self.commit("code")
        result = gate.run_gate(self.repo, "origin/main")
        self.assertFalse(result["green"])
        self.assertIn("Exitcode 3", result["report"])

    def test_check_command_comes_from_base(self):
        # The branch tries to replace a failing check with a passing one: the base wins.
        git(self.repo, "checkout", "-q", "main")
        self.write(".fabrik/fabrik.yml", CONFIG.replace("sys.exit(0)", "sys.exit(5)"))
        base_sha = self.commit("strict")
        git(self.repo, "checkout", "-q", "-b", "cheat")
        self.write(".fabrik/fabrik.yml", CONFIG)
        self.commit("weaken check")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        result = subprocess.run([sys.executable, KIT_SCRIPTS / "gate.py", "--base", base_sha],
                                cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Exitcode 5, Konfiguration aus Basis", result.stdout)

    def test_report_uses_risk_mapping_from_base(self):
        self.write(".fabrik/fabrik.yml", CONFIG + "risk:\n  daten_loeschen: [src/cleanup/]\n")
        base = self.commit("map cleanup to T3")
        self.write(".fabrik/fabrik.yml", CONFIG)
        self.write("src/cleanup/purge.py", "limit = 1\n")
        self.commit("change cleanup and remove mapping")
        result = gate.run_gate(self.repo, base)
        self.assertIn("Stufe: T3 (daten_loeschen)", result["report"])

    def test_missing_check_is_red(self):
        git(self.repo, "checkout", "-q", "main")
        self.write(".fabrik/fabrik.yml", "main_branch: main\n")
        self.commit("no check")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        result = gate.run_gate(self.repo, "origin/main")
        self.assertFalse(result["green"])
        self.assertIn("Prüfbefehl fehlt", result["report"])

    def test_protected_path_change_is_red_but_new_file_is_fine(self):
        git(self.repo, "checkout", "-q", "main")
        self.write("locked/schema.sql", "create table a;\n")
        self.commit("locked")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "w")
        self.write("locked/new.sql", "create table b;\n")
        self.commit("add")
        self.assertTrue(gate.run_gate(self.repo, "origin/main")["green"])
        self.write("locked/schema.sql", "drop table a;\n")
        self.commit("change")
        result = gate.run_gate(self.repo, "origin/main")
        self.assertFalse(result["green"])
        self.assertIn("- locked/schema.sql", result["report"])

    def test_red_green_probe_green(self):
        self.write("src/__init__.py", "")
        self.write("src/calc.py", "def double(x):\n    return x * 2\n")
        self.write("tests/test_calc.py", FAILING_TEST)
        self.commit("feature with test")
        result = gate.run_gate(self.repo, "origin/main", behavior_change=True)
        self.assertTrue(result["green"], result["report"])
        self.assertIn("Rot-Grün-Probe grün", result["report"])

    def test_red_green_probe_red_when_test_passes_on_old_code(self):
        git(self.repo, "checkout", "-q", "main")
        self.write("src/__init__.py", "")
        self.write("src/calc.py", "def double(x):\n    return x * 2\n")
        self.commit("feature exists")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "w")
        self.write("tests/test_calc.py", FAILING_TEST)
        self.commit("test only")
        result = gate.run_gate(self.repo, "origin/main", behavior_change=True)
        self.assertFalse(result["green"])
        self.assertIn("Tests auf altem Stand grün", result["report"])

    def test_behavior_change_without_tests_is_red(self):
        self.write("src/a.py", "x = 2\n")
        self.commit("code")
        result = gate.run_gate(self.repo, "origin/main", behavior_change=True)
        self.assertFalse(result["green"])
        self.assertIn("ohne neue oder geänderte Tests", result["report"])

    def test_large_slice_warns(self):
        self.write("src/big.py", "".join(f"x{i} = {i}\n" for i in range(200)))
        self.commit("big")
        report = gate.run_gate(self.repo, "origin/main")["report"]
        self.assertIn("Warnung: Scheibe mit 200", report)

    def test_slice_base_limits_probe_to_current_slice(self):
        self.write("src/__init__.py", "")
        self.write("src/calc.py", "def double(x):\n    return x * 2\n")
        self.write("tests/test_calc.py", FAILING_TEST)
        first = self.commit("slice 1")
        self.write("src/other.py", "y = 1\n")
        self.commit("slice 2 without test")
        result = gate.run_gate(self.repo, "origin/main", slice_base=first, behavior_change=True)
        self.assertFalse(result["green"])


if __name__ == "__main__":
    unittest.main()
