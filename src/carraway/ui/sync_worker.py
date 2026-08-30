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

from datetime import date, datetime, timedelta

from PySide6.QtCore import QObject, QThread, Signal

from ..core import backup, db

# How stale an automatic sync tolerates before it bothers. Banks feed
# SimpleFIN about daily, so anything shorter spends requests to learn nothing.
AUTO_SYNC_INTERVAL = timedelta(hours=6)

# The shortest gap between two manual refreshes. Long enough that leaning on
# the button cannot drain the day's budget, short enough that someone who just
# made a purchase and wants to see it does not feel blocked.
MANUAL_COOLDOWN = timedelta(minutes=2)

# SimpleFIN allows 24 requests a day, and one full sync spends about six.
# Kept below that so a scheduled run always has room: a background timer
# silently failing on quota is worse than a button that says "not yet".
DAILY_REQUEST_BUDGET = 18

_LAST_SYNC_KEY = "last_sync_at"
_USAGE_KEY = "sync_requests_today"


def last_sync(conn) -> datetime | None:
    raw = db.get_setting(conn, _LAST_SYNC_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _usage(conn) -> tuple[str, int]:
    """(day, requests spent) for today, resetting when the date rolls over."""
    stored = db.get_setting(conn, _USAGE_KEY)
    today = date.today().isoformat()
    if isinstance(stored, dict) and stored.get("date") == today:
        return today, int(stored.get("requests", 0))
    return today, 0


def record_usage(conn, requests: int) -> None:
    today, spent = _usage(conn)
    db.set_setting(conn, _USAGE_KEY, {"date": today, "requests": spent + max(requests, 0)})


def requests_left(conn) -> int:
    """How much of today's budget remains."""
    _, spent = _usage(conn)
    return max(DAILY_REQUEST_BUDGET - spent, 0)


def is_due(conn) -> bool:
    """Whether an automatic sync should run now."""
    if requests_left(conn) < 6:
        return False
    previous = last_sync(conn)
    return previous is None or datetime.now() - previous >= AUTO_SYNC_INTERVAL


def refusal_reason(conn) -> str | None:
    """Why a manual refresh should not run yet, or None if it may.

    Two separate limits. The cooldown stops a button being leaned on; the
    budget stops a day's worth of patient clicking from exhausting the quota
    the scheduled sync depends on.
    """
    if requests_left(conn) < 6:
        return (
            "Today's bank requests are used up. SimpleFIN allows a limited "
            "number a day, and the scheduled sync needs what is left. "
            "It resets at midnight."
        )
    previous = last_sync(conn)
    if previous is not None:
        waited = datetime.now() - previous
        if waited < MANUAL_COOLDOWN:
            seconds = int((MANUAL_COOLDOWN - waited).total_seconds())
            return f"Just refreshed. Try again in {seconds}s."
    return None


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
            provider = SimpleFinProvider(access_url, account_ids=known)
            result = provider.fetch()
            record_usage(conn, provider.requests_made)

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
