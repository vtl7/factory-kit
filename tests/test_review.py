import io
import json
import os
import shlex
import subprocess
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import lib, FakeGh, RepoCase, pr_json, verdict_comment, git, quota_json
import review

APPROVE = json.dumps({"urteil": "APPROVE", "kriterien": [{"nr": 1, "satz": "Der Test belegt x."}],
                      "funde": [], "frage": ""})
CHANGES = '{"urteil": "CHANGES", "funde": ["src/a.py: Grenzfall fehlt"], "frage": ""}'


def prompt_id(prompt):
    example = next(line for line in prompt.splitlines() if line.startswith('{"urteil"'))
    return json.loads(example)["pruef_id"]


def signed_answer(prompt, answer=APPROVE):
    return json.dumps(dict(json.loads(answer), pruef_id=prompt_id(prompt)))


class ParseAnswerTest(unittest.TestCase):
    def test_last_json_object_wins(self):
        text = f"Erst dachte ich {APPROVE}\n...\nDann doch:\n{CHANGES}\n"
        answer = review.parse_answer(text)
        self.assertEqual(answer["urteil"], "CHANGES")
        self.assertEqual(answer["funde"], ["src/a.py: Grenzfall fehlt"])

    def test_fallback_without_valid_json(self):
        self.assertEqual(review.parse_answer('urteil: "ESCALATE" weil ...')["urteil"], "ESCALATE")

    def test_plain_verdict_after_json_replaces_all_fields(self):
        answer = review.parse_answer(CHANGES + '\nUrteil = "escalate"')
        self.assertEqual(answer, {"urteil": "ESCALATE", "kriterien": [], "funde": [], "frage": ""})
        self.assertEqual(review.parse_answer("urteil: ESCALATE\n" + APPROVE)["urteil"], "APPROVE")

    def test_nested_criteria_and_braces_in_text(self):
        criteria = [{"nr": 1, "satz": "Der Test prüft {x}."}, {"nr": "2a", "satz": "y fehlt."}]
        text = CHANGES + "\n" + json.dumps({"urteil": "changes", "kriterien": criteria})
        answer = review.parse_answer(text + '\n{"urteil":"unbekannt"}')
        self.assertEqual(answer["kriterien"], [{"nr": "1", "satz": "Der Test prüft {x}."}, criteria[1]])

    def test_closing_plain_verdict_with_markdown(self):
        for line in ("Prüfurteil: CHANGES", "**PRÜFURTEIL: changes**", "**Urteil:** **CHANGES**",
                     '**Prüfurteil** = "Changes"'):
            with self.subTest(line=line):
                answer = review.parse_answer(APPROVE + "\n" + line)
                self.assertEqual((answer["urteil"], answer["kriterien"]), ("CHANGES", []))
        self.assertIsNone(review.parse_answer(CHANGES + "\nUrteil: **APPROVE**"))

    def test_final_approve_requires_a_number_and_a_sentence(self):
        for criteria in (None, {}, [], ["OK"], [{"satz": "OK"}], [{"nr": 1, "satz": " "}],
                         [{"nr": None, "satz": "OK"}], [{"nr": "1", "satz": 5}]):
            with self.subTest(criteria=criteria):
                text = json.dumps({"urteil": "APPROVE", "kriterien": criteria})
                self.assertIsNone(review.parse_answer(CHANGES + "\n" + text))
        self.assertIsNone(review.parse_answer("urteil: APPROVE"))

    def test_approve_without_order_sheet_needs_no_criteria(self):
        for criteria in (None, []):
            with self.subTest(criteria=criteria):
                text = json.dumps({"urteil": "APPROVE", "kriterien": criteria})
                self.assertIsNone(review.parse_answer(text))
                answer = review.parse_answer(text, require_criteria=False)
                self.assertEqual((answer["urteil"], answer["kriterien"]), ("APPROVE", []))
        for text in ('Fertig.\n  {"urteil":"APPROVE"}  \n', '```json\n{"urteil":"APPROVE"}\n```',
                     'Geprüft.\n' + APPROVE + '\n\n'):
            with self.subTest(text=text):
                self.assertEqual(review.parse_answer(text, require_criteria=False)["urteil"], "APPROVE")
        answer = review.parse_answer(APPROVE + "\nPrüfurteil: CHANGES", require_criteria=False)
        self.assertEqual(answer["urteil"], "CHANGES")

    def test_no_verdict(self):
        self.assertIsNone(review.parse_answer("Sieht gut aus."))
        self.assertIsNone(review.parse_answer('{"urteil": "MAYBE"}'))


class ReviewCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)
        self.write("src/a.py", "x = 1\n")
        self.base = git(self.repo, "rev-parse", "origin/main").strip()
        self.sha = self.commit("code")
        self.issue_comments = []
        self.pr_comments = []
        self.pr_body = "Refs #3"
        self.base_ref = "main"
        self.commits = [{"oid": self.sha, "messageHeadline": "code", "messageBody": ""}]
        self.labels = []
        self.fake = (FakeGh()
                     .on(["pr", "view"], lambda a: (0, self.pr_view(a), ""))
                     .on(["issue", "view"], lambda a: (0, self.issue_view(a), ""))
                     .on(["pr", "diff"], "diff --git a/src/a.py b/src/a.py\n+x = 1\n")
                     .on(["pr", "comment"], lambda a: self.store(self.pr_comments, a))
                     .on(["issue", "comment"], lambda a: self.store(self.issue_comments, a)))
        self.partner = self.tmp / "partner.yml"
        patcher = mock.patch.dict(os.environ, {"FABRIK_PARTNER_FILE": str(self.partner),
                                               "CLAUDE_PROJECT_DIR": str(self.repo)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def pr_view(self, args):
        data = pr_json(sha=self.sha, comments=self.pr_comments, body=self.pr_body, labels=self.labels)
        data.update(baseRefName=self.base_ref, baseRefOid=self.base, commits=self.commits)
        return json.dumps({key: data[key] for key in args[-1].split(",")})

    def issue_view(self, args):
        if "title,body" in args:
            return json.dumps({"title": "Auftrag", "body": "Ziel: x"})
        return json.dumps({"comments": [{"body": b} for b in self.issue_comments],
                           "labels": [{"name": label} for label in self.labels]})

    @staticmethod
    def store(target, args):
        target.append(args[args.index("--body") + 1])
        return 0, "", ""

    def call(self, *argv):
        out = io.StringIO()
        with mock.patch.object(lib, "_run_impl", self.fake), redirect_stdout(out):
            code = review.main(list(argv))
        return code, out.getvalue()

    def set_partner(self, answer, rc=0):
        self.partner.write_text(
            f"name: testpartner\nbauen: 'true'\npruefen: cat > /dev/null; echo '{answer}'; exit {rc}\n"
            "kontingent: 'true'\n", encoding="utf-8")

    def base_limits(self, text):
        git(self.repo, "checkout", "main")
        self.write("kit/known_limits.md", text)
        self.base = self.commit("document limits")
        git(self.repo, "checkout", "work")
        git(self.repo, "rebase", "main")
        self.sha = git(self.repo, "rev-parse", "HEAD").strip()
        script = self.repo.resolve() / "kit/review.py"
        script.symlink_to(Path(review.__file__).resolve())
        return mock.patch.object(review, "__file__", str(script))


class ModelAndBudgetTest(ReviewCase):
    def set_staged_partner(self, quota, command=None):
        capture = self.tmp / "command"
        command = command or (f"printf '%s %s' {{modell}} {{aufwand}} > {shlex.quote(str(capture))}; "
                              f"cat >/dev/null; printf %s {shlex.quote(APPROVE)}")
        config = ("name: testpartner\nmodelle:\n  standard: einfach\n  tief: gründlich\n"
                  "aufwand:\n  standard: wenig\n  tief: mittel\n")
        self.partner.write_text(config + "pruefen: " + json.dumps(command, ensure_ascii=False) + "\n"
                                + "kontingent: " + json.dumps("printf %s " + shlex.quote(quota)) + "\n")
        return capture

    def test_partner_and_subagent_stage_at_all_risks(self):
        for path, stage, model, command in (
            ("notes.md", "standard", "sonnet", "einfach wenig"),
            ("source.py", "standard", "sonnet", "einfach wenig"),
            ("requirements.txt", "tief", "opus", "gründlich mittel"),
            ("auth/handler.py", "tief", "opus", "gründlich mittel"),
        ):
            with self.subTest(path=path):
                self.pr_comments.clear()
                self.issue_comments.clear()
                git(self.repo, "reset", "--hard", "origin/main")
                self.write(path, "change")
                self.sha = self.commit("change")
                self.commits = [{"oid": self.sha, "messageHeadline": "code", "messageBody": ""}]
                self.partner.unlink(missing_ok=True)
                code, out = self.call("run", "--pr", "7")
                self.assertEqual(code, 3, out)
                self.assertIn(f"Modell für den Subagenten: {model}", out)
                capture = self.set_staged_partner(quota_json())
                self.commits[0]["messageBody"] = "Gebaut-von: testpartner"
                code, out = self.call("run", "--pr", "7")
                self.assertEqual(code, 12, out)
                self.assertIn(f"Modell für den Subagenten: {model}", out)
                code, out = self.call("prompt", "--pr", "7")
                self.assertEqual(code, 0, out)
                self.assertIn(f"Modell für den Subagenten: {model}", out)
                self.commits[0]["messageBody"] = ""
                code, out = self.call("run", "--pr", "7")
                self.assertEqual(code, 0, out)
                self.assertIn(f"Stufe: {stage}", out)
                self.assertEqual(capture.read_text(), command)
                self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["stufe"], stage)

    def test_risk_label_raises_stage_for_partner_and_fallback(self):
        self.labels = ["T3"]
        capture = self.set_staged_partner(quota_json())
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 0, out)
        self.assertEqual(capture.read_text(), "gründlich mittel")

        self.pr_comments.clear()
        self.set_partner("Ohne Urteil")
        for _ in range(2):
            code, out = self.call("run", "--pr", "7")
            self.assertEqual(code, 6, out)
            self.assertIn("Risikostufe: T3", out)

        self.partner.unlink()
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 3, out)
        self.assertIn("Risikostufe: T3", out)
        self.assertIn("Modell für den Subagenten: opus", out)
        self.commits[0]["messageBody"] = "Gebaut-von: testpartner"
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 12, out)
        self.assertIn("Modell für den Subagenten: opus", out)

    def test_risk_uses_base_config(self):
        git(self.repo, "reset", "--hard", "origin/main")
        override = "extra_categories:\n  sensitive: T3\nrisk:\n  sensitive:\n    - src/\n"
        self.write(".fabrik/fabrik.yml", self.config + override)
        self.base = self.commit("base")
        self.write(".fabrik/fabrik.yml", self.config.replace("main_branch: main", "main_branch: current"))
        self.write("src/a.py", "new")
        self.sha = self.commit("head")
        with mock.patch.object(review.risk, "classify", wraps=review.risk.classify) as classify:
            code, prompt = self.call("prompt", "--pr", "7")
            self.assertEqual(code, 0, prompt)
            self.assertEqual(classify.call_args.args[1]["main_branch"], "main")

    def test_partner_quota_threshold_and_urgent(self):
        now = int(time.time())
        for quota, labels, expected in (
            (quota_json(89, 99, now + 600), [], 0),
            (quota_json(90, 1, now + 600), [], 14),
            (quota_json(90, 1, now + 600), ["Dringend"], 3),
            (quota_json(90, 99, now - 60), [], 0),
            (quota_json(0, 99), [], 0),
            ("unlesbar", [], 0),
        ):
            with self.subTest(quota=quota, labels=labels):
                self.labels = labels
                self.pr_comments.clear()
                self.issue_comments.clear()
                capture = self.set_staged_partner(quota)
                capture.unlink(missing_ok=True)
                code, output = self.call("run", "--pr", "7")
                self.assertEqual(code, expected, output)
                self.assertEqual(capture.exists(), not expected)
                if expected:
                    self.assertRegex(output, r"Partner am Limit bis \d{2}:\d{2}")
                    self.assertEqual(self.usage()[-1]["ereignis"], "bremse")
                    self.assertEqual(self.usage()[-1]["pr"], 7)
                else:
                    start, end = self.usage()[-2:]
                    self.assertEqual([start["ereignis"], end["ereignis"]], ["lauf_start", "lauf_ende"])
                    self.assertEqual(end["rolle"], "pruefen")
                    if quota == "unlesbar":
                        self.assertIn("unbekannt", start["grund"])
                if expected == 3:
                    self.assertIn("Modell für den Subagenten: sonnet", output)

    def test_design_partner_reads_input_and_uses_deep_stage(self):
        self.set_staged_partner(quota_json(), "echo {modell} {aufwand}; cat")
        with mock.patch("sys.stdin", io.StringIO("Entwurf aus stdin")):
            code, output = self.call("design", "--issue", "3")
        self.assertEqual(code, 0, output)
        self.assertIn("gründlich mittel\nEntwurf aus stdin", output)
        self.assertEqual([event["rolle"] for event in self.usage()], ["design", "design"])
        self.set_staged_partner(quota_json(90))
        with mock.patch("sys.stdin", io.StringIO("Entwurf")):
            self.assertEqual(self.call("design", "--issue", "3")[0], 14)
        self.labels = ["Dringend"]
        with mock.patch("sys.stdin", io.StringIO("Entwurf")):
            code, output = self.call("design", "--issue", "3")
        self.assertEqual(code, 3, output)
        self.assertIn("opus", output)
        self.partner.unlink()
        code, output = self.call("design")
        self.assertEqual(code, 3, output)
        self.assertIn("opus", output)

    def test_design_checks_character_limit_before_partner_start(self):
        self.set_staged_partner(quota_json(), "cat")
        with mock.patch("sys.stdin", io.StringIO("ä" * 139_999)):
            code, output = self.call("design")
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[-1], "ä" * 139_999)

        with mock.patch("sys.stdin", io.StringIO("ä" * 140_000)), mock.patch.object(
                review, "run_partner") as partner:
            code, output = self.call("design")
        self.assertEqual(code, 2, output)
        self.assertIn("140 000 Zeichen", output)
        partner.assert_not_called()

    def test_timeout_and_failure_use_quota_after_run(self):
        self.set_staged_partner(quota_json(), "false")
        free = lib.parse_quota(quota_json(89))
        limited = lib.parse_quota(quota_json(90))
        run = self.fake

        def run_with_failure(args, cwd=None, input_text=None, timeout=None):
            if input_text and args == ["sh", "-c", "false"]:
                if timed_out:
                    raise subprocess.TimeoutExpired(args, timeout)
                return 1, "", "Fehler beim Partner"
            return run(args, cwd=cwd, input_text=input_text, timeout=timeout)

        self.fake = run_with_failure
        for timed_out in (False, True):
            for urgent, final, expected in ((False, limited, 14), (True, limited, 3), (False, free, 3)):
                with self.subTest(timed_out=timed_out, urgent=urgent, expected=expected):
                    self.labels = ["Dringend"] if urgent else []
                    with mock.patch.object(lib, "read_quota", side_effect=[free, final]) as read:
                        code, output = self.call("run", "--pr", "7")
                    self.assertEqual(read.call_count, 2)
                    self.assertEqual(code, expected, output)
                    if final is limited:
                        self.assertIn("Partner am Limit", output)
                        self.assertEqual(self.usage()[-1]["ereignis"], "bremse")

    def test_review_limit_during_run_waits(self):
        self.set_staged_partner(quota_json(), "echo 'Usage limit reached' >&2; exit 1")
        code, output = self.call("run", "--pr", "7")
        self.assertEqual(code, 14, output)
        self.assertEqual([event["ereignis"] for event in self.usage()], ["lauf_start", "lauf_ende", "bremse"])


