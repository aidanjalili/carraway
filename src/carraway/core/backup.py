"""Protect the archive.

Carraway's database is not a cache. A bank exposes only the last few months
through an aggregator, so once a transaction is synced, this file is the only
place it still exists — sync regularly and the ledger grows into a permanent
history that outlives what any provider will still tell you.

That makes the file irreplaceable, and one irreplaceable file with no copies is
a bad plan. A rotating snapshot is taken before anything writes to it.

Snapshots use SQLite's own backup API rather than copying bytes, because a
plain copy of a database mid-write produces a file that looks fine and is
subtly corrupt.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

# Enough to survive a bad import going unnoticed for a few days, without
# quietly consuming disk on a file that only grows.
KEEP = 10


def backup_dir(database: Path) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "carraway" / "backups"


def snapshot(database: Path, *, tag: str = "") -> Path | None:
    """Copy the database aside. Returns the snapshot path, or None if empty.

    A missing or empty database is not worth a snapshot — there is nothing to
    lose yet, and a directory of empty files makes the useful ones harder to
    find.
    """
    database = Path(database)
    if not database.exists() or database.stat().st_size == 0:
        return None

    target_dir = backup_dir(database)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    target = target_dir / f"carraway-{stamp}{suffix}.db"

    source = sqlite3.connect(database)
    destination = sqlite3.connect(target)
    try:
        # The backup API takes a consistent snapshot even while the source is
        # being written to; shutil.copy does not.
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    prune(database)
    return target


def prune(database: Path, keep: int = KEEP) -> int:
    """Delete all but the newest `keep` snapshots. Returns how many went."""
    snapshots = sorted(backup_dir(database).glob("carraway-*.db"))
    stale = snapshots[:-keep] if keep > 0 else snapshots
    for path in stale:
        path.unlink(missing_ok=True)
    return len(stale)


def list_snapshots(database: Path) -> list[tuple[Path, date, int]]:
    """Existing snapshots as (path, date taken, size in bytes), oldest first."""
    out = []
    for path in sorted(backup_dir(database).glob("carraway-*.db")):
        stat = path.stat()
        out.append((path, date.fromtimestamp(stat.st_mtime), stat.st_size))
    return out
