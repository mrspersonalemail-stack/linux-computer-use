"""Install the pinned x86-64 wdotool release to a user-only location."""

import hashlib
import io
from pathlib import Path, PurePosixPath
import platform
import tarfile
import urllib.request

URL = (
    "https://github.com/cushycush/wdotool/releases/download/v0.5.3/"
    "wdotool-x86_64-unknown-linux-gnu.tar.xz"
)
ARCHIVE_SHA256 = "292913047cba9494ee700e158a5ec2b2e99fd6c3191ab380d8af491870842fcf"
BINARY_SHA256 = "c83d8936e688108c0760b0bad3eb89c818825f7ab4eb4417d5a9c7e6e5884c04"
DEST = Path.home() / ".local/libexec/codex-linux-desktop/wdotool"


def main():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit("This pinned release supports x86-64 Linux.")
    if DEST.is_symlink():
        raise SystemExit("The input helper path must be a regular file.")
    if DEST.exists():
        if not DEST.is_file() or hashlib.sha256(DEST.read_bytes()).hexdigest() != BINARY_SHA256:
            raise SystemExit("Existing input helper does not match the pinned release.")
        print(DEST)
        return
    with urllib.request.urlopen(URL, timeout=30) as response:
        data = response.read(16_000_001)
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise SystemExit("Input archive checksum mismatch.")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = next(entry for entry in archive if entry.isfile() and PurePosixPath(entry.name).name == "wdotool")
        with archive.extractfile(member) as stream:
            payload = stream.read(16_000_001)
    if hashlib.sha256(payload).hexdigest() != BINARY_SHA256:
        raise SystemExit("Input executable checksum mismatch.")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with DEST.open("xb") as stream:
        stream.write(payload)
    DEST.chmod(0o755)
    print(DEST)


if __name__ == "__main__":
    main()
