"""Budgets you set for a stretch of time, and how you are doing against them.

`analysis.budget` answers "what would I have to do differently to save $5,000?"
— it derives a repeating per-period allowance from a net-worth goal. This
module answers a smaller, more human question: *"September. Here is what I am
allowed to spend. How am I doing?"*

The differences that matter:

* **Any stretch of days**, not a calendar period. "1–30 September" and "the
  eleven days I am away in late September" are both budgets someone actually
  sets, and only one of them is a month.
* **It is saved and named.** A goal you cannot come back to next week is not a
  goal, it is a calculation.
* **Three ways to arrive at the numbers**, because people reach a budget from
  whichever end they happen to know:

      suggest    — "what do I normally spend?"      (from history)
      total      — "I have $1,200 for September."   (split by history)
      backwards  — "I make $4,000, want to save     (income - saving - fixed)
                    $800, and $1,900 is fixed."

  All three land on the same thing — a list of envelopes — and every figure
  stays editable afterwards, because the history is evidence and not an
  instruction.

Baselines are **median monthly totals**, for the reason `analysis.budget`
gives: one December or one wedding would otherwise inflate an allowance
permanently, and a budget built from a holiday is one nobody can hit. The
median is then converted to a daily rate and scaled to the window, which is
what lets an eleven-day budget mean anything.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..core.models import Transaction
from ..core.money import Money
from .budget import allocate
from .categorize import INCOME, TRANSFER, categorize

# The mean Gregorian month, used to turn a median monthly figure into a daily
# rate. Not 30: over a six-month lookback that error compounds to most of a
# day's spending.
DAYS_PER_MONTH = Decimal("30.4375")

# How far back a suggestion looks. Six complete months is enough for a median
# to shrug off one unusual one, and short enough that a year-old spending habit
# does not set this year's budget.
DEFAULT_LOOKBACK_MONTHS = 6

# Categories that are not spending. Income is the other side of the equation
# and a transfer is money the user still has.
NON_SPENDING = frozenset({INCOME, TRANSFER})

# Below this, a median is being taken over so few months that one unusual one
# still moves it, and saying so is more use than a confident-looking figure.
MIN_MONTHS_FOR_CONFIDENCE = 3

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# -- what a budget is -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Envelope:
    """One line: a category and what it may cost over the whole window.

    A magnitude, not a signed amount — a Travel budget reads as $600, not
    -$600 — even though every transaction behind it is negative.
    """

    category: str
    allowance: Money


@dataclass(frozen=True, slots=True)
class Budget:
    """A named spending limit over a stretch of days.

    `ends_on` is inclusive, because "1–30 September" is how people say it and
    an off-by-one here is a whole day of spending.
    """

    id: str
    name: str
    starts_on: date
    ends_on: date
    envelopes: tuple[Envelope, ...] = ()
    # Which accounts this budget watches. Empty means all of them, which is
    # the right default: money spent is money spent, and someone who pays for
    # dinner on a credit card has not spent less than someone who paid cash.
    # Narrowing it is for the case where one account is genuinely separate —
    # a shared card, or a trip paid for out of one place.
    accounts: tuple[str, ...] = ()
    # Kept only when the budget was reached the "backwards" way. They are the
    # user's own reasoning and worth showing back to them later, when "why is
    # this $1,300?" is a question they can no longer answer from memory.
    expected_income: Money | None = None
    savings_target: Money | None = None
    fixed_costs: Money | None = None

    def __post_init__(self) -> None:
        if self.ends_on < self.starts_on:
            raise ValueError(
                f"A budget cannot end ({self.ends_on}) before it starts ({self.starts_on})."
            )

    @property
    def days(self) -> int:
        """Length of the window in days, counting both endpoints."""
        return (self.ends_on - self.starts_on).days + 1

    @property
    def total(self) -> Money:
        """Everything the budget permits across every category."""
        if not self.envelopes:
            return Money.zero()
        out = Money.zero(self.envelopes[0].allowance.currency)
        for envelope in self.envelopes:
            out = out + envelope.allowance
        return out

    def covers(self, day: date) -> bool:
        return self.starts_on <= day <= self.ends_on

    def watches(self, account_id: str) -> bool:
        """Whether spending on this account counts. No scope means all of them."""
        return not self.accounts or account_id in self.accounts

    def allowance_for(self, category: str) -> Money | None:
        for envelope in self.envelopes:
            if envelope.category == category:
                return envelope.allowance
        return None


# -- saying where a figure came from --------------------------------------


@dataclass(frozen=True, slots=True)
class Basis:
    """How much history a suggestion rests on, and which months.

    Every figure this module suggests is a median over complete calendar
    months, and how many of those there were changes how much the number is
    worth. Six months of history and one month of history produce a figure
    that looks identical on screen, so the difference has to be said out loud
    rather than left for the user to guess at.
    """

    months_with_data: int
    months_examined: int
    first_month: tuple[int, int] | None
    last_month: tuple[int, int] | None
    asof: date

    @property
    def confident(self) -> bool:
        return self.months_with_data >= MIN_MONTHS_FOR_CONFIDENCE

    @property
    def span(self) -> str:
        """The months covered, as a person would say them."""
        if self.first_month is None or self.last_month is None:
            return ""
        first_year, first_month = self.first_month
        last_year, last_month = self.last_month
        first = _MONTH_NAMES[first_month - 1]
        last = _MONTH_NAMES[last_month - 1]
        if self.first_month == self.last_month:
            return f"{first} {first_year}"
        if first_year == last_year:
            return f"{first}–{last} {first_year}"
        return f"{first} {first_year} – {last} {last_year}"

    def describe(self) -> str:
        """One sentence a screen can show beside the numbers."""
        if not self.months_with_data:
            return (
                "No complete months of spending to go on yet, so there is nothing "
                "to suggest. Type the figures you want."
            )
        current = _MONTH_NAMES[self.asof.month - 1]
        months = self.months_with_data
        plural = "" if months == 1 else "s"
        base = f"Median of your {months} complete month{plural} of spending ({self.span})."
        if not self.confident:
            return (
                f"{base} That is little to go on — one unusual month still moves it, "
                "so treat these as a starting point rather than a finding."
            )
        return (
            f"{base} {current} so far is left out, since a month in progress "
            "drags every figure down."
        )


def history_basis(
    transactions: Sequence[Transaction],
    *,
    asof: date | None = None,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    categories: Mapping[str, str] | None = None,
    accounts: Sequence[str] | None = None,
) -> Basis:
    """Which complete months a suggestion would actually be drawn from.

    Counts the months that contain spending rather than the months in the
    window: a ledger imported three weeks ago has six months of window and one
    month of evidence, and the second number is the one that matters.
    """
    today = asof or date.today()
    cutoff = today.replace(day=1)
    earliest = cutoff
    for _ in range(lookback_months):
        earliest = (earliest - timedelta(days=1)).replace(day=1)

    scope = set(accounts) if accounts else None
    seen: set[tuple[int, int]] = set()
    for tx in transactions:
        if not (earliest <= tx.date < cutoff):
            continue
        if scope is not None and tx.account_id not in scope:
            continue
        if _category_of(tx, categories) in NON_SPENDING or tx.is_transfer:
            continue
        if tx.amount.minor >= 0:
            continue
        seen.add((tx.date.year, tx.date.month))

    ordered = sorted(seen)
    return Basis(
        months_with_data=len(ordered),
        months_examined=lookback_months,
        first_month=ordered[0] if ordered else None,
        last_month=ordered[-1] if ordered else None,
        asof=today,
    )


@dataclass(frozen=True, slots=True)
class Estimate:
    """A figure the app worked out, and the reason it believes it.

    The reason travels with the number because a prefilled box is a claim.
    Someone who cannot see where $6,450 came from has to either accept it on
    faith or delete it, and both are worse than being told it is the monthly
    rate of the two things they marked as income.
    """

    amount: Money
    source: str
    confident: bool = True

    @property
    def known(self) -> bool:
        """Whether there is a figure worth offering at all."""
        return self.amount.minor > 0


# -- reaching the numbers -------------------------------------------------


def monthly_baselines(
    transactions: Sequence[Transaction],
    *,
    asof: date | None = None,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    categories: Mapping[str, str] | None = None,
    accounts: Sequence[str] | None = None,
) -> dict[str, Money]:
    """Median spend per complete calendar month, by category.

    Only complete months count. The month in progress is always short and
    would drag every median down by however far through it we happen to be —
    a budget set on the 3rd would come out at a tenth of the truth.
    """
    today = asof or date.today()
    # First day of the current month: everything from here on is incomplete.
    cutoff = today.replace(day=1)
    earliest = cutoff
    for _ in range(lookback_months):
        earliest = (earliest - timedelta(days=1)).replace(day=1)

    scope = set(accounts) if accounts else None
    per_month: dict[str, dict[tuple[int, int], int]] = {}
    for tx in transactions:
        if not (earliest <= tx.date < cutoff):
            continue
        if scope is not None and tx.account_id not in scope:
            continue
        category = _category_of(tx, categories)
        if category in NON_SPENDING or tx.is_transfer:
            continue
        # Outflows only, as magnitudes: a refund should reduce the month it
        # lands in rather than register as a category of its own.
        bucket = per_month.setdefault(category, {})
        key = (tx.date.year, tx.date.month)
        bucket[key] = bucket.get(key, 0) - tx.amount.minor

    months = _months_between(earliest, cutoff)
    out: dict[str, Money] = {}
    for category, buckets in per_month.items():
        # Months with no spending in this category are real zeros, not absent
        # data: leaving them out would make an occasional category look like a
        # monthly one. A category seen once in six months should budget as a
        # sixth of that, not all of it.
        values = [max(buckets.get(month, 0), 0) for month in months]
        if not any(values):
            continue
        out[category] = Money(int(statistics.median(values)))
    return out


def scale_to_window(monthly: Money, days: int) -> Money:
    """A monthly figure as it applies over `days` days."""
    daily = Decimal(monthly.minor) / DAYS_PER_MONTH
    return Money(int((daily * Decimal(days)).to_integral_value()), monthly.currency)


def suggest(
    transactions: Sequence[Transaction],
    starts_on: date,
    ends_on: date,
    *,
    asof: date | None = None,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    categories: Mapping[str, str] | None = None,
    accounts: Sequence[str] | None = None,
) -> list[Envelope]:
    """What this window would cost at the user's usual rate, per category.

    The honest default for "start a budget": it is not a recommendation to
    spend this much, it is what will happen if nothing changes. Deciding what
    to cut is the user's job, and they can only do it against a number.
    """
    days = (ends_on - starts_on).days + 1
    baselines = monthly_baselines(
        transactions,
        asof=asof,
        lookback_months=lookback_months,
        categories=categories,
        accounts=accounts,
    )
    lines = [
        Envelope(category=name, allowance=scale_to_window(amount, days))
        for name, amount in baselines.items()
    ]
    lines = [line for line in lines if line.allowance.minor > 0]
    lines.sort(key=lambda line: (-line.allowance.minor, line.category))
    return lines


def split(total: Money, weights: Mapping[str, Money]) -> list[Envelope]:
    """Divide `total` across categories in proportion to what they usually cost.

    Largest-remainder allocation, so the envelopes sum to exactly the total
    rather than to a cent either side of it. Asking someone to find $50 in a
    $600 grocery bill and $50 in a $60 coffee habit are different requests,
    and only proportional splitting treats them as such.
    """
    names = [name for name, amount in weights.items() if amount.minor > 0]
    if not names:
        return []
    names.sort(key=lambda name: (-weights[name].minor, name))
    shares = allocate(total, [weights[name].minor for name in names])
    return [
        Envelope(category=name, allowance=share)
        for name, share in zip(names, shares, strict=True)
        if share.minor > 0
    ]


def spendable(income: Money, saving: Money, fixed: Money) -> Money:
    """What is left to spend freely: income, less saving, less what is spoken for.

    Can come out negative, and is returned negative rather than clamped. A
    plan that does not add up is a fact the user needs, and a zero here would
    read as "you may spend nothing" when the truth is "this does not fit".
    """
    return Money(income.minor - saving.minor - fixed.minor, income.currency)


# -- budgets that argue with each other ------------------------------------


@dataclass(frozen=True, slots=True)
class Clash:
    """Two budgets whose windows overlap, and what that means.

    Overlap on its own is not a mistake — "September" plus "the trip in late
    September" is a perfectly sensible pair, and the trip is meant to be part
    of the month. What matters is that the *same dollar* is counted by both,
    so the two can only be followed at once if the inner one fits inside what
    the outer one allows.
    """

    other: Budget
    overlap_days: int
    contained: bool  # this budget's window sits wholly inside the other's
    tighter: tuple[tuple[str, Money, Money], ...] = ()  # category, mine, theirs
    total_exceeds: bool = False

    @property
    def contradicts(self) -> bool:
        """True when following this budget must break the other one."""
        return self.contained and bool(self.tighter or self.total_exceeds)


def _shares_accounts(one: Budget, other: Budget) -> bool:
    """Whether the two watch any of the same money. No scope means all of it."""
    if not one.accounts or not other.accounts:
        return True
    return bool(set(one.accounts) & set(other.accounts))


def _overlap_days(one: Budget, other: Budget) -> int:
    start = max(one.starts_on, other.starts_on)
    end = min(one.ends_on, other.ends_on)
    return max((end - start).days + 1, 0)


def clashes(budget: Budget, others: Sequence[Budget]) -> list[Clash]:
    """Which existing budgets `budget` overlaps, and where it contradicts them.

    A contradiction is only claimed when it can be *proved*, which needs the
    windows to nest: if every day of this budget falls inside another one,
    then every dollar it permits also counts against that one, so allowing
    more here than the whole of the outer budget allows is a plan that cannot
    be followed. Two partially overlapping budgets are reported as overlapping
    and nothing stronger is asserted — the spending could land in the days
    they do not share, and guessing would be a false alarm.
    """
    out: list[Clash] = []
    for other in others:
        if other.id == budget.id:
            continue
        days = _overlap_days(budget, other)
        if not days or not _shares_accounts(budget, other):
            continue

        contained = budget.starts_on >= other.starts_on and budget.ends_on <= other.ends_on
        tighter: list[tuple[str, Money, Money]] = []
        exceeds = False
        if contained:
            for envelope in budget.envelopes:
                theirs = other.allowance_for(envelope.category)
                if theirs is not None and envelope.allowance.minor > theirs.minor:
                    tighter.append((envelope.category, envelope.allowance, theirs))
            exceeds = budget.total.minor > other.total.minor
        out.append(
            Clash(
                other=other,
                overlap_days=days,
                contained=contained,
                tighter=tuple(tighter),
                total_exceeds=exceeds,
            )
        )
    out.sort(key=lambda clash: (not clash.contradicts, -clash.overlap_days))
    return out


def describe_clashes(clashes_found: Sequence[Clash]) -> str:
    """One line a UI can show. Empty when there is nothing worth saying."""
    if not clashes_found:
        return ""
    bad = [c for c in clashes_found if c.contradicts]
    if bad:
        first = bad[0]
        if first.tighter:
            category, mine, theirs = first.tighter[0]
            more = f" (and {len(first.tighter) - 1} more)" if len(first.tighter) > 1 else ""
            return (
                f"This sits inside “{first.other.name}”, which only allows "
                f"{theirs.format()} for {category}{more}. Allowing {mine.format()} here "
                "means breaking that one."
            )
        return (
            f"This sits inside “{first.other.name}” but allows more in total "
            f"({first.other.total.format()} there). Following both is not possible."
        )
    names = ", ".join(f"“{c.other.name}”" for c in clashes_found[:2])
    extra = f" and {len(clashes_found) - 2} more" if len(clashes_found) > 2 else ""
    return f"Overlaps {names}{extra} — spending in those days counts against both."


# -- how it is going ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnvelopeStatus:
    """One budget line part way through the window."""

    category: str
    allowance: Money
    spent: Money
    pace: Money  # what "on schedule" looks like today

    @property
    def remaining(self) -> Money:
        """What is left. Negative once the line is overspent."""
        return self.allowance - self.spent

    @property
    def over(self) -> bool:
        return self.spent.minor > self.allowance.minor

    @property
    def on_track(self) -> bool:
        return self.spent.minor <= self.pace.minor

    @property
    def unbudgeted(self) -> bool:
        """Spending in a category the budget never mentioned."""
        return self.allowance.minor == 0 and self.spent.minor > 0

    @property
    def fraction_used(self) -> Decimal:
        """Spent over allowance, for a bar. Above 1 means over."""
        if not self.allowance.minor:
            return Decimal(1) if self.spent.minor else Decimal(0)
        return Decimal(self.spent.minor) / Decimal(self.allowance.minor)


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """A snapshot of one budget, as of a day."""

    budget: Budget
    asof: date
    elapsed_days: int
    total_days: int
    lines: list[EnvelopeStatus] = field(default_factory=list)
    # What has gone out over the shorter horizons. A month's figure answers
    # "am I on track"; these answer "can I buy this now", which is the
    # question actually being asked while standing in a shop.
    spent_today: Money = field(default_factory=Money.zero)
    spent_this_week: Money = field(default_factory=Money.zero)

    @property
    def started(self) -> bool:
        return self.asof >= self.budget.starts_on

    @property
    def finished(self) -> bool:
        return self.asof > self.budget.ends_on

    @property
    def days_left(self) -> int:
        """Days you can still spend in, today included. Zero once it has closed.

        Today counts, because you can still spend today — on the 30th of a
        September budget there is one day left, not none. That is deliberately
        not the complement of `elapsed_days`, which counts today as gone
        because by tonight it will be: the two answer different questions and
        pace would be wrong with either one's definition.
        """
        if self.finished:
            return 0
        if not self.started:
            return self.total_days
        return self.total_days - self.elapsed_days + 1

    @property
    def spent(self) -> Money:
        return _sum(line.spent for line in self.lines)

    @property
    def allowance(self) -> Money:
        return self.budget.total

    @property
    def remaining(self) -> Money:
        return self.allowance - self.spent

    @property
    def pace(self) -> Money:
        """What should have been spent by now to finish exactly on budget."""
        if not self.total_days:
            return self.allowance
        fraction = Decimal(self.elapsed_days) / Decimal(self.total_days)
        return Money(
            int((Decimal(self.allowance.minor) * fraction).to_integral_value()),
            self.allowance.currency,
        )

    @property
    def on_track(self) -> bool:
        return self.spent.minor <= self.pace.minor

    @property
    def daily_remaining(self) -> Money | None:
        """What is left, per day left — the number that changes a decision today.

        None once the window has closed, when there are no days to spread it
        over and the answer is simply what was left.
        """
        if self.days_left <= 0:
            return None
        return Money(self.remaining.minor // self.days_left, self.remaining.currency)

    @property
    def days_left_this_week(self) -> int:
        """Days left in the calendar week, and inside the budget. At least one.

        The calendar week rather than a rolling seven days, because "this
        week" means the one ending on Sunday to everybody who is not a
        computer -- and a budget's weekly figure is only useful if it matches
        the week the user is living in.
        """
        if self.days_left <= 0:
            return 0
        # Monday is 0, so this is however many days remain including today.
        to_sunday = 7 - self.asof.weekday()
        return max(1, min(to_sunday, self.days_left))

    @property
    def weekly_remaining(self) -> Money | None:
        """What is left to spend between now and Sunday.

        The daily figure multiplied by the days left this week rather than the
        whole remaining balance: spending the month's entire remainder before
        Sunday is exactly the thing this is meant to prevent.
        """
        per_day = self.daily_remaining
        if per_day is None:
            return None
        return Money(per_day.minor * self.days_left_this_week, per_day.currency)

    @property
    def left_today(self) -> Money | None:
        """Today's share, less what has already gone out today."""
        per_day = self.daily_remaining
        if per_day is None:
            return None
        return Money(per_day.minor - self.spent_today.minor, per_day.currency)

    @property
    def left_this_week(self) -> Money | None:
        weekly = self.weekly_remaining
        if weekly is None:
            return None
        return Money(weekly.minor - self.spent_this_week.minor, weekly.currency)

    @property
    def overspent(self) -> list[EnvelopeStatus]:
        """Lines behind schedule, worst first — what to put in front of someone."""
        return sorted(
            (line for line in self.lines if not line.on_track),
            key=lambda line: (line.spent - line.pace).minor,
            reverse=True,
        )


