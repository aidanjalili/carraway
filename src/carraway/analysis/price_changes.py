"""Detect price changes inside a recurring charge.

Recurring detection answers *what do I pay for?*. This module answers the
follow-up question, which is the one that actually costs people money:
*when did it get more expensive, and by how much a year?*

    Netflix went from $15.49 to $17.99 in March — that is $30/year more.

The whole problem is telling a genuine price change from ordinary variation.
A subscription that reads 15.49, 15.49, 15.49, 17.99, 17.99, 17.99 has plainly
changed price. A utility bill that reads 84, 142, 61, 178, 95 has not: it is
merely a bill that varies, and reporting it as a "70% increase" would train the
user to ignore the feature entirely. Comparing the first amount with the last
one gets both of these wrong.

So we look for a **step**: a point where the series is stable before, stable
after, and clearly different across.

1. **Split.** Try every division of the run into a before and an after, and
   keep the one that minimises within-side squared deviation. That is textbook
   binary segmentation, and it lands on a real boundary rather than merely
   somewhere in the middle.
2. **Test.** Accept the split only if the gap between the two levels is large
   compared with the worst wobble *inside* either level (see MIN_SEPARATION),
   only if it clears both a percentage and an absolute floor, and only if the
   old price never comes back (see _reverts).
3. **Recurse.** A price that rose twice in three years contains a step inside
   each half, so re-run the search on both sides of an accepted step. A
   rejected step ends that branch: if the strongest division of a run is not
   convincing, no weaker one inside it will be.
4. **Annualise.** A $2.50 rise costs $30/year monthly and $130/year weekly.
   The cadence is the entire point, so it comes from the `RecurringSeries`
   when one is supplied and is measured from the charge dates when it is not.

Sign convention is the project's throughout: **negative is money leaving you**.
A subscription moving from -15.49 to -17.99 is an *increase* — you pay more —
and its `annual_impact` is -30.00, meaning $30 more leaves you each year.
`direction` compares magnitudes so it reads correctly for income too: a raise
on a paycheck is also an "increase".
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..core.models import RecurringSeries, Transaction
from ..core.money import Money
from .recurring import _classify_gaps, normalise_merchant

# How many billing periods a cadence fits into a year. Mirrors
# RecurringSeries.annualised, and is why annual_impact needs a cadence at all.
PERIODS_PER_YEAR: dict[str, int] = {
    "weekly": 52,
    "biweekly": 26,
    "monthly": 12,
    "quarterly": 4,
    "yearly": 1,
}

DAYS_PER_YEAR = 365.25

# Charges at each price before we believe the price changed. One charge at the
# new figure is as likely to be a proration, a late fee or a partial refund as
# a new price; two in a row is the same argument recurring.py makes for calling
# three charges a pattern, applied to each side of the step separately.
MIN_RUN = 2

# ...but two charges only establish a price if they are the *same* charge. Two
# consecutive DoorDash orders happen to land within a few dollars of each other
# all the time, and against a noisy history that reads as a step down. A level
# built from fewer than three charges must therefore be exactly repeated; below
# that it is a coincidence, not a price. This mirrors the argument recurring.py
# makes for letting a yearly series qualify on a single interval: what counts
# as enough evidence depends on how easily chance produces it.
MIN_VARYING_RUN = 3

# The two floors. Both must be cleared, so the stricter one governs.
#
# 3% is above the noise that moves a fixed price without anyone changing it:
# month-to-month FX drift on a subscription billed abroad, and any plausible
# sales-tax revision. It is well below a real price rise, which is almost never
# under 5% because rounding to a marketable figure ($15.49 -> $16.49) makes it
# larger. Deliberately not 5%: rent going up 4% is $80 a month and matters.
MIN_CHANGE_PCT = 0.03

# ...and a dollar in absolute terms, because a percentage floor alone lets
# through changes nobody would call a price change: a $2.99 app rising 12 cents
# clears 3% but is tax rounding, not news. Expressed in minor units of the
# transactions' own currency.
MIN_CHANGE_MINOR = 100

# How many times larger the step must be than the wobble inside the two levels.
# This is the single number that separates Netflix from an electricity bill.
# Measured on real-shaped data, a variable utility bill scores under 2 at its
# best available split, a mobile plan changing tier scores around 6, and a
# fixed-price subscription scores in the hundreds because its wobble is zero.
# 3.0 sits in the empty space between the first two, and errs toward silence:
# a missed price rise is a disappointment, a false one is a reason to stop
# trusting the app.
MIN_SEPARATION = 3.0

MIN_CONFIDENCE = 0.5


@dataclass(slots=True)
class PriceChange:
    """One detected step in what a merchant charges.

    Amounts keep the sign of the transactions they came from, so `old_amount`
    and `new_amount` are negative for a subscription. `annual_impact` follows
    the same rule: negative means the change sends more money out of the
    account each year. Use `direction` — which compares magnitudes — to say
    whether the price went up or down.
    """

    merchant: str
    account_id: str
    cadence: str  # as RecurringSeries, plus "irregular" when no cadence fits
    old_amount: Money
    new_amount: Money
    changed_on: date  # date of the first charge at the new price
    transactions_before: int
    transactions_after: int
    annual_impact: Money
    confidence: float  # 0.0-1.0, how cleanly the step stands out from the noise

    @property
    def direction(self) -> str:
        """ "increase" when the amount grew in magnitude, else "decrease".

        Magnitude rather than signed value, so that paying more on an outflow
        and being paid more on an inflow are both increases.
        """
        return "increase" if abs(self.new_amount.minor) > abs(self.old_amount.minor) else "decrease"

    @property
    def change_pct(self) -> float:
        """Size of the step relative to the old price, as a fraction."""
        old = abs(self.old_amount.minor)
        if old == 0:
            return 0.0
        return (abs(self.new_amount.minor) - old) / old


def _level(minors: list[int]) -> int:
    """The representative amount of a run of charges.

    Prefers the most frequently billed figure over the median, because the
    price we show the user should be one they can find on a statement: the
    median of an even-length run averages its two middle values and can invent
    a price ($15.495) that was never charged.

    That preference only holds when the mode is *unique*, though. A seasonal
    bill repeats several of its amounts equally often, and picking one of them
    would hand back whichever tied value the tiebreak happened to favour —
    which, if it favoured the extreme, would make a heating bill look like a
    step down from its January peak. With no clear mode, the median is the
    honest summary of a run that has no single price.
    """
    ranked = Counter(minors).most_common()
    if ranked[0][1] > 1 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        return ranked[0][0]
    return int(statistics.median(minors))


def _spread(minors: list[int], centre: int) -> float:
    """Mean absolute distance from the run's own level.

    Absolute rather than squared deviation: one prorated month should widen the
    estimate of "how much this bill wobbles" a little, not dominate it.
    """
    return statistics.fmean([abs(m - centre) for m in minors])


def _segment_cost(minors: list[int]) -> float:
    """Within-segment squared deviation, the cost function for choosing a split."""
    if len(minors) < 2:
        return 0.0
    mean = statistics.fmean(minors)
    return sum((m - mean) ** 2 for m in minors)


def _best_split(minors: list[int], min_run: int) -> int | None:
    """Index of the division that best explains the run as two flat levels.

    Squared deviation here, unlike everywhere else in this module, because it
    is what makes a boundary crisp: it charges heavily for leaving a value on
    the wrong side of the split, so the minimum lands on the real step rather
    than drifting toward the middle of the run.
    """
    if len(minors) < 2 * min_run:
        return None
    best_k, best_cost = None, None
    for k in range(min_run, len(minors) - min_run + 1):
        cost = _segment_cost(minors[:k]) + _segment_cost(minors[k:])
        if best_cost is None or cost < best_cost:
            best_k, best_cost = k, cost
    return best_k


def _annualise(delta: Money, cadence: str, median_gap: float) -> Money:
    """Scale a per-charge difference to a per-year one.

    The reason this module needs a cadence at all: the same $2.50 is $30 a year
    monthly and $130 a year weekly.
    """
    periods = PERIODS_PER_YEAR.get(cadence)
    if periods is not None:
        return delta * periods
    # No cadence matched, so bill frequency is measured rather than assumed.
    # Less tidy than "monthly", but an honest rate beats a guessed one.
    if median_gap <= 0:
        return Money.zero(delta.currency)
    return delta * Decimal(str(round(DAYS_PER_YEAR / median_gap, 4)))


def _cadence_of(dates: list[date]) -> tuple[str, float]:
    """Name the billing rhythm of a run, and give the median gap in days."""
    gaps = [
        float((b - a).days)
        # Ragged by design: pairing each date with its successor.
        for a, b in zip(dates, dates[1:], strict=False)
        if (b - a).days > 0
    ]
    if not gaps:
        return "irregular", 0.0
    median_gap = statistics.median(gaps)
    match = _classify_gaps(gaps)
    return (match[0] if match else "irregular"), median_gap


@dataclass(slots=True, frozen=True)
class _Thresholds:
    """The knobs `find_price_changes` exposes, carried through the recursion."""

    min_run: int
    min_change_pct: float
    min_change_minor: int
    min_separation: float
    min_confidence: float


def _score_step(before: list[int], after: list[int], limits: _Thresholds) -> tuple[int, int, float]:
    """Judge one division of a run: `(old_level, new_level, separation)`.

    A separation of 0.0 means "not a price change" — either the step is too
    small to bother reporting, or it is not large enough next to the variation
    *inside* the two levels to be distinguishable from that variation. The
    second test is the one that separates Netflix from an electricity bill.
    """
    old_level, new_level = _level(before), _level(after)
    delta = new_level - old_level
    if abs(delta) < limits.min_change_minor:
        return old_level, new_level, 0.0
    if abs(old_level) == 0 or abs(delta) / abs(old_level) < limits.min_change_pct:
        return old_level, new_level, 0.0

    before_spread, after_spread = _spread(before, old_level), _spread(after, new_level)
    if (len(before) < MIN_VARYING_RUN and before_spread > 0) or (
        len(after) < MIN_VARYING_RUN and after_spread > 0
    ):
        return old_level, new_level, 0.0

    # The worse of the two sides, not their average: averaging lets a short run
    # that happens to be tight hide a wildly noisy one on the other side of the
    # boundary, which is how a seasonal electricity bill passed for a 127% rise.
    # A price change has to stand clear of the worst variation around it.
    #
    # The one-cent floor keeps a perfectly fixed price (spread of exactly 0)
    # from dividing by zero; a real subscription then scores in the hundreds,
    # which is the intent.
    spread = max(before_spread, after_spread, 1.0)
    separation = abs(delta) / spread
    if separation < limits.min_separation:
        return old_level, new_level, 0.0
    return old_level, new_level, separation


def _boundaries(minors: list[int], offset: int, limits: _Thresholds) -> list[int]:
    """Indices where the run steps from one price level to the next.

    Recursion continues only into the halves of an *accepted* step: if the most
    convincing division of a run is not convincing, no division inside it will
    be either, and searching on would only chop a noisy bill into arbitrary
    pieces until one of them happened to cross the floors.
    """
    k = _best_split(minors, limits.min_run)
    if k is None:
        return []
    if _score_step(minors[:k], minors[k:], limits)[2] == 0.0:
        return []
    return (
        _boundaries(minors[:k], offset, limits)
        + [offset + k]
        + _boundaries(minors[k:], offset + k, limits)
    )


def _reverts(levels: list[int], i: int, old_level: int, new_level: int) -> bool:
    """True when the run merely oscillates between two prices at this step.

    The rule that kills seasonal bills, which are otherwise the worst false
    positive here: a heating bill spends three months near $160 and three near
    $45, and any boundary between those is a beautifully clean step by every
    other test in this module. What gives it away is that the old price comes
    back. Real prices do not: nobody's rent alternates.

    So a step is discarded if a *later* level sits nearer the old price than
    the new one, or if an *earlier* level was already nearer the new price.
    Both tests are directional on purpose — under a symmetric one, the second
    rise of a price that went up twice would look like a return to the first.

    The cost is that a genuine rise followed by a genuine fall back to the old
    figure — a promotional rate ending and later returning — reports neither.
    That is the right way round: an amount that visits a price twice is better
    described as a bill that varies than as a price change.
    """
    return any(
        abs(level - old_level) < abs(level - new_level)
        if j > i + 1
        else abs(level - new_level) < abs(level - old_level)
        for j, level in enumerate(levels)
        if j < i or j > i + 1
    )


def _find_steps(
    rows: list[tuple[date, int]], limits: _Thresholds
) -> list[tuple[int, int, int, int, date, float]]:
    """Split one run into price levels, returning `(old, new, n_before,
    n_after, changed_on, confidence)` for each step between them.

    Two passes, because a segment is only trustworthy once it is fully
    segmented. The first split of 29.99 x8, 34.99 x8, 39.99 x8 correctly finds
    the first boundary, but at that moment everything after it is a single
    "level" holding two prices, and reading a level off it gives the wrong
    figure. So boundaries are found first and the prices either side of each
    one are measured afterwards, against the neighbouring *final* segments.
    """
    minors = [minor for _, minor in rows]
    cuts = _boundaries(minors, 0, limits)
    if not cuts:
        return []

    edges = [0, *cuts, len(minors)]
    # Ragged by design: pairing each edge with the next one.
    levels = [_level(minors[a:b]) for a, b in zip(edges, edges[1:], strict=False)]
    steps = []
    for i, cut in enumerate(cuts):
        before = minors[edges[i] : cut]
        after = minors[cut : edges[i + 2]]
        old_level, new_level, separation = _score_step(before, after, limits)
        # Re-tested against the refined levels: a boundary that only looked
        # like a step because a later change inflated the segment beyond it is
        # dropped here.
        if separation == 0.0:
            continue
        if _reverts(levels, i, old_level, new_level):
            continue

        # Confidence weights separation over volume: a clean step seen twice on
        # each side is better evidence than a blurry one seen ten times.
        # Evidence uses the *smaller* side, since a long history before the
        # change says nothing about whether the new price stuck.
        clarity = min(separation / (2 * limits.min_separation), 1.0)
        evidence = min(min(len(before), len(after)) / 3.0, 1.0)
        confidence = round(0.6 * clarity + 0.4 * evidence, 3)
        if confidence < limits.min_confidence:
            continue
        steps.append((old_level, new_level, len(before), len(after), rows[cut][0], confidence))
    return steps


def _group_from_series(
    transactions: list[Transaction], series: list[RecurringSeries]
) -> dict[tuple[str, str, bool], tuple[str, list[Transaction]]]:
    """Group transactions by the recurring series they belong to.

    Keyed by (merchant, account, direction) rather than by individual series,
    because recurring.detect splits a merchant into fixed-amount clusters when
    the merchant as a whole does not score — which is exactly what a price
    change looks like from its point of view. Unioning the clusters back
    together is what lets us see the step it had to break apart.

    Restricting to merchants that recur is also the main defence against false
    positives: two coffees at different prices are not a price change.
    """
    by_id = {tx.id: tx for tx in transactions}
    grouped: dict[tuple[str, str, bool], tuple[str, list[Transaction]]] = {}
    for entry in sorted(series, key=lambda s: -s.confidence):
        outflow = entry.typical_amount.minor < 0
        key = (entry.merchant.upper(), entry.account_id, outflow)
        # The highest-confidence series for a merchant names the cadence; the
        # weaker clusters only contribute their transactions.
        cadence, txs = grouped.setdefault(key, (entry.cadence, []))
        seen = {tx.id for tx in txs}
        txs.extend(by_id[tid] for tid in entry.transaction_ids if tid in by_id and tid not in seen)
        grouped[key] = (cadence, txs)
    return grouped


def _group_by_merchant(
    transactions: list[Transaction], include_inflows: bool
) -> dict[tuple[str, str, bool], tuple[str, list[Transaction]]]:
    """Group merchants the way recurring.detect does, when no series is given.

    Same normalisation and the same split by direction, so that a refund is
    never read as a price cut of the charge it reverses.
    """
    groups: dict[tuple[str, str, bool], list[Transaction]] = defaultdict(list)
    for tx in transactions:
        if tx.is_transfer or tx.pending:
            continue
        if not include_inflows and not tx.is_outflow:
            continue
        key = tx.merchant.upper() if tx.merchant else normalise_merchant(tx.description)
        if not key:
            continue
        groups[(key, tx.account_id, tx.is_outflow)].append(tx)
    # Empty cadence: measured from the dates, since nothing has classified
    # these groups as recurring.
    return {key: ("", txs) for key, txs in groups.items()}


def find_price_changes(
    transactions: list[Transaction],
    *,
    series: list[RecurringSeries] | None = None,
    min_change_pct: float = MIN_CHANGE_PCT,
    min_change_minor: int = MIN_CHANGE_MINOR,
    min_separation: float = MIN_SEPARATION,
    min_run: int = MIN_RUN,
    min_confidence: float = MIN_CONFIDENCE,
    include_inflows: bool = False,
) -> list[PriceChange]:
    """Find price changes in a transaction list, most expensive first.

    Pass `series` from `recurring.detect` whenever you have it: it supplies the
    cadence that `annual_impact` depends on, and it confines the search to
    charges that actually repeat. Without it, merchants are grouped the same
    way `recurring.detect` groups them and the cadence is measured from the
    charge dates — usable, but noisier, since nothing has vouched for the group
    being a recurring charge in the first place.

    `include_inflows` only applies to that fallback grouping; when `series` is
    supplied, direction comes from the series, so pass
    `detect(..., include_inflows=True)` to catch a change in income.

    Results are ordered by annual impact, because a $0.50/month rise on a
    subscription is true but not worth reading before a $40/month one.
    """
    limits = _Thresholds(
        min_run=min_run,
        min_change_pct=min_change_pct,
        min_change_minor=min_change_minor,
        min_separation=min_separation,
        min_confidence=min_confidence,
    )
    grouped = (
        _group_from_series(transactions, series)
        if series is not None
        else _group_by_merchant(transactions, include_inflows)
    )

    changes: list[PriceChange] = []
    for (merchant, account_id, _), (known_cadence, txs) in grouped.items():
        if len(txs) < 2 * min_run:
            continue
        currencies = {tx.amount.currency for tx in txs}
        # Mixing currencies would make the minor units incomparable, and a
        # merchant that switched billing currency has not changed its price.
        if len(currencies) != 1:
            continue
        currency = currencies.pop()

        txs = sorted(txs, key=lambda t: t.date)
        rows = [(tx.date, tx.amount.minor) for tx in txs]
        measured_cadence, median_gap = _cadence_of([tx.date for tx in txs])
        cadence = known_cadence or measured_cadence

        for old, new, n_before, n_after, changed_on, confidence in _find_steps(rows, limits):
            delta = Money(new - old, currency)
            changes.append(
                PriceChange(
                    merchant=merchant.title(),
                    account_id=account_id,
                    cadence=cadence,
                    old_amount=Money(old, currency),
                    new_amount=Money(new, currency),
                    changed_on=changed_on,
                    transactions_before=n_before,
                    transactions_after=n_after,
                    annual_impact=_annualise(delta, cadence, median_gap),
                    confidence=confidence,
                )
            )

    changes.sort(key=lambda c: (-abs(c.annual_impact.minor), -c.confidence, c.changed_on))
    return changes


def summarise(changes: list[PriceChange]) -> str:
    """One line per change, for the CLI and for logs.

    >>> change = PriceChange(
    ...     merchant="Netflix",
    ...     account_id="acct1",
    ...     cadence="monthly",
    ...     old_amount=Money(-1549),
    ...     new_amount=Money(-1799),
    ...     changed_on=date(2026, 3, 14),
    ...     transactions_before=3,
    ...     transactions_after=3,
    ...     annual_impact=Money(-3000),
    ...     confidence=0.95,
    ... )
    >>> print(summarise([change]))
    Netflix: $15.49 -> $17.99 monthly from 2026-03-14 (+$30.00/yr)
    """
    if not changes:
        return "No price changes detected."
    lines = []
    for c in changes:
        # Magnitudes throughout, with the arrow and the sign carrying the
        # story: "-$15.49 -> -$17.99 (-$30.00/yr)" reads as a saving to
        # everyone who has not memorised the sign convention.
        sign = "+" if c.direction == "increase" else "-"
        lines.append(
            f"{c.merchant}: {abs(c.old_amount)} -> {abs(c.new_amount)} {c.cadence} "
            f"from {c.changed_on.isoformat()} ({sign}{abs(c.annual_impact)}/yr)"
        )
    return "\n".join(lines)
