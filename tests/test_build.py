import fcntl
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import KIT_SCRIPTS, RepoCase, git, lib, pr_json, quota_json
import build
import review


class BuildTest(RepoCase):
    def setUp(self):
        super().setUp()
        self.partner = self.tmp / "partner.yml"
        self.cwd = self.repo
        self.state = self.repo / ".git" / "fabrik" / "build.json"
        fake_gh = self.tmp / "gh"
        fake_gh.write_text("""#!/bin/sh
case "$*" in
  'issue view 3 --json title,body') echo '{"title":"Ein Auftrag","body":"Das Ziel des Auftrags"}' ;;
  'issue view 3 --json labels') printf '{"labels":%s}\\n' "${TEST_LABELS:-[]}" ;;
  'issue view 3 --json comments')
    if [ -s "$TEST_MARKER" ]; then
      python3 -c 'import json, sys; print(json.dumps({"comments": [{"body": open(sys.argv[1]).read()}]}))' "$TEST_MARKER"
    else
      echo '{}'
    fi ;;
  'issue comment 3 --body '*) printf '%s' "$5" > "$TEST_MARKER" ;;
  *) exit 1 ;;
esac
""")
        fake_gh.chmod(0o755)
        self.env = dict(os.environ, FABRIK_PARTNER_FILE=str(self.partner),
                        CLAUDE_PROJECT_DIR=str(self.repo), TEST_MARKER=str(self.tmp / "marker"), PATH=str(self.tmp) + ":" + os.environ["PATH"])

    def set_partner(self, command, quota=None):
        config = "name: testpartner\nbauen: " + json.dumps(command) + "\n"
        if quota is not None:
            config += "kontingent: " + json.dumps("printf %s " + shlex.quote(quota)) + "\n"
        self.partner.write_text(config)

    def call(self, *args, code=0, script="build.py"):
        done = subprocess.run([sys.executable, str(KIT_SCRIPTS / script), *args], cwd=self.cwd,
                              env=self.env, capture_output=True, text=True, timeout=12)
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        return done.stdout

    def start(self, code=0, *options):
        return self.call("start", "--issue", "3", "--slice", "1/2", "--task", "Nur die Ausgabe", *options, code=code)

    def finish(self, code):
        output = self.call("wait", "--max-wait", "5", code=code)
        self.assertFalse(self.state.exists())
        return output

    def test_success_prompt_state_and_commit_command(self):
        git(self.repo, "config", "commit.cleanup", "strip")
        git(self.repo, "config", "core.commentChar", "G")
        git(self.repo, "config", "trailer.ifMissing", "doNothing")
        git(self.repo, "config", "trailer.separators", "=:")
        self.write(".gitignore", "ignored\n")
        head = self.commit("ignore")
        self.write("ignored", "local")
        self.set_partner("cat > new-file; echo 'Datei angelegt, Test grün.'",
                         '{"rate_limits":{"primary":{"used_percent":89.9}}}')
        self.start()
        state = json.loads(self.state.read_text())
        self.assertEqual([state[k] for k in ("partner", "issue", "slice", "head")],
                         ["testpartner", 3, "1/2", head])
        self.assertGreater(state["pid"], 0)
        self.assertEqual(Path(state["log"]).parent.resolve(), self.state.parent.resolve())
        output = self.finish(0)
        self.assertIn("?? new-file", output)
        self.assertIn("Datei angelegt, Test grün.", output)
        self.assertNotIn("ignored", output)
        command = next(line for line in output.splitlines() if line.startswith("git "))
        git(self.repo, "add", "new-file")
        git(self.repo, *shlex.split(command.replace("<Betreff>", "build slice"))[1:])
        sha, headline, body = git(self.repo, "log", "-1", "--format=%H%x00%s%x00%b").rstrip().split("\0")
        self.assertEqual(body, "Gebaut-von: testpartner")
        pr = pr_json(sha=sha)
        pr["commits"] = [{"oid": sha, "messageHeadline": headline, "messageBody": body}]
        (self.tmp / "gh").write_text("#!/bin/sh\necho " + shlex.quote(json.dumps(pr)) + "\n")
        self.assertIn("Partner testpartner hat gebaut", self.call("run", "--pr", "7",
                                                                  script="review.py", code=12))
        prompt = (self.repo / "new-file").read_text()
        for part in ("Ein Auftrag", "Das Ziel des Auftrags", "Scheibe 1 von 2", "Nur die Ausgabe",
                     "python3 {files}", "sys.exit(0)", "locked/", "Test", "kein Netz", ".venv/bin/",
                     "git commit", "Branches", "Push", "Testergebnis"):
            self.assertIn(part, prompt)
        self.assertIn("Kein Bau", self.call("status"))

    def test_commit_commands_without_trailers(self):
        self.set_partner("echo x > new-file")
        self.start()
        output = self.finish(0)
        self.assertIn('git commit --cleanup=verbatim -m "<Betreff>" -m "Gebaut-von: testpartner"\n', output)
        self.assertTrue(output.rstrip().endswith('git commit --cleanup=verbatim -m "<Betreff>"'))

    def test_configured_trailers_follow_provenance_in_one_paragraph(self):
        trailers = ['Co-Authored-By: Agent "A" <a@example.com>', "Reviewed-Note: $HOME `x` \\ y"]
        git(self.repo, "checkout", "-q", "main")
        self.write(".fabrik/fabrik.yml", (self.repo / ".fabrik/fabrik.yml").read_text()
                   + "commit_trailers:\n" + "".join(f"  - {json.dumps(t)}\n" for t in trailers))
        self.commit("trailers")
        git(self.repo, "checkout", "-q", "work")
        git(self.repo, "merge", "-q", "main")
        self.set_partner("echo x > new-file")
        self.start()
        output = self.finish(0)
        partner_cmd = output.split("Partnerstand mit Trailer committen:\n", 1)[1].split(
            "\nEigene Korrekturen", 1)[0]
        own_cmd = output.split("ohne Gebaut-von committen:\n", 1)[1].strip()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        messages = []
        for command, subject in ((partner_cmd, "partner"), (own_cmd, "own")):
            self.write(f"{subject}.txt", subject)
            git(self.repo, "add", "-A")
            subprocess.run(["sh", "-c", command.replace("<Betreff>", subject)], cwd=self.repo, env=env,
                           check=True, capture_output=True)
            messages.append(git(self.repo, "log", "-1", "--format=%B"))
        self.assertNotIn("erkennt Git diese Trailer nicht", output)
        parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=messages[0], cwd=self.repo,
                                capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(parsed, ["Gebaut-von: testpartner", *trailers])
        parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=messages[1], cwd=self.repo,
                                capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(parsed, trailers)
        commits = [{"oid": "1", "messageHeadline": "partner",
                    "messageBody": messages[0].split("\n", 2)[2]},
                   {"oid": "2", "messageHeadline": "own", "messageBody": messages[1].split("\n", 2)[2]}]
        context = object.__new__(review.Context)
        context.__dict__["revision"] = ("2", commits)
        self.assertEqual(review.Context.builders.func(context), {"testpartner", None})

    def test_inline_trailer_with_comma_and_comment_char_hint(self):
        self.write(".fabrik/fabrik.yml", (self.repo / ".fabrik/fabrik.yml").read_text()
                   + 'commit_trailers: ["Co-Authored-By: Doe, Jane <j@example.org>"]  # Projektregel\n')
        self.commit("trailers")
        git(self.repo, "config", "core.commentChar", "C")
        self.set_partner("echo x > new-file")
        self.start()
        output = self.finish(0)
        self.assertIn('-m "Gebaut-von: testpartner\nCo-Authored-By: Doe, Jane <j@example.org>"', output)
        self.assertIn("erkennt Git diese Trailer nicht: Co-Authored-By: Doe, Jane <j@example.org>.", output)

    def test_git_config_that_hides_trailers_is_reported(self):
        self.write(".fabrik/fabrik.yml", (self.repo / ".fabrik/fabrik.yml").read_text()
                   + 'commit_trailers: ["Co-Authored-By: A <a@example.org>"]\n')
        self.commit("trailers")
        for key, value, hidden in (("trailer.separators", "=:", False), ("core.commentString", "Co-", True),
                                   ("trailer.separators", "=", True)):
            with self.subTest(key=key, value=value):
                git(self.repo, "config", key, value)
                self.set_partner("echo x > new-file")
                self.start()
                self.assertEqual("erkennt Git diese Trailer nicht" in self.finish(0), hidden)
                git(self.repo, "config", "--unset", key)
                (self.repo / "new-file").unlink()

    def test_failed_partner_with_changes_prints_commit_commands(self):
        self.set_partner("echo x > new-file; echo kaputt >&2; exit 17")
        self.start()
        output = self.finish(2)
        self.assertIn("Verwertbare Teiländerungen", output)
        self.assertIn('-m "Gebaut-von: testpartner"', output)
        self.assertIn("Eigene Korrekturen", output)
        self.set_partner("echo kaputt >&2; exit 17")
        git(self.repo, "add", "-A")
        self.commit("partner")
        self.start()
        self.assertNotIn("Verwertbare", self.finish(2))

    def test_invalid_commit_trailers_keep_build_open(self):
        self.set_partner("echo x > new-file")
        self.start()
        for value in ('"Co-Authored-By"', '[""]', '["kein Trailer"]', '["A: b\\nC: d"]', "[1]",
                      '["gEbAuT-vOn: andere"]', '["A: b\\rGebaut-von: x"]', '["Gebaut-von:x"]'):
            with self.subTest(value=value):
                self.write(".fabrik/fabrik.yml", (self.repo / ".fabrik/fabrik.yml").read_text().split(
                    "commit_trailers:")[0] + f"commit_trailers: {value}\n")
                output = self.call("wait", "--max-wait", "5", code=2)
                self.assertIn("commit_trailers", output)
                self.assertTrue(self.state.exists())

    def test_build_gate_risk_and_review_in_linked_worktree(self):
        linked = self.tmp / "linked worktree"
        git(self.repo, "worktree", "add", "-b", "linked", str(linked))
        self.cwd = linked / ".fabrik"
        self.set_partner("echo fertig > result")
        self.start()
        self.assertFalse(self.state.exists())
        directory = Path(git(linked, "rev-parse", "--absolute-git-dir").strip())
        self.assertTrue((directory / "fabrik/build.json").is_file())
        self.assertIn("?? result", self.finish(0))
        self.assertTrue((linked / "result").is_file())
        self.assertFalse((directory / "fabrik/build.json").exists())
        git(linked, "add", "result")
        git(linked, "commit", "-qm", "build slice")
        sha = git(linked, "rev-parse", "HEAD").strip()
        self.assertIn("Stufe: T1", self.call(script="gate.py"))
        self.assertIn("result", json.loads(self.call("--json", script="risk.py"))["files"])
        pr = shlex.quote(json.dumps(pr_json(sha=sha, body="")))
        (self.tmp / "gh").write_text(f"""#!/bin/sh
case "$1 $2" in
  'pr view') echo {pr} ;;
  'pr diff') git diff origin/main...HEAD ;;
  *) exit 1 ;;
esac
""")
        prompt = self.call("prompt", "--pr", "7", script="review.py")
        self.assertIn("Stufe: T1", prompt)
        self.assertIn("+fertig", prompt)
        self.assertEqual((directory / "fabrik/prompt-7.sha").read_text().splitlines()[0], sha)
        self.assertFalse((self.state.parent / "prompt-7.sha").exists())

    def test_project_dir_fallback_outside_git(self):
        self.cwd = self.tmp
        self.set_partner("echo fertig > result")
        self.start()
        self.assertIn("?? result", self.finish(0))

    def test_bare_repository_does_not_redirect_to_project_dir(self):
        self.cwd = self.tmp / "bare.git"
        git(self.tmp, "init", "-q", "--bare", str(self.cwd))
        self.set_partner("echo fertig > result")
        self.assertIn("git rev-parse", self.start(2))
        self.assertFalse(self.state.exists())

    def test_only_untracked_kit_bytecode_is_ignored(self):
        git(self.repo, "config", "core.excludesFile", os.devnull)
        self.write(".claude/fabrik/__pycache__/module.pyc", "cache")
        self.write(".claude/fabrik/sub/module.pyc", "cache")
        self.set_partner("touch .claude/fabrik/__pycache__/another.pyc")
        self.start()
        self.finish(8)
        for path in (".claude/fabrik/module.py", "src/__pycache__/module.pyc"):
            with self.subTest(path=path):
                self.write(path, "new")
                self.assertIn("sauber", self.start(2))
                (self.repo / path).unlink()
        self.commit("track bytecode")
        self.set_partner("echo edit >> .claude/fabrik/sub/module.pyc")
        self.start()
        self.assertIn(" M .claude/fabrik/sub/module.pyc", self.finish(0))
        self.assertIn("sauber", self.start(2))

    def test_model_values_are_shell_quoted_and_legacy_commands_pass_through(self):
        command = "printf '%s\\n' {modell} {aufwand} '{anderes}'"
        names = {"standard": "modell; echo falsch", "tief": "Modell mit 'Anführungszeichen'"}
        partner = {"name": "testpartner", "bauen": command,
                   "modelle": names, "aufwand": {"standard": "$(echo falsch)"}}
        for stage in names:
            actual = lib.partner_command(partner, "bauen", stage)
            code, out, _ = lib.run(["sh", "-c", actual], cwd=self.repo)
            self.assertEqual(code, 0)
            self.assertEqual(out.splitlines(), [names[stage], "$(echo falsch)", "{anderes}"])
        partner["modelle"].pop("tief")
        self.assertIn(shlex.quote(names["standard"]), lib.partner_command(partner, "bauen", "tief"))
        self.set_partner("echo {modell}")
        self.assertIn("fehlt 'modelle'", self.start(2))
        self.assertFalse(self.state.exists())
        legacy = "echo unverändert; printf '%s' '{anderes}'"
        self.assertEqual(lib.partner_command({"bauen": legacy}, "bauen", "tief"), legacy)

    def test_no_partner(self):
        self.assertIn("Kein Partner: Claude baut selbst.", self.start(3))
        self.assertFalse(self.state.exists())

    def test_partner_budget_and_start_thresholds(self):
        now = int(time.time())
        cases = (
            (quota_json(89, 99, now + 3600), 0),
            (quota_json(90, 1, now + 3600), 14),
            (quota_json(90, 99, now - 3600), 0),
            (quota_json(0, 99), 0),
            ('"rate_limits":{"primary":{"used_percent":95}}}', 14),
            ("unlesbar", 0),
        )
        for output, expected in cases:
            with self.subTest(output=output):
                self.set_partner("true", output)
                self.call("budget", code=expected)
                message = self.start(expected)
                if expected:
                    self.assertIn("Warten auf den Reset", message)
                    self.assertFalse(self.state.exists())
                    self.assertEqual(self.usage()[-1]["ereignis"], "bremse")
                else:
                    self.finish(8)

    def test_urgent_can_build_with_claude_at_partner_limit(self):
        self.env["TEST_LABELS"] = '[{"name":"Dringend"}]'
        self.set_partner("true", quota_json(90))
        self.assertIn("sonnet", self.start(3))
        self.assertEqual(self.usage()[-1]["entscheidung"], "claude")

    def test_escalation_is_used_once_and_stays_in_issue(self):
        self.set_partner("test {modell} = tiefes-modell && test {aufwand} = mittel || exit 19")
        with self.partner.open("a") as stream:
            stream.write("modelle:\n  standard: einfach\n  tief: tiefes-modell\n"
                         "aufwand:\n  standard: wenig\n  tief: mittel\n")
        self.assertIn("Modell: tiefes-modell, Stufe: tief", self.start(0, "--eskalation"))
        self.assertEqual(json.loads(self.state.read_text())["stufe"], "tief")
        self.finish(8)
        marker = self.tmp / "marker"
        self.assertEqual(lib._markers([marker.read_text()], lib.DEEP_MARK), [{"issue": 3, "slice": "1/2"}])
        self.assertIn("bereits verbraucht", self.start(0, "--eskalation"))
        self.assertEqual(json.loads(self.state.read_text())["stufe"], "standard")
        self.finish(2)

    def test_claude_claims_escalation_only_after_budget_check(self):
        marker = self.tmp / "marker"
        self.start(3, "--eskalation")
        self.assertFalse(marker.exists())

        self.env["TEST_LABELS"] = '[{"name":"Dringend"}]'
        self.set_partner("true", quota_json(90))
        self.start(3, "--eskalation")
        self.assertFalse(marker.exists())

        self.assertEqual(self.call("eskalation", "--issue", "3", "--slice", "1/2").strip(), "opus")
        self.assertEqual(lib._markers([marker.read_text()], lib.DEEP_MARK), [{"issue": 3, "slice": "1/2"}])
        self.assertEqual(self.call("eskalation", "--issue", "3").strip(), "bereits verbraucht, sonnet")

    def test_urgent_takeover_after_deep_partner_run_keeps_escalation_spent(self):
        self.env["TEST_LABELS"] = '[{"name":"Dringend"}]'
        self.set_partner("echo 'Usage limit reached' >&2; exit 1")
        self.start(0, "--eskalation")
        self.assertIn("Modell für Claudes Bau: sonnet", self.finish(3))
        self.assertEqual(self.call("eskalation", "--issue", "3").strip(), "bereits verbraucht, sonnet")

    def test_claude_budget_rules_do_not_change_partner_exitcode(self):
        duration = 10080 * 60
        for percent, elapsed, decision in ((69, 65, "frei"), (70, 70, "gebremst"),
                                            (24, 0, "frei"), (25, 15, "frei"),
                                            (26, 15, "gebremst")):
            with self.subTest(percent=percent, elapsed=elapsed):
                self.set_partner("true", quota_json(20))
                reset = time.time() + duration * (1 - elapsed / 100)
                command = "printf %s " + shlex.quote(quota_json(0, percent, week_reset=reset))
                with self.partner.open("a") as stream:
                    stream.write("claude_kontingent: " + json.dumps(command) + "\n")
                self.assertIn(f"claude: {decision}", self.call("budget"))
        self.partner.unlink()
        self.call("budget", code=3)

    def test_usage_reads_new_source_at_end_and_snapshot(self):
        source = self.tmp / "quota"
        source.write_text(quota_json(10) + "\nstand: 1\n")
        after = quota_json(20)
        self.set_partner(f"printf %s {shlex.quote(after)} > {shlex.quote(str(source))}")
        with self.partner.open("a") as stream:
            stream.write("kontingent: " + json.dumps("cat " + shlex.quote(str(source))) + "\n")
            stream.write("claude_kontingent: " + json.dumps("printf %s " + shlex.quote(quota_json(1))) + "\n")
        self.start()
        self.finish(8)
        self.call("snapshot", script="usage.py")
        start, end, partner, claude = self.usage()
        self.assertEqual([row["ereignis"] for row in (start, end, partner, claude)],
                         ["lauf_start", "lauf_ende", "schnappschuss", "schnappschuss"])
        self.assertEqual(start["quelle_stand"], 1)
        self.assertIn("veraltet", start["grund"])
        self.assertEqual(start["fenster_5h"]["prozent"], 10)
        self.assertEqual(end["fenster_5h"]["prozent"], 20)
        self.assertEqual(end["issue"], 3)
        self.assertEqual(end["rolle"], "bauen")
        self.assertEqual(claude["hersteller"], "claude")

    def test_start_is_logged_before_worker_release(self):
        worker = mock.MagicMock(pid=123)
        order = mock.Mock()
        order.attach_mock(worker.stdin.write, "release")
        self.state.parent.mkdir()
        args = SimpleNamespace(issue=3, slice="1/2")
        quota = lib.parse_quota(quota_json())
        partner = {"name": "testpartner", "bauen": "true"}
        launcher = SimpleNamespace(Popen=mock.Mock(return_value=worker), PIPE=subprocess.PIPE,
                                   STDOUT=subprocess.STDOUT)
        with mock.patch.object(build, "build_prompt", return_value=""), mock.patch.object(
                build, "subprocess", launcher), mock.patch.object(
                build.usage, "log", order.log):
            build.launch(self.repo, self.state, args, partner, quota, "standard")
        self.assertEqual(order.mock_calls, [
            mock.call.log(self.repo, "lauf_start", "testpartner", "bauen", quota, None, "standard",
                          issue=3, decision="starten"),
            mock.call.release(b"start\n"),
        ])

    def test_usage_write_failure_does_not_break_build(self):
        self.env["FABRIK_USAGE_LOG"] = str(self.tmp)
        self.set_partner("true")
        self.start()
        self.finish(8)

    def test_usage_with_unresolvable_home_warns_and_continues(self):
        stderr = io.StringIO()
        quota = lib.parse_quota(quota_json())
        with mock.patch.dict(os.environ), mock.patch.object(
                Path, "home", side_effect=RuntimeError("Home-Verzeichnis fehlt")), redirect_stderr(stderr):
            os.environ.pop("FABRIK_USAGE_LOG", None)
            build.usage.log(self.repo, "lauf_start", "testpartner", "bauen", quota)
        self.assertIn("Warnung: Nutzungsprotokoll", stderr.getvalue())
        self.assertIn("Home-Verzeichnis fehlt", stderr.getvalue())

    def test_unreadable_or_empty_quota_and_no_changes(self):
        for quota in ("", '{"used_percent":99', "unlesbar"):
            self.set_partner("true", quota)
            self.start()
            self.assertIn("keine Dateien", self.finish(8))

    def test_quota_uses_reset_of_each_window(self):
        for first, second, exhausted in ((99, 99, False), (99, 101, False), (101, 99, True)):
            limits = {"primary": {"used_percent": 100, "resets_at": first},
                      "secondary": {"used_percent": 100, "resets_at": second}}
            with self.subTest(limits=limits), mock.patch.object(build.time, "time", return_value=100):
                self.assertEqual(lib.quota_exhausted(lib.parse_quota(json.dumps({"rate_limits": limits}))), exhausted)

    def test_quota_timeout_allows_build_and_does_not_lock_state(self):
        self.assertLessEqual(lib.QUOTA_TIMEOUT_S, 30)
        self.set_partner("true")
        script = shlex.join([sys.executable, str(KIT_SCRIPTS / "build.py")])
        report = self.tmp / "quota-report"
        # Das Kind hält die Dateisperre, solange der Kontingentbefehl lebt.
        child = ("import fcntl, sys, time; fcntl.flock(sys.stdout, fcntl.LOCK_EX); "
                 "print('bereit', flush=True); time.sleep(60)")
        command = (f"{{ {script} status && {script} wait --max-wait 0; echo $?; "
                   f"{shlex.join([sys.executable, '-c', child])}; :; }} > {shlex.quote(str(report))}")
        with self.partner.open("a") as config:
            config.write("kontingent: " + json.dumps(command) + "\n")

        # Nur die Frist verkürzen. Der Partner und die parallelen CLI-Aufrufe laufen regulär.
        entry = ("import sys; sys.path.insert(0, sys.argv.pop(1)); import build, fabrik_lib as lib; "
                 "lib.QUOTA_TIMEOUT_S = 2; sys.exit(build.main())")
        done = subprocess.run([sys.executable, "-c", entry, str(KIT_SCRIPTS), "start", "--issue", "3",
                               "--slice", "1/2", "--task", "Test"], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, timeout=8)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.set_partner("true")
        self.finish(8)
        self.assertEqual(report.read_text().splitlines(), ["Kein Bau offen.", "Kein Bau offen.", "2", "bereit"])
        with report.open() as stream:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)

    def test_partner_failure_and_usage_limit_in_log(self):
        for message, code in (("Test fehlgeschlagen", 2), ("Usage limit reached", 14),
                              ("Rate limit exceeded", 14), ("Error: usage limit", 14),
                              ("Rate limit configuration", 2), ("Reading usage limit settings", 2)):
            self.set_partner("echo " + shlex.quote(message) + " >&2; exit 17")
            self.start()
            output = self.finish(code)
            self.assertIn(message, output)
            self.assertIn("17", output)

    def test_usage_limit_only_in_last_twenty_lines(self):
        for lines_after, code in ((20, 2), (19, 14)):
            with self.subTest(lines_after=lines_after):
                message = "Hit your USAGE LIMIT\n" + "Ausgabe\n" * (lines_after - 1) + "Fehler"
                self.set_partner("echo " + shlex.quote(message) + " >&2; exit 17")
                self.start()
                output = self.finish(code)
                self.assertEqual("Nutzungslimit erreicht" in output, code == 14)

    def test_competing_start_during_quota_keeps_its_state(self):
        self.set_partner("true")
        second = self.tmp / "second.yml"
        second.write_text(self.partner.read_text().replace("testpartner", "second"))
        start = shlex.join([sys.executable, str(KIT_SCRIPTS / "build.py"), "start", "--issue", "3",
                            "--slice", "1/2", "--task", "Test"])
        quota = f"FABRIK_PARTNER_FILE={shlex.quote(str(second))} {start} > /dev/null && " + \
                "echo '{\"primary\":{\"used_percent\":100}}'"
        with self.partner.open("a") as config:
            config.write("kontingent: " + json.dumps(quota) + "\n")
        self.assertIn("offen", self.start(2))
        self.assertEqual(json.loads(self.state.read_text())["partner"], "second")
        self.finish(8)

    def test_dirty_including_staged_and_untracked(self):
        self.set_partner("true")
        self.write("new-file", "local")
        self.start(2)
        git(self.repo, "add", "new-file")
        self.assertIn("sauber", self.start(2))
        self.assertFalse(self.state.exists())

    @contextmanager
    def waiting_partner(self):
        release = self.tmp / "release"
        self.set_partner("while [ ! -f " + shlex.quote(str(release)) +
                         " ]; do sleep 0.05; done; printf fertig > new-file")
        self.start()
        try:
            yield release
        finally:
            release.touch()

    def test_timeout_status_and_duplicate_start_preserve_build(self):
        with self.waiting_partner():
            state = self.state.read_text()
            self.assertIn("läuft noch", self.call("wait", "--max-wait", "0", code=9))
            self.assertIn("läuft", self.call("status"))
            self.assertIn("läuft", self.call("clear", code=2))
            self.assertIn("offen", self.start(2))
            self.assertEqual(self.state.read_text(), state)
        self.finish(0)

    def test_finished_partner_may_leave_a_child_with_log_open(self):
        release = self.tmp / "release"
        self.set_partner("(while [ -d .git ] && [ ! -f " + shlex.quote(str(release)) +
                         " ]; do sleep 0.05; done) & echo fertig > result")
        self.start()
        try:
            self.finish(0)
        finally:
            release.touch()

    def test_dead_wrapper_with_inherited_log_lock_reports_abort(self):
        log = self.state.parent / "build-test.log"
        log.parent.mkdir()
        wrapper = subprocess.Popen([sys.executable, "-c", "pass"])
        wrapper.wait(timeout=5)
        self.state.write_text(json.dumps({"pid": wrapper.pid, "log": str(log),
                                         "head": git(self.repo, "rev-parse", "HEAD").strip()}))
        with log.open("w") as inherited:
            fcntl.flock(inherited, fcntl.LOCK_EX)
            self.assertIn("abgebrochen", self.call("wait", "--max-wait", "0", code=2))
        self.assertFalse(self.state.exists())

    def test_dead_wrapper_with_changes_prints_commit_commands(self):
        log = self.state.parent / "build-test.log"
        log.parent.mkdir()
        log.write_text("")
        wrapper = subprocess.Popen([sys.executable, "-c", "pass"])
        wrapper.wait(timeout=5)
        self.state.write_text(json.dumps({"pid": wrapper.pid, "log": str(log), "partner": "testpartner",
                                         "head": git(self.repo, "rev-parse", "HEAD").strip()}))
        self.write("partial.txt", "x")
        self.write(".fabrik/fabrik.yml", (self.repo / ".fabrik/fabrik.yml").read_text()
                   + "commit_trailers: [kaputt]\n")
        self.assertIn("commit_trailers", self.call("wait", "--max-wait", "0", code=2))
        self.assertTrue(self.state.exists())
        git(self.repo, "checkout", "-q", "--", ".fabrik/fabrik.yml")
        output = self.call("wait", "--max-wait", "0", code=2)
        self.assertIn("abgebrochen", output)
        self.assertIn('-m "Gebaut-von: testpartner"', output)
        self.assertFalse(self.state.exists())

    def test_dead_wrapper_with_live_partner_keeps_build_open(self):
        ready = self.tmp / "ready"
        release = self.tmp / "release"
        self.set_partner(f"touch {shlex.quote(str(ready))}; "
                         f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.05; done")
        self.start()
        state = self.state.read_text()
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            os.kill(json.loads(state)["pid"], signal.SIGKILL)
            self.assertIn("Partnerprozesse laufen noch", self.call("wait", code=9))
            self.assertIn("läuft", self.call("status"))
            self.call("clear", code=2)
            self.assertIn("offen", self.start(2))
            self.assertEqual(self.state.read_text(), state)
        finally:
            release.touch()
        deadline = time.monotonic() + 5
        while "läuft" in self.call("status") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("abgebrochen", self.finish(2))

    def test_head_change_preserves_state_until_soft_reset_or_clear(self):
        start = git(self.repo, "rev-parse", "HEAD").strip()
        with self.waiting_partner():
            self.write("other", "another commit")
            self.commit("intervening change")
        state = self.state.read_text()
        output = self.call("wait", "--max-wait", "5", code=2)
        for hint in ("HEAD", f"git reset --soft {start}", "Gebaut-von: testpartner",
                     "build.py wait", "build.py clear"):
            self.assertIn(hint, output)
        self.assertEqual(self.state.read_text(), state)
        self.assertIn("offen", self.start(2))
        git(self.repo, "reset", "--soft", start)
        self.assertIn("Gebaut-von: testpartner", self.finish(0))

        self.commit("recovered\n\nGebaut-von: testpartner")
        self.set_partner("true")
        self.start()
        self.write("another", "second change")
        self.commit("another change")
        self.call("wait", "--max-wait", "5", code=2)
        self.call("clear")
        self.assertFalse(self.state.exists())
        self.start()
        self.finish(8)


if __name__ == "__main__":
    unittest.main()