def status(
    budget: Budget,
    transactions: Sequence[Transaction],
    *,
    asof: date | None = None,
    categories: Mapping[str, str] | None = None,
) -> BudgetStatus:
    """Compare spending inside the budget's window against its envelopes.

    "On track" is measured against a pace rather than the whole allowance: two
    days into September nobody has spent their month, and on the 30th spending
    almost all of it is exactly right.

    Spending in a category the budget never mentioned is still reported, with
    an allowance of zero. It is real money that left the account, and hiding
    it would make this screen disagree with the bank statement.

    Only the accounts the budget watches are counted. A card, a debit card and
    cash are all just ways of spending, so the default scope is every account;
    narrowing it is for when one account is genuinely a separate pot.
    """
    today = asof or date.today()
    total_days = budget.days
    # Clamped so a finished budget reports itself complete and one that has
    # not started yet reports nothing spent, rather than either running off
    # the end of its own window.
    if today < budget.starts_on:
        elapsed = 0
    else:
        elapsed = min((today - budget.starts_on).days + 1, total_days)

    # Monday of the week `today` falls in, never earlier than the budget.
    week_starts = max(today - timedelta(days=today.weekday()), budget.starts_on)

    spent: dict[str, int] = {}
    today_minor = week_minor = 0
    for tx in transactions:
        if not budget.covers(tx.date) or tx.date > today:
            continue
        if not budget.watches(tx.account_id):
            continue
        category = _category_of(tx, categories)
        if category in NON_SPENDING or tx.is_transfer:
            continue
        outflow = -tx.amount.minor
        spent[category] = spent.get(category, 0) + outflow
        if tx.date == today:
            today_minor += outflow
        if tx.date >= week_starts:
            week_minor += outflow

    fraction = Decimal(elapsed) / Decimal(total_days) if total_days else Decimal(0)
    lines: list[EnvelopeStatus] = []
    for envelope in budget.envelopes:
        allowance = envelope.allowance
        lines.append(
            EnvelopeStatus(
                category=envelope.category,
                allowance=allowance,
                spent=Money(max(spent.pop(envelope.category, 0), 0), allowance.currency),
                pace=Money(
                    int((Decimal(allowance.minor) * fraction).to_integral_value()),
                    allowance.currency,
                ),
            )
        )
    # Whatever is left was never budgeted for.
    for category, minor in spent.items():
        if minor <= 0:
            continue
        lines.append(
            EnvelopeStatus(
                category=category,
                allowance=Money.zero(),
                spent=Money(minor),
                pace=Money.zero(),
            )
        )
    lines.sort(key=lambda line: (-line.spent.minor, line.category))

    return BudgetStatus(
        budget=budget,
        asof=today,
        elapsed_days=elapsed,
        total_days=total_days,
        lines=lines,
        spent_today=Money(max(today_minor, 0)),
        spent_this_week=Money(max(week_minor, 0)),
    )


