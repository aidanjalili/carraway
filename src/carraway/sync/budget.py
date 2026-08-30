"""How often a provider may be asked for data.

Separate from the UI because none of it is about widgets: it is arithmetic
over dates and stored settings, the CLI needs the same answers as the desktop
app, and keeping it here means it can be tested without Qt installed.

SimpleFIN allows 24 requests a day and one full sync spends about six, so an
unguarded refresh button can drain a day's quota in under a minute — including
the share a scheduled sync depends on. Three limits, each stopping something
different:

* **Staleness** — opening the app repeatedly should not refetch data that is
  minutes old.
* **Cooldown** — a refresh button should not be leanable on.
* **Daily budget** — nor should patient clicking every few minutes all
  afternoon.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..core import db

# Banks feed SimpleFIN about daily, so a shorter interval spends requests to
# learn nothing.
AUTO_SYNC_INTERVAL = timedelta(hours=6)

# Long enough that leaning on the button cannot drain the day, short enough
# that someone who just made a purchase does not feel blocked.
MANUAL_COOLDOWN = timedelta(minutes=2)

# Below the provider's own limit so a scheduled run always has room: a
# background timer failing silently on quota is worse than a button that says
# "not yet".
DAILY_REQUEST_BUDGET = 18

# Roughly what one full sync costs, measured rather than guessed.
REQUESTS_PER_SYNC = 6

LAST_SYNC_KEY = "last_sync_at"
USAGE_KEY = "sync_requests_today"


def last_sync(conn) -> datetime | None:
    raw = db.get_setting(conn, LAST_SYNC_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def mark_synced(conn, when: datetime | None = None) -> None:
    db.set_setting(conn, LAST_SYNC_KEY, (when or datetime.now()).isoformat())


def _usage(conn) -> tuple[str, int]:
    """(day, requests spent) for today, resetting when the date rolls over."""
    stored = db.get_setting(conn, USAGE_KEY)
    today = date.today().isoformat()
    if isinstance(stored, dict) and stored.get("date") == today:
        return today, int(stored.get("requests", 0))
    return today, 0


def record_usage(conn, requests: int) -> None:
    today, spent = _usage(conn)
    db.set_setting(conn, USAGE_KEY, {"date": today, "requests": spent + max(requests, 0)})


def requests_left(conn) -> int:
    _, spent = _usage(conn)
    return max(DAILY_REQUEST_BUDGET - spent, 0)


def is_due(conn) -> bool:
    """Whether an automatic sync should run now."""
    if requests_left(conn) < REQUESTS_PER_SYNC:
        return False
    previous = last_sync(conn)
    return previous is None or datetime.now() - previous >= AUTO_SYNC_INTERVAL


def refusal_reason(conn) -> str | None:
    """Why a manual refresh should not run yet, or None if it may."""
    if requests_left(conn) < REQUESTS_PER_SYNC:
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
