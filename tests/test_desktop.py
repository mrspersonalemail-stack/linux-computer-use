import struct
import sys
import time
import unittest
from unittest.mock import patch

from desktop import Desktop, DesktopError, _dimensions, _read_command


def monitor(**changes):
    raw = {"name": "DP-1", "id": 0, "width": 1920, "height": 1080,
           "x": 0, "y": 0, "transform": 0, "scale": 1, "focused": True}
    return {**raw, **changes}


def window(**changes):
    raw = {"address": "0x42", "stableId": "7", "class": "fixture", "title": "Test window",
           "at": [20, 30], "size": [400, 300], "pid": 123, "monitor": 0,
           "mapped": True, "visible": True}
    return {**raw, **changes}


def png_header(width, height):
    # Only container boundaries and IHDR dimensions are inspected by the backend.
    return (b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + struct.pack(">II", width, height)
            + bytes(9) + b"\0\0\0\0IEND\xaeB\x60\x82")


class DesktopTests(unittest.TestCase):
    def test_scaled_rotated_output_with_negative_origin(self):
        rotated = monitor(width=2400, height=1200, transform=1, scale=1.5, x=-800, y=-40)
        with patch("desktop._hypr", return_value=[rotated]):
            desktop = Desktop()
            output = desktop.monitors()[0]
            self.assertEqual((output["width"], output["height"]), (800, 1600))
            self.assertEqual((output["physical_width"], output["physical_height"]), (1200, 2400))
            self.assertEqual(desktop.pixel_to_global("DP-1", 799, 1300), (-1, 1260))
            with self.assertRaises(DesktopError):
                desktop.pixel_to_global("DP-1", 800, 1300)

    def test_disconnected_status_does_not_raise(self):
        with patch("desktop._hypr", side_effect=DesktopError("unavailable")):
            self.assertEqual(Desktop().status()["available"], False)

    def test_monitor_capture_stays_on_selected_logical_output(self):
        first = monitor(name="eDP-1", width=2880, height=1800, scale=1.5)
        second = monitor(id=1, x=-1920, focused=False)
        image = png_header(1920, 1200)
        with patch("desktop._hypr", return_value=[first, second]), patch("desktop._read_command", return_value=image) as read:
            self.assertEqual(Desktop().screenshot("monitor", "eDP-1"), image)
            self.assertEqual(read.call_args.args[0], ["grim", "-s", "1", "-t", "png", "-o", "eDP-1", "-"])

    def test_window_capture_is_isolated_and_never_falls_back(self):
        image = png_header(400, 300)
        with patch("desktop._hypr", side_effect=[[monitor()], [window()]]), patch("desktop._read_command", return_value=image) as read:
            self.assertEqual(Desktop().screenshot("window", "0x42"), image)
            self.assertEqual(read.call_args.args[0], ["grim", "-s", "1", "-t", "png", "-T", "7", "-"])
        with patch("desktop._hypr", side_effect=[[monitor()], [window(stableId="")]]), patch("desktop._read_command") as read:
            with self.assertRaisesRegex(DesktopError, "stable identifier"):
                Desktop().screenshot("window", "0x42")
            read.assert_not_called()

    def test_monitor_capture_rejects_overlapping_output(self):
        second = monitor(name="DP-2", id=1, x=10)
        with patch("desktop._hypr", return_value=[monitor(), second]), patch("desktop._read_command") as read:
            with self.assertRaisesRegex(DesktopError, "intersects"):
                Desktop().screenshot("monitor", "DP-1")
            read.assert_not_called()

    def test_hidden_window_cannot_be_captured(self):
        with patch("desktop._hypr", side_effect=[[monitor()], [window(visible=False)]]), patch("desktop._read_command") as read:
            with self.assertRaisesRegex(DesktopError, "not visible"):
                Desktop().screenshot("window", "0x42")
            read.assert_not_called()

    def test_full_screen_capture_rejects_overlarge_layout_before_capture(self):
        far_away = monitor(name="DP-2", id=1, x=25_000)
        with patch("desktop._hypr", return_value=[monitor(), far_away]), patch("desktop._read_command") as read:
            with self.assertRaisesRegex(DesktopError, "pixel limit"):
                Desktop().screenshot()
            read.assert_not_called()

    def test_accessibility_rejects_changed_window(self):
        states = [{"id": "0x42", "pid": 1}, {"id": "0x43", "pid": 2}]
        with patch.object(Desktop, "active_window", side_effect=states), patch("desktop._read_json", return_value=[]):
            with self.assertRaisesRegex(DesktopError, "window changed"):
                Desktop().accessibility()

    def test_incomplete_png_or_oversize_header_rejected(self):
        with self.assertRaises(DesktopError):
            _dimensions(png_header(1, 2)[:-1])
        with self.assertRaisesRegex(DesktopError, "pixel limit"):
            _dimensions(png_header(10_000, 10_000))

    def test_command_reader_enforces_size_timeout_and_redaction(self):
        with self.assertRaisesRegex(DesktopError, "size limit"):
            _read_command([sys.executable, "-c", "print('x' * 16)"], limit=8)
        with self.assertRaisesRegex(DesktopError, "timed out"):
            _read_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
        with self.assertRaisesRegex(DesktopError, r"^python[^ ]* failed$"):
            _read_command([sys.executable, "-c", "raise SystemExit('do-not-display')"])

    def test_command_reader_cancels_a_silent_process(self):
        class Revoked(Exception):
            pass

        started = time.monotonic()

        def check():
            if time.monotonic() - started > 0.09:
                raise Revoked

        with self.assertRaises(Revoked):
            _read_command([sys.executable, "-c", "import time; time.sleep(5)"], check=check)
        self.assertLess(time.monotonic() - started, 1)
