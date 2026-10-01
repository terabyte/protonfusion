"""Owner-only file writes for files that hold credentials or the user's data."""

import os
import tempfile
from pathlib import Path


def write_private_file(path: Path, text: str):
    """Atomically write text to path, readable only by the owner (0600).

    The text goes to a uniquely named temp file next to path (created by
    mkstemp: O_EXCL, 0600, so a symlink or another run's temp file is never
    reused), is fsynced, then renamed over path, so a crash never leaves half
    a file and a reader never sees one. The temp file is removed on failure.

    Parent directories this call creates are 0700. An existing parent keeps
    its permissions: tightening a directory the user made is not our call.
    """
    path = Path(path)
    old_umask = os.umask(0o077)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
