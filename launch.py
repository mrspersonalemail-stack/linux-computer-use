"""Start the MCP server using the current local login's Wayland environment."""
import os
from pathlib import Path
import subprocess
import sys

keys = {
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
    "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY",
}
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
if not all(os.environ.get(key) for key in ("HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY")):
    try:
        variables = subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True, text=True, timeout=2, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        variables = ""
    for line in variables.splitlines():
        key, _, value = line.partition("=")
        if key in keys:
            os.environ.setdefault(key, value)

server = Path(__file__).resolve().with_name("server.py")
os.execv(sys.executable, [sys.executable, str(server)])
