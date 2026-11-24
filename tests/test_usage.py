import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import usage


NOW = 1_796_000_000


def quota(remaining, duration, reset=NOW + 1000):
    return {"usedPercent": 100 - remaining, "windowDurationMins": duration, "resetsAt": reset}


def response(five_hour, weekly):
    return {"rateLimitsByLimitId": {"codex": {"primary": five_hour, "secondary": weekly}}}


class UsageTests(unittest.TestCase):
    def status(self, payload, **kwargs):
        with patch("usage.time.time", return_value=NOW), patch("usage.read_rate_limits", return_value=payload):
            return usage.usage_status(**kwargs)

    def test_uses_canonical_bucket_and_matches_window_duration(self):
        payload = response(quota(20, 10080), quota(50, 300))
        payload["rateLimits"] = {"primary": quota(1, 300)}
        result = self.status(payload)
        windows = result["subscription"]["windows"]
        self.assertEqual(windows["five_hour"]["remaining_percent"], 50)
        self.assertEqual(windows["weekly"]["remaining_percent"], 20)
        self.assertEqual(windows["weekly"]["reset_at"], usage.utc(NOW + 1000))
        self.assertEqual(result["adaptive_mode"], "conservation")
        self.assertEqual(result["context"]["state"], "unavailable")

    def test_thresholds_and_backend_permission(self):
        for remaining, mode in [(4, "critical"), (5, "completion"), (14, "completion"),
                                (15, "conservation"), (30, "conservation"), (31, "normal")]:
            with self.subTest(remaining=remaining):
                self.assertEqual(self.status(response(quota(90, 300), quota(remaining, 10080)))["adaptive_mode"], mode)
        custom = self.status(response(quota(4, 300), quota(80, 10080)), thresholds=(3, 6, 10))
        self.assertEqual(custom["adaptive_mode"], "completion")
        denied = self.status({"ordinaryUsageAllowed": False})
        self.assertEqual(denied["adaptive_mode"], "critical")
        self.assertTrue(denied["checkpoint_recommended"])

    def test_expired_window_does_not_simulate_reset(self):
        result = self.status(response(quota(1, 300, NOW), quota(90, 10080)))
        self.assertEqual(result["subscription"]["windows"]["five_hour"]["state"], "stale")
        self.assertEqual(result["subscription"]["windows"]["five_hour"]["remaining_percent"], 1)
        self.assertEqual(result["adaptive_mode"], "conservation")
        self.assertTrue(result["checkpoint_recommended"])

    def test_other_duration_or_bucket_does_not_pass_for_subscription(self):
        for payload in (
            response(quota(30, 15), None),
            {"rateLimits": {"limitId": "other", "primary": quota(30, 300)}},
            response(quota(30, 300), quota(60, 300)),
        ):
            with self.subTest(payload=payload):
                result = self.status(payload)
                self.assertEqual(result["subscription"]["windows"]["five_hour"]["state"], "unavailable")
                self.assertTrue(result["checkpoint_recommended"])

    def test_cache_fallback_is_stale_and_privately_written(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "usage.json"
            self.status(response(quota(2, 300), quota(60, 10080)), cache=cache)
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            with patch("usage.time.time", return_value=NOW + 1), patch(
                "usage.read_rate_limits", side_effect=usage.UsageUnavailable("app_server_read_failed")
            ):
                result = usage.usage_status(cache=cache)
            self.assertEqual(result["subscription"]["last_successful_measurement"], usage.utc(NOW))
            self.assertEqual(result["subscription"]["windows"]["five_hour"]["state"], "stale")
            self.assertEqual(result["adaptive_mode"], "conservation")
            self.assertTrue(result["checkpoint_recommended"])

    def test_context_is_separate_and_thread_bound(self):
        notification = {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "A", "tokenUsage": {
                "last": {"totalTokens": 2500},
                "total": {"totalTokens": 90000},
                "modelContextWindow": 10000,
            }},
        }
        payload = response(quota(60, 300), quota(60, 10080))
        result = self.status(payload, notification=notification, thread_id="A", observed_at=NOW - 1)
        self.assertEqual(result["context"]["used_percent"], 25)
        self.assertEqual(result["context"]["state"], "reliable")
        self.assertEqual(result["adaptive_mode"], "normal")
        stale = self.status(payload, notification=notification, thread_id="A", observed_at=NOW - 121)
        self.assertEqual(stale["context"]["state"], "stale")
        other = self.status(payload, notification=notification, thread_id="B", observed_at=NOW - 1)
        self.assertEqual(other["context"]["state"], "unavailable")

    def test_checkpoint_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project" / "checkpoint.json"
            completed = subprocess.run([
                sys.executable, usage.__file__, "checkpoint", str(path), "--milestone", "usage",
                "--completed", "measure quota", "--test-result", "pass", "--remaining", "desktop QA",
                "--next-step", "run isolated compositor",
            ], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(completed.stdout)["checkpoint_saved"], str(path))
            self.assertEqual(json.loads(path.read_text())["remaining"], ["desktop QA"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_app_server_request_is_read_only(self):
        script = """
import json
import sys

def receive():
    return json.loads(sys.stdin.readline())

request = receive()
assert request["method"] == "initialize"
print(json.dumps({"id": request["id"], "result": {}}), flush=True)
assert receive()["method"] == "initialized"
request = receive()
assert request["method"] == "account/rateLimits/read"
print(json.dumps({"method": "account/updated", "params": {}}))
print(json.dumps({"id": request["id"], "result": {"marker": True}}), flush=True)
"""
        popen = subprocess.Popen
        with patch("usage.subprocess.Popen", side_effect=lambda args, **kwargs: popen(
            [sys.executable, "-c", script], **kwargs
        )) as launch:
            self.assertEqual(usage.read_rate_limits(timeout=2), {"marker": True})
        self.assertEqual(launch.call_args.args[0], ["codex", "app-server"])

    def test_app_server_timeout_reaps_child(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        with patch("usage.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(usage.UsageUnavailable, "app_server_timeout"):
                usage.read_rate_limits(timeout=0.05)
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
