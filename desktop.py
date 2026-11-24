"""Read-only Wayland desktop access through Hyprland, grim, and optional GI."""

import json
import math
import os
import re
import selectors
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path


class DesktopError(RuntimeError):
    """A desktop operation failed without exposing subprocess or desktop content."""


MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
SYSTEM_PYTHON = "/usr/bin/python3"


def _read_command(args, *, limit=1024 * 1024, timeout=3.0, check=None):
    """Bound wall time and stdout while suppressing untrusted diagnostics."""
    command = Path(args[0]).name
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        if check:
            check()
        with subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise DesktopError(f"{command} timed out")
                        if check:
                            check()
                        if not selector.select(min(0.06, remaining) if check else remaining):
                            continue
                        chunk = os.read(process.stdout.fileno(), min(64 * 1024, limit - len(output) + 1))
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > limit:
                            raise DesktopError(f"{command} exceeded the response size limit")
                while process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DesktopError(f"{command} timed out")
                    if check:
                        check()
                    try:
                        process.wait(timeout=min(0.06, remaining) if check else remaining)
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    raise DesktopError(f"{command} failed")
                if check:
                    check()
            except BaseException:
                if process.poll() is None:
                    process.kill()
                raise
    except OSError as exc:
        raise DesktopError(f"{command} is unavailable") from exc
    return bytes(output)


def _read_json(args, *, expected, timeout=3.0, check=None):
    try:
        result = json.loads(_read_command(args, timeout=timeout, check=check))
    except (ValueError, RecursionError) as exc:
        raise DesktopError("Desktop service returned invalid JSON") from exc
    if not isinstance(result, expected):
        raise DesktopError("Desktop service returned an unexpected response")
    return result


def _hypr(query, expected, *, check=None):
    return _read_json(["hyprctl", "-j", query], expected=expected, check=check)


def _short_text(value):
    return value[:256] if isinstance(value, str) else ""


def _monitor(raw):
    transform = int(raw["transform"])
    physical_width, physical_height = int(raw["width"]), int(raw["height"])
    if transform % 2:
        physical_width, physical_height = physical_height, physical_width
    scale = float(raw["scale"])
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("invalid scale")
    width, height = round(physical_width / scale), round(physical_height / scale)
    if min(width, height) < 1 or not isinstance(raw["name"], str) or not raw["name"]:
        raise ValueError("invalid monitor geometry")
    return {
        "id": raw["name"], "index": raw["id"], "x": int(raw["x"]), "y": int(raw["y"]),
        "width": width, "height": height, "physical_width": physical_width,
        "physical_height": physical_height, "scale": scale, "transform": transform,
        "focused": bool(raw.get("focused")), "powered": bool(raw.get("dpmsStatus", True)),
    }


def _window(raw, monitors):
    x, y = map(int, raw["at"])
    width, height = map(int, raw["size"])
    address = raw["address"]
    if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", address):
        raise ValueError("invalid window identifier")
    monitor_id = next((monitor["id"] for monitor in monitors if monitor["index"] == raw["monitor"]), None)
    return {
        "id": address, "stable_id": _short_text(raw.get("stableId")), "app": _short_text(raw.get("class")),
        "title": _short_text(raw.get("title")), "pid": int(raw.get("pid", -1)), "monitor_id": monitor_id,
        "x": x, "y": y, "width": width, "height": height,
        "visible": bool(raw.get("mapped") and not raw.get("hidden") and raw.get("visible", True)),
    }


