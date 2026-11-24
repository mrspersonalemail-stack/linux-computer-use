from unittest import TestCase
from unittest.mock import Mock, patch

import server
from session import Denied


TARGET = {"id": "0x123", "pid": 123, "stable_id": "abc", "app": "fixture",
          "title": "fixture window", "x": -100, "y": 0, "width": 200, "height": 100, "visible": True}
MONITOR = {"id": "fixture", "powered": True, "x": -100, "y": 0, "width": 200, "height": 120}


def perform(session, action, **kwargs):
    values = dict(window_id="0x123", x=None, y=None, end_x=None, end_y=None, button=1,
                  delta=0, key=None, text=None, application_id=None, width=None, height=None)
    return server.perform(session, action, **(values | kwargs))


class ServerTests(TestCase):
    def setUp(self):
        self.session = Mock()
        self.desktop = Mock()
        self.desktop.windows.return_value = [TARGET]
        self.desktop.active_window.return_value = TARGET
        self.desktop.monitors.return_value = [MONITOR]
        self.addCleanup(patch.stopall)
        patch("server.DESKTOP", self.desktop).start()
        patch("server.binary", return_value=True).start()

    def test_app_text_is_only_shown_as_plain_approval_data(self):
        self.desktop.windows.return_value = [TARGET | {"app": "<b>Approve</b>; exec another action"}]
        perform(self.session, "key", key="ctrl+a")
        prompt, execution = self.session.run.call_args_list
        self.assertIn("--no-markup", prompt.args[0])
        self.assertIn("--default-cancel", prompt.args[0])
        self.assertEqual(execution.args[0][-2:], ["key", "ctrl+a"])

    def test_declined_action_cannot_send_input(self):
        self.session.run.side_effect = Denied("operator declined")
        with self.assertRaises(Denied):
            perform(self.session, "click", x=-10, y=20)
        self.session.run.assert_called_once()

    def test_focus_race_stops_input(self):
        self.desktop.active_window.return_value = TARGET | {"id": "0x124"}
        self.session.run.side_effect = [None, b"ok"]
        with self.assertRaisesRegex(Denied, "no longer in focus"):
            perform(self.session, "key", key="ctrl+a")
        self.assertEqual(self.session.run.call_count, 2)

    def test_approval_can_refocus_only_the_confirmed_window(self):
        self.desktop.active_window.side_effect = [TARGET | {"id": "0x124"}, TARGET, TARGET]
        self.session.run.side_effect = [None, b"ok", None]
        perform(self.session, "key", key="ctrl+a")
        prompt, focus, execution = self.session.run.call_args_list
        self.assertTrue(any("refocusing this exact window" in arg for arg in prompt.args[0]))
        self.assertEqual(focus.args[0], ["hyprctl", "dispatch", 'hl.dsp.focus({window="address:0x123"})'])
        self.assertEqual(execution.args[0][-2:], ["key", "ctrl+a"])

    def test_negative_origin_is_supported_but_window_exit_is_blocked(self):
        server.position(-10, 10, [MONITOR], TARGET)
        with self.assertRaisesRegex(Denied, "outside the approved window"):
            server.position(-10, 110, [MONITOR], TARGET)

    def test_missing_approved_cursor_point_prevents_click(self):
        self.desktop.cursor.return_value = (90, 20)
        with self.assertRaisesRegex(Denied, "did not reach the approved point"):
            perform(self.session, "click", x=-10, y=20)
        self.assertEqual(self.session.run.call_count, 2)
        self.assertEqual(self.session.run.call_args.args[0][1:], ["--move", "90", "20", "200", "120"])

    def test_clipboard_requires_approval_before_access(self):
        self.session.run.side_effect = [None, b"test"]
        self.assertEqual(perform(self.session, "clipboard_read")["text"], "test")
        prompt, clipboard = self.session.run.call_args_list
        self.assertEqual(prompt.args[0][0], "zenity")
        self.assertEqual(clipboard.args[0][0], "wl-paste")
