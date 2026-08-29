"""Aggregate spending into periods, categories and merchants.

`networth.py` answers "how much do I have"; this module answers "where is it
going", in the three shapes a chart needs: a series over time (bar), a split by
category (pie), and a ranking by merchant (table). One pass of arithmetic backs
all three so they can never disagree with each other on screen.

Four decisions drive everything here.

**Spending is reported positive.** Internally a purchase is negative — money
leaving you — but a chart is read as "I spent $420 on Dining", not "-$420", and
a pie chart cannot draw a negative slice at all. So every figure this module
returns is `-tx.amount`: an outflow of `-1299` becomes `Money(1299)` spent.

**Transfers and income are not spending.** Moving $500 to savings is not $500
gone, and a payslip in the same pie as the grocery bill makes every slice
meaningless. Both halves of a matched transfer are dropped, and so is anything
the categoriser calls `Transfer` — a card autopay drawn from chequing is money
moving between your own accounts even when the other half was never imported.
Inflows are dropped too unless `include_income=True`.

The cost of that default is that refunds do not net off: a $200 jacket returned
for $200 still shows as $200 of Shopping. That is the right way round. A refund
usually lands weeks after the purchase, so netting it silently would carve
money out of a later period that was never spent in it — and with
`include_income=True` an inflow subtracts from its category, which is exactly
the netting behaviour, available to a caller who wants it and knows what they
asked for.

**Empty periods are emitted as zero buckets, never skipped.** A chart that
draws only the periods with activity re-spaces its own axis: a quiet fortnight
disappears and the two spending weeks either side of it end up neighbours. The
user reads that as "I spent every week". A zero bar is a fact, and it is the
fact they need.

**Weeks start on Monday** (`date.weekday() == 0`), matching `budget.py` and the
ISO week that `networth.py`'s Sunday week-ends close. A week that starts on
Sunday would put a Saturday night out in the following week's total, which is
not how anyone accounts for a weekend.

Money stays an integer count of cents throughout: totals are Money additions
only, so the parts always sum to exactly the whole with no rounding drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from ..core.models import Transaction
from ..core.money import Money
from .categorize import TRANSFER, UNCATEGORIZED
from .recurring import normalise_merchant

Period = Literal["daily", "weekly", "monthly", "yearly"]

PERIODS: tuple[str, ...] = ("daily", "weekly", "monthly", "yearly")

# Written out rather than taken from `calendar.month_abbr` or `strftime("%b")`,
# both of which follow LC_TIME. A label that changes with the machine's locale
# makes the same chart read differently on two computers.
_MONTH_ABBR = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


@dataclass(frozen=True, slots=True)
class Bucket:
    """What was spent in one period.

    `start` is inclusive and `end` is **exclusive** — `end` is the first day of
    the next period, so consecutive buckets tile the timeline with no gap and
    no overlap, and `end - start` is the period's true length. `last_day` is
    the inclusive final day, which is what an axis tick or a tooltip wants.

    `total` and every value in `by_category` are positive amounts spent (see
    the module docstring), and `total` is exactly the sum of `by_category`.
    """

    start: date
    end: date
    label: str
    total: Money
    by_category: dict[str, Money] = field(default_factory=dict)

    @property
    def last_day(self) -> date:
        """The final day the bucket covers, inclusive."""
        return self.end - timedelta(days=1)

    @property
    def is_empty(self) -> bool:
        """True when no transaction landed in this period at all.

        Distinct from a zero `total`, which a refund offsetting a purchase can
        also produce under `include_income=True`. A chart may want to draw
        those two differently: one is nothing happening, the other is a wash.
        """
        return not self.by_category


@dataclass(frozen=True, slots=True)
class CategoryChange:
    """How one category moved between two periods."""

    category: str
    before: Money
    after: Money
    change: Money  # after - before; positive means more was spent
    # None when nothing was spent in this category before, where a percentage
    # is undefined rather than infinite — see `compare`.
    percent_change: float | None


@dataclass(frozen=True, slots=True)
class Comparison:
    """The difference between two buckets, in total and per category."""

    before: Bucket
    after: Bucket
    change: Money
    percent_change: float | None
    # Biggest mover first, by absolute change, so a UI can take the top few.
    categories: list[CategoryChange] = field(default_factory=list)


# -- period arithmetic ---------------------------------------------------


def period_start(day: date, period: Period = "monthly") -> date:
    """The first day of the period containing `day`.

    >>> period_start(date(2026, 8, 6), "weekly")   # a Thursday
    datetime.date(2026, 8, 3)
    >>> period_start(date(2026, 8, 6), "monthly")
    datetime.date(2026, 8, 1)
    """
    _check_period(period)
    if period == "daily":
        return day
    if period == "weekly":
        return day - timedelta(days=day.weekday())
    if period == "monthly":
        return day.replace(day=1)
    return day.replace(month=1, day=1)


def period_end(day: date, period: Period = "monthly") -> date:
    """The first day of the *next* period — an exclusive end, so ranges compose.

    >>> period_end(date(2026, 8, 6), "monthly")
    datetime.date(2026, 9, 1)
    """
    start = period_start(day, period)
    if period == "daily":
        return start + timedelta(days=1)
    if period == "weekly":
        return start + timedelta(days=7)
    if period == "monthly":
        return (
            date(start.year + 1, 1, 1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    return date(start.year + 1, 1, 1)


def period_label(day: date, period: Period = "monthly") -> str:
    """A short human label for the period containing `day`.

    >>> [period_label(date(2026, 8, 6), p) for p in PERIODS]
    ['2026-08-06', 'Aug 3-9', '2026-08', '2026']

    A week spanning a month boundary names both months, so "Aug 31-Sep 6"
    cannot be misread as a week in August.
    """
    start = period_start(day, period)
    if period == "daily":
        return start.isoformat()
    if period == "weekly":
        last = start + timedelta(days=6)
        if last.month == start.month:
            return f"{_MONTH_ABBR[start.month]} {start.day}-{last.day}"
        return f"{_MONTH_ABBR[start.month]} {start.day}-{_MONTH_ABBR[last.month]} {last.day}"
    if period == "monthly":
        return f"{start.year:04d}-{start.month:02d}"
    return f"{start.year:04d}"


# -- the entry points ----------------------------------------------------


def buckets(
    transactions: Sequence[Transaction],
    *,
    period: Period = "monthly",
    categories: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    include_income: bool = False,
) -> list[Bucket]:
    """Spending per period, oldest first, with **no periods missing**.

    Every period between the first and last is emitted, including the ones
    with nothing in them — see the module docstring for why a gap is worse
    than a zero.

    `categories` is the parallel list `categorize.categorize_all` returns: one
    label per transaction, same order. It is checked against the length of
    `transactions` rather than trusted, because a list that has slipped by one
    would file every remaining purchase under its neighbour's category and
    look entirely plausible on screen. Omit it and each transaction's own
    `category` is used, falling back to "Uncategorized".

    `start` and `end` are both **inclusive** day bounds on the transactions
    considered. Bucket boundaries are always whole periods, so a range
    beginning mid-month yields a first bucket covering that whole month but
    counting only the days from `start` — align the range with
    `period_start()` when that matters. Supplying both bounds also lets a
    range wider than the data return zero buckets across the whole span, which
    is how a chart says "nothing here yet" instead of drawing nothing.

    >>> txs = [
    ...     Transaction("1", "c", date(2026, 1, 6), Money(-2500), "Blue Bottle"),
    ...     Transaction("2", "c", date(2026, 3, 4), Money(-4000), "Trader Joe's"),
    ... ]
    >>> [(b.label, b.total.format()) for b in buckets(txs, categories=["Dining", "Groceries"])]
    [('2026-01', '$25.00'), ('2026-02', '$0.00'), ('2026-03', '$40.00')]

    February is quiet, not absent.
    """
    _check_period(period)
    rows = _rows(transactions, categories, start, end, include_income=include_income)

    first = start if start is not None else min((tx.date for tx, _, _ in rows), default=None)
    last = end if end is not None else max((tx.date for tx, _, _ in rows), default=None)
    if first is None or last is None or last < first:
        return []

    currency = _currency(transactions)
    totals: dict[date, dict[str, Money]] = {}
    for tx, category, spent in rows:
        by_category = totals.setdefault(period_start(tx.date, period), {})
        running = by_category.get(category)
        by_category[category] = running + spent if running is not None else spent

    out: list[Bucket] = []
    cursor = period_start(first, period)
    while cursor <= last:
        by_category = totals.get(cursor, {})
        out.append(
            Bucket(
                start=cursor,
                end=period_end(cursor, period),
                label=period_label(cursor, period),
                total=_sum(by_category.values(), currency),
                by_category=dict(by_category),
            )
        )
        cursor = period_end(cursor, period)
    return out


def category_totals(
    transactions: Sequence[Transaction],
    *,
    categories: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[tuple[str, Money, int]]:
    """`(category, total, count)` over the whole range, biggest spend first.

    This is the pie chart. Categories with nothing in them are absent rather
    than zero — the opposite of `buckets`, and for the same reason: an empty
    period is a fact about the timeline, while a zero-width slice is only
    clutter around the edge of a circle.

    Ties break on count and then name, so the order is stable across runs and
    a chart's colours do not shuffle when two categories happen to level.

    >>> txs = [
    ...     Transaction("1", "c", date(2026, 1, 6), Money(-2500), "Blue Bottle"),
    ...     Transaction("2", "c", date(2026, 1, 9), Money(-1500), "Philz"),
    ...     Transaction("3", "c", date(2026, 1, 4), Money(-4000), "Trader Joe's"),
    ... ]
    >>> [(c, m.format(), n) for c, m, n in
    ...  category_totals(txs, categories=["Dining", "Dining", "Groceries"])]
    [('Dining', '$40.00', 2), ('Groceries', '$40.00', 1)]
    """
    rows = _rows(transactions, categories, start, end, include_income=False)
    return _ranked((category, spent) for _, category, spent in rows)


def top_merchants(
    transactions: Sequence[Transaction],
    *,
    limit: int = 10,
    categories: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[tuple[str, Money, int]]:
    """`(merchant, total, count)` for the biggest `limit` merchants, biggest first.

    The "where did it actually go" view: a category answers what kind of thing
    the money was, a merchant answers who has it now.

    Pass `categories` from `categorize_all` here too. Without it only a matched
    `transfer_group` can hide a transfer, and on real data that is not enough:
    an unmatched card autopay and a Venmo repayment are large, frequent and
    unmistakably *not* purchases, and they take the top of the table away from
    the merchants the user is actually looking for.

    Merchants are grouped case-insensitively on the transaction's own
    `merchant` when an importer or the user has set one, and on
    `recurring.normalise_merchant` otherwise, so "SQ *BLUE BOTTLE #402 SF" and
    a hand-tidied "Blue Bottle" are one row rather than two.

    >>> txs = [
    ...     Transaction("1", "c", date(2026, 1, 6), Money(-2500), "SQ *BLUE BOTTLE #402 SF"),
    ...     Transaction("2", "c", date(2026, 1, 9), Money(-1500), "Coffee", merchant="BLUE BOTTLE"),
    ... ]
    >>> [(m, t.format(), n) for m, t, n in top_merchants(txs)]
    [('Blue Bottle', '$40.00', 2)]
    """
    if limit < 0:
        raise ValueError(f"limit must not be negative, got {limit}")
    rows = _rows(transactions, categories, start, end, include_income=False)
    ranked = _ranked((_merchant_of(tx), spent) for tx, _, spent in rows)
    return ranked[:limit]


def compare(a: Bucket, b: Bucket) -> Comparison:
    """What changed between two buckets, per category — "Dining is up $120".

    `a` is the baseline (last month) and `b` is the period being described
    (this month), so `change` is `b - a` and a positive number means more was
    spent. Categories present in either bucket appear, biggest absolute move
    first, so a UI can take the top three and have the three that matter.

    Percent change from a base of zero is **undefined**, and is reported as
    `None` rather than as infinity or a nan. A new category is not an infinite
    increase; it is a new category, and "Pets +inf%" is a bug on screen.

    >>> jan = Bucket(date(2026, 1, 1), date(2026, 2, 1), "2026-01", Money(10000),
    ...              {"Dining": Money(10000)})
    >>> feb = Bucket(date(2026, 2, 1), date(2026, 3, 1), "2026-02", Money(16000),
    ...              {"Dining": Money(12000), "Pets": Money(4000)})
    >>> [(c.category, c.change.format(), c.percent_change) for c in compare(jan, feb).categories]
    [('Pets', '$40.00', None), ('Dining', '$20.00', 20.0)]
    """
    currency = a.total.currency
    zero = Money.zero(currency)
    changes = []
    for category in sorted(set(a.by_category) | set(b.by_category)):
        before = a.by_category.get(category, zero)
        after = b.by_category.get(category, zero)
        changes.append(
            CategoryChange(
                category=category,
                before=before,
                after=after,
                change=after - before,
                percent_change=_percent(after - before, before),
            )
        )
    changes.sort(key=lambda c: (-abs(c.change.minor), c.category))

    change = b.total - a.total
    return Comparison(
        before=a,
        after=b,
        change=change,
        percent_change=_percent(change, a.total),
        categories=changes,
    )


# -- internals -----------------------------------------------------------


def _check_period(period: str) -> None:
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}, got {period!r}")


def _rows(
    transactions: Sequence[Transaction],
    categories: Sequence[str] | None,
    start: date | None,
    end: date | None,
    *,
    include_income: bool,
) -> list[tuple[Transaction, str, Money]]:
    """The transactions that count as spending, each with a category and a positive amount."""
    if categories is not None and len(categories) != len(transactions):
        raise ValueError(
            f"categories must be parallel to transactions: got {len(categories)} labels "
            f"for {len(transactions)} transactions"
        )
    labels = (
        list(categories)
        if categories is not None
        else [tx.category or UNCATEGORIZED for tx in transactions]
    )

    rows = []
    for tx, category in zip(transactions, labels, strict=True):
        # The categoriser's Transfer verdict is trusted alongside the matched
        # `transfer_group`, since a card payment often imports with only one
        # half present and is still not spending.
        if tx.is_transfer or category == TRANSFER:
            continue
        if start is not None and tx.date < start:
            continue
        if end is not None and tx.date > end:
            continue
        if not include_income and not tx.is_outflow:
            continue
        # Negating flips an outflow to the positive figure a chart shows, and
        # leaves an included inflow negative so it offsets its own category.
        rows.append((tx, category or UNCATEGORIZED, -tx.amount))
    return rows


def _ranked(pairs) -> list[tuple[str, Money, int]]:
    """Group `(name, amount)` pairs into `(name, total, count)`, biggest total first.

    Grouping is case-insensitive, and the first spelling seen is the one shown:
    the same shop arrives shouted from the bank and title-cased from a user's
    edit, and two rows for one coffee shop is the fragmentation these views
    exist to undo.
    """
    names: dict[str, str] = {}
    totals: dict[str, Money] = {}
    counts: dict[str, int] = {}
    for name, amount in pairs:
        key = name.casefold()
        names.setdefault(key, name)
        running = totals.get(key)
        totals[key] = running + amount if running is not None else amount
        counts[key] = counts.get(key, 0) + 1
    return sorted(
        ((names[key], amount, counts[key]) for key, amount in totals.items()),
        key=lambda row: (-row[1].minor, -row[2], row[0]),
    )


def _merchant_of(tx: Transaction) -> str:
    """A display name for the merchant, stable enough to group on.

    Grouping is case-insensitive because the same shop arrives shouted from the
    bank and title-cased from a user's edit, and two rows for one coffee shop
    is exactly the fragmentation this view exists to undo.
    """
    return (
        tx.merchant.strip() if tx.merchant.strip() else normalise_merchant(tx.description).title()
    )


def _sum(amounts, currency: str) -> Money:
    result = Money.zero(currency)
    for amount in amounts:
        result = result + amount
    return result


def _percent(change: Money, base: Money) -> float | None:
    """Percent change against `base`, or None where that is not a number.

    A base of zero has no percentage, and a negative base — possible only under
    `include_income`, where refunds outweighed purchases — produces a figure
    with the wrong sign, which is worse than no figure at all. Both are None,
    the same rule `networth.summarise` follows.
    """
    if base.minor <= 0:
        return None
    return round(change.minor / base.minor * 100, 2)


def _currency(transactions: Sequence[Transaction]) -> str:
    """The currency empty buckets are denominated in.

    Taken from the data rather than defaulted to USD, so a euro account's quiet
    month reads "€0.00". Mixing currencies is left to raise out of Money's own
    arithmetic, which is the right outcome: adding EUR to USD needs a rate.
    """
    return transactions[0].amount.currency if transactions else "USD"
