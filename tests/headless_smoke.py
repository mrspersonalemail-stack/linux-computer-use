"""Opt-in end-to-end test in an isolated compositor with disposable GTK windows."""

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PYTHON = "/usr/bin/python3"
SWAY_XDG_ADAPTER = """
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
struct wl_display;
struct wlr_xdg_shell;
struct wlr_xdg_shell *wlr_xdg_shell_create(struct wl_display *display, uint32_t version) {
    struct wlr_xdg_shell *(*create)(struct wl_display *, uint32_t) =
        dlsym(RTLD_NEXT, "wlr_xdg_shell_create");
    return create(display, version < 6 ? 6 : version);
}
"""


def wait_for(probe, label, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError(f"Timed out waiting for {label}")


def terminate(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def create_fixture(events):
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk

    def record(event):
        with events.open("a") as stream:
            stream.write(event + "\n")

    app = Gtk.Application(application_id="org.example.CodexDesktopFixture")

    def activate(app):
        colors = Gtk.CssProvider()
        colors.load_from_string(
            ".fixture-button {background: #168434; color: white; border: 0; border-radius: 0;}"
            ".other-button {background: #2433c4; color: white; border: 0; border-radius: 0;}"
        )
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), colors, 800)
        for title, css in (("Codex Test Target", "fixture-button"), ("Codex Test Other", "other-button")):
            window = Gtk.ApplicationWindow(application=app, title=title, decorated=False)
            window.set_default_size(400, 300)
            button = Gtk.Button(label=title)
            button.add_css_class(css)
            window.set_child(button)
            if css == "fixture-button":
                button.connect("clicked", lambda *_: record("click"))
                pointer = Gtk.EventControllerMotion()
                pointer.connect("enter", lambda _, x, y: record(f"motion {round(x)} {round(y)}"))
                pointer.connect("motion", lambda _, x, y: record(f"motion {round(x)} {round(y)}"))
                button.add_controller(pointer)
                scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
                scroll.connect("scroll", lambda *_: record("scroll") or False)
                button.add_controller(scroll)
                drag = Gtk.GestureDrag()
                drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                drag.connect("drag-end", lambda _, x, y: record(f"drag {round(x)} {round(y)}"))
                button.add_controller(drag)
                keys = Gtk.EventControllerKey()
                keys.connect("key-pressed", lambda _controller, keyval, *_: record(f"key {Gdk.keyval_name(keyval)}") or False)
                window.add_controller(keys)
            window.present()

    app.connect("activate", activate)
    app.run([])


def image_pixel(png, x, y):
    return json.loads(subprocess.check_output(
        [SYSTEM_PYTHON, "-I", "-c",
         "from PIL import Image; import io,json,sys; "
         "im=Image.open(io.BytesIO(sys.stdin.buffer.read())).convert('RGB'); "
         "print(json.dumps([im.size, im.getpixel(tuple(map(int,sys.argv[1:])))]))",
         str(x), str(y)],
        input=png, timeout=3,
    ))


