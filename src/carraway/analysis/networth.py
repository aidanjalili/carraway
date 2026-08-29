"""Reconstruct net worth over time from transactions and today's balances.

Every personal-finance product's headline chart is net worth over time, and
Carraway cannot draw it the obvious way: nothing stores a historical balance.
A sync provider reports one number per account — what it holds *right now* —
and an imported CSV reports no balance at all. The history has to be inferred.

The inference is a backwards walk. If the balance at the end of a day is known,
then the balance at the end of the day before is that figure with the day's
transactions taken back out:

    balance(t - 1) = balance(t) - sum(transactions on t)

Getting that subtraction the wrong way round is the easiest mistake in this
file, and it is invisible in aggregate: the result still looks like a plausible
net worth curve, only mirrored about today. The doctest on `reconstruct()`
pins the direction down.

Everything else follows from one internal convention: **a signed balance where
positive is value held and negative is value owed**, for every account type.
Transaction signs already work that way — negative is money leaving you, on a
card as well as a chequing account — so the recurrence above holds unchanged
for a credit card, a transfer between your own accounts is two halves summing
to zero and therefore moves money without moving the total, and the split into
assets and liabilities at each point is just the sign of each balance. Only the
*input* balances need normalising, because providers report a card's balance as
an amount owed rather than as value held.

What we refuse to do is guess. An account with no known current balance is
excluded from the series entirely and named in `NetWorthPoint.excluded`. The
tempting alternative — treat the unknown as zero — is much worse than it looks:
the walk would still be internally consistent, so the shape of that account's
history would be right while every point in it sat a constant offset away from
the truth, silently biasing the total and the chart with nothing to show that
anything was wrong.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..core.models import Account, Transaction
from ..core.money import Money

GRANULARITIES = ("daily", "weekly", "monthly")


@dataclass(frozen=True, slots=True)
class NetWorthPoint:
    """Net worth at the end of one day.

    `liabilities` is stored **positive**, as an amount owed: a $791.76 card
    balance is `Money(79176)`, not `Money(-79176)`. Reading "liabilities" as a
    negative number is a habit worth not encouraging, and `net` already carries
    the sign — `assets - liabilities`.
    """

    date: date
    assets: Money
    liabilities: Money
    net: Money
    # Signed per-account balances at this point: positive held, negative owed.
    # Kept so a caller can attribute a movement in the total to an account
    # without re-running the whole walk.
    balances: dict[str, Money] = field(default_factory=dict)
    # Accounts left out of the series for want of a current balance. Identical
    # on every point — it describes the series, not the day — but carried here
    # so that a caller plotting points cannot fail to notice.
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NetWorthSummary:
    """Change in net worth across a series, for a "you are up $X" headline."""

    start: date | None
    end: date | None
    start_net: Money
    end_net: Money
    change: Money
    # None when the starting net worth was zero or negative, where a percentage
    # is not merely awkward but undefined — see `summarise()`.
    percent_change: float | None
    best_month: tuple[str, Money] | None
    worst_month: tuple[str, Money] | None


def accounts_missing_balances(
    accounts: list[Account], current_balances: dict[str, Money]
) -> list[str]:
    """Account ids that `reconstruct()` would have to leave out, sorted.

    Worth calling before drawing anything, so the user can be told the chart
    covers three of their five accounts rather than being shown a total that
    quietly is not their net worth.
    """
    return sorted(a.id for a in accounts if a.id not in current_balances)


def normalise_balance(account: Account, balance: Money) -> Money:
    """Convert a reported balance into the internal signed convention.

    Assets pass through untouched, including an overdrawn chequing account,
    whose negative balance really is money owed.

    Liabilities are forced negative. Providers disagree about the sign of a
    card balance — SimpleFIN reports `-791.76` for $791.76 owed, while a
    statement says `791.76` for exactly the same debt — and both must land on
    the same net worth:

    >>> from carraway.core.models import AccountType
    >>> visa = Account("v", "Visa", AccountType.CREDIT_CARD)
    >>> normalise_balance(visa, Money(-79176)).format()
    '-$791.76'
    >>> normalise_balance(visa, Money(79176)).format()
    '-$791.76'

    The cost of that rule is that a *credit* balance on a card — a genuine
    overpayment, where the issuer owes you — is read as debt. That trade is
    deliberate. Overpaid cards are rare and small; taking a $791 debt for a
    $791 asset is a $1,583 error in the headline figure, and it flatters the
    user in the one direction a money manager must never flatter them.
    """
    if account.type.is_liability:
        return -abs(balance)
    return balance


def reconstruct(
    accounts: list[Account],
    transactions: list[Transaction],
    current_balances: dict[str, Money],
    *,
    start: date | None = None,
    granularity: str = "daily",
    as_of: date | None = None,
) -> list[NetWorthPoint]:
    """Rebuild the net worth series, oldest point first.

    `current_balances` maps account id to what that account holds today, in
    whatever sign the provider used; liabilities are normalised by
    `normalise_balance()`. Accounts absent from the mapping are excluded and
    named in `NetWorthPoint.excluded` — see the module docstring for why that
    beats assuming zero.

    `as_of` is the date the balances are true as of, defaulting to the most
    recent transaction. Pass `date.today()` for a series that runs to today
    with a flat tail; transactions dated after `as_of` are ignored, since the
    balance is taken as the last word on what has actually settled.

    Balances at the end of each day, walking backwards from a known total:

    >>> from carraway.core.models import AccountType
    >>> checking = Account("c", "Checking", AccountType.CHECKING)
    >>> coffee = Transaction("t1", "c", date(2026, 3, 2), Money(-2500), "Blue Bottle")
    >>> points = reconstruct([checking], [coffee], {"c": Money(10000)},
    ...                      start=date(2026, 3, 1), as_of=date(2026, 3, 2))
    >>> [(p.date.isoformat(), p.net.format()) for p in points]
    [('2026-03-01', '$125.00'), ('2026-03-02', '$100.00')]

    $100 today, after a $25 coffee, means $125 the day before — not $75. The
    balance is the anchor and the transactions are the road back to the past.
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {GRANULARITIES}, got {granularity!r}")

    included = [a for a in accounts if a.id in current_balances]
    excluded = tuple(accounts_missing_balances(accounts, current_balances))
    if not included:
        return []

    # Currency comes from the accounts themselves. Mixing them raises out of
    # Money's own arithmetic, which is the right outcome: adding EUR to USD
    # needs an exchange rate, and a rate for a date in the past needs a rate
    # *history*, which is a different feature with a different data source.
    currency = included[0].currency or "USD"
    signed = {a.id: normalise_balance(a, current_balances[a.id]) for a in included}

    # Transactions on accounts we are not tracking would corrupt the walk, so
    # they are dropped rather than applied to a balance that does not exist.
    # Transfers are deliberately *kept*: they move real money between real
    # accounts, and they cancel out in the total on their own.
    known = set(signed)
    txs = [t for t in transactions if t.account_id in known]
    if as_of is None:
        as_of = max((t.date for t in txs), default=date.today())
    txs = [t for t in txs if t.date <= as_of]

    if start is None:
        start = min((t.date for t in txs), default=as_of)
    start = min(start, as_of)

    by_day: dict[date, list[Transaction]] = defaultdict(list)
    for tx in txs:
        by_day[tx.date].append(tx)
    tx_days = sorted(by_day)

    sample_dates = _sample_dates(start, as_of, granularity)

    # Walk newest to oldest, unwinding the transactions between each pair of
    # sample dates, then reverse. `cursor` sweeps tx_days downwards so no day
    # is visited twice, which keeps a decade of monthly points cheap.
    balances = dict(signed)
    cursor = len(tx_days) - 1
    reversed_points: list[NetWorthPoint] = []
    for i in range(len(sample_dates) - 1, -1, -1):
        day = sample_dates[i]
        reversed_points.append(_point(day, balances, currency, excluded))
        if i == 0:
            break
        previous = sample_dates[i - 1]
        # Take back everything in (previous, day]: those transactions had not
        # happened yet as of `previous`.
        while cursor >= 0 and tx_days[cursor] > previous:
            for tx in by_day[tx_days[cursor]]:
                balances[tx.account_id] = balances[tx.account_id] - tx.amount
            cursor -= 1

    reversed_points.reverse()
    return reversed_points


