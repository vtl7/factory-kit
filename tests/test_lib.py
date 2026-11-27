import unittest

from helpers import lib, verdict_comment


class YamlTest(unittest.TestCase):
    def test_nested_maps_lists_and_scalars(self):
        data = lib.load_yaml("""
# comment
check: pytest -q  # trailing comment
codes: [1, 2]
quoted: "a # not a comment"
single: 'it''s'
empty:
flags:
  on: true
risk:
  auth:
    - src/auth/
    - "src/login/"
  payment: [src/pay/]
list:
  - a
  - b
none: {}
""")
        self.assertEqual(data["check"], "pytest -q")
        self.assertEqual(data["codes"], [1, 2])
        self.assertEqual(data["quoted"], "a # not a comment")
        self.assertEqual(data["single"], "it's")
        self.assertIsNone(data["empty"])
        self.assertEqual(data["flags"], {"on": True})
        self.assertEqual(data["risk"], {"auth": ["src/auth/", "src/login/"], "payment": ["src/pay/"]})
        self.assertEqual(data["list"], ["a", "b"])
        self.assertEqual(data["none"], {})

    def test_escaped_double_quotes(self):
        self.assertEqual(lib.load_yaml('cmd: "say \\"hi\\""')["cmd"], 'say "hi"')

    def test_quoted_commas_and_hashes_stay_in_values(self):
        data = lib.load_yaml('a: ["x, y", \'z, #w\', plain]  # Kommentar\n'
                             'b:\n  - "sagt \\" # kein Kommentar"  # Kommentar\n'
                             'c: "Ende \\\\"  # Kommentar\n')
        self.assertEqual(data["a"], ["x, y", "z, #w", "plain"])
        self.assertEqual(data["b"], ['sagt " # kein Kommentar'])
        self.assertEqual(data["c"], "Ende \\")

    def test_apostrophe_inside_plain_value_is_no_quote(self):
        data = lib.load_yaml("p: [docs/owner's-guide.md, config/**]\ncheck: echo it's ok  # Kommentar\n"
                             "q: [docs/owner:'s-guide.md, config/**]  # Kommentar\n"
                             "r: [a 'b, c]\ns:\n  - docs/owner:'s-guide.md  # Kommentar\n")
        self.assertEqual(data["p"], ["docs/owner's-guide.md", "config/**"])
        self.assertEqual(data["q"], ["docs/owner:'s-guide.md", "config/**"])
        self.assertEqual(data["r"], ["a 'b", "c"])
        self.assertEqual(data["s"], ["docs/owner:'s-guide.md"])
        self.assertEqual(data["check"], "echo it's ok")

    def test_inline_map_is_rejected(self):
        with self.assertRaises(lib.FabrikError):
            lib.load_yaml("a: {b: 1}")

    def test_config_defaults_and_type_checks(self):
        config = lib.load_config(".", "check: make test\n")
        self.assertEqual(config["main_branch"], "main")
        self.assertEqual(config["test_fail_codes"], [1, 2])
        with self.assertRaises(lib.FabrikError):
            lib.load_config(".", "test_paths: tests/\n")


class PatternTest(unittest.TestCase):
    def test_gitignore_like_matching(self):
        m = lib.path_matches
        self.assertTrue(m("docs/a/b.md", ["docs/"]))
        self.assertTrue(m("sub/docs/x.md", ["docs/"]))
        self.assertTrue(m("README.md", ["*.md"]))
        self.assertTrue(m("a/b/README.md", ["*.md"]))
        self.assertFalse(m("srcx/auth/a.py", ["src/auth/"]))
        self.assertTrue(m("src/auth/a.py", ["src/auth/"]))
        self.assertTrue(m("app/migrations/0001.py", ["**/migrations/**"]))
        self.assertTrue(m("migrations/0001.py", ["**/migrations/**"]))
        self.assertTrue(m(".claude/settings.json", [".claude/"]))
        self.assertTrue(m("./tests/test_a.py", ["tests/"]))
        self.assertFalse(m("src/main.py", ["tests/", "*.md"]))


