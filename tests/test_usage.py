import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from helpers import KIT_SCRIPTS  # noqa: F401
import usage


LIMITS = {"primary": {"used_percent": 42, "window_minutes": 300}}


class CodexQuotaTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sessions = Path(directory.name)

    def session(self, name, mtime, *events):
        path = self.sessions / "2026/11/22" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def event(self, stamp, limits):
        timestamp = datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")
        return {"timestamp": timestamp, "payload": {"rate_limits": limits}}

    def test_finds_latest_usable_event_across_more_than_twenty_sessions(self):
        for index in range(21):
            self.session(f"empty-{index}", 300 + index, {"type": "event_msg"})
        self.session("newest-file", 200, self.event(80, {"secondary": {
            "used_percent": 12, "window_minutes": 10080,
        }}))
        self.session("best-measurement", 120, self.event(100, LIMITS),
                     self.event(110, {"primary": None, "secondary": None}),
                     self.event(111, {"primary": {"used_percent": 93}}))
        self.assertEqual(usage.latest_codex_quota(self.sessions), (100, LIMITS))

    def test_skips_files_that_disappear_or_cannot_be_read(self):
        self.session("valid", 120, self.event(100, LIMITS))
        removed = self.session("removed", 140)
        unreadable = self.session("unreadable", 130)
        open_file = Path.open
        stat_file = Path.stat

        def stat(path, *args, **kwargs):
            if path == removed:
                removed.unlink()
            return stat_file(path, *args, **kwargs)

        def open_session(path, *args, **kwargs):
            if path == unreadable:
                raise PermissionError(path)
            return open_file(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat), mock.patch.object(Path, "open", open_session):
            self.assertEqual(usage.latest_codex_quota(self.sessions), (100, LIMITS))

    def test_stops_at_mtime_older_than_best_event(self):
        best = self.session("best", 130, self.event(100, LIMITS))
        newer_mtime = self.session("newer-mtime", 110, self.event(90, LIMITS))
        equal_mtime = self.session("equal-mtime", 100)
        self.session("older-mtime", 99, self.event(98, LIMITS))
        opened = []
        open_file = Path.open

        def open_session(path, *args, **kwargs):
            opened.append(path)
            return open_file(path, *args, **kwargs)

        with mock.patch.object(Path, "open", open_session):
            self.assertEqual(usage.latest_codex_quota(self.sessions), (100, LIMITS))
        self.assertEqual(opened, [best, newer_mtime, equal_mtime])
