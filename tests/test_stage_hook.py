import io
import json
import unittest
from unittest import mock

from helpers import lib, FakeGh, RepoCase, pr_json, verdict_comment, CONFIG, git
import stage_hook as hook

SHA = "a" * 40


class FindTargetsTest(unittest.TestCase):
    def kinds(self, command, ship=None):
        return [(k, d.get("ref")) for k, d in hook.find_targets(command, ship)]

    def test_merge_forms(self):
        self.assertEqual(self.kinds("gh pr merge 12 --squash"), [("merge", "12")])
        self.assertEqual(self.kinds("gh pr merge --squash --delete-branch 12"), [("merge", "12")])
        self.assertEqual(self.kinds("gh pr merge -R o/r --subject 'x y' 9"), [("merge", "9")])
        self.assertEqual(self.kinds("gh pr merge"), [("merge", None)])
        self.assertEqual(self.kinds("cd x && gh pr merge 3 && echo ok"), [("merge", "3")])
        self.assertEqual(self.kinds("gh api -X PUT repos/o/r/pulls/5/merge"), [("merge", "5")])

    def test_repo_flag_is_kept(self):
        targets = hook.find_targets("gh pr merge 4 --repo own/repo")
        self.assertEqual(targets[0][1]["repo"], "own/repo")

    def test_not_a_merge(self):
        self.assertEqual(self.kinds("gh pr view 12"), [])
        self.assertEqual(self.kinds("git merge origin/main"), [])
        self.assertEqual(self.kinds("echo 'gh pr list'"), [])

    def test_push_to_main(self):
        self.assertEqual(self.kinds("git push origin main"), [("push-main", None)])
        self.assertEqual(self.kinds("git push origin HEAD:main"), [("push-main", None)])
        self.assertEqual(self.kinds("git push -f origin +refs/heads/main"), [("push-main", None)])
        self.assertEqual(self.kinds("git push -u origin fabrik/12-x"), [])

    def test_ship_command(self):
        self.assertEqual(self.kinds("./ship.sh", "./ship.sh"), [("ship", None)])
        self.assertEqual(self.kinds("bash ship.sh --prod", "./ship.sh"), [("ship", None)])
        self.assertEqual(self.kinds("make deploy", "make deploy"), [("ship", None)])
        self.assertEqual(self.kinds("make test", "make deploy"), [])
        self.assertEqual(self.kinds("cat ship.sh.bak", "./ship.sh"), [])


class DecideMergeTest(unittest.TestCase):
    config = lib.load_config(".", "test_paths: [tests/]\n")

    def info(self, **kw):
        data = pr_json(**kw)
        fake = FakeGh().on(["pr", "view"], data)
        with mock.patch.object(lib, "_run_impl", fake):
            return lib.pr_info(data["number"])

    def test_without_verdict_denies_every_stage(self):
        for files in (["README.md"], ["src/a.py"], ["src/auth/a.py"]):
            decision, reason = hook.decide_merge(self.info(files=files), self.config)
            self.assertEqual(decision, hook.DENY)
            self.assertIn("kein Prüfurteil", reason)

    def test_verdict_for_older_commit_does_not_count(self):
        info = self.info(files=["src/a.py"], comments=[verdict_comment("APPROVE", "b" * 40)])
        self.assertEqual(hook.decide_merge(info, self.config)[0], hook.DENY)

    def test_changes_verdict_denies(self):
        info = self.info(files=["src/a.py"], comments=[verdict_comment("CHANGES")])
        self.assertEqual(hook.decide_merge(info, self.config)[0], hook.DENY)

    def test_gate_not_green_denies(self):
        for checks in ([], [{"name": lib.GATE_CHECK_NAME, "status": "IN_PROGRESS"}],
                       [{"name": lib.GATE_CHECK_NAME, "status": "COMPLETED", "conclusion": "FAILURE"}]):
            info = self.info(files=["src/a.py"], comments=[verdict_comment("APPROVE")], checks=checks)
            self.assertEqual(hook.decide_merge(info, self.config)[0], hook.DENY)

    def test_t0_t1_pass(self):
        for files in (["README.md"], ["src/a.py"]):
            info = self.info(files=files, comments=[verdict_comment("APPROVE")])
            self.assertEqual(hook.decide_merge(info, self.config)[0], hook.PASS)

    def test_t2_t3_ask(self):
        for files in (["requirements.txt"], ["src/auth/a.py"]):
            info = self.info(files=files, comments=[verdict_comment("APPROVE")])
            decision, reason = hook.decide_merge(info, self.config)
            self.assertEqual(decision, hook.ASK)
            self.assertIn("Bestätigung", reason)

    def test_stage_label_cannot_lower_computed_stage(self):
        info = self.info(files=["src/auth/a.py"], labels=["T0"], comments=[verdict_comment("APPROVE")])
        self.assertEqual(hook.decide_merge(info, self.config)[0], hook.ASK)

    def test_higher_label_raises_stage(self):
        info = self.info(files=["src/a.py"], labels=["T3"], comments=[verdict_comment("APPROVE")])
        self.assertEqual(hook.decide_merge(info, self.config)[0], hook.ASK)


class DecideEndToEndTest(RepoCase):
    config = CONFIG + 'ship: "./ship.sh"\n'

    def decide(self, command, fake):
        with mock.patch.object(lib, "_run_impl", fake):
            return hook.decide(command, self.repo)

    def test_unrelated_command_passes_without_gh(self):
        fake = FakeGh()
        self.assertEqual(self.decide("ls -la", fake), (hook.PASS, ""))
        self.assertEqual(fake.calls, [])

    def test_merge_denied_and_reason_given(self):
        fake = FakeGh().on(["pr", "view"], pr_json(files=["src/a.py"]))
        decision, reason = self.decide("gh pr merge 7 --squash", fake)
        self.assertEqual(decision, hook.DENY)
        self.assertIn("PR #7", reason)

    def test_merge_passes_with_verdict_and_gate(self):
        fake = FakeGh().on(["pr", "view"], pr_json(files=["src/a.py"],
                                                    comments=[verdict_comment("APPROVE")]))
        self.assertEqual(self.decide("gh pr merge 7", fake)[0], hook.PASS)

    def test_merge_denied_when_intermediate_verdict_quotes_final_approval(self):
        body = (verdict_comment("APPROVE", slice="1/2") +
                "\n- Kriterium 1: Beispiel für ein Gesamturteil: " + verdict_comment("APPROVE"))
        pr = pr_json(files=["src/a.py"], comments=[body])
        self.assertIsNone(lib.final_verdict_for([body], pr["headRefOid"]))
        fake = FakeGh().on(["pr", "view"], pr)
        decision, reason = self.decide("gh pr merge 7", fake)
        self.assertEqual(decision, hook.DENY)
        self.assertIn("nur Zwischenurteil für Scheibe 1/2", reason)

    def test_unreadable_pr_denies(self):
        fake = FakeGh().on(["pr", "view"], lambda args: (1, "", "no pull requests found"))
        self.assertEqual(self.decide("gh pr merge 99", fake)[0], hook.DENY)

    def test_push_main_denied(self):
        self.assertEqual(self.decide("git push origin main", FakeGh())[0], hook.DENY)

    def test_ship_t1_passes_t3_asks_no_pr_denies(self):
        api = ["api"]
        fake = FakeGh().on(api, [{"number": 7}]).on(
            ["pr", "view"], pr_json(files=["src/a.py"], comments=[verdict_comment("APPROVE")]))
        self.assertEqual(self.decide("./ship.sh", fake)[0], hook.PASS)
        fake = FakeGh().on(api, [{"number": 7}]).on(
            ["pr", "view"], pr_json(files=["src/auth/a.py"], comments=[verdict_comment("APPROVE")]))
        self.assertEqual(self.decide("./ship.sh", fake)[0], hook.ASK)
        fake = FakeGh().on(api, [])
        self.assertEqual(self.decide("./ship.sh", fake)[0], hook.DENY)

    def test_ship_without_verdict_denied(self):
        fake = FakeGh().on(["api"], [{"number": 7}]).on(["pr", "view"], pr_json(files=["src/a.py"]))
        self.assertEqual(self.decide("sh ./ship.sh", fake)[0], hook.DENY)

    def test_merge_and_ship_require_whole_pr_approval(self):
        for command, action in (("gh pr merge 7", "Merge"), ("./ship.sh", "Ship")):
            for scope, expected in (("1/2", hook.DENY), ("2/2", hook.PASS)):
                with self.subTest(command=command, scope=scope):
                    fake = FakeGh().on(["api"], [{"number": 7}]).on(
                        ["pr", "view"], pr_json(files=["src/a.py"], comments=[verdict_comment("APPROVE", slice=scope)]))
                    decision, reason = self.decide(command, fake)
                    self.assertEqual(decision, expected)
                    if expected == hook.DENY:
                        self.assertIn(f"{action} gesperrt: nur Zwischenurteil für Scheibe 1/2", reason)

    def test_ship_needs_approve_and_green_gate(self):
        for comments, checks in (([verdict_comment("CHANGES")], None),
                                 ([verdict_comment("ESCALATE")], None),
                                 ([verdict_comment("APPROVE")], []),
                                 ([verdict_comment("APPROVE")], [{"name": lib.GATE_CHECK_NAME,
                                   "status": "COMPLETED", "conclusion": "FAILURE"}])):
            fake = FakeGh().on(["api"], [{"number": 7}]).on(
                ["pr", "view"], pr_json(files=["src/a.py"], comments=comments, checks=checks))
            self.assertEqual(self.decide("./ship.sh", fake)[0], hook.DENY, (comments, checks))

    def test_newer_intermediate_changes_blocks_merge_and_ship(self):
        comments = [verdict_comment("APPROVE"), verdict_comment("CHANGES", slice="1/2")]
        fake = FakeGh().on(["api"], [{"number": 7}]).on(["pr", "view"], pr_json(comments=comments))
        for command in ("gh pr merge 7", "./ship.sh"):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command, fake)[0], hook.DENY)

    def test_push_without_refspec_on_main_is_denied(self):
        git(self.repo, "checkout", "-q", "main")
        for command in ("git push", "git push origin", "git push -u origin HEAD"):
            self.assertEqual(self.decide(command, FakeGh())[0], hook.DENY, command)
        git(self.repo, "checkout", "-q", "work")
        self.assertEqual(self.decide("git push", FakeGh())[0], hook.PASS)
        self.assertEqual(self.decide("git push origin HEAD", FakeGh())[0], hook.PASS)
        self.assertEqual(self.decide("git push --all origin", FakeGh())[0], hook.DENY)

    def test_push_default_upstream_to_main_is_denied(self):
        git(self.repo, "config", "push.default", "upstream")
        git(self.repo, "config", "branch.work.remote", "origin")
        git(self.repo, "config", "branch.work.merge", "refs/heads/main")
        git(self.repo, "remote", "add", "origin", str(self.tmp / "none.git"))
        self.assertEqual(self.decide("git push", FakeGh())[0], hook.DENY)
        self.assertEqual(self.decide("git push origin fabrik/1-x", FakeGh())[0], hook.PASS)

    def test_push_default_upstream_without_tracking_ref_is_denied(self):
        git(self.repo, "config", "push.default", "upstream")
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        git(self.repo, "config", "branch.work.remote", "origin")
        git(self.repo, "config", "branch.work.merge", "refs/heads/main")
        self.assertEqual(self.decide("git push", FakeGh())[0], hook.DENY)
        git(self.repo, "config", "--unset", "branch.work.merge")
        self.assertEqual(self.decide("git push", FakeGh())[0], hook.DENY)

    def test_remote_push_refspec_or_matching_is_denied(self):
        git(self.repo, "config", "remote.origin.push", "refs/heads/work:refs/heads/main")
        self.assertEqual(self.decide("git push", FakeGh())[0], hook.DENY)
        git(self.repo, "config", "--unset", "remote.origin.push")
        git(self.repo, "config", "push.default", "matching")
        self.assertEqual(self.decide("git push origin", FakeGh())[0], hook.DENY)

    def test_branch_cannot_rename_ship_or_main_to_escape(self):
        self.write(".fabrik/fabrik.yml", CONFIG + 'ship: "./other.sh"\nmain_branch: trunk\n')
        self.commit("rename on branch")
        fake = FakeGh().on(["api"], [{"number": 7}]).on(["pr", "view"], pr_json(files=["src/a.py"]))
        self.assertEqual(self.decide("./ship.sh", fake)[0], hook.DENY)
        self.assertEqual(self.decide("git push origin main", FakeGh())[0], hook.DENY)

    def test_deleted_working_config_does_not_switch_hook_off(self):
        (self.repo / ".fabrik" / "fabrik.yml").unlink()
        self.commit("drop config")
        self.assertEqual(self.decide("git push origin main", FakeGh())[0], hook.DENY)
        fake = FakeGh().on(["pr", "view"], pr_json(files=["src/a.py"]))
        self.assertEqual(self.decide("gh pr merge 7", fake)[0], hook.DENY)

    def test_branch_cannot_lower_stage_via_own_config(self):
        # Branch maps nothing, main maps src/core/ to T3: main's config is used.
        git(self.repo, "checkout", "-q", "main")
        self.write(".fabrik/fabrik.yml", self.config + "risk:\n  auth_secrets: [src/core/]\n")
        self.commit("map core")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "lower")
        self.write(".fabrik/fabrik.yml", self.config)
        self.commit("drop mapping")
        fake = FakeGh().on(["pr", "view"], pr_json(files=["src/core/a.py"],
                                                    comments=[verdict_comment("APPROVE")]))
        self.assertEqual(self.decide("gh pr merge 7", fake)[0], hook.ASK)


class MainTest(RepoCase):
    def run_hook(self, payload, fake):
        out = io.StringIO()
        with mock.patch.object(lib, "_run_impl", fake), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch("sys.stdout", out):
            self.assertEqual(hook.main(), 0)
        return out.getvalue()

    def test_emits_deny_json(self):
        fake = FakeGh().on(["pr", "view"], pr_json(files=["src/a.py"]))
        out = self.run_hook({"tool_name": "Bash", "cwd": str(self.repo),
                             "tool_input": {"command": "gh pr merge 7"}}, fake)
        data = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(data["permissionDecision"], "deny")
        self.assertEqual(data["hookEventName"], "PreToolUse")

    def test_silent_on_pass_and_other_tools(self):
        self.assertEqual(self.run_hook({"tool_name": "Bash", "cwd": str(self.repo),
                                        "tool_input": {"command": "pytest"}}, FakeGh()), "")
        self.assertEqual(self.run_hook({"tool_name": "Write", "cwd": str(self.repo),
                                        "tool_input": {}}, FakeGh()), "")


if __name__ == "__main__":
    unittest.main()
