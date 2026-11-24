import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from session import Denied, Session


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": self.directory.name,
            "HYPRLAND_INSTANCE_SIGNATURE": "test-compositor",
            "WAYLAND_DISPLAY": "test-wayland",
        }).start()
        self.session = Session()

    def make_grant(self, mode="control", seconds=30):
        grant = {"mode": mode, "expires": time.monotonic() + seconds, "token": "fixture"}
        self.session.lease_path.write_text(json.dumps(grant))
        return grant

    def test_requires_live_operator_and_permission_mode(self):
        self.make_grant("observe")
        self.assertIsNone(self.session.lease())
        with self.session.lock("operator"):
            self.assertEqual(self.session.require()["mode"], "observe")
            with self.assertRaises(Denied):
                self.session.require(control=True)
            self.make_grant(seconds=-1)
            self.assertIsNone(self.session.lease())

    def test_only_one_controller_can_hold_action_lock(self):
        with self.session.lock("action"):
            with self.assertRaises(Denied), Session().lock("action"):
                self.fail("overlapping operation received the lock")

    def test_stop_cancels_inflight_command(self):
        marker = Path(self.directory.name) / "mutation"
        with self.session.lock("operator"):
            grant = self.make_grant()
            stop = threading.Timer(0.12, self.session.lease_path.unlink)
            stop.start()
            self.addCleanup(stop.join)
            with self.assertRaisesRegex(Denied, "paused"):
                self.session.run(
                    [sys.executable, "-c",
                     "import pathlib,sys,time;time.sleep(1);pathlib.Path(sys.argv[1]).touch()",
                     str(marker)],
                    grant,
                )
        self.assertFalse(marker.exists())

    def test_declining_operator_dialog_creates_no_lease(self):
        with patch("session.subprocess.run") as prompt:
            prompt.return_value.returncode = 1
            with self.assertRaisesRegex(Denied, "declined"):
                self.session.open_operator("observe", 5)
        self.assertFalse(self.session.lease_path.exists())

    def test_audit_is_private_and_records_only_fixed_fields(self):
        self.session.audit("clipboard_read", "completed")
        audit_path = self.session.directory / "audit.jsonl"
        self.assertEqual(audit_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(audit_path.read_text())), {"time", "action", "result"})


if __name__ == "__main__":
    unittest.main()