def summarise(points: list[NetWorthPoint]) -> NetWorthSummary:
    """Change over the series, in Money and percent, with the best/worst month.

    Percent change is `None` whenever the series starts at zero or below.
    Dividing by zero is the obvious half of that; the other half is that a
    percentage of a negative base is actively misleading — climbing out of
    -$5,000 of debt to -$1,000 is a $4,000 improvement that the arithmetic
    renders as "-80%", which reads as a loss. A number that says the opposite
    of what happened is worse than no number, so the caller is handed `None`
    and can print the absolute change instead.

    Months are `"YYYY-MM"` labels, and each month's figure is the sum of the
    point-to-point movements landing in it, so this works at any granularity.
    A month only partly covered by the series is summed over the part covered.
    """
    if not points:
        zero = Money.zero()
        return NetWorthSummary(None, None, zero, zero, zero, None, None, None)

    first, last = points[0], points[-1]
    change = last.net - first.net

    percent: float | None = None
    if first.net.minor > 0:
        percent = round(change.minor / first.net.minor * 100, 2)

    monthly: dict[str, Money] = {}
    for earlier, later in zip(points, points[1:], strict=False):
        key = f"{later.date.year:04d}-{later.date.month:02d}"
        delta = later.net - earlier.net
        monthly[key] = monthly[key] + delta if key in monthly else delta

    best = worst = None
    if monthly:
        best = max(monthly.items(), key=lambda kv: kv[1].minor)
        worst = min(monthly.items(), key=lambda kv: kv[1].minor)

    return NetWorthSummary(
        start=first.date,
        end=last.date,
        start_net=first.net,
        end_net=last.net,
        change=change,
        percent_change=percent,
        best_month=best,
        worst_month=worst,
    )