class CompactPromptTest(ReviewCase):
    def test_follow_up_has_delta_stat_and_findings(self):
        previous = self.sha
        self.set_partner(CHANGES)
        code, output = self.call("run", "--pr", "7")
        self.assertEqual(code, 10, output)
        verdict = lib.verdict_for(self.pr_comments, previous)
        self.assertEqual(verdict["merge_base"], self.base)
        self.assertRegex(verdict.get("blatt", ""), r"^[0-9a-f]{16}$")
        self.assertIsNone(verdict["ausstehend"])
        self.pr_comments[-1] += "\n- Voriger Fund"
        self.write("src/b.py", "fixed = True\n")
        self.sha = self.commit("fix")
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        self.assertIn(f"Änderung seit der Vorrunde (Commit {previous[:7]})", prompt)
        self.assertIn("- Voriger Fund", prompt)
        self.assertIn("+fixed = True", prompt)
        self.assertIn("src/a.py", prompt.split("Dateiliste des Gesamtdiffs")[1].split("###")[0])
        self.assertIn("jedem Abnahmekriterium", prompt)
        self.assertNotIn("+x = 1", prompt)

    def test_pending_controls_diff_for_partner_and_subagent(self):
        command = ("--pr", "7", "--slice", "1/2", "--pending")
        base_head = self.sha
        self.set_partner(CHANGES)
        self.write("src/b.py", "fixed = True\n")
        next_head = self.commit("fix")
        for mode in ("run", "prompt"):
            self.pr_comments.clear()
            self.issue_comments.clear()
            self.sha = base_head
            git(self.repo, "checkout", "--detach", self.sha)
            code, output = self.call(mode, *command, "Export")
            if mode == "prompt":
                self.assertEqual(code, 0, output)
                with mock.patch("sys.stdin", io.StringIO(signed_answer(output, CHANGES))):
                    code, output = self.call("record", "--pr", "7", "--reviewer", "claude")
            self.assertEqual(code, 10, output)
            marker = lib.verdict_for(self.pr_comments, base_head)
            self.sha = next_head
            git(self.repo, "checkout", "--detach", self.sha)
            for pending, legacy, full in (("Export", False, False), ("Vorschau", False, True),
                                          ("Export", True, True)):
                with self.subTest(mode=mode, pending=pending, legacy=legacy):
                    fields = dict(marker)
                    if legacy:
                        fields.pop("ausstehend", None)
                    self.pr_comments = [lib.format_marker(lib.VERDICT_MARK, fields)]
                    self.issue_comments.clear()
                    if mode == "run":
                        with mock.patch.object(lib, "run", wraps=lib.run) as calls:
                            code, output = self.call(mode, *command, pending)
                        self.assertEqual(code, 10, output)
                        prompt = next(call.kwargs["input_text"] for call in calls.call_args_list
                                      if call.args[0][0] == "sh")
                    else:
                        code, prompt = self.call(mode, *command, pending)
                    self.assertEqual(code, 10 if mode == "run" else 0, prompt)
                    self.assertEqual("+x = 1" in prompt, full)
                    self.assertEqual("Änderung seit der Vorrunde" in prompt, not full)
            self.assertEqual(marker["ausstehend"], "3664895579f0a7e6")

    def test_full_diff_when_review_scope_changes(self):
        self.set_partner(CHANGES)
        code, output = self.call("run", "--pr", "7", "--slice", "1/2", "--pending", "Export")
        self.assertEqual(code, 10, output)
        self.write("src/b.py", "fixed = True\n")
        self.sha = self.commit("fix")
        code, prompt = self.call("prompt", "--pr", "7", "--slice", "2/2")
        self.assertEqual(code, 0, prompt)
        self.assertIn("## Schlussprüfung", prompt)
        self.assertIn("+x = 1", prompt)
        self.assertNotIn("Änderung seit der Vorrunde", prompt)

    def test_full_diff_when_order_sheet_or_marker_changes(self):
        previous = self.sha
        self.set_partner(CHANGES)
        code, output = self.call("run", "--pr", "7")
        self.assertEqual(code, 10, output)
        marker = lib.verdict_for(self.pr_comments, previous)
        self.write("src/b.py", "fixed = True\n")
        self.sha = self.commit("fix")
        for case in ("Titel", "Text", "anderes Issue", "andere Stufe", "alter Marker"):
            with self.subTest(case=case):
                fields = dict(marker)
                self.pr_body = "Refs #4" if case == "anderes Issue" else "Refs #3"
                self.labels = ["T3"] if case == "andere Stufe" else []
                self.fake.on(["issue", "view", "3", "--json", "title,body"], {
                    "title": "Neuer Auftrag" if case == "Titel" else "Auftrag",
                    "body": "Ziel: y" if case == "Text" else "Ziel: x",
                })
                if case == "alter Marker":
                    del fields["blatt"]
                self.pr_comments = [lib.format_marker(lib.VERDICT_MARK, fields)]
                code, prompt = self.call("prompt", "--pr", "7")
                self.assertEqual(code, 0, prompt)
                self.assertIn("+x = 1", prompt)
                self.assertNotIn("Änderung seit der Vorrunde", prompt)

    def test_record_keeps_order_sheet_from_prompt(self):
        previous = self.sha
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        self.fake.on(["issue", "view", "3", "--json", "title,body"], {
            "title": "Auftrag", "body": "Ziel: y",
        })
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt, CHANGES))):
            self.assertEqual(self.call("record", "--pr", "7", "--reviewer", "claude")[0], 10)
        self.write("src/b.py", "fixed = True\n")
        self.sha = self.commit("fix")
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        self.assertEqual(lib.verdict_for(self.pr_comments, previous)["issue"], 3)
        self.assertIn("+x = 1", prompt)
        self.assertNotIn("Änderung seit der Vorrunde", prompt)

    def test_record_keeps_review_stage_from_prompt(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        self.assertIn("Modell für den Subagenten: sonnet", prompt)
        self.labels = ["T3"]
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt, CHANGES))):
            self.assertEqual(self.call("record", "--pr", "7", "--reviewer", "claude")[0], 10)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["stufe"], "standard")

    def test_full_diff_when_base_changed_or_is_unknown(self):
        old_base, previous = self.base, self.sha
        self.set_partner(CHANGES)
        code, output = self.call("run", "--pr", "7")
        self.assertEqual(code, 10, output)
        marker = lib.verdict_for(self.pr_comments, previous)
        self.write("src/b.py", "fixed = True\n")
        self.sha = self.commit("fix")
        for current_base, fields in (
            (previous, {"merge_base": old_base}),
            (old_base, {}),
            (old_base, {"diff_base": old_base}),
            (None, {"merge_base": old_base}),
        ):
            with self.subTest(current_base=current_base, fields=fields):
                self.base = current_base
                scoped = {key: marker[key] for key in ("issue", "slice", "stufe", "blatt", "ausstehend")}
                self.pr_comments = [verdict_comment("CHANGES", previous, **scoped, **fields)]
                code, prompt = self.call("prompt", "--pr", "7")
                self.assertEqual(code, 0, prompt)
                self.assertIn("+x = 1", prompt)
                self.assertNotIn("Änderung seit der Vorrunde", prompt)

    def test_full_diff_when_git_cannot_determine_merge_base(self):
        previous = self.sha
        self.write("src/b.py", "fixed = True\n")
        self.sha = self.commit("fix")
        self.pr_comments = [verdict_comment("CHANGES", previous, merge_base=self.base)]
        self.base = "b" * 40
        with mock.patch.object(lib, "_run_impl", self.fake):
            heading, _, diff = review.prompt_diff(review.Context(self.repo, 7))
        self.assertIn("## Diff", heading)
        self.assertIn("+x = 1", diff)

    def test_merge_base_changes_when_pr_merges_older_base_commit(self):
        original_base, previous = self.base, self.sha
        git(self.repo, "checkout", "main")
        self.write("deleted.txt", "wird im PR gelöscht\n")
        merged_base = self.commit("add file")
        self.write("later.txt", "später\n")
        self.base = self.commit("advance base")
        git(self.repo, "checkout", "work")
        self.set_partner(CHANGES)
        code, output = self.call("run", "--pr", "7")
        self.assertEqual(code, 10, output)
        self.assertEqual(lib.verdict_for(self.pr_comments, previous)["merge_base"], original_base)

        git(self.repo, "merge", "--no-edit", "-s", "ours", merged_base)
        self.sha = git(self.repo, "rev-parse", "HEAD").strip()
        self.fake.on(["pr", "diff"], git(self.repo, "diff", f"{self.base}...{self.sha}"))
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        self.assertIn("-wird im PR gelöscht", prompt)
        self.assertNotIn("Änderung seit der Vorrunde", prompt)

    def test_full_diff_if_prior_commit_missing_unrelated_or_approved(self):
        unrelated = self.sha
        self.set_partner(CHANGES)
        code, output = self.call("run", "--pr", "7")
        self.assertEqual(code, 10, output)
        marker = lib.verdict_for(self.pr_comments, unrelated)
        git(self.repo, "reset", "--hard", "origin/main")
        self.write("src/a.py", "rebased")
        self.sha = self.commit("new head")
        for verdict, previous in (("CHANGES", "b" * 40), ("ESCALATE", unrelated), ("APPROVE", self.base)):
            with self.subTest(verdict=verdict, previous=previous):
                fields = dict(marker, urteil=verdict, sha=previous)
                comment = lib.format_marker(lib.VERDICT_MARK, fields)
                self.pr_comments = [comment + "\n- Früherer Fund"]
                code, prompt = self.call("prompt", "--pr", "7")
                self.assertEqual(code, 0, prompt)
                self.assertIn("+x = 1", prompt)
                self.assertNotIn("Früherer Fund", prompt)

    def test_prompt_total_limit_reserves_room_for_criteria_and_gate(self):
        self.fake.on(["issue", "view", "3", "--json", "title,body"], {"title": "Auftrag", "body": "a" * 120_000})
        self.fake.on(["pr", "diff"], "d" * 200_000)
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)
        self.assertLess(len(prompt), 140_000)
        self.assertIn("[Diff gekürzt", prompt)
        self.assertIn("jedem Abnahmekriterium", prompt)
        self.assertIn("GATE: GRÜN", prompt)

    def test_gate_excludes_green_check_output_and_bounds_error_tail(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                git(self.repo, "reset", "--hard", "origin/main")
                suffix = "; false" if failure else ""
                check = """check: python3 -c 'print("Ab" + "deckung"); print("x" * 5000); print("Fehler" + "ende")'"""
                self.write(".fabrik/fabrik.yml", check + suffix + "\n")
                self.base = self.commit("base")
                self.write("notes.md", "note")
                self.sha = self.commit("head")
                code, prompt = self.call("prompt", "--pr", "7")
                self.assertEqual(code, 7 if failure else 0, prompt)
                self.assertIn("Stufe: T0", prompt)
                self.assertNotIn("Abdeckung", prompt)
                self.assertEqual("Fehlerende" in prompt, failure)
                self.assertLess(len(prompt), 6000)


class RunTest(ReviewCase):
    def test_no_partner_forwards_explicit_issue_to_both_commands(self):
        code, out = self.call("run", "--pr", "7", "--issue", "42", "--slice", "2/2")
        self.assertEqual(code, review.NO_PARTNER)
        for command in ("prompt", "record"):
            line = next(line for line in out.splitlines() if f"review.py {command}" in line)
            args = shlex.split(line)
            self.assertEqual(args[args.index("--issue") + 1], "42")

    def test_no_partner_tells_claude_route(self):
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, review.NO_PARTNER)
        self.assertIn("--reviewer claude ", out)

    def test_no_partner_preserves_quoted_scope_in_prompt_command(self):
        pending = "Export für 'große Dateien' & Vorschau"
        for options in (["--slice", "1/2", "--pending", pending], ["--slice", "2/2"], []):
            with self.subTest(options=options):
                code, out = self.call("run", "--pr", "7", *options)
                self.assertEqual(code, review.NO_PARTNER)
                command = next(line for line in out.splitlines() if "review.py prompt" in line)
                self.assertEqual(shlex.split(command), ["python3", ".claude/fabrik/review.py",
                                                        "prompt", "--pr", "7", *options])

    def test_intermediate_review_requires_pending_before_any_work(self):
        for command in ("run", "prompt"):
            for options in ([], ["--pending", ""], ["--pending", " \t"]):
                with self.subTest(command=command, options=options):
                    err = io.StringIO()
                    with mock.patch.object(lib, "repo_root") as repo_root, redirect_stderr(err), \
                            self.assertRaises(SystemExit) as raised:
                        self.call(command, "--pr", "7", "--slice", "1/2", *options)
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn("--pending", err.getvalue())
                    repo_root.assert_not_called()

    def test_partner_approve_is_recorded_on_pr_and_issue(self):
        self.set_partner(APPROVE)
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 0, out)
        verdict = lib.verdict_for(self.pr_comments, self.sha)
        self.assertEqual((verdict["urteil"], verdict["pruefer"], verdict["issue"]),
                         ("APPROVE", "testpartner", 3))
        self.assertEqual(len(self.issue_comments), 1)
        self.assertIsNone(verdict["slice"])
        self.assertIn("- Kriterium 1: Der Test belegt x.", self.pr_comments[-1])

    def test_partner_receives_and_records_slice(self):
        self.set_partner(CHANGES)
        with mock.patch.object(lib, "run", wraps=lib.run) as calls:
            code, out = self.call("run", "--pr", "7", "--slice", "1/2", "--pending", "Export")
        self.assertEqual(code, 10, out)
        prompt = next(call.kwargs["input_text"] for call in calls.call_args_list if call.args[0][0] == "sh")
        self.assertNotIn("pruef_id", prompt)
        self.assertIn("## Zwischenprüfung", prompt)
        self.assertIn("Scheibe 1 von 2", prompt)
        self.assertIn("Noch ausstehend: Export", prompt)
        for comments in (self.pr_comments, self.issue_comments):
            self.assertEqual(lib.verdict_for(comments, self.sha)["slice"], "1/2")
        self.assertIn("Scheibe 1/2", self.pr_comments[-1])

    def test_invalid_slice_is_a_usage_error(self):
        for command in ("run", "prompt"):
            for value in ("abc", "1", "1/2/3", "0/1", "1/0", "3/2", "-1/2"):
                with self.subTest(command=command, value=value):
                    err = io.StringIO()
                    with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                        self.call(command, "--pr", "7", "--slice", value)
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn("--slice", err.getvalue())
        self.assertFalse(self.fake.calls)

    def test_partner_records_approve_outside_closing_line_as_changes(self):
        answer = APPROVE + "\nIch kann die Freigabe nicht bestätigen."
        command = "cat >/dev/null; printf %s " + shlex.quote(answer)
        self.partner.write_text("name: testpartner\npruefen: " + json.dumps(command) + "\n",
                                encoding="utf-8")
        code, output = self.call("run", "--pr", "7")
        self.assertEqual(code, 10, output)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["urteil"], "CHANGES")
        self.assertIn("Das APPROVE steht nicht in der Schlusszeile", self.pr_comments[-1])

    def test_partner_approve_without_criteria_is_not_recorded(self):
        self.set_partner('{"urteil":"APPROVE"}')
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 6)
        self.assertIn("Kriteriumssatz", out)
        self.assertEqual(self.pr_comments, [])
        self.assertEqual(self.issue_comments, [])

    def test_partner_approve_without_order_sheet_is_recorded(self):
        self.pr_body = "Kleine Korrektur ohne Auftrag"
        self.set_partner('{"urteil":"APPROVE"}')
        with mock.patch.object(lib, "run", wraps=lib.run) as calls:
            code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 0, out)
        prompt = next(call.kwargs["input_text"] for call in calls.call_args_list if call.args[0][0] == "sh")
        self.assertIn("kein Auftragsblatt verknüpft", prompt)
        self.assertNotIn("zu jedem Abnahmekriterium", prompt)
        verdict = lib.verdict_for(self.pr_comments, self.sha)
        self.assertEqual((verdict["urteil"], verdict["issue"]), ("APPROVE", None))
        self.assertIsNone(verdict["blatt"])
        self.assertEqual(self.issue_comments, [])

    def test_partner_receives_final_slice_and_known_limits(self):
        self.set_partner(CHANGES)
        with self.base_limits("- Grenze aus der Basis"), mock.patch.object(lib, "run", wraps=lib.run) as calls:
            code, out = self.call("run", "--pr", "7", "--slice", "2/2")
        self.assertEqual(code, 10, out)
        prompt = next(call.kwargs["input_text"] for call in calls.call_args_list if call.args[0][0] == "sh")
        self.assertIn("## Schlussprüfung\nScheibe 2 von 2.", prompt)
        self.assertIn("Keine spätere Scheibe folgt", prompt)
        self.assertNotIn("Zwischenprüfung", prompt)
        self.assertIn("## Bekannte Grenzen des Kits", prompt)

    def test_same_commit_is_not_reviewed_twice(self):
        self.set_partner(APPROVE)
        self.call("run", "--pr", "7")
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, review.NO_NEW_DIFF)
        self.assertIn("Kein neuer Diff", out)

    def test_partner_failure_means_unavailable(self):
        self.set_partner(APPROVE, rc=1)
        self.assertEqual(self.call("run", "--pr", "7")[0], review.NO_PARTNER)

    def test_answer_without_verdict(self):
        self.set_partner("alles prima")
        self.assertEqual(self.call("run", "--pr", "7")[0], review.NO_VERDICT)
        self.assertEqual(self.pr_comments, [])

    def test_gate_red_means_no_review(self):
        git(self.repo, "checkout", "-q", "main")
        self.write(".fabrik/fabrik.yml", "check: 'false'\nmain_branch: main\n")
        self.base = self.commit("red check")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "w2")
        self.write("src/b.py", "y = 1\n")
        self.sha = self.commit("more")
        self.set_partner(APPROVE)
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, review.GATE_RED)
        self.assertIn("kein Modell prüft", out)

    def test_local_head_must_match_pr(self):
        self.set_partner(APPROVE)
        self.sha = "f" * 40
        self.assertEqual(self.call("run", "--pr", "7")[0], review.ERROR)


