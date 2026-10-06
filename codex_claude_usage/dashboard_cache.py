"""Best-effort, private startup snapshots, separate from the live query cache.

These files contain previous complete responses, never evidence that the database
is current. The authenticated dashboard labels them as saved until a scan and a
fresh read finish. A missing, incompatible or unsafe snapshot is simply a miss.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from .db import UnsafeDatabasePathError, secure_db_permissions
from .safefile import read_bounded_regular_file

SNAPSHOT_FORMAT = 1
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
SNAPSHOT_KEYS = frozenset(("sources", "claude", "codex"))


def _location(db_path, key):
    if key not in SNAPSHOT_KEYS:
        raise ValueError("invalid dashboard snapshot key")
    path, _, identity = secure_db_permissions(db_path, return_identity=True)
    if identity is None:
        return None, None
    return (path.with_name(path.name + ".dashboard-" + key + ".json"),
            [str(part) for part in identity[:2]])


def snapshot_identity(db_path):
    """Capture the database identity before building a response for persistence."""
    try:
        return _location(db_path, "sources")[1]
    except (OSError, ValueError, TypeError, UnsafeDatabasePathError):
        return None


def read_snapshot(db_path, key, app_version):
    """Read without opening SQLite, including while a startup scan owns it."""
    try:
        path, identity = _location(db_path, key)
        if path is None:
            return None
        raw = read_bounded_regular_file(
            path, MAX_SNAPSHOT_BYTES, follow_symlinks=False,
            single_link=True, owner_only=True)
        if raw is None:
            return None
        saved = json.loads(raw)
        if (not isinstance(saved, dict)
                or saved.get("format") != SNAPSHOT_FORMAT
                or saved.get("app_version") != app_version
                or saved.get("database") != identity
                or saved.get("key") != key
                or not isinstance(saved.get("saved_at"), (int, float))
                or not 0 < saved["saved_at"] <= time.time()
                or not isinstance(saved.get("data"), dict)
                or "error" in saved["data"]
                or saved["data"].get("unscanned")):
            return None
        if _location(db_path, key)[1] != identity:
            return None
        return {"saved_at": saved["saved_at"], "data": saved["data"]}
    except (OSError, ValueError, TypeError, RecursionError, UnsafeDatabasePathError):
        return None


def save_snapshot(db_path, key, data, app_version, *, expected_identity):
    """Atomically retain a successful response; cache failures never fail a read.

    The database guard also validates the parent directory. A unique private
    temporary file is replaced into place, so even a swapped final symlink is
    replaced instead of followed. No credentials are included in the envelope.
    """
    temporary = None
    try:
        if not isinstance(data, dict) or "error" in data or data.get("unscanned"):
            return False
        target, identity = _location(db_path, key)
        if target is None or identity != expected_identity:
            return False
        raw = json.dumps({
            "format": SNAPSHOT_FORMAT, "app_version": app_version,
            "database": identity, "key": key, "saved_at": time.time(),
            "data": data,
        }, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_SNAPSHOT_BYTES:
            return False
        fd, name = tempfile.mkstemp(prefix=".dashboard-snapshot-", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Do not associate the old response with a replacement database.
        if _location(db_path, key)[1] != identity:
            return False
        os.replace(temporary, target)
        temporary = None
        return True
    except (OSError, ValueError, TypeError, RecursionError, UnsafeDatabasePathError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