# -- helpers --------------------------------------------------------------


def _category_of(transaction: Transaction, categories: Mapping[str, str] | None) -> str:
    """The category a transaction counts under.

    Prefers a lookup the caller supplies — the UI has already applied the
    user's own rules and any guesses — and falls back to the built-in rules so
    this module works without one.
    """
    if categories is not None:
        found = categories.get(transaction.id)
        if found:
            return found
    return transaction.category or categorize(transaction)


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    """(year, month) for every month from `start` up to but excluding `end`."""
    out: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) < (end.year, end.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _sum(amounts) -> Money:
    out = Money.zero()
    for amount in amounts:
        out = out + amount
    return out


def split_with_commitments(
    spare: Money,
    weights: Mapping[str, Money],
    committed: Mapping[str, Money],
) -> list[Envelope]:
    """Share `spare` out, on top of commitments that already have a size.

    Rent is not a suggestion, so it takes its own line at what it actually
    costs and is not scaled by whatever is left over. But a category is rarely
    *only* a commitment: a Shopping line can hold a $20 subscription and $290
    of ordinary shopping, and the ordinary part still needs an allowance.

    Excluding any category that held a commitment from the leftover was the
    bug this replaces. It budgeted $20.03 for Shopping against $307.79 usually
    spent -- seven per cent, blown on the first purchase -- while Dining, which
    happened to contain no commitment, kept ninety-seven per cent of its usual.
    The totals were right and the plan was unusable.

    So the leftover is shared across every category, weighted by what each has
    left *after* its commitment. A category that is nothing but commitment
    draws none of it, which is correct: its cost is already covered.
    """
    discretionary: dict[str, Money] = {}
    for name, usual in weights.items():
        already = committed.get(name)
        remainder = usual.minor - already.minor if already is not None else usual.minor
        if remainder > 0:
            discretionary[name] = Money(remainder, usual.currency)

    # Nothing discretionary anywhere: every category is spoken for, so the
    # leftover has no sensible home but the usual proportions.
    shares = split(spare, discretionary or weights)

    totals: dict[str, Money] = dict(committed)
    for envelope in shares:
        running = totals.get(envelope.category)
        totals[envelope.category] = (
            Money(running.minor + envelope.allowance.minor, running.currency)
            if running is not None
            else envelope.allowance
        )

    # Biggest first, so the lines a person most needs to argue with are at the
    # top rather than sorted by an accident of which held a subscription.
    ordered = sorted(totals.items(), key=lambda item: (-item[1].minor, item[0]))
    return [
        Envelope(category=name, allowance=amount) for name, amount in ordered if amount.minor > 0
    ]


@dataclass(frozen=True, slots=True)
class Line:
    """One category in a plan: what it costs now, and what it may cost.

    Carries the commitment as well as the allowance, because the difference
    between "spend $80 less on this" and "you cannot spend less on this" is
    the whole question a budget is trying to answer.
    """

    category: str
    usual: Money
    committed: Money
    allowance: Money

    @property
    def change(self) -> Money:
        """What has to change. Negative means spending less than you do now."""
        return Money(self.allowance.minor - self.usual.minor, self.usual.currency)

    # How much of a category must be committed before there is no point
    # asking the user to change it. Not 100%: the commitment and the usual
    # spend are two different estimates of the same bill -- a detected series
    # against a median of what was actually paid -- and they disagree by a few
    # dollars. Without the tolerance, rent came out "flexible" on the strength
    # of a $13 difference, and the one line nobody can do anything about was
    # sitting at the top of the list of things to change.
    LOCKED_AT = 0.9

    @property
    def locked(self) -> bool:
        """True when essentially the whole of it is already spoken for.

        Rent is not a decision this month. Neither is an insurance premium
        that costs more than the average of what has lately been paid. A line
        with nothing discretionary in it cannot be part of the answer to
        "what do I change", so it is shown apart from the ones that can.
        """
        if self.usual.minor <= 0:
            return self.committed.minor > 0
        return self.committed.minor >= self.usual.minor * self.LOCKED_AT

    @property
    def discretionary(self) -> Money:
        """The part of the usual spend that is a choice."""
        return Money(max(0, self.usual.minor - self.committed.minor), self.usual.currency)

    @property
    def percent_change(self) -> float | None:
        """How far it has to move, or None when there is no base to divide by."""
        if self.usual.minor == 0:
            return None
        return self.change.minor / abs(self.usual.minor) * 100


# Categories that cannot be changed inside the window, whatever the detected
# commitments come to.
#
# Subscriptions: cancelling Netflix today does not refund this month's charge,
# so the whole category is already spent as far as this budget is concerned --
# and detection only ever finds the ones it recognises, so the rest would
# otherwise read as a choice.
#
# Utilities: technically you could use less heat. Practically the bill arrives
# for what the house already did, and a budget that asks someone to find forty
# dollars in their gas bill this month is asking for something that will not
# happen -- which makes every other figure on the screen wrong too, since the
# money has to come from somewhere real.
ALWAYS_COMMITTED = ("Subscriptions", "Utilities")


def plan(
    budgeted: Money,
    weights: Mapping[str, Money],
    committed: Mapping[str, Money],
    always: Sequence[str] = ALWAYS_COMMITTED,
) -> list[Line]:
    """What each category may cost, given what is committed and what is left.

    `budgeted` is everything available to spend -- income less savings -- and
    every commitment comes out of it first, at its real size. A commitment is
    not a suggestion: rent does not get smaller because the savings target
    grew. What is left over is then shared across the categories that have
    something discretionary in them, in proportion to how much.

    That last part is the point. Deciding to save more money can only ever be
    a decision to spend less on the things you choose, so the whole of that
    squeeze lands on them and none of it on the rent.

    Locked lines come last, since the answer to "what do I change" is never
    among them.
    """
    # A category that cannot be changed is committed at what it actually
    # costs, not merely at the part of it detection recognised.
    fixed = dict(committed)
    for name in always:
        usual = weights.get(name)
        if usual is None:
            continue
        already = fixed.get(name)
        if already is None or already.minor < usual.minor:
            fixed[name] = usual

    spare = Money(budgeted.minor - sum(a.minor for a in fixed.values()), budgeted.currency)

    discretionary: dict[str, Money] = {}
    for name, usual in weights.items():
        already = fixed.get(name)
        remainder = usual.minor - already.minor if already is not None else usual.minor
        if remainder > 0:
            discretionary[name] = Money(remainder, usual.currency)

    # A negative spare means the commitments alone overrun the budget. Sharing
    # out a negative number would hand every category a negative allowance,
    # which is not a plan; the caller reports the shortfall instead.
    shares = (
        {e.category: e.allowance for e in split(spare, discretionary)}
        if spare.minor > 0 and discretionary
        else {}
    )

    lines = [
        Line(
            category=name,
            usual=usual,
            committed=fixed.get(name, Money(0, usual.currency)),
            allowance=Money(
                fixed.get(name, Money(0, usual.currency)).minor
                + shares.get(name, Money(0, usual.currency)).minor,
                usual.currency,
            ),
        )
        for name, usual in weights.items()
    ]
    lines.sort(key=lambda line: (line.locked, -line.allowance.minor, line.category))
    return lines


def totals(lines: Sequence[Line]) -> Line:
    """One row summing the lot, for the bottom of the table."""
    currency = lines[0].usual.currency if lines else "USD"
    return Line(
        category="Total",
        usual=Money(sum(line.usual.minor for line in lines), currency),
        committed=Money(sum(line.committed.minor for line in lines), currency),
        allowance=Money(sum(line.allowance.minor for line in lines), currency),
    )


@dataclass(frozen=True, slots=True)
class Impact:
    """What one budget does to another it sits inside.

    "This contradicts that" is true and not much help when the question is
    "can I afford the trip". What answers it is the arithmetic: this many
    days cost this much, which leaves that much for the days either side, and
    here is what those days were going to get.
    """

    outer: Budget
    inner: Budget
    days: int  # days the inner one occupies
    outer_days: int
    fits: bool
    inner_total: Money
    outer_total: Money
    left_for_the_rest: Money
    normal_per_day: Money
    per_day_after: Money

    @property
    def other_days(self) -> int:
        return max(self.outer_days - self.days, 0)

    @property
    def squeeze(self) -> Money:
        """How much less each remaining day gets. Negative means more."""
        return Money(
            self.normal_per_day.minor - self.per_day_after.minor,
            self.normal_per_day.currency,
        )

    def describe(self) -> str:
        """One paragraph a person can decide on."""
        span = f"{self.days} day{'s' if self.days != 1 else ''}"
        if not self.fits:
            short = Money(-self.left_for_the_rest.minor, self.outer_total.currency)
            return (
                f"{self.inner_total.format()} over {span} is more than "
                f"“{self.outer.name}” has left in total ({self.outer_total.format()}). "
                f"You would be {short.format()} short before the other "
                f"{self.other_days} days start."
            )
        return (
            f"{self.inner_total.format()} over {span} leaves "
            f"{self.left_for_the_rest.format()} for the other {self.other_days} days of "
            f"“{self.outer.name}” — {self.per_day_after.format()} a day instead of "
            f"{self.normal_per_day.format()}."
        )


def impact(inner: Budget, outer: Budget) -> Impact | None:
    """What `inner` costs `outer`, or None when it does not sit inside it.

    Only computed for a nested window, for the same reason `clashes` only
    claims a contradiction there: if every day of the inner budget falls
    inside the outer one, every dollar it permits also comes out of the outer
    one. Partial overlap could have its spending land in unshared days, and
    the arithmetic would be a guess dressed as an answer.
    """
    if not (inner.starts_on >= outer.starts_on and inner.ends_on <= outer.ends_on):
        return None
    if not _shares_accounts(inner, outer):
        return None

    inner_days = inner.days
    outer_days = outer.days
    other_days = max(outer_days - inner_days, 0)

    left = Money(outer.total.minor - inner.total.minor, outer.total.currency)
    normal = (
        Money(outer.total.minor // outer_days, outer.total.currency) if outer_days else Money(0)
    )
    after = Money(left.minor // other_days, left.currency) if other_days else Money(0)

    return Impact(
        outer=outer,
        inner=inner,
        days=inner_days,
        outer_days=outer_days,
        fits=left.minor >= 0,
        inner_total=inner.total,
        outer_total=outer.total,
        left_for_the_rest=left,
        normal_per_day=normal,
        per_day_after=after,
    )


def impacts(inner: Budget, others: Sequence[Budget]) -> list[Impact]:
    """Every existing budget this one would eat into, tightest first."""
    found = [
        made
        for other in others
        if other.id != inner.id and (made := impact(inner, other)) is not None
    ]
    found.sort(key=lambda made: (made.fits, -made.squeeze.minor))
    return found