class BuilderReviewTest(ReviewCase):
    def add_commit(self, filename, message):
        self.write(filename, "x")
        self.sha = self.commit(message)
        headline, _, body = message.partition("\n\n")
        self.commits.append({"oid": self.sha, "messageHeadline": headline, "messageBody": body})

    def record_answer(self, reviewer):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            return self.call("record", "--pr", "7", "--reviewer", reviewer)

    def test_partner_in_earlier_commit_gets_full_subagent_command_even_at_t3(self):
        git(self.repo, "config", "trailer.separators", "=:")
        self.set_partner(APPROVE)
        self.labels.append("T3")
        self.add_commit("b", "build\n\nGebaut-von: Testpartner")
        self.add_commit("c", "fix")
        args = ["--pr", "7", "--issue", "42", "--slice", "1/2", "--pending", "Test für 'x'"]
        code, out = self.call("run", *args)
        self.assertEqual(code, 12, out)
        self.assertIn("Partner testpartner hat gebaut. Prüfung durch einen frischen Claude-Subagenten", out)
        command = next(line for line in out.splitlines() if "review.py prompt" in line)
        self.assertEqual(shlex.split(command), ["python3", ".claude/fabrik/review.py", "prompt", *args])
        self.assertFalse(self.pr_comments or self.issue_comments)

    def assert_pr_change_skips_partner(self, head, commits):
        self.set_partner(APPROVE)

        def change_pr(args):
            self.sha, self.commits = head, commits
            return 0, "+changed\n", ""

        self.fake.on(["pr", "diff"], change_pr)
        with mock.patch.object(lib, "run", wraps=lib.run) as calls:
            code, message = self.call("run", "--pr", "7")
        self.assertEqual(code, 13, message)
        self.assertIn("erneut prüfen", message)
        self.assertFalse(any(call.args[0][0] == "sh" for call in calls.call_args_list))
        self.assertFalse(self.pr_comments or self.issue_comments)
        self.assertFalse(self.fake.called("pr", "edit") or self.fake.called("issue", "edit"))

    def test_new_head_while_fetching_diff_skips_partner(self):
        self.assert_pr_change_skips_partner("f" * 40, self.commits)

    def test_new_partner_commit_while_fetching_diff_skips_partner(self):
        commit = {"oid": "b" * 40, "messageHeadline": "build", "messageBody": "Gebaut-von: testpartner"}
        self.assert_pr_change_skips_partner(self.sha, [*self.commits, commit])

    def test_no_verdict_if_partner_commit_arrives_during_review(self):
        self.set_partner(APPROVE)
        original_run = lib.run

        def run(args, **kwargs):
            if args[0] == "sh":
                self.add_commit("later", "build\n\nGebaut-von: testpartner")
            return original_run(args, **kwargs)

        with mock.patch.object(lib, "run", run):
            code, message = self.call("run", "--pr", "7")
        self.assertEqual(code, 2)
        self.assertIn("PR-Commit", message)
        self.assertFalse(self.pr_comments or self.issue_comments)

    def test_only_trailers_within_pr_identify_builder(self):
        self.set_partner(APPROVE)
        self.add_commit("base", "base\n\nGebaut-von: testpartner")
        self.base = self.sha
        self.commits.clear()
        self.add_commit("new", "slice\n\nNur ein Zitat: Gebaut-von: testpartner")
        self.assertEqual(self.call("run", "--pr", "7")[0], 0)

    def test_partner_marker_before_co_author_paragraph_routes_to_claude(self):
        self.set_partner(APPROVE)
        self.add_commit("b", "build\n\nGebaut-von: testpartner\n\nCo-Authored-By: X <x@y>")
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 12, out)
        self.assertIn("Partner testpartner hat gebaut", out)

    def test_trailer_alias_skips_partner(self):
        git(self.repo, "config", "trailer.gebaut-von.key", "Built-by")
        self.set_partner(APPROVE)
        self.add_commit("b", "build\n\nGebaut-von: testpartner")
        with mock.patch.object(lib, "run", wraps=lib.run) as calls:
            code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 12, out)
        self.assertFalse(any(call.args[0][0] == "sh" for call in calls.call_args_list))

    def test_older_partner_trailer_with_equals_sign(self):
        self.add_commit("b", "build\n\nHinweis zum Bau.\n  gEbAuT-vOn \t=\t Testpartner \n\n")
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 12, out)
        self.assertIn("testpartner", out)

    def test_record_refuses_each_named_builder_and_keeps_prompt(self):
        self.add_commit("b", "build\n\nGebaut-von: one\nGebaut-von: two")
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        for name in ("one", "TWO"):
            with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
                self.assertEqual(self.call("record", "--pr", "7", "--reviewer", name)[0], 2)
            self.assertFalse(self.pr_comments or self.issue_comments)
            self.assertFalse(self.fake.called("pr", "edit") or self.fake.called("issue", "edit"))
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            self.assertEqual(self.call("record", "--pr", "7", "--reviewer", "claude")[0], 0)
        for kind in ("pr", "issue"):
            self.assertIn(lib.LABEL_CLAUDE_ONLY, self.fake.called(kind, "edit")[0])

    def test_cross_review_removes_previous_claude_only_label(self):
        self.labels.append(lib.LABEL_CLAUDE_ONLY)
        self.base = self.sha
        self.commits.clear()
        self.add_commit("b", "build\n\nGebaut-von: testpartner")
        self.assertEqual(self.record_answer("claude")[0], 0)
        for kind in ("pr", "issue"):
            self.assertEqual(self.fake.called(kind, "edit")[0][-2:],
                             ["--remove-label", lib.LABEL_CLAUDE_ONLY])

    def test_entirely_partner_built_is_cross_review_for_both_claude_names(self):
        self.base = self.sha
        self.commits.clear()
        self.add_commit("b", "build\n\nGebaut-von: one")
        self.add_commit("c", "fix\n\nGebaut-von: two")
        for name in ("claude-allein", "claude"):
            self.pr_comments.clear()
            code, out = self.record_answer(name)
            self.assertEqual(code, 0, out)
            self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["pruefer"], "claude")
            self.assertIn("(claude, Commit", self.pr_comments[0])
            self.assertFalse(self.fake.called("pr", "edit") or self.fake.called("issue", "edit"))


    def test_any_partner_trailer_routes_to_claude_with_or_without_partner_file(self):
        self.add_commit("b", "build\n\nGebaut-von: codex-alt")
        for configured in (False, True):
            with self.subTest(configured=configured):
                if configured:
                    self.set_partner(APPROVE)
                code, out = self.call("run", "--pr", "7")
                self.assertEqual(code, 12, out)
                self.assertIn("codex-alt", out)

    def test_record_refuses_unrelated_partner_name(self):
        self.commits = [{"messageHeadline": "build", "messageBody": "Gebaut-von: codex-alt"}]
        self.assertEqual(self.record_answer("codex")[0], 2)
        self.assertFalse(self.pr_comments or self.issue_comments)

    def test_commits_come_from_github_without_local_history(self):
        self.set_partner(APPROVE)
        self.commits = [{"messageHeadline": "remote build",
                         "messageBody": "Bericht.\n---\nWeitere Details.\n\nGebaut-von: testpartner"}]
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 12, out)
        self.assertTrue(self.fake.called("pr", "view", "7", "--json", "headRefOid,commits"))

    def test_partner_on_later_commit_page(self):
        self.commits = [{"messageHeadline": "slice", "messageBody": "", "oid": f"{n:040x}"}
                        for n in range(100)]
        last = {"messageHeadline": "last slice", "messageBody": "Gebaut-von: codex-alt"}
        pages = []
        for commits in (self.commits, [last]):
            connection = {"nodes": [{"commit": commit} for commit in commits]}
            pages.append({"data": {"repository": {"pullRequest": {"commits": connection}}}})
        self.fake.on(["api", "graphql", "--paginate", "--slurp"], pages)
        code, out = self.call("run", "--pr", "7")
        self.assertEqual(code, 12, out)
        self.assertIn("codex-alt", out)


