"""What a stretch of time actually looked like, against the one before it.

The overview screen used to show money in, money out and a category
breakdown -- all of it for the whole ledger, all time. Those are technically
numbers but they are not answers: "you have spent $41,000 since 2024" is not
something anybody can act on, and the category bars said the same thing the
Spending screen said, only without a way to change the period.

The question this screen should answer is "how am I doing?", and that
question is always comparative. So everything here is a period against the
period before it: the same figures, plus which way they moved, plus the few
categories that moved the most. A number that has not changed is not news
and does not need a screen.

No Qt in here on purpose -- the CLI wants the same answers, and this way
they can be tested without a GUI installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..core.models import Transaction
from ..core.money import Money, total

# What the period picker offers, in the order it offers them.
PRESETS = (
    "This month",
    "Last month",
    "Last 30 days",
    "Last 90 days",
    "This year",
    "All time",
)
DEFAULT_PRESET = "This month"


@dataclass(frozen=True, slots=True)
class Period:
    """A closed range of dates, both ends included."""

    starts_on: date
    ends_on: date

    @property
    def days(self) -> int:
        return (self.ends_on - self.starts_on).days + 1

    def contains(self, when: date) -> bool:
        return self.starts_on <= when <= self.ends_on

    def before(self) -> Period:
        """The same length of time, ending the day this one starts."""
        end = self.starts_on - timedelta(days=1)
        return Period(end - timedelta(days=self.days - 1), end)

    def describe(self) -> str:
        if self.starts_on == self.ends_on:
            return self.starts_on.isoformat()
        return f"{self.starts_on.isoformat()} to {self.ends_on.isoformat()}"


def _add_months(when: date, months: int) -> date:
    """Shift a date by whole months, clamping to the end of a short one."""
    index = when.year * 12 + (when.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    # 31 January minus one month is 31 December, but 31 March minus one is 28
    # or 29 February. Clamping is the only sane answer.
    last = _days_in_month(year, month)
    return date(year, month, min(when.day, last))


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def preset(name: str, today: date, earliest: date | None = None) -> tuple[Period, Period | None]:
    """A named range and what to compare it against. None means no comparison.

    The comparison is not always "the same number of days immediately before".
    For a month in progress the useful question is "how am I doing against
    the same stretch of last month", not against the tail end of it -- half
    of September belongs beside half of August, not beside 17-31 August.
    """
    if name == "Last month":
        this_month = date(today.year, today.month, 1)
        start = _add_months(this_month, -1)
        end = this_month - timedelta(days=1)
        previous_start = _add_months(start, -1)
        return Period(start, end), Period(previous_start, start - timedelta(days=1))

    if name == "Last 30 days":
        current = Period(today - timedelta(days=29), today)
        return current, current.before()

    if name == "Last 90 days":
        current = Period(today - timedelta(days=89), today)
        return current, current.before()

    if name == "This year":
        current = Period(date(today.year, 1, 1), today)
        # The same stretch of last year, so a January comparison is not being
        # measured against a whole twelve months.
        return current, Period(date(today.year - 1, 1, 1), _same_day_last_year(today))

    if name == "All time":
        return Period(earliest or today, today), None

    # "This month", and anything unrecognised, which should still show a
    # sensible screen rather than nothing.
    start = date(today.year, today.month, 1)
    # The same stretch of last month: the 1st through the same day of the
    # month, clamped, so half of September is compared with half of August
    # rather than with 17-31 August, which is what "the same number of days
    # immediately before" would have given.
    return Period(start, today), Period(_add_months(start, -1), _add_months(today, -1))


def _same_day_last_year(today: date) -> date:
    try:
        return today.replace(year=today.year - 1)
    except ValueError:  # 29 February
        return today.replace(year=today.year - 1, day=28)


@dataclass(frozen=True, slots=True)
class Movement:
    """One spending category, this period against last.

    `now` and `before` keep the ledger's sign, where money going out is
    negative. Everything derived from them is expressed as a *magnitude*
    instead: spending 300 where you spent 100 is "200 more", up 200%, and it
    would be actively misleading to report that as a fall because -300 is
    less than -100. Every category here is spending, so there is no case
    where the sign carries information the magnitude loses.
    """

    category: str
    now: Money
    before: Money

    @property
    def change(self) -> Money:
        """How much more went out than last time. Negative means less did."""
        return Money(abs(self.now.minor) - abs(self.before.minor), self.now.currency)

    @property
    def rose(self) -> bool:
        return self.change.minor > 0

    @property
    def is_new(self) -> bool:
        """Nothing at all last time. A percentage would be meaningless."""
        return self.before.minor == 0 and self.now.minor != 0

    @property
    def is_gone(self) -> bool:
        return self.now.minor == 0 and self.before.minor != 0

    @property
    def percent(self) -> float | None:
        """How far it moved, or None when there is no base to divide by."""
        if self.before.minor == 0:
            return None
        return self.change.minor / abs(self.before.minor) * 100


@dataclass(frozen=True, slots=True)
class Summary:
    """Everything the overview screen shows, already worked out."""

    period: Period
    previous: Period | None
    earned: Money
    spent: Money
    count: int
    previous_earned: Money | None
    previous_spent: Money | None
    previous_count: int | None
    movements: tuple[Movement, ...]
    categories: tuple[tuple[str, Money, int], ...]

    @property
    def net(self) -> Money:
        return self.earned + self.spent

    @property
    def previous_net(self) -> Money | None:
        if self.previous_earned is None or self.previous_spent is None:
            return None
        return self.previous_earned + self.previous_spent

    @property
    def daily_burn(self) -> Money:
        """Average spent per day, which is the figure that predicts anything."""
        days = max(self.period.days, 1)
        return Money(abs(self.spent.minor) // days, self.spent.currency)


def _totals(
    transactions: list[Transaction],
    categories: dict[str, str],
    period: Period,
    *,
    transfer_label: str,
) -> tuple[Money, Money, int, dict[str, tuple[Money, int]]]:
    earned: list[Money] = []
    spent: list[Money] = []
    by_category: dict[str, list[Money]] = {}
    counts: dict[str, int] = {}
    seen = 0

    for tx in transactions:
        if not period.contains(tx.date) or tx.is_transfer:
            continue
        seen += 1
        if tx.is_outflow:
            spent.append(tx.amount)
            name = categories.get(tx.id, "Uncategorized")
            # Money moved between the user's own accounts is still theirs;
            # counting it as spending buries the categories that are not.
            if name != transfer_label:
                by_category.setdefault(name, []).append(tx.amount)
                counts[name] = counts.get(name, 0) + 1
        else:
            earned.append(tx.amount)

    rolled = {name: (total(amounts), counts[name]) for name, amounts in by_category.items()}
    return total(earned), total(spent), seen, rolled


def summarise(
    transactions: list[Transaction],
    categories: dict[str, str],
    period: Period,
    previous: Period | None = None,
    *,
    transfer_label: str = "Transfer",
    movements: int = 5,
) -> Summary:
    """Work out one period, and how it compares with the one before it.

    `categories` maps a transaction id to its category name, so the caller
    decides whether guessed categories count -- this module should not have
    an opinion about that.
    """
    earned, spent, count, rolled = _totals(
        transactions, categories, period, transfer_label=transfer_label
    )

    if previous is None:
        ranked = sorted(rolled.items(), key=lambda item: abs(item[1][0].minor), reverse=True)
        return Summary(
            period=period,
            previous=None,
            earned=earned,
            spent=spent,
            count=count,
            previous_earned=None,
            previous_spent=None,
            previous_count=None,
            movements=(),
            categories=tuple((name, amount, n) for name, (amount, n) in ranked),
        )

    before_earned, before_spent, before_count, before_rolled = _totals(
        transactions, categories, previous, transfer_label=transfer_label
    )

    zero = Money(0, spent.currency)
    moves = [
        Movement(
            category=name,
            now=rolled.get(name, (zero, 0))[0],
            before=before_rolled.get(name, (zero, 0))[0],
        )
        for name in set(rolled) | set(before_rolled)
    ]
    # Biggest mover first, in either direction: a category that fell by $200
    # is exactly as much news as one that rose by $200.
    moves.sort(key=lambda m: abs(m.change.minor), reverse=True)
    ranked = sorted(rolled.items(), key=lambda item: abs(item[1][0].minor), reverse=True)

    return Summary(
        period=period,
        previous=previous,
        earned=earned,
        spent=spent,
        count=count,
        previous_earned=before_earned,
        previous_spent=before_spent,
        previous_count=before_count,
        movements=tuple(m for m in moves if m.change.minor != 0)[:movements],
        categories=tuple((name, amount, n) for name, (amount, n) in ranked),
    )
