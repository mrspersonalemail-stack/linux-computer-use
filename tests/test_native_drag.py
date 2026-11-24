import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = ROOT / "native/wlr-virtual-pointer-unstable-v1.xml"


class NativeDragTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = os.environ.get("CC", "cc")
        if not all(shutil.which(name) for name in ("pkg-config", "wayland-scanner", compiler)):
            raise unittest.SkipTest("Requires a C compiler, pkg-config, and wayland-scanner.")
        if subprocess.run(["pkg-config", "--exists", "wayland-client", "wayland-server"]).returncode:
            raise unittest.SkipTest("Requires Wayland client and server development packages.")
        temporary = tempfile.TemporaryDirectory(prefix="codex-drag-test-")
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        cls.drag = cls.directory / "drag"
        cls.server = cls.directory / "wayland-test-server"
        subprocess.run([ROOT / "native/build.sh", cls.drag], check=True, timeout=15)
        for mode, name in (
            ("server-header", "virtual-pointer-server.h"),
            ("private-code", "virtual-pointer.c"),
        ):
            subprocess.run(["wayland-scanner", mode, PROTOCOL, cls.directory / name], check=True, timeout=10)
        flags = subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "wayland-server"], text=True, timeout=10
        )
        subprocess.run(
            [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", cls.directory,
             "-o", cls.server, ROOT / "tests/wayland_test_server.c", cls.directory / "virtual-pointer.c",
             *shlex.split(flags)],
            check=True, timeout=15,
        )

    def setUp(self):
        runtime = tempfile.TemporaryDirectory(dir=self.directory)
        self.addCleanup(runtime.cleanup)
        # Supply only the fake server's private runtime. In particular, never
        # inherit WAYLAND_SOCKET from a compositor that handed over a live FD.
        self.env = {"XDG_RUNTIME_DIR": runtime.name, "WAYLAND_DISPLAY": "missing"}

    def start_server(self):
        self.compositor = subprocess.Popen(
            [self.server], env=self.env, stdout=subprocess.PIPE, bufsize=0,
        )
        self.addCleanup(self.stop_server)
        name = self.event()
        self.assertRegex(name, r"^wayland-\d+$")
        self.assertTrue((Path(self.env["XDG_RUNTIME_DIR"]) / name).is_socket())
        self.env["WAYLAND_DISPLAY"] = name

    def stop_server(self):
        if self.compositor.poll() is None:
            self.compositor.terminate()
        try:
            self.compositor.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.compositor.kill()
            self.compositor.wait(timeout=2)
        self.compositor.stdout.close()

    def event(self):
        source = self.compositor.stdout
        self.assertTrue(select.select([source], [], [], 2)[0], "Wayland event was not received")
        line = source.readline()
        self.assertTrue(line, "Fake Wayland server closed the event stream")
        return line.decode("ascii").strip()

    def drag_command(self):
        return [self.drag, "5", "5", "20", "20", "100", "100"]

    def test_moves_press_and_release_in_frames(self):
        self.start_server()
        subprocess.run(self.drag_command(), env=self.env, check=True, timeout=3)
        events = [self.event() for _ in range(22)]
        self.assertEqual(events[1::2], ["frame"] * 11)
        self.assertEqual(events[:4], ["move 5 5 100 100", "frame", "press 272", "frame"])
        self.assertEqual(events[-4:], ["move 20 20 100 100", "frame", "release 272", "frame"])

    def test_move_does_not_press_a_button(self):
        self.start_server()
        subprocess.run([self.drag, "--move", "25", "7", "1440", "600"],
                       env=self.env, check=True, timeout=3)
        self.assertEqual(self.event(), "move 25 7 1440 600")
        self.assertEqual(self.event(), "frame")
        self.assertFalse(select.select([self.compositor.stdout], [], [], 0.06)[0])

    def test_signal_releases_pressed_button(self):
        self.start_server()
        action = subprocess.Popen(self.drag_command(), env=self.env, stderr=subprocess.PIPE, text=True)
        with action:
            try:
                self.assertEqual(self.event(), "move 5 5 100 100")
                self.assertEqual(self.event(), "frame")
                self.assertEqual(self.event(), "press 272")
                action.terminate()
                self.assertIn("Drag cancelled.", action.communicate(timeout=3)[1])
                self.assertEqual(action.returncode, 130)
                remaining = [self.event()]
                for _ in range(17):
                    if remaining[-1] == "release 272":
                        break
                    remaining.append(self.event())
                self.assertEqual(remaining[-1], "release 272")
                self.assertEqual(self.event(), "frame")
            finally:
                if action.poll() is None:
                    action.kill()

    def test_invalid_coordinates_and_missing_display(self):
        invalid = subprocess.run(
            [self.drag, "-1", "5", "20", "20", "100", "100"],
            env=self.env, capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(invalid.returncode, 2)
        disconnected = subprocess.run(
            self.drag_command(), env=self.env, capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(disconnected.returncode, 1)
        self.assertIn("Could not connect", disconnected.stderr)