class RoundsTest(ReviewCase):
    def new_commit(self, n):
        self.write(f"src/f{n}.py", f"v = {n}\n")
        self.sha = self.commit(f"fix {n}")

    def test_three_rounds_then_stuck_across_prs(self):
        self.set_partner(CHANGES)
        self.assertEqual(self.call("run", "--pr", "7")[0], 10)
        self.new_commit(1)
        self.pr_comments = []  # a restart with a new PR: rounds still count on the issue
        self.assertEqual(self.call("run", "--pr", "8")[0], 10)
        self.new_commit(2)
        code, out = self.call("run", "--pr", "8", "--slice", "1/2", "--pending", "Export")
        self.assertEqual(code, review.STUCK)
        self.assertIn("FESTGEFAHREN", out)
        labels = [c for c in self.fake.called("issue", "edit") if lib.LABEL_STUCK in c]
        self.assertTrue(labels)
        self.new_commit(3)
        self.assertEqual(self.call("run", "--pr", "8")[0], review.STUCK)

    def test_reset_with_reason_opens_new_rounds(self):
        self.issue_comments = [verdict_comment("CHANGES", str(i)) for i in range(3)]
        self.set_partner(APPROVE)
        self.assertEqual(self.call("run", "--pr", "7")[0], review.STUCK)
        self.call("reset", "--issue", "3", "--reason", "Product Owner hat Schnittstelle festgelegt")
        self.assertEqual(self.call("run", "--pr", "7")[0], 0)

    def test_escalate_waits_for_owner(self):
        self.set_partner('{"urteil": "ESCALATE", "funde": [], "frage": "A oder B?"}')
        self.assertEqual(self.call("run", "--pr", "7")[0], 11)
        self.assertTrue([c for c in self.fake.called("issue", "edit") if lib.LABEL_WAITING in c])


