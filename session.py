"""Operator lease, command cancellation, and redacted desktop audit."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from contextlib import contextmanager


class Denied(RuntimeError):
    """The requested desktop access is unavailable or was declined."""


class Session:
    def __init__(self):
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        display = os.environ.get("WAYLAND_DISPLAY")
        if not all((runtime, signature, display)):
            raise Denied("Hyprland Wayland session is unavailable.")
        base = Path(runtime)
        self.check_private(base)
        scope = hashlib.sha256(f"{signature}\0{display}".encode()).hexdigest()[:20]
        self.directory = base / f"codex-desktop-{scope}"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.check_private(self.directory)
        self.lease_path = self.directory / "lease.json"

    @staticmethod
    def check_private(path):
        state = path.lstat()
        if path.is_symlink() or not path.is_dir() or state.st_uid != os.getuid() or state.st_mode & 0o077:
            raise Denied("The desktop runtime directory must be private and owned by this user.")

    @contextmanager
    def lock(self, name):
        fd = os.open(self.directory / f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Denied("Another desktop operation is already in progress.") from None
            yield

    def lease(self):
        try:
            grant = json.loads(self.lease_path.read_text())
            if (grant["mode"] not in ("observe", "control")
                    or not isinstance(grant["token"], str)
                    or type(grant["expires"]) not in (float, int)):
                return None
            fd = os.open(self.directory / "operator.lock", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass  # The visible operator session still owns the lock.
                else:
                    return None
            if grant["expires"] > time.monotonic():
                return grant
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def require(self, control=False, token=None):
        grant = self.lease()
        if not grant or (control and grant["mode"] != "control") or (token and token != grant["token"]):
            raise Denied("Desktop access is paused or this permission was revoked.")
        return grant

    def audit(self, action, result):
        # Fixed vocabulary only. Never include tool arguments or exception text.
        record = json.dumps({"time": time.time(), "action": action, "result": result}) + "\n"
        fd = os.open(self.directory / "last-action.tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(record)
        os.replace(self.directory / "last-action.tmp", self.directory / "last-action.json")
        fd = os.open(self.directory / "audit.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            if os.fstat(stream.fileno()).st_size < 256_000:
                stream.write(record)

    def last_action(self):
        try:
            return json.loads((self.directory / "last-action.json").read_text())
        except (OSError, ValueError):
            return None

    def run(self, args, grant, *, data=None, seconds=5, output=False, check=None):
        self.require(token=grant["token"])
        if check:
            check()
        with tempfile.TemporaryFile(dir=self.directory) as result:
            with subprocess.Popen(
                args, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                stdout=result if output else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            ) as process:
                deadline = time.monotonic() + seconds
                try:
                    first = True
                    while True:
                        self.require(token=grant["token"])
                        if check:
                            check()
                        if output and os.fstat(result.fileno()).st_size > 65_536:
                            raise Denied("Desktop text exceeds 64 KiB.")
                        if time.monotonic() >= deadline:
                            raise Denied("Desktop operation timed out.")
                        try:
                            process.communicate(input=data if first else None, timeout=0.06)
                            break
                        except subprocess.TimeoutExpired:
                            first = False
                    self.require(token=grant["token"])
                    if check:
                        check()
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=0.3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            if process.returncode:
                raise Denied("Desktop operation was declined or the native command failed.")
            if output:
                result.seek(0)
                payload = result.read(65_537)
                if len(payload) > 65_536:
                    raise Denied("Desktop text exceeds 64 KiB.")
                return payload

    def open_operator(self, mode, minutes):
        with self.lock("operator"):
            self.lease_path.unlink(missing_ok=True)
            accepted = subprocess.run(
                ["zenity", "--question", "--no-markup", "--default-cancel",
                 "--title=Grant Codex desktop access", "--ok-label=Grant access",
                 f"--text=Allow {mode} access to this desktop for up to {minutes} minutes? "
                 "Codex can view desktop content. Each input and clipboard action will ask separately."],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60,
            )
            if accepted.returncode:
                raise Denied("Desktop access was declined.")
            # The operator starts this command. Its indicator stays visible
            # until Cancel, expiry, or an emergency stop from a second shell.
            with subprocess.Popen(
                ["zenity", "--progress", "--pulsate", "--title=Codex desktop access",
                 f"--text={mode.title()} access on this desktop. Cancel to stop immediately.",
                 "--cancel-label=Stop desktop access"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ) as panel:
                deadline = time.monotonic() + minutes * 60
                try:
                    time.sleep(0.35)
                    if panel.poll() is not None:
                        raise Denied("Could not open the desktop access indicator.")
                    fd = os.open(self.lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        json.dump({"mode": mode, "expires": deadline, "token": secrets.token_hex(16)}, stream)
                    while panel.poll() is None and time.monotonic() < deadline and self.lease_path.exists():
                        time.sleep(0.05)
                finally:
                    self.lease_path.unlink(missing_ok=True)
                    if panel.poll() is None:
                        panel.terminate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    grant = commands.add_parser("grant")
    grant.add_argument("mode", choices=("observe", "control"))
    grant.add_argument("--minutes", type=int, choices=range(1, 31), default=10, metavar="1–30")
    commands.add_parser("stop")
    commands.add_parser("status", help="print local operator access state without desktop content")
    args = parser.parse_args()
    try:
        session = Session()
        if args.command == "stop":
            session.lease_path.unlink(missing_ok=True)
        elif args.command == "status":
            grant = session.lease()
            print(json.dumps({"state": "active" if grant else "paused",
                              "mode": grant["mode"] if grant else None,
                              "seconds_left": max(0, int(grant["expires"] - time.monotonic())) if grant else 0}))
        else:
            session.open_operator(args.mode, args.minutes)
    except (Denied, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
