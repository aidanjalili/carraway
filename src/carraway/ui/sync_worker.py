"""Pull from a provider without freezing the window.

A sync is several HTTPS round trips over a slow link, so it cannot run on the
thread that paints. It runs on a QThread and reports back by signal; the
window stays usable throughout and reloads once when it finishes.

Rate limiting matters more than it looks. SimpleFIN allows 24 requests a day
and history pagination spends several per sync, so syncing on every window
open would exhaust the quota by lunchtime for anyone who keeps closing and
reopening the app. An automatic sync is therefore skipped when a recent one
already ran; pressing Refresh always syncs, because that is a person asking.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QThread, Signal

from ..core import backup, db

# How stale an automatic sync tolerates before it bothers. Banks feed
# SimpleFIN about daily, so anything shorter spends requests to learn nothing.
AUTO_SYNC_INTERVAL = timedelta(hours=6)
_LAST_SYNC_KEY = "last_sync_at"


def last_sync(conn) -> datetime | None:
    raw = db.get_setting(conn, _LAST_SYNC_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def is_due(conn) -> bool:
    """Whether an automatic sync should run now."""
    previous = last_sync(conn)
    return previous is None or datetime.now() - previous >= AUTO_SYNC_INTERVAL


def is_configured() -> bool:
    """Whether there is a provider to sync from at all."""
    from ..sync import credentials

    return bool(credentials.load("simplefin-access-url"))


class SyncWorker(QObject):
    """Runs one sync and reports what happened."""

    finished = Signal(int, int, list)  # new, skipped, warnings
    failed = Signal(str)

    def __init__(self, database) -> None:
        super().__init__()
        self.database = database

    def run(self) -> None:
        from ..sync import credentials
        from ..sync.simplefin import SimpleFinError, SimpleFinProvider

        access_url = credentials.load("simplefin-access-url")
        if not access_url:
            self.failed.emit("No provider is connected.")
            return

        try:
            conn = db.connect(self.database)
            # Snapshot first, for the same reason the CLI does: a sync is the
            # moment most likely to write something unexpected.
            backup.snapshot(self.database, tag="sync")

            known = {a.external_id: a.id for a in db.list_accounts(conn) if a.external_id}
            result = SimpleFinProvider(access_url, account_ids=known).fetch()

            # Accounts are only linked automatically here when they already
            # match something known. A genuinely new account is left for the
            # CLI, which can ask before merging two accounts together.
            named = {a.id: a.name for a in db.list_accounts(conn)}
            for account in result.accounts:
                if account.id in named:
                    from dataclasses import replace

                    account = replace(account, name=named[account.id])
                db.upsert_account(conn, account)
            for account_id, balance in result.balances.items():
                db.record_balance(conn, account_id, balance)

            inserted, skipped = db.insert_transactions(conn, result.transactions)
            db.set_setting(conn, _LAST_SYNC_KEY, datetime.now().isoformat())
            conn.close()
            self.finished.emit(inserted, skipped, list(result.warnings))
        except SimpleFinError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            # A sync failing must never take the window down with it.
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SyncRunner(QObject):
    """Owns the thread, so callers do not have to."""

    started = Signal()
    finished = Signal(int, int, list)
    failed = Signal(str)

    def __init__(self, database, parent=None) -> None:
        super().__init__(parent)
        self.database = database
        self._thread: QThread | None = None
        self._worker: SyncWorker | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self) -> bool:
        """Begin a sync. Returns False if one is already in flight."""
        if self.running:
            return False

        self._thread = QThread(self)
        self._worker = SyncWorker(self.database)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._done)
        self._worker.failed.connect(self._error)
        self.started.emit()
        self._thread.start()
        return True

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
            self._worker = None

    def _done(self, inserted: int, skipped: int, warnings: list) -> None:
        self._teardown()
        self.finished.emit(inserted, skipped, warnings)

    def _error(self, message: str) -> None:
        self._teardown()
        self.failed.emit(message)