class MarkerTest(unittest.TestCase):
    def test_leading_legacy_verdict_survives_quoted_markers(self):
        legacy = verdict_comment("CHANGES", "s1")
        quoted = verdict_comment("APPROVE", "s1")
        bodies = [" \t\n" + legacy + "\n- Fund: " + quoted, "Beispiel:\n" + quoted]
        self.assertEqual(lib.final_verdict_for(bodies, "s1"), {"urteil": "CHANGES", "sha": "s1"})

    def test_latest_final_verdict_for_exact_commit(self):
        bodies = [verdict_comment("APPROVE", "s1"), verdict_comment("APPROVE", "s2", slice="2/2"),
                  verdict_comment("CHANGES", "s1", slice="1/2")]
        self.assertIsNone(lib.final_verdict_for(bodies, "s1"))
        bodies.append(verdict_comment("APPROVE", "s1", slice=None))
        self.assertEqual(lib.final_verdict_for(bodies, "s1")["urteil"], "APPROVE")
        self.assertEqual(lib.final_verdict_for(bodies, "s2")["slice"], "2/2")
        self.assertIsNone(lib.final_verdict_for(bodies, "s3"))
        for scope in ("1/2", "0/0", "2/1", "x/x", "", False):
            with self.subTest(scope=scope):
                self.assertIsNone(lib.final_verdict_for([verdict_comment("APPROVE", "s1", slice=scope)], "s1"))

    def test_verdict_for_exact_commit(self):
        bodies = [verdict_comment("CHANGES", "s1"), "noise", verdict_comment("APPROVE", "s2")]
        self.assertEqual(lib.verdict_for(bodies, "s2")["urteil"], "APPROVE")
        self.assertEqual(lib.verdict_for(bodies, "s1")["urteil"], "CHANGES")
        self.assertIsNone(lib.verdict_for(bodies, "s3"))

    def test_rounds_count_non_approve_since_reset(self):
        bodies = [verdict_comment("CHANGES", "1"), verdict_comment("ESCALATE", "2"),
                  verdict_comment("APPROVE", "3")]
        self.assertEqual(lib.rounds_used(bodies), 2)
        reset = lib.format_marker(lib.RESET_MARK, {"grund": "neu"})
        bodies.append("Beispiel:\n" + reset)
        self.assertEqual(lib.rounds_used(bodies), 2)
        bodies.append(" \n" + reset)
        bodies.append(verdict_comment("CHANGES", "4"))
        self.assertEqual(lib.rounds_used(bodies), 1)

    def test_broken_marker_is_ignored(self):
        self.assertEqual(lib.verdicts(["<!-- fabrik-urteil {kaputt} -->"]), [])


class GateStateTest(unittest.TestCase):
    def run_state(self, **check):
        return lib.gate_state([{"__typename": "CheckRun", "name": lib.GATE_CHECK_NAME, **check}])

    def test_states(self):
        self.assertEqual(self.run_state(status="COMPLETED", conclusion="SUCCESS"), "success")
        self.assertEqual(self.run_state(status="COMPLETED", conclusion="FAILURE"), "failure")
        self.assertEqual(self.run_state(status="IN_PROGRESS", conclusion=""), "pending")
        self.assertEqual(lib.gate_state([{"name": "other", "status": "COMPLETED",
                                          "conclusion": "SUCCESS"}]), "missing")
        self.assertEqual(lib.gate_state([{"__typename": "StatusContext",
                                          "context": lib.GATE_CHECK_NAME, "state": "SUCCESS"}]),
                         "success")


class TextTest(unittest.TestCase):
    def test_referenced_issues(self):
        self.assertEqual(lib.referenced_issues("Refs #12, und Refs: #3\nCloses #12"), [3, 12])
        self.assertEqual(lib.referenced_issues("PR ohne Bezug #x"), [])

    def test_behavior_change(self):
        self.assertTrue(lib.behavior_change("Verhaltensänderung: ja"))
        self.assertTrue(lib.behavior_change("**Verhaltensaenderung:** ja"))
        self.assertFalse(lib.behavior_change("Verhaltensänderung: nein"))

    def test_stage_max(self):
        self.assertEqual(lib.stage_max("T1", None, "T3", "T0"), "T3")
        self.assertEqual(lib.label_stage(["bug", "T2"]), "T2")
        self.assertIsNone(lib.label_stage(["bug"]))


if __name__ == "__main__":
    unittest.main()