def run_checks(runtime, start):
    sys.path.insert(0, str(ROOT))
    import desktop
    import server
    from session import Denied, Session

    # A headless nested seat has no physical input devices. Keep its normal
    # Wayland virtual capabilities available before GTK binds to the seat.
    start("input", [str(server.WDOTOOL), "prime"])
    wait_for(lambda: "ready" in (runtime / "input.log").read_text(), "virtual input seat")
    events = runtime / "events"
    fixture = start("gtk", [SYSTEM_PYTHON, str(Path(__file__).resolve()), "--fixture", str(events)])
    wait_for(lambda: len(desktop.Desktop().windows()) == 2, "both GTK windows")
    session = Session()
    assert server.desktop_status()["state"] == "paused"
    try:
        server.desktop_screenshot("screen")
    except Denied:
        pass
    else:
        raise AssertionError("Screenshot was accepted without operator permission")

    def recorded(predicate):
        return wait_for(lambda: (
            next((line for line in events.read_text().splitlines() if predicate(line)), None)
            if events.is_file() else None
        ), "GTK event")

    # Grant and approve through test fixtures only, in this private compositor.
    with session.lock("operator"), patch.object(server, "approve"):
        session.lease_path.write_text(json.dumps({"mode": "control", "expires": time.monotonic() + 40, "token": "test"}))
        assert server.desktop_status()["state"] == "active"
        target = next(item for item in server.desktop_inspect("windows")["data"] if item["title"] == "Codex Test Target")
        target_id = target["id"]
        assert server.desktop_action("focus", window_id=target_id)["completed"]
        assert server.desktop_action("resize", window_id=target_id, width=360, height=280)["completed"]
        target = server.window(target_id)
        x, y = target["x"], target["y"]
        monitor = next(item for item in server.desktop_inspect("monitors")["data"] if item["id"] == target["monitor_id"])
        shots = {}
        for kind, identifier, px, py in (
            ("window", target_id, 40, 40),
            ("monitor", monitor["id"], x + 40 - monitor["x"], y + 40 - monitor["y"]),
            ("screen", None, x + 40 - monitor["x"], y + 40 - monitor["y"]),
        ):
            started = time.monotonic()
            image = server.desktop_screenshot(kind, identifier)
            shots[kind] = round((time.monotonic() - started) * 1000, 1)
            dimensions, rgb = image_pixel(image.data, px, py)
            expected = [360, 280] if kind == "window" else [monitor["width"], monitor["height"]]
            assert dimensions == expected, (kind, dimensions, expected)
            assert rgb[1] > max(rgb[0], rgb[2]) + 45, (kind, rgb)
        events.write_text("")
        assert server.desktop_action("move", window_id=target_id, x=x + 80, y=y + 80)["completed"]
        cursor = json.loads(subprocess.check_output(["hyprctl", "-j", "cursorpos"], text=True, timeout=3))
        assert abs(cursor["x"] - (x + 80)) <= 2 and abs(cursor["y"] - (y + 80)) <= 2, (cursor, x, y)
        motion = recorded(lambda line: line.startswith("motion "))
        moved_x, moved_y = map(int, motion.split()[1:])
        events.write_text("")
        assert server.desktop_action("move", window_id=target_id, x=x + 90, y=y + 95)["completed"]
        motion = recorded(lambda line: line.startswith("motion "))
        next_x, next_y = map(int, motion.split()[1:])
        assert abs(next_x - moved_x - 10) <= 2 and abs(next_y - moved_y - 15) <= 2
        assert server.desktop_action("click", window_id=target_id, x=x + 80, y=y + 80)["completed"]
        recorded(lambda line: line == "click")
        assert server.desktop_action("key", window_id=target_id, key="a")["completed"]
        recorded(lambda line: line == "key a")
        assert server.desktop_action("type", window_id=target_id, text="xy")["completed"]
        recorded(lambda line: line == "key x")
        recorded(lambda line: line == "key y")
        assert server.desktop_action("scroll", window_id=target_id, x=x + 80, y=y + 80, delta=120)["completed"]
        recorded(lambda line: line == "scroll")
        assert server.desktop_action("drag", window_id=target_id, x=x + 80, y=y + 80,
                                     end_x=x + 160, end_y=y + 140)["completed"]
        drag = recorded(lambda line: line.startswith("drag 80 ") or line.startswith("drag 79 "))
        dx, dy = map(int, drag.split()[1:])
        assert abs(dx - 80) <= 2 and abs(dy - 60) <= 2, (dx, dy)
        assert any(item["name"] == "Codex Test Target" for item in server.desktop_inspect("accessibility")["data"])
        assert server.desktop_action("clipboard_write", text="isolated fixture")["completed"]
        assert server.desktop_action("clipboard_read")["text"] == "isolated fixture"
        subprocess.run(["hyprctl", "output", "create", "headless", "CODEX-TEST-2"], check=True,
                       capture_output=True, timeout=3)
        second = wait_for(lambda: next((item for item in desktop.Desktop().monitors()
                                       if item["id"] == "CODEX-TEST-2" and item["scale"] == 1.25), None),
                          "fractionally scaled second output")
        assert (second["x"], second["y"], second["width"], second["height"]) == (-640, 0, 640, 480), second
        assert desktop.Desktop().pixel_to_global("CODEX-TEST-2", 40, 20) == (-600, 20)
        capture = server.desktop_screenshot("monitor", "CODEX-TEST-2")
        assert image_pixel(capture.data, 40, 20)[0] == [640, 480]
        assert server.desktop_action("focus", window_id=target_id)["completed"]
        target = server.window(target_id)
        x, y = target["x"], target["y"]
        assert server.desktop_action("move", window_id=target_id, x=x + 80, y=y + 80)["completed"]
        cursor = json.loads(subprocess.check_output(["hyprctl", "-j", "cursorpos"], text=True, timeout=3))
        assert abs(cursor["x"] - (x + 80)) <= 2 and abs(cursor["y"] - (y + 80)) <= 2, (cursor, x, y)
        session.lease_path.unlink()
        assert server.desktop_status()["state"] == "paused"
        terminate(fixture)
        wait_for(lambda: not desktop.Desktop().windows(), "GTK crash cleanup")
        applications = Path(os.environ["XDG_DATA_HOME"]) / "applications"
        applications.mkdir(parents=True)
        desktop_id = "org.example.CodexDesktopFixture.desktop"
        (applications / desktop_id).write_text(
            "[Desktop Entry]\nType=Application\nName=Codex isolated fixture\n"
            f'Exec={SYSTEM_PYTHON} "{Path(__file__).resolve()}" --fixture "{events}"\n'
        )
        session.lease_path.write_text(json.dumps({"mode": "control", "expires": time.monotonic() + 10, "token": "restart"}))
        assert any(app["id"] == desktop_id for app in server.desktop_inspect("applications")["data"])
        assert server.desktop_action("launch", application_id=desktop_id)["completed"]
        wait_for(lambda: len(desktop.Desktop().windows()) == 2, "GTK restart")
        session.lease_path.unlink()

    operator = start("operator", [sys.executable, str(ROOT / "session.py"), "grant", "observe", "--minutes", "1"])
    wait_for(lambda: any(item["title"] == "Grant Codex desktop access" for item in desktop.Desktop().windows()),
             "operator approval dialog")
    assert server.desktop_status()["state"] == "paused"
    for key in ("Tab", "Return"):
        subprocess.run([str(server.WDOTOOL), "--backend", "wlr-protocols", "key", key],
                       check=True, timeout=3)
    wait_for(lambda: server.desktop_status()["state"] == "active", "approved operator lease")
    assert any(item["title"] == "Codex desktop access" for item in desktop.Desktop().windows())
    subprocess.run([sys.executable, str(ROOT / "session.py"), "stop"], check=True, timeout=3)
    assert operator.wait(timeout=3) == 0
    assert server.desktop_status()["state"] == "paused"
    print(json.dumps({"result": "passed", "screenshot_latency_ms": shots}))