class ClaudeOnlyTest(ReviewCase):
    def test_record_label_failure_leaves_verdict_unpublished_and_retryable(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        args = ("record", "--pr", "7", "--reviewer", "claude")
        for kind in ("pr", "issue"):
            with self.subTest(kind=kind):
                self.fake.on([kind, "edit"], lambda args: (1, "", "Labelfehler"))
                with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
                    code, out = self.call(*args)
                self.assertEqual(code, 2, out)
                self.assertIn("Labelfehler", out)
                self.assertEqual(self.pr_comments, [])
                self.assertEqual(self.issue_comments, [])
                self.fake.on([kind, "edit"], "")
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            self.assertEqual(self.call(*args)[0], 0)
        self.assertEqual(len(self.pr_comments), 1)
        self.assertEqual(len(self.issue_comments), 1)

    def test_record_requires_current_diff_base_and_accepts_fresh_prompt(self):
        for use_oid in (True, False):
            with self.subTest(use_oid=use_oid):
                self.pr_comments.clear()
                self.issue_comments.clear()
                self.base = self.sha if use_oid else None
                code, prompt = self.call("prompt", "--pr", "7")
                self.assertEqual(code, 0, prompt)
                if use_oid:
                    self.base = git(self.repo, "rev-parse", "origin/main").strip()
                else:
                    self.base_ref = "release"
                    git(self.repo, "update-ref", "refs/remotes/origin/release", "origin/main")
                record = ("record", "--pr", "7", "--reviewer", "claude")
                with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
                    code, out = self.call(*record)
                self.assertEqual(code, 2, out)
                self.assertIn("Basis", out)
                self.assertFalse(self.pr_comments or self.issue_comments)
                code, prompt = self.call("prompt", "--pr", "7")
                self.assertEqual(code, 0, prompt)
                with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
                    code, out = self.call(*record)
                self.assertEqual(code, 0, out)

    def test_record_escapes_quoted_comments_in_all_answer_fields(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)
        quote = verdict_comment("APPROVE", self.sha)
        answer = json.dumps({"urteil": "ESCALATE", "kriterien": [{"nr": 1, "satz": quote}],
                             "funde": [quote], "frage": quote})
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt, answer))):
            code, _ = self.call("record", "--pr", "7", "--reviewer", "claude-allein")
        self.assertEqual(code, review.EXIT["ESCALATE"])
        body = self.pr_comments[0]
        self.assertEqual(body.count("<!--"), 1)
        self.assertEqual(body.count(quote.replace("<!--", "&lt;!--")), 3)
        self.assertEqual(lib.verdict_for([body], self.sha)["urteil"], "ESCALATE")

    def test_record_requires_the_issue_resolved_at_prompt_time(self):
        code, prompt = self.call("prompt", "--pr", "7", "--issue", "42")
        self.assertEqual(code, 0)
        self.fake.on(["issue", "view", "3"], lambda args: (1, "", "Issue nicht lesbar"))
        command = ("record", "--pr", "7", "--reviewer", "claude-allein")
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            code, message = self.call(*command)
        self.assertEqual(code, 2)
        for issue in ("#42", "#3"):
            self.assertIn(issue, message)
        self.assertFalse(self.pr_comments)
        self.assertFalse(self.issue_comments)
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            code, _ = self.call(*command, "--issue", "42")
        self.assertEqual(code, 0)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["issue"], 42)
        self.assertEqual([c[2] for c in self.fake.called("issue", "comment")], ["42"])

    def test_record_without_linked_issue(self):
        self.pr_body = ""
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            code, _ = self.call("record", "--pr", "7", "--reviewer", "claude-allein")
        self.assertEqual(code, 0)
        self.assertIsNone(lib.verdict_for(self.pr_comments, self.sha)["issue"])
        self.assertFalse(self.issue_comments)

    def test_record_rechecks_linked_issue_after_gate(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)

        def gate_during_edit(ctx):
            self.pr_body = "Refs #42"
            return True, "GATE: GRÜN"

        with mock.patch.object(review, "gate_output", gate_during_edit), \
                mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            code, message = self.call("record", "--pr", "7", "--reviewer", "claude-allein")
        self.assertEqual(code, 2)
        for issue in ("#3", "#42"):
            self.assertIn(issue, message)
        self.assertFalse(self.pr_comments)
        self.assertFalse(self.issue_comments)

    def test_record_with_correct_id_and_no_verdict(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)
        example = next(line for line in prompt.splitlines() if line.startswith('{"urteil"'))
        no_verdict = signed_answer(prompt, '{}')
        for answer in (no_verdict, signed_answer(prompt, '{"urteil":"offen"}'), example,
                       "Vorläufiges Urteil: CHANGES\n" + no_verdict,
                       signed_answer(prompt) + "\n" + no_verdict):
            with self.subTest(answer=answer), mock.patch("sys.stdin", io.StringIO(answer)):
                code, message = self.call("record", "--pr", "7", "--reviewer", "claude-allein")
                self.assertEqual(code, 6)
                self.assertIn("ohne gültiges Urteil", message)
                self.assertFalse(self.pr_comments)
                self.assertFalse(self.issue_comments)

    def test_only_latest_prompt_id_can_record(self):
        command = ("prompt", "--pr", "7", "--slice", "1/2", "--pending", "Export")
        code, old = self.call(*command)
        self.assertEqual(code, 0)
        code, current = self.call(*command)
        self.assertEqual(code, 0)
        self.assertNotEqual(prompt_id(old), prompt_id(current))
        record = ("record", "--pr", "7", "--reviewer", "claude-allein")
        for answer in (signed_answer(old), APPROVE):
            with self.subTest(answer=answer), mock.patch("sys.stdin", io.StringIO(answer)):
                code, message = self.call(*record)
                self.assertEqual(code, 2)
                self.assertIn("Prüf-ID", message)
                self.assertFalse(self.pr_comments)
                self.assertFalse(self.issue_comments)
        with mock.patch("sys.stdin", io.StringIO(signed_answer(current))):
            self.assertEqual(self.call(*record)[0], 0)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["slice"], "1/2")

    def test_prompt_example_is_never_a_verdict(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)
        example = next(line for line in prompt.splitlines() if line.startswith('{"urteil"'))
        self.assertIsNone(review.parse_answer(example))
        self.assertEqual(review.parse_answer(CHANGES + "\n" + example)["funde"],
                         ["src/a.py: Grenzfall fehlt"])

    def test_record_treats_signed_approve_outside_closing_line_as_changes(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        answer = signed_answer(prompt) + "\nIch kann die Freigabe nicht bestätigen."
        with mock.patch("sys.stdin", io.StringIO(answer)):
            code, output = self.call("record", "--pr", "7", "--reviewer", "claude")
        self.assertEqual(code, 10, output)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["urteil"], "CHANGES")
        self.assertIn("Das APPROVE steht nicht in der Schlusszeile", self.pr_comments[-1])

    def test_record_accepts_plain_changes_after_signed_approve(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        answer = signed_answer(prompt) + "\nUrteil: CHANGES"
        with mock.patch("sys.stdin", io.StringIO(answer)):
            code, output = self.call("record", "--pr", "7", "--reviewer", "claude")
        self.assertEqual(code, 10, output)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["urteil"], "CHANGES")

    def test_record_treats_signed_approve_before_plain_approve_as_changes(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt) + "\nurteil: APPROVE")):
            code, output = self.call("record", "--pr", "7", "--reviewer", "claude-allein")
        self.assertEqual(code, 10, output)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["urteil"], "CHANGES")
        self.assertIn("Das APPROVE steht nicht in der Schlusszeile", self.pr_comments[-1])

    def test_prompt_preserves_open_scope_for_record(self):
        command = ("prompt", "--pr", "7")
        first = ("--slice", "1/3", "--pending", "Export")
        code, prompt = self.call(*command, *first)
        self.assertEqual(code, 0)
        stamp = self.repo / ".git" / "fabrik" / "prompt-7.sha"
        original = stamp.read_text(encoding="utf-8")
        self.assertIn(prompt_id(prompt), original)
        for options in ([], ["--slice", "2/3", "--pending", "Export"], ["--slice", "3/3"]):
            with self.subTest(options=options):
                code, message = self.call(*command, *options)
                self.assertEqual(code, 2)
                for hint in ("record", "löschen", str(stamp.resolve())):
                    self.assertIn(hint, message)
                self.assertEqual(stamp.read_text(encoding="utf-8"), original)
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            self.assertEqual(self.call("record", "--pr", "7", "--reviewer", "claude-allein")[0], 0)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["slice"], "1/3")

    def test_new_commit_can_replace_an_open_unsliced_stamp(self):
        self.assertEqual(self.call("prompt", "--pr", "7")[0], 0)
        command = ("prompt", "--pr", "7", "--slice", "1/2", "--pending", "Export")
        self.assertEqual(self.call(*command)[0], 2)
        self.write("src/new.py", "x = 2\n")
        self.sha = self.commit("next")
        self.assertEqual(self.call(*command)[0], 0)

    def test_record_rejects_legacy_stamp_and_changed_scope(self):
        command = ("prompt", "--pr", "7", "--slice", "1/2", "--pending", "Export")
        code, prompt = self.call(*command)
        self.assertEqual(code, 0)
        stamp = self.repo / ".git" / "fabrik" / "prompt-7.sha"
        for content, missing in ((f"{self.sha}\n1/2\n", "Prüf-ID"),
                                 (f"{self.sha}\n2/2\n{prompt_id(prompt)}\n", "Prüf-ID"),
                                 (f"{self.sha}\n1/2\n{prompt_id(prompt)}\n", "Issue-Angabe"),
                                 (f"{self.sha}\n1/2\n{prompt_id(prompt)}\n#3\n", "Basis"),
                                 (f"{self.sha}\n1/2\n{prompt_id(prompt)}\n#3\n{self.base}\n",
                                  "Auftragsblatt")):
            with self.subTest(content=content):
                stamp.write_text(content, encoding="utf-8")
                with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
                    code, message = self.call("record", "--pr", "7", "--reviewer", "claude-allein")
                self.assertEqual(code, 2)
                self.assertIn(missing, message)
                self.assertFalse(self.pr_comments)
                self.assertFalse(self.issue_comments)

    def test_record_rechecks_pr_head_after_gate(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)

        def gate_during_push(ctx):
            self.sha = "f" * 40
            return True, "GATE: GRÜN"

        with mock.patch.object(review, "gate_output", gate_during_push), \
                mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
            code, message = self.call("record", "--pr", "7", "--reviewer", "claude-allein")
        self.assertEqual(code, 2)
        self.assertIn("PR-Commit", message)
        self.assertFalse(self.pr_comments)
        self.assertFalse(self.issue_comments)

    def test_concurrent_prompts_cannot_both_claim_different_slices(self):
        command = ("prompt", "--pr", "7", "--pending", "Export")

        def gate_with_second_prompt(ctx):
            self.assertEqual(self.call(*command, "--slice", "2/3")[0], 2)
            return True, "GATE: GRÜN"

        with mock.patch.object(review, "gate_output", gate_with_second_prompt):
            self.assertEqual(self.call(*command, "--slice", "1/3")[0], 0)
        self.assertEqual(self.call(*command, "--slice", "2/3")[0], 2)

    def test_prompt_allows_deferred_evidence_in_every_scope(self):
        answer = json.dumps({"urteil": "APPROVE", "kriterien": [{"nr": 1, "satz": "offen bis zur Abnahme"}]})
        for options in ([], ["--slice", "1/2", "--pending", "Export"], ["--slice", "2/2"]):
            with self.subTest(options=options):
                self.pr_comments.clear()
                code, prompt = self.call("prompt", "--pr", "7", *options)
                self.assertEqual(code, 0, prompt)
                for wording in ("Belegvertrag", "nach dem Merge", "Verify", "Product Owner",
                                "„offen bis zur Abnahme“", "Das ist kein Fund"):
                    self.assertIn(wording, prompt)
                with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt, answer))):
                    self.assertEqual(self.call("record", "--pr", "7", "--reviewer", "claude-allein")[0], 0)
                self.assertIn("- Kriterium 1: offen bis zur Abnahme", self.pr_comments[-1])

    def test_prompt_then_record(self):
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)
        self.assertIn("Ziel: x", prompt)
        self.assertIn("GATE: GRÜN", prompt)
        self.assertIn("+x = 1", prompt)
        self.assertNotIn("Zwischenprüfung", prompt)
        answer = self.tmp / "answer.txt"
        answer.write_text("Geprüft.\n" + signed_answer(prompt), encoding="utf-8")
        code, _ = self.call("record", "--pr", "7", "--reviewer", "claude-allein", "--file", str(answer))
        self.assertEqual(code, 0)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["pruefer"], "claude")
        labelled = [c for c in self.fake.called("pr", "edit") if lib.LABEL_CLAUDE_ONLY in c]
        self.assertTrue(labelled)

    def test_slice_survives_prompt_and_record(self):
        for value in ("1/2", "2/2"):
            with self.subTest(value=value):
                self.pr_comments.clear()
                code, prompt = self.call("prompt", "--pr", "7", "--slice", value, "--pending", "Export")
                self.assertEqual(code, 0, prompt)
                self.assertEqual("## Zwischenprüfung" in prompt, value == "1/2")
                self.assertEqual("Export" in prompt, value == "1/2")
                args = ("record", "--pr", "7", "--reviewer", "claude-allein")
                with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt, '{"urteil":"APPROVE"}'))):
                    code, out = self.call(*args)
                self.assertEqual(code, 6)
                self.assertIn("Kriteriumssatz", out)
                with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt))):
                    self.assertEqual(self.call(*args)[0], 0)
                self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["slice"], value)
                self.assertIn(f"Scheibe {value}", self.pr_comments[-1])

    def test_final_slice_prompt_names_number_and_total(self):
        for value in ("2/2", "1/1"):
            with self.subTest(value=value):
                (self.repo / ".git" / "fabrik" / "prompt-7.sha").unlink(missing_ok=True)
                code, prompt = self.call("prompt", "--pr", "7", "--slice", value)
                self.assertEqual(code, 0, prompt)
                current, total = value.split("/")
                self.assertIn(f"## Schlussprüfung\nScheibe {current} von {total}.", prompt)
                section = prompt.split("## Schlussprüfung", 1)[1].split("\n## ", 1)[0]
                self.assertIn("bis zum Merge belegbar", section)
                self.assertIn("offen bis zur Abnahme", section)
                self.assertIn("erfüllt oder verletzt ist", prompt)
        (self.repo / ".git" / "fabrik" / "prompt-7.sha").unlink()
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        self.assertNotIn("## Schlussprüfung", prompt)
        self.assertNotIn("## Zwischenprüfung", prompt)

    def test_record_without_order_sheet_accepts_approve_without_criteria(self):
        self.pr_body = "Kleine Korrektur ohne Auftrag"
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        self.assertIn("(kein Issue verknüpft)", prompt)
        with mock.patch("sys.stdin", io.StringIO(signed_answer(prompt, '{"urteil":"APPROVE"}'))):
            code, out = self.call("record", "--pr", "7", "--reviewer", "claude")
        self.assertEqual(code, 0, out)
        self.assertEqual(lib.verdict_for(self.pr_comments, self.sha)["urteil"], "APPROVE")

    def test_prompt_lists_known_limits_as_no_finding(self):
        with self.base_limits("- Testgrenze ist dokumentiert"):
            code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        section = prompt.split("## Bekannte Grenzen des Kits\n", 1)[1].split("\n## PR-Text", 1)[0]
        for phrase in ("bewusst angenommen", "kein Fund", "verschärft", "- Testgrenze ist dokumentiert"):
            self.assertIn(phrase, section)

    def test_prompt_accepts_only_limits_from_merge_base(self):
        git(self.repo, "checkout", "main")
        self.write("kit/known_limits.md", "- Grenze aus der Basis")
        self.base = self.commit("document limit")
        git(self.repo, "checkout", "work")
        git(self.repo, "rebase", "main")
        self.write("kit/known_limits.md", "- Grenze aus der Basis\n- Neue Grenze im PR")
        self.sha = self.commit("change limits")
        self.fake.on(["pr", "diff"], git(self.repo, "diff", f"{self.base}...{self.sha}"))
        with mock.patch.object(review, "__file__", str(self.repo / "kit/review.py")), \
                mock.patch.object(lib, "_run_impl", self.fake):
            prompt = review.build_prompt(review.Context(self.repo, 7), "GATE: GRÜN")
        section = prompt.split("## Bekannte Grenzen des Kits\n")[1].split("\n## PR-Text")[0]
        self.assertIn("- Grenze aus der Basis", section)
        self.assertNotIn("Neue Grenze im PR", section)
        self.assertIn("werden mitgeprüft", section)
        self.assertIn("+- Neue Grenze im PR", prompt.split("## Diff")[1])

    def test_prompt_omits_symlink_limits_from_merge_base(self):
        git(self.repo, "checkout", "main")
        self.write("kit/target.md", "- Grenze hinter dem Symlink")
        (self.repo / "kit/known_limits.md").symlink_to("target.md")
        self.base = self.commit("link limits")
        git(self.repo, "checkout", "work")
        git(self.repo, "rebase", "main")
        self.sha = git(self.repo, "rev-parse", "HEAD").strip()
        with mock.patch.object(review, "__file__", str(self.repo / "kit/review.py")), \
                mock.patch.object(lib, "_run_impl", self.fake):
            prompt = review.build_prompt(review.Context(self.repo, 7), "GATE: GRÜN")
        self.assertNotIn("## Bekannte Grenzen des Kits", prompt)

    def test_prompt_uses_repo_path_of_symlinked_script(self):
        git(self.repo, "checkout", "main")
        self.write("kit/known_limits.md", "- Grenze aus der Basis")
        self.base = self.commit("document limit")
        git(self.repo, "checkout", "work")
        git(self.repo, "rebase", "main")
        self.sha = git(self.repo, "rev-parse", "HEAD").strip()
        installed = self.tmp / "installed"
        installed.mkdir()
        (installed / "review.py").touch()
        (installed / "known_limits.md").write_text("- Externe Grenze", encoding="utf-8")
        script = self.repo / "kit/review.py"
        script.symlink_to(installed / "review.py")
        with mock.patch.object(review, "__file__", str(script)), \
                mock.patch.object(lib, "_run_impl", self.fake):
            prompt = review.build_prompt(review.Context(self.repo, 7), "GATE: GRÜN")
        self.assertIn("- Grenze aus der Basis", prompt)
        self.assertNotIn("- Externe Grenze", prompt)

    def test_prompt_omits_limits_added_by_pr(self):
        self.write("kit/known_limits.md", "- Neue Grenze im PR")
        self.sha = self.commit("add limits")
        with mock.patch.object(review, "__file__", str(self.repo / "kit/review.py")), \
                mock.patch.object(lib, "_run_impl", self.fake):
            prompt = review.build_prompt(review.Context(self.repo, 7), "GATE: GRÜN")
        self.assertNotIn("## Bekannte Grenzen des Kits", prompt)

    def test_prompt_omits_unversioned_limits(self):
        self.write("kit/known_limits.md", "- Lokal installierte Grenze")
        with mock.patch.object(review, "__file__", str(self.repo / "kit/review.py")), \
                mock.patch.object(lib, "_run_impl", self.fake):
            prompt = review.build_prompt(review.Context(self.repo, 7), "GATE: GRÜN")
        self.assertNotIn("## Bekannte Grenzen des Kits", prompt)

    def test_limits_inside_repo_are_omitted_without_merge_base(self):
        self.write("kit/known_limits.md", "- Neue Grenze im PR")
        self.sha = self.commit("add limits")
        self.base = None
        with mock.patch.object(review, "__file__", str(self.repo / "kit/review.py")), \
                mock.patch.object(lib, "_run_impl", self.fake):
            prompt = review.build_prompt(review.Context(self.repo, 7), "GATE: GRÜN")
        self.assertNotIn("## Bekannte Grenzen des Kits", prompt)

    def test_prompt_omits_limits_outside_repo(self):
        fake_script = self.tmp / "installed" / "review.py"
        fake_script.parent.mkdir()
        (fake_script.parent / "known_limits.md").write_text("- Externe Grenze", encoding="utf-8")
        with mock.patch.object(review, "__file__", str(fake_script)), \
                mock.patch.object(lib, "_run_impl", self.fake):
            prompt = review.build_prompt(review.Context(self.repo, 7), "GATE: GRÜN")
        self.assertNotIn("## Bekannte Grenzen des Kits", prompt)

    def test_prompt_truncates_oversized_limits_from_merge_base(self):
        with self.base_limits("- Grenze\n" * 2000 + "Überhang"):
            code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        section = prompt.split("## Bekannte Grenzen des Kits\n", 1)[1].split("\n## PR-Text", 1)[0]
        self.assertIn("[gekürzt]", section)
        self.assertNotIn("Überhang", section)
        self.assertLess(len(section), review.MAX_LIMITS_CHARS + 500)

    def test_prompt_limits_pr_text_and_keeps_newest_comments(self):
        self.pr_body = "Refs #3\n".ljust(3000, "x") + "Textüberhang"
        self.pr_comments = ["Alter Kommentar", "A" * 2000, "B" * 2000, "C" * 2000,
                            "D" * 2000 + "Kommentarüberhang", verdict_comment("CHANGES", "früher")]
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        body = prompt.split("## PR-Text und Kommentare\n")[1].split("\n\n### Kommentare")[0]
        self.assertEqual(body, self.pr_body[:3000 - len("[gekürzt]")] + "[gekürzt]")
        self.assertNotIn("Textüberhang", prompt)
        comments = prompt.split("### Kommentare\n")[1].split("\n\n## Gate-Ausgabe")[0]
        self.assertEqual(comments, "\n\n".join(["[ältere Kommentare ausgelassen]",
                                             "C" * 2000, "D" * (2000 - len("[gekürzt]")) + "[gekürzt]"]))
        self.assertLessEqual(len(body) + len(comments), 8000)

    def test_comments_at_exact_limit_and_marker_filter(self):
        self.pr_body = "p" * 3000
        self.pr_comments = ["a" * 996, "b" * 2000, verdict_comment("CHANGES", "früher"), "c" * 2000]
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        comments = prompt.split("### Kommentare\n")[1].split("\n\n## Gate-Ausgabe")[0]
        self.assertIn(self.pr_body, prompt)
        self.assertEqual(len(self.pr_body) + len(comments), 8000)
        self.assertEqual(comments, "\n\n".join(self.pr_comments[:2] + self.pr_comments[3:]))

    def test_prompt_keeps_quoted_and_malformed_markers(self):
        marker = verdict_comment("CHANGES", "früher")
        quote = "Im letzten Kommentar stand " + marker
        malformed = f"<!-- {lib.VERDICT_MARK} {{kein JSON}} -->"
        self.pr_comments = [quote, "  \n" + marker, malformed]
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0, prompt)
        comments = prompt.split("### Kommentare\n")[1].split("\n\n## Gate-Ausgabe")[0]
        self.assertEqual(comments, quote + "\n\n" + malformed)

    def test_record_needs_prompt_for_same_commit(self):
        answer = self.tmp / "answer.txt"
        answer.write_text(APPROVE, encoding="utf-8")
        record = ("record", "--pr", "7", "--reviewer", "claude-allein", "--file", str(answer))
        code, out = self.call(*record)
        self.assertEqual(code, review.ERROR)
        self.assertIn("Kein Prompt", out)
        code, prompt = self.call("prompt", "--pr", "7")
        self.assertEqual(code, 0)
        answer.write_text(signed_answer(prompt), encoding="utf-8")
        self.write("src/later.py", "z = 1\n")
        self.sha = self.commit("changed after prompt")
        self.assertEqual(self.call(*record)[0], review.ERROR)
        self.assertEqual(self.pr_comments, [])

    def test_record_refuses_when_gate_is_red(self):
        self.call("prompt", "--pr", "7")
        answer = self.tmp / "answer.txt"
        answer.write_text(APPROVE, encoding="utf-8")
        with mock.patch.object(review, "gate_output", return_value=(False, "GATE: ROT")):
            code, _ = self.call("record", "--pr", "7", "--reviewer", "claude-allein", "--file", str(answer))
        self.assertEqual(code, review.GATE_RED)
        self.assertEqual(self.pr_comments, [])

    def test_status(self):
        code, out = self.call("status", "--pr", "7")
        self.assertEqual(code, 0)
        self.assertIn("Runden 0 von 3", out)


if __name__ == "__main__":
    unittest.main()
