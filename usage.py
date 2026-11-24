"""Read Codex usage through the supported App Server protocol."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time


WINDOWS = {"five_hour": 300, "weekly": 10080}
THRESHOLDS = (5, 15, 30)
SOURCE = "Codex App Server account/rateLimits/read"
ACTION = {
    "normal": "Work normally and delegate when beneficial.",
    "conservation": "Consolidate work and prefer verified free or local delegation.",
    "completion": "Finish the current milestone and run its essential checks.",
    "critical": "Save a checkpoint and finish only small, safe, essential work.",
}


class UsageUnavailable(Exception):
    """A fixed diagnostic suitable for display without account data."""


def read_rate_limits(timeout=8):
    """Perform one read-only account request without starting a model turn."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as child, selectors.DefaultSelector() as ready:
        ready.register(child.stdout, selectors.EVENT_READ)
        buffer = bytearray()

        def send(message):
            child.stdin.write(json.dumps(message).encode() + b"\n")
            child.stdin.flush()

        def receive(request_id):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UsageUnavailable("app_server_timeout")
                if b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer[:] = rest
                    message = json.loads(line)
                    if not isinstance(message, dict) or message.get("id") != request_id:
                        continue
                    if not isinstance(message.get("result"), dict):
                        raise UsageUnavailable("app_server_read_failed")
                    return message["result"]
                if not ready.select(remaining):
                    raise UsageUnavailable("app_server_timeout")
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    raise UsageUnavailable("app_server_disconnected")
                buffer.extend(chunk)
                if len(buffer) > 2 * 1024 * 1024:
                    raise UsageUnavailable("app_server_response_too_large")

        try:
            send({
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "linux_desktop_usage", "version": "1.0"}},
            })
            receive(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            return receive(2)
        finally:
            child.kill()


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def object_or_empty(value):
    return value if isinstance(value, dict) else {}


def subscription_snapshot(response, measured_at):
    buckets = object_or_empty(response.get("rateLimitsByLimitId"))
    bucket = object_or_empty(buckets.get("codex"))
    if not bucket:
        legacy = object_or_empty(response.get("rateLimits"))
        if legacy.get("limitId") in (None, "codex"):
            bucket = legacy
    windows = {}
    for name, duration in WINDOWS.items():
        matches = []
        for slot in ("primary", "secondary"):
            window = object_or_empty(bucket.get(slot))
            used = window.get("usedPercent")
            if window.get("windowDurationMins") == duration and type(used) is int and 0 <= used <= 100:
                matches.append(window)
        if len(matches) == 1:
            reset = matches[0].get("resetsAt")
            windows[name] = {
                "remaining_percent": 100 - matches[0]["usedPercent"],
                "reset_epoch": reset if type(reset) is int and 0 < reset <= 253402300799 else None,
            }
    allowed = response.get("ordinaryUsageAllowed")
    if not windows and allowed is not False:
        raise UsageUnavailable("requested_quota_windows_missing")
    return {
        "windows": windows,
        "measured_epoch": measured_at,
        "ordinary_usage_allowed": allowed if type(allowed) is bool else None,
    }


def context_status(notification, thread_id, observed_at, now, max_age):
    unavailable = {"state": "unavailable", "reason": "active_thread_notification_unavailable"}
    params = object_or_empty(object_or_empty(notification).get("params"))
    if not thread_id or params.get("threadId") != thread_id or observed_at is None:
        return unavailable
    if notification.get("method") != "thread/tokenUsage/updated" or not 0 < observed_at <= now:
        return unavailable
    usage = object_or_empty(params.get("tokenUsage"))
    window = usage.get("modelContextWindow")
    tokens = object_or_empty(usage.get("last")).get("totalTokens")
    if type(window) is not int or window <= 0 or type(tokens) is not int or tokens < 0:
        return unavailable
    return {
        "state": "stale" if now - observed_at > max_age else "reliable",
        "source": "Codex App Server thread/tokenUsage/updated",
        "used_percent": round(100 * min(tokens, window) / window, 2),
        "last_usage_tokens": tokens,
        "model_context_window": window,
        "measured_at": utc(observed_at),
    }


def save_json(path, value):
    """Commit one private file atomically and clean up failed writes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def adaptive_mode(windows, allowed, thresholds):
    critical, completion, conservation = thresholds
    known = [entry["remaining_percent"] for entry in windows.values() if entry["state"] == "reliable"]
    if allowed is False or (known and min(known) < critical):
        return "critical"
    if known and min(known) < completion:
        return "completion"
    if len(known) != len(WINDOWS) or min(known) <= conservation:
        return "conservation"
    return "normal"


def usage_status(*, timeout=8, cache=None, thresholds=THRESHOLDS, notification=None,
                 thread_id=None, observed_at=None, max_context_age=120):
    """Return machine-readable subscription and context status independently."""
    critical, completion, conservation = thresholds
    if not 0 < critical < completion < conservation < 100:
        raise ValueError("thresholds must increase and be between 0 and 100")
    warnings = []
    snapshot = {}
    stale = False
    try:
        response = read_rate_limits(timeout)
        snapshot = subscription_snapshot(response, int(time.time()))
    except (UsageUnavailable, OSError, ValueError) as error:
        warnings.append(str(error) if isinstance(error, UsageUnavailable) else "app_server_unavailable")
        if cache is not None:
            try:
                snapshot = object_or_empty(json.loads(Path(cache).read_text(encoding="utf-8")))
                stale = True
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                warnings.append("cache_read_failed")
    else:
        if cache is not None:
            try:
                save_json(cache, snapshot)
            except OSError:
                warnings.append("cache_write_failed")

    now = int(time.time())
    windows = {}
    saved = object_or_empty(snapshot.get("windows"))
    for name in WINDOWS:
        entry = object_or_empty(saved.get(name))
        percent, reset = entry.get("remaining_percent"), entry.get("reset_epoch")
        if type(percent) is not int or not 0 <= percent <= 100:
            windows[name] = {"state": "unavailable", "remaining_percent": None, "reset_at": None}
            continue
        valid_reset = type(reset) is int and 0 < reset <= 253402300799
        windows[name] = {
            "state": "stale" if stale or (valid_reset and reset <= now) else "reliable",
            "remaining_percent": percent,
            "reset_at": utc(reset) if valid_reset else None,
        }
    allowed = None if stale else snapshot.get("ordinary_usage_allowed")
    mode = adaptive_mode(windows, allowed, thresholds)
    measured = snapshot.get("measured_epoch")
    return {
        "subscription": {
            "source": SOURCE,
            "windows": windows,
            "last_successful_measurement": utc(measured) if type(measured) is int and 0 < measured <= now else None,
            "ordinary_usage_allowed": allowed,
        },
        "context": context_status(notification, thread_id, observed_at, now, max_context_age),
        "adaptive_mode": mode,
        "next_action": ACTION[mode],
        "checkpoint_recommended": mode == "critical" or not all(
            entry["state"] == "reliable" for entry in windows.values()
        ),
        "diagnostics": warnings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="command", required=True)
    status = modes.add_parser("status", help="read subscription and context usage as JSON")
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    status.add_argument("--cache", type=Path, default=state_home / "codex-linux-desktop/usage.json")
    status.add_argument("--no-cache", action="store_true")
    status.add_argument("--thresholds", type=float, nargs=3, default=THRESHOLDS,
                        metavar=("CRITICAL", "COMPLETION", "CONSERVATION"))
    status.add_argument("--timeout", type=float, default=8)
    status.add_argument("--context-notification", type=Path)
    status.add_argument("--thread-id")
    status.add_argument("--context-observed-at", type=int, help="notification receipt time in Unix seconds")
    status.add_argument("--max-context-age", type=int, default=120)
    checkpoint = modes.add_parser("checkpoint", help="save resumable milestone notes locally")
    checkpoint.add_argument("path", type=Path)
    checkpoint.add_argument("--milestone", required=True)
    checkpoint.add_argument("--completed", action="append", required=True)
    checkpoint.add_argument("--test-result", action="append", required=True)
    checkpoint.add_argument("--remaining", action="append", required=True)
    checkpoint.add_argument("--decision", action="append", default=[])
    checkpoint.add_argument("--failure", action="append", default=[])
    checkpoint.add_argument("--next-step", required=True)
    args = parser.parse_args()

    try:
        if args.command == "status":
            if not 0 < args.timeout <= 60 or args.max_context_age <= 0:
                parser.error("timeout must be in (0, 60] and context age must be positive")
            fields = args.context_notification, args.thread_id, args.context_observed_at
            if any(x is not None for x in fields) and not all(x is not None for x in fields):
                parser.error("context requires a notification path, thread ID, and observation time")
            notification = json.loads(args.context_notification.read_text(encoding="utf-8")) if args.context_notification else None
            result = usage_status(
                timeout=args.timeout,
                cache=None if args.no_cache else args.cache,
                thresholds=args.thresholds,
                notification=notification,
                thread_id=args.thread_id,
                observed_at=args.context_observed_at,
                max_context_age=args.max_context_age,
            )
        else:
            result = {key: value for key, value in vars(args).items() if key not in {"command", "path"}}
            result["saved_at"] = utc(int(time.time()))
            save_json(args.path, result)
            result = {"checkpoint_saved": str(args.path)}
        print(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(2, f"usage: {type(error).__name__}\n")


if __name__ == "__main__":
    main()