def worker(sway, sway_libs):
    assert not list(Path("/dev/dri").glob("card*"))
    assert not (Path("/run/user") / str(os.getuid())).exists()
    assert not os.environ.get("WAYLAND_DISPLAY")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix="cdx-") as directory, ExitStack() as stack:
        runtime = Path(directory)
        os.environ.update({
            "XDG_RUNTIME_DIR": directory, "XDG_CONFIG_HOME": str(runtime / "config"),
            "XDG_DATA_HOME": str(runtime / "data"), "XDG_CACHE_HOME": str(runtime / "cache"),
            "HYPRLAND_NO_SD_VARS": "1", "HYPRLAND_NO_SD_NOTIFY": "1", "HYPRLAND_NO_SD_TARGET": "1",
            "HYPRLAND_NO_CRASHREPORTER": "1", "GDK_BACKEND": "wayland", "GTK_A11Y": "atspi",
            "GTK_USE_PORTAL": "0", "GSETTINGS_BACKEND": "memory",
        })

        def start(label, command, extra=None):
            stream = stack.enter_context((runtime / (label + ".log")).open("wb"))
            process = subprocess.Popen(command, cwd=runtime, env=os.environ | (extra or {}),
                                       stdout=stream, stderr=stream)
            stack.callback(terminate, process)
            return process

        sway_config = runtime / "sway.conf"
        sway_config.write_text("xwayland disable\n")
        sway_env = {
            "WLR_BACKENDS": "headless", "WLR_HEADLESS_OUTPUTS": "1", "WLR_RENDERER": "gles2",
            "WLR_RENDER_DRM_DEVICE": str(min(Path("/dev/dri").glob("renderD*"))),
            "LD_PRELOAD": "/tmp/codex-test-tools/sway-xdg6.so",
        }
        if sway_libs:
            sway_env["LD_LIBRARY_PATH"] = sway_libs
        start("sway", [sway, "-c", str(sway_config)], sway_env)

        def sockets():
            return {item.name for item in runtime.glob("wayland-*") if item.is_socket()}

        try:
            outer = wait_for(sockets, "headless Sway")
        except AssertionError:
            print((runtime / "sway.log").read_text(errors="replace")[-1200:], file=sys.stderr)
            raise
        hypr_config = runtime / "hyprland.lua"
        hypr_config.write_text(
            'hl.monitor({output = "", mode = "800x600@60", position = "auto", scale = "1"})\n'
            'hl.monitor({output = "CODEX-TEST-2", mode = "800x600@60", position = "-640x0", scale = "1.25"})\n'
            'hl.config({animations = {enabled = false}, xwayland = {enabled = false}})\n'
            'hl.window_rule({match = {class = "org.example.CodexDesktopFixture"}, float = true})\n'
        )
        start("hypr", ["Hyprland", "--config", str(hypr_config)], {"WAYLAND_DISPLAY": next(iter(outer))})
        try:
            os.environ["WAYLAND_DISPLAY"] = next(iter(wait_for(lambda: sockets() - outer, "nested Hyprland")))
            socket = wait_for(lambda: next(iter(runtime.glob("hypr/*/.socket.sock")), None), "Hyprland IPC")
            os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = socket.parent.name
            run_checks(runtime, start)
        except (AssertionError, RuntimeError, OSError):
            for name in ("sway", "hypr", "gtk"):
                log = runtime / (name + ".log")
                if log.exists():
                    print(log.read_text(errors="replace")[-1200:], file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sway", default=shutil.which("sway"), help="Sway 1.12 with wlroots 0.20")
    parser.add_argument("--sway-libs", help="Shared libraries to use for a disposable Sway package")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixture:
        create_fixture(args.fixture)
        return
    if args.worker:
        worker(args.sway, args.sway_libs)
        return
    if not args.sway:
        parser.error("Sway is required. Install it or pass --sway.")
    nodes = sorted(Path("/dev/dri").glob("renderD*"))
    if not nodes:
        parser.error("A GPU render node is required.")
    with tempfile.TemporaryDirectory(prefix="codex-test-tools-") as directory:
        tools = Path(directory)
        source = tools / "sway-xdg6.c"
        source.write_text(SWAY_XDG_ADAPTER)
        subprocess.run(["cc", "-shared", "-fPIC", source, "-o", tools / "sway-xdg6.so"], check=True)
        command = ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev",
                   "--proc", "/proc", "--tmpfs", "/run", "--tmpfs", "/tmp", "--dir", "/dev/dri", "--clearenv",
                   "--setenv", "PATH", "/usr/bin"]
        for node in nodes:
            command.extend(["--dev-bind", str(node), str(node)])
        sway = Path(args.sway).resolve()
        command.extend(["--ro-bind", str(sway.parent), "/tmp/codex-test-sway-bin"])
        args_inside = ["--sway", f"/tmp/codex-test-sway-bin/{sway.name}"]
        if args.sway_libs:
            command.extend(["--ro-bind", str(Path(args.sway_libs).resolve()), "/tmp/codex-test-sway-libs"])
            args_inside.extend(["--sway-libs", "/tmp/codex-test-sway-libs"])
        command.extend(["--ro-bind", directory, "/tmp/codex-test-tools", "--", "dbus-run-session",
                        sys.executable, str(Path(__file__).resolve()), "--worker", *args_inside])
        subprocess.run(command, cwd=ROOT, check=True, timeout=50)


if __name__ == "__main__":
    main()