def monthly_cashflow(transactions: list[Transaction]) -> list[tuple[str, Money, Money, Money]]:
    """Income, spending and net per calendar month, oldest month first.

    Transfers are excluded. Moving $500 from chequing to savings is not $500 of
    income and not $500 of spending, and counting it as either turns a budget
    into fiction.

    `spending` is returned **positive**, as an amount spent, so that
    `net == income - spending` reads the way a person would say it out loud.

    Months with no activity are emitted as zeros rather than skipped. A gap in
    a cashflow chart is read as "no data", but a month in the middle of a
    statement history with nothing in it is a real, informative zero.

    >>> pay = Transaction("1", "c", date(2026, 1, 5), Money(240000), "Payroll")
    >>> rent = Transaction("2", "c", date(2026, 1, 6), Money(-180000), "Rent")
    >>> [(m, i.format(), s.format(), n.format()) for m, i, s, n in monthly_cashflow([pay, rent])]
    [('2026-01', '$2,400.00', '$1,800.00', '$600.00')]
    """
    real = [t for t in transactions if not t.is_transfer]
    if not real:
        return []

    currency = real[0].amount.currency
    income: dict[str, Money] = defaultdict(lambda: Money.zero(currency))
    spending: dict[str, Money] = defaultdict(lambda: Money.zero(currency))
    for tx in real:
        key = f"{tx.date.year:04d}-{tx.date.month:02d}"
        if tx.is_outflow:
            spending[key] = spending[key] + abs(tx.amount)
        else:
            income[key] = income[key] + tx.amount

    rows = []
    for key in _month_keys(min(t.date for t in real), max(t.date for t in real)):
        got, spent = income[key], spending[key]
        rows.append((key, got, spent, got - spent))
    return rows


# -- internals -----------------------------------------------------------


def _point(
    day: date, balances: dict[str, Money], currency: str, excluded: tuple[str, ...]
) -> NetWorthPoint:
    """Split a set of signed balances into assets and liabilities for one day.

    The split is by the sign of the balance rather than by account type, which
    is both simpler and more truthful: an overdrawn chequing account is a debt
    that day, and a card carrying a credit balance is an asset that day.
    """
    assets = Money.zero(currency)
    liabilities = Money.zero(currency)
    for amount in balances.values():
        if amount.minor < 0:
            liabilities = liabilities + abs(amount)
        else:
            assets = assets + amount
    return NetWorthPoint(
        date=day,
        assets=assets,
        liabilities=liabilities,
        net=assets - liabilities,
        balances=dict(balances),
        excluded=excluded,
    )


def _sample_dates(start: date, end: date, granularity: str) -> list[date]:
    """Dates to emit a point for, ascending, each meaning "end of this day".

    `start` and `end` are always included even when they fall mid-period, so
    that the series covers exactly the range asked for. Without the `start`
    anchor a monthly series beginning on the 15th would silently discard the
    first half-month of movement from every total computed over it.
    """
    if end <= start:
        return [start]

    dates = [start]
    if granularity == "daily":
        day = start + timedelta(days=1)
        while day < end:
            dates.append(day)
            day += timedelta(days=1)
    elif granularity == "weekly":
        day = _week_end(start)
        while day < end:
            if day > start:
                dates.append(day)
            day += timedelta(days=7)
    else:  # monthly
        day = _month_end(start)
        while day < end:
            if day > start:
                dates.append(day)
            day = _month_end(day + timedelta(days=1))
    dates.append(end)
    return dates


def _week_end(day: date) -> date:
    """The Sunday closing `day`'s ISO week, so weeks break where calendars do."""
    return day + timedelta(days=6 - day.weekday())


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _month_keys(first: date, last: date) -> list[str]:
    """Every "YYYY-MM" from `first` to `last` inclusive, with no gaps."""
    keys = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys
