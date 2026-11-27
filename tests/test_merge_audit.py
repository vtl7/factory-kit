import unittest
from unittest import mock

from helpers import lib, FakeGh, RepoCase, pr_json, verdict_comment, git
import merge_audit

HEAD = "b" * 40


class AuditTest(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("src/a.py", "x = 1\n")
        self.sha = self.commit("feature")

    def find(self, fake):
        with mock.patch.object(lib, "_run_impl", fake):
            return merge_audit.find_problem(self.repo, self.sha)

    def pulls(self, head_ref="fabrik/3-x", login="someone"):
        return [{"number": 7, "merge_commit_sha": self.sha, "head": {"ref": head_ref},
                 "user": {"login": login}}]

    def test_bootstrap_commit_is_exempt(self):
        first = git(self.repo, "rev-list", "--max-parents=0", "HEAD").strip()
        with mock.patch.object(lib, "_run_impl", FakeGh()):
            self.assertEqual(merge_audit.find_problem(self.repo, first), (None, None))

    def test_merge_with_approve_is_fine(self):
        fake = FakeGh().on(["api"], self.pulls()).on(
            ["pr", "view"], pr_json(sha=HEAD, comments=[verdict_comment("APPROVE", HEAD)]))
        self.assertEqual(self.find(fake), (None, 7))

    def test_merge_without_verdict_is_incident(self):
        fake = FakeGh().on(["api"], self.pulls()).on(["pr", "view"], pr_json(sha=HEAD))
        problem, pr = self.find(fake)
        self.assertIn("ohne Prüfurteil", problem)
        self.assertEqual(pr, 7)

    def test_only_final_slice_approval_clears_audit(self):
        for scope in ("1/2", "2/2"):
            with self.subTest(scope=scope):
                fake = FakeGh().on(["api"], self.pulls()).on(
                    ["pr", "view"], pr_json(sha=HEAD, comments=[verdict_comment("APPROVE", HEAD, slice=scope)]))
                problem, pr = self.find(fake)
                self.assertEqual(pr, 7)
                self.assertEqual(problem is None, scope == "2/2")

    def test_verdict_for_older_commit_is_incident(self):
        fake = FakeGh().on(["api"], self.pulls()).on(
            ["pr", "view"], pr_json(sha=HEAD, comments=[verdict_comment("APPROVE", "c" * 40)]))
        self.assertIsNotNone(self.find(fake)[0])

    def test_direct_push_is_incident(self):
        problem, pr = self.find(FakeGh().on(["api"], []))
        self.assertIn("ohne PR", problem)
        self.assertIsNone(pr)

    def test_revert_pr_of_the_audit_is_exempt(self):
        fake = FakeGh().on(["api"], self.pulls("fabrik/revert-abc1234", merge_audit.BOT_LOGIN))
        self.assertEqual(self.find(fake), (None, 7))

    def test_branch_named_like_revert_is_not_exempt(self):
        fake = FakeGh().on(["api"], self.pulls("fabrik/revert-abc1234", "someone")).on(
            ["pr", "view"], pr_json(sha=HEAD))
        self.assertIsNotNone(self.find(fake)[0])

    def test_every_pushed_commit_is_checked(self):
        before = git(self.repo, "rev-parse", "HEAD~1").strip()
        self.write("src/b.py", "y = 1\n")
        last = self.commit("second")
        self.assertEqual(merge_audit.pushed_commits(self.repo, before, last), ([self.sha, last], True))
        self.assertEqual(merge_audit.pushed_commits(self.repo, "0" * 40, last), ([last], True))

    def test_missing_or_rewritten_before_is_reported_not_ignored(self):
        with mock.patch.object(lib, "_run_impl", FakeGh()):
            self.assertEqual(merge_audit.pushed_commits(self.repo, "e" * 40, self.sha),
                             ([self.sha], False))
        git(self.repo, "checkout", "-q", "-b", "side", "HEAD~1")
        self.write("src/c.py", "z = 1\n")
        side = self.commit("rewritten history")
        self.assertEqual(merge_audit.pushed_commits(self.repo, self.sha, side), ([side], False))
        fake = (FakeGh().on(["api"], [{"number": 8, "merge_commit_sha": side, "head": {"ref": "x"},
                                       "user": {}}])
                .on(["pr", "view"], pr_json(sha=HEAD, comments=[verdict_comment("APPROVE", HEAD)]))
                .on(["issue", "create"], "https://github.com/o/r/issues/5\n"))
        with mock.patch.object(lib, "_run_impl", fake), \
                mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(self.repo)}):
            code = merge_audit.main(["--sha", side, "--before", self.sha])
        self.assertEqual(code, 1)
        self.assertIn(lib.LABEL_INCIDENT, fake.called("issue", "create")[0])

    def test_main_reports_early_bad_commit_in_multi_commit_push(self):
        before = git(self.repo, "rev-parse", "HEAD~1").strip()
        bad = self.sha
        self.write("src/b.py", "y = 1\n")
        good = self.commit("second")
        opened = []

        def pulls(args):
            import json
            sha = args[1].split("/")[-2]
            return 0, json.dumps([] if sha == bad else [{"number": 8, "merge_commit_sha": good,
                                                           "head": {"ref": "x"}, "user": {}}]), ""
        fake = FakeGh().on(["api"], pulls).on(
            ["pr", "view"], pr_json(sha=HEAD, comments=[verdict_comment("APPROVE", HEAD)]))
        with mock.patch.object(lib, "_run_impl", fake), \
                mock.patch.object(merge_audit, "open_incident",
                                  lambda root, sha, problem, pr: opened.append(sha) or ("1", None)), \
                mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(self.repo)}):
            code = merge_audit.main(["--sha", good, "--before", before])
        self.assertEqual(code, 1)
        self.assertEqual(opened, [bad])

    def test_incident_opens_issue_and_revert_pr(self):
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", str(bare))
        git(self.repo, "remote", "add", "origin", str(bare))
        fake = (FakeGh().on(["issue", "create"], "https://github.com/o/r/issues/42\n")
                .on(["pr", "create"], "https://github.com/o/r/pull/43\n"))
        with mock.patch.object(lib, "_run_impl", fake):
            issue, pr_url = merge_audit.open_incident(self.repo, self.sha, "PR #7 ohne Urteil", 7)
        self.assertEqual((issue, pr_url), ("42", "https://github.com/o/r/pull/43"))
        self.assertIn(lib.LABEL_INCIDENT, fake.called("issue", "create")[0])
        branch = f"{merge_audit.REVERT_PREFIX}{self.sha[:7]}"
        self.assertIn(branch, git(bare, "branch"))
        self.assertFalse((self.repo / "src/a.py").exists())  # reverted on the branch
        self.assertIn("Refs #42", fake.called("pr", "create")[0][-1])

    def test_incident_without_revert_pr_leaves_instructions(self):
        fake = FakeGh().on(["issue", "create"], "https://github.com/o/r/issues/42\n")
        with mock.patch.object(lib, "_run_impl", fake):  # no origin remote: push fails
            issue, pr_url = merge_audit.open_incident(self.repo, self.sha, "x", 7)
        self.assertIsNone(pr_url)
        self.assertIn("git revert", fake.called("issue", "comment")[0][-1])


if __name__ == "__main__":
    unittest.main()