def _dimensions(image):
    if len(image) < 45 or not image.startswith(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR"):
        raise DesktopError("Screenshot returned an invalid PNG")
    if not image.endswith(b"\0\0\0\0IEND\xaeB\x60\x82"):
        raise DesktopError("Screenshot returned an incomplete PNG")
    width, height = struct.unpack_from(">II", image, 16)
    if min(width, height) < 1 or width * height > MAX_IMAGE_PIXELS:
        raise DesktopError("Screenshot exceeds the pixel limit")
    return width, height


def _layout_size(monitors):
    if not monitors:
        raise DesktopError("No displays are available")
    left = min(item["x"] for item in monitors)
    top = min(item["y"] for item in monitors)
    width = max(item["x"] + item["width"] for item in monitors) - left
    height = max(item["y"] + item["height"] for item in monitors) - top
    return width, height


def _overlap(first, second):
    return (
        first["x"] < second["x"] + second["width"] and second["x"] < first["x"] + first["width"]
        and first["y"] < second["y"] + second["height"] and second["y"] < first["y"] + first["height"]
    )


class Desktop:
    def cursor(self):
        raw = _hypr("cursorpos", dict)
        try:
            return int(raw["x"]), int(raw["y"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DesktopError("Hyprland returned an invalid cursor position") from exc

    def monitors(self, *, check=None):
        try:
            return [_monitor(raw) for raw in _hypr("monitors", list, check=check)]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DesktopError("Hyprland returned invalid monitor geometry") from exc

    def windows(self, *, check=None):
        monitors = self.monitors(check=check)
        try:
            return [_window(raw, monitors) for raw in _hypr("clients", list, check=check) if raw.get("mapped")]
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DesktopError("Hyprland returned invalid window metadata") from exc

    def active_window(self, *, check=None):
        raw = _hypr("activewindow", dict, check=check)
        if not raw:
            return None
        try:
            return _window(raw, self.monitors(check=check))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DesktopError("Hyprland returned invalid active window metadata") from exc

    def status(self, *, include_window=True):
        try:
            monitors = self.monitors()
            active = self.active_window() if include_window else None
        except DesktopError as exc:
            return {"available": False, "active_window": None, "target_monitor": None, "error": str(exc)}
        focused = next((monitor["id"] for monitor in monitors if monitor["focused"]), None)
        return {
            "available": bool(monitors), "active_window": active, "target_monitor": focused,
            "capture_available": bool(monitors and shutil.which("grim") and os.environ.get("WAYLAND_DISPLAY")),
        }

    def screenshot(self, target="screen", identifier=None, *, check=None):
        """Return PNG bytes. Display captures use one pixel per logical unit."""
        args = ["grim", "-s", "1", "-t", "png"]
        if target == "screen" and identifier is None:
            width, height = _layout_size(self.monitors(check=check))
        elif target == "monitor" and isinstance(identifier, str):
            outputs = self.monitors(check=check)
            monitor = next((item for item in outputs if item["id"] == identifier), None)
            if not monitor or not monitor["powered"]:
                raise DesktopError("Monitor is unavailable")
            # grim -o crops the layout and can blend intersecting outputs.
            if any(item["id"] != identifier and _overlap(monitor, item) for item in outputs):
                raise DesktopError("Monitor intersects another output. Use an isolated window screenshot.")
            width, height = monitor["width"], monitor["height"]
            args.extend(["-o", monitor["id"]])
        elif target == "window" and isinstance(identifier, str):
            window = next((item for item in self.windows(check=check) if item["id"] == identifier), None)
            if not window or not window["visible"]:
                raise DesktopError("Window is unavailable or not visible")
            handle = window["stable_id"]
            if not re.fullmatch(r"[0-9a-fA-F]{1,32}", handle):
                raise DesktopError("Isolated window capture requires a Hyprland stable identifier")
            # Do not substitute a desktop crop. It can expose windows above the target.
            width, height = window["width"], window["height"]
            args.extend(["-T", handle])
        else:
            raise DesktopError("Invalid screenshot target or identifier")
        if min(width, height) < 1 or width * height > MAX_IMAGE_PIXELS:
            raise DesktopError("Screenshot exceeds the pixel limit")
        image = _read_command([*args, "-"], limit=MAX_IMAGE_BYTES, timeout=8.0, check=check)
        _dimensions(image)
        return image

    def pixel_to_global(self, monitor, x, y):
        """Translate a pixel in a logical-size monitor screenshot for pointer input."""
        output = next((item for item in self.monitors() if item["id"] == monitor), None)
        if not output or not output["powered"]:
            raise DesktopError("Monitor is unavailable")
        if type(x) is not int or type(y) is not int or not (0 <= x < output["width"] and 0 <= y < output["height"]):
            raise DesktopError("Pixel is outside the monitor")
        return output["x"] + x, output["y"] + y

    def applications(self, *, check=None):
        return _read_json(
            [SYSTEM_PYTHON, "-I", str(Path(__file__).resolve()), "applications"],
            expected=list, check=check,
        )

    def accessibility(self, *, check=None):
        window = self.active_window(check=check)
        if not window or window["pid"] <= 0:
            raise DesktopError("No active application for accessibility discovery")
        elements = _read_json(
            [SYSTEM_PYTHON, "-I", str(Path(__file__).resolve()), "accessibility", str(window["pid"])],
            expected=list, timeout=4.0, check=check,
        )
        current = self.active_window(check=check)
        if not current or current["id"] != window["id"] or current["pid"] != window["pid"]:
            raise DesktopError("Active window changed during accessibility discovery")
        return elements


def _applications():
    from gi.repository import Gio

    apps = (
        {"id": app.get_id(), "name": _short_text(app.get_display_name())}
        for app in Gio.AppInfo.get_all()
        if app.should_show() and app.get_id() and re.fullmatch(r"[\w][\w.+-]*\.desktop", app.get_id())
    )
    return sorted(apps, key=lambda app: (app["name"].casefold(), app["id"]))[:512]


def _accessibility(pid):
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    Atspi.set_timeout(250, 250)
    desktop = Atspi.get_desktop(0)
    if desktop is None:
        return []
    nodes = []
    for index in range(min(64, desktop.get_child_count())):
        try:
            app = desktop.get_child_at_index(index)
            if app is not None and app.get_process_id() == pid:
                nodes.append((app, 0))
        except GLib.Error:
            continue
    elements = []
    remaining = 128
    while nodes and remaining:
        node, depth = nodes.pop(0)
        remaining -= 1
        try:
            role = node.get_role()
            states = node.get_state_set()
            if role == Atspi.Role.PASSWORD_TEXT or states.contains(Atspi.StateType.DEFUNCT):
                continue
            if depth > 1 and not states.contains(Atspi.StateType.SHOWING):
                continue
            # Expose control names and roles only. Never read values or text interfaces.
            if role in (Atspi.Role.PUSH_BUTTON, Atspi.Role.CHECK_BOX, Atspi.Role.RADIO_BUTTON,
                        Atspi.Role.COMBO_BOX, Atspi.Role.MENU_ITEM, Atspi.Role.PAGE_TAB):
                elements.append({"role": _short_text(node.get_role_name()), "name": _short_text(node.get_name())})
            if depth < 6:
                for index in range(min(24, node.get_child_count(), max(0, remaining - len(nodes)))):
                    child = node.get_child_at_index(index)
                    if child is not None:
                        nodes.append((child, depth + 1))
        except GLib.Error:
            continue
    return elements


if __name__ == "__main__":
    if sys.argv[1:] == ["applications"]:
        print(json.dumps(_applications()))
    elif len(sys.argv) == 3 and sys.argv[1] == "accessibility":
        print(json.dumps(_accessibility(int(sys.argv[2]))))
    else:
        sys.exit("Use the Codex desktop integration")
