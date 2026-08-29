"""Turn a net-worth goal into a spending budget you could actually follow.

Every other module here answers "where did the money go". This one answers the
question that comes next: *"what would I have to do differently?"* The user
states a target — "+$5,000 over six months" — and the engine works **backwards**
from it to a per-category allowance for each week or month.

The whole design rests on one observation: **not all spending is cuttable.**

    income  -  committed  -  saving target  =  what is left to spend

Rent, insurance and the electricity bill are already spoken for. A budget that
says "spend less on rent" is not a budget, it is a wish, so committed spending
comes off the top and is never proposed as a saving. What remains is
discretionary, and that is the only place a cut can honestly come from.

Which recurring charges count as committed is not this module's judgement to
make: `analysis.subscriptions` already draws that line (a *bill* or a
*subscription* is a commitment, a *habit* is not), and the user's own stored
verdict overrules the catalog. Pass `series` from `recurring.detect` and the
distinction comes for free; omit it and every penny is treated as cuttable,
which is a worse budget but never a wrong one.

Three more decisions worth stating up front:

* **Baselines are medians, not means.** One December, one wedding or one flight
  would otherwise permanently inflate the allowance it lands in, and a budget
  built from a holiday is a budget nobody can hit.
* **Cuts are allocated proportionally** to what each category currently costs.
  Asking someone to find $50 in a $600 grocery bill and $50 in a $60 coffee
  habit are wildly different requests; only one of them is arithmetic.
* **An impossible goal is reported, never rendered.** If the target cannot be
  met even at zero discretionary spending, `feasible` is False and the
  explanation names the shortfall. Silently emitting a budget with a negative —
  or merely unachievable — allowance is worse than admitting the goal does not
  fit, because the user would follow it perfectly and still miss.

Money stays an integer count of cents throughout, and every division of money
across categories uses largest-remainder allocation so the parts sum to exactly
the whole. See `allocate`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from ..core.models import RecurringSeries, Transaction
from ..core.money import Money
from . import subscriptions
from .categorize import INCOME, TRANSFER, UNCATEGORIZED, categorize

Period = Literal["weekly", "monthly"]

PERIODS_PER_YEAR: dict[str, int] = {"weekly": 52, "monthly": 12}

# How much history a baseline is taken over. Six months is enough for a median
# to shrug off one unusual month; thirteen weeks is the same span in weeks,
# because six weeks of history is too thin for a median to mean anything.
DEFAULT_LOOKBACK: dict[str, int] = {"weekly": 13, "monthly": 6}

# The kinds of recurring charge that are commitments rather than choices.
# A "habit" — the weekly corner shop — repeats just as reliably and is exactly
# the kind of spending a budget is allowed to ask about.
COMMITTED_KINDS = frozenset({subscriptions.BILL, subscriptions.SUBSCRIPTION})

# Categories that are not spending: income is the other side of the equation,
# and a transfer is money the user still has.
NON_SPENDING = frozenset({INCOME, TRANSFER})


# -- period arithmetic ---------------------------------------------------


def start_of_period(day: date, period: Period) -> date:
    """The first day of the week (Monday) or month containing `day`.

    >>> start_of_period(date(2026, 5, 14), "monthly")
    datetime.date(2026, 5, 1)
    >>> start_of_period(date(2026, 5, 14), "weekly")
    datetime.date(2026, 5, 11)
    """
    if period == "weekly":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def end_of_period(day: date, period: Period) -> date:
    """The first day of the *next* period — an exclusive end, so ranges compose."""
    start = start_of_period(day, period)
    if period == "weekly":
        return start + timedelta(days=7)
    if start.month == 12:
        return date(start.year + 1, 1, 1)
    return date(start.year, start.month + 1, 1)


def periods_between(start: date, end: date, period: Period) -> int:
    """How many whole periods separate the period containing each date.

    >>> periods_between(date(2026, 8, 29), date(2027, 2, 1), "monthly")
    6
    """
    a, b = start_of_period(start, period), start_of_period(end, period)
    if period == "weekly":
        return (b - a).days // 7
    return (b.year - a.year) * 12 + (b.month - a.month)


# -- exact division of money ---------------------------------------------


def allocate(amount: Money, weights: Sequence[int]) -> list[Money]:
    """Split `amount` in proportion to `weights`, losing and inventing nothing.

    >>> [m.minor for m in allocate(Money(100), [1, 1, 1])]
    [34, 33, 33]
    >>> [m.minor for m in allocate(Money(1000), [600, 60])]
    [909, 91]

    Largest remainder (Hamilton's method): floor every share, then hand the
    leftover cents to whoever was rounded down hardest. The parts sum to
    exactly `amount` by construction, which naive per-category rounding does
    not — and a budget whose lines do not add up to its own total is one a user
    will stop believing after the first time they check it with a calculator.

    Ties go to the larger weight, so the odd cent lands on the bigger line
    where it is proportionally least visible. Weights that are all zero split
    the amount as evenly as possible instead.
    """
    if not weights:
        return []
    if any(w < 0 for w in weights):
        raise ValueError("allocate() weights must be non-negative")

    sign = -1 if amount.minor < 0 else 1
    magnitude = abs(amount.minor)
    total_weight = sum(weights)
    if total_weight == 0:
        weights = [1] * len(weights)
        total_weight = len(weights)

    shares: list[int] = []
    remainders: list[tuple[int, int]] = []
    for weight in weights:
        share, remainder = divmod(magnitude * weight, total_weight)
        shares.append(share)
        remainders.append((remainder, weight))

    leftover = magnitude - sum(shares)
    order = sorted(range(len(shares)), key=lambda i: (-remainders[i][0], -remainders[i][1], i))
    for i in order[:leftover]:
        shares[i] += 1
    return [Money(sign * s, amount.currency) for s in shares]


def _median_minor(values: Sequence[int]) -> int:
    """Median of a list of cent counts, in cents.

    `statistics.median` returns a float for an even-length list, and a float is
    the one thing money is never allowed to become.
    """
    if not values:
        return 0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    pair = Decimal(ordered[mid - 1] + ordered[mid]) / 2
    return int(pair.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


# -- the goal ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Goal:
    """What the user is trying to achieve, and by when.

    `target` is the net-worth *change* being aimed at, as a positive amount:
    `Money.parse("5000")` means "end up $5,000 better off". `horizon` is either
    a deadline date or a plain count of periods, because both are natural ways
    to say it and neither is more correct than the other.
    """

    target: Money
    horizon: date | int
    period: Period = "monthly"

    def periods(self, asof: date | None = None) -> int:
        """How many periods the goal has to run.

        >>> Goal(Money(500000), date(2027, 2, 1)).periods(date(2026, 8, 29))
        6

        A deadline is counted from the period containing `asof`, so a goal set
        on the 29th still has the whole of next month to work with.
        """
        count = (
            self.horizon
            if isinstance(self.horizon, int)
            else periods_between(asof or date.today(), self.horizon, self.period)
        )
        if count < 1:
            raise ValueError(
                f"A {self.period} goal needs at least one period to run; got {count}. "
                f"A deadline that has already passed cannot be planned for."
            )
        return count


# -- the plan ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CategoryBudget:
    """One line of the budget: what this category costs, and what it may cost.

    All three figures are magnitudes rather than signed amounts — a Dining
    budget reads as $400, not -$400 — even though the transactions behind them
    are negative under the usual sign convention.

    `committed` is the part of the baseline that is a fixed commitment and is
    therefore never cut; `allowance` can never fall below it.
    """

    category: str
    baseline: Money
    committed: Money
    allowance: Money

    @property
    def discretionary(self) -> Money:
        """The part of the baseline a budget is allowed to touch."""
        return self.baseline - self.committed

    @property
    def cut(self) -> Money:
        """How much less this category may cost per period than it does today."""
        return self.baseline - self.allowance

    @property
    def cut_fraction(self) -> Decimal:
        """The cut as a fraction of the current baseline, for "down 18%" in a UI."""
        if not self.baseline:
            return Decimal(0)
        return Decimal(self.cut.minor) / Decimal(self.baseline.minor)

    @property
    def is_fixed(self) -> bool:
        """True when the whole line is committed, so the user has no lever here."""
        return bool(self.baseline) and self.discretionary.minor <= 0


@dataclass(frozen=True, slots=True)
class BudgetPlan:
    """A budget derived from a goal: follow it and you hit the target.

    ...unless `feasible` is False, in which case there is no such budget and
    `shortfall` says by how much per period the goal misses. The allowances are
    still populated in that case — at zero discretionary spending, the best
    that can be done — so a UI can show the user exactly how close the most
    extreme possible budget gets.
    """

    goal: Goal
    period: Period
    periods: int
    income: Money  # median net inflow per period
    committed: Money  # fixed commitments per period, as a magnitude
    baseline: Money  # total typical spending per period, as a magnitude
    required: Money  # net saving needed each period to hit the target
    categories: list[CategoryBudget]
    feasible: bool
    shortfall: Money  # per period; zero when feasible
    slack: Money  # per period spare when feasible; zero when not
    explanation: str
    periods_observed: int  # how many complete periods the baselines came from

    @property
    def allowance(self) -> Money:
        """Total permitted spending per period."""
        return _sum([c.allowance for c in self.categories], self.income.currency)

    @property
    def cut(self) -> Money:
        """Total spending reduction the plan asks for, per period."""
        return self.baseline - self.allowance

    @property
    def projected(self) -> Money:
        """What following this plan would actually save over the whole horizon."""
        return (self.income - self.allowance) * self.periods

    def for_category(self, category: str) -> CategoryBudget | None:
        for line in self.categories:
            if line.category == category:
                return line
        return None


def plan(
    goal: Goal,
    transactions: Sequence[Transaction],
    *,
    series: Sequence[RecurringSeries] | None = None,
    period: Period | None = None,
    verdicts: Mapping[str, str] | None = None,
    asof: date | None = None,
    lookback: int | None = None,
) -> BudgetPlan:
    """Work backwards from `goal` to a per-category allowance for each period.

    `series` comes from `recurring.detect`. It is optional, but without it the
    engine has no way to know that the rent is not negotiable and will happily
    propose cutting it — so pass it whenever it is available.

    `verdicts` is the user's own answers about what a merchant is (see
    `core.db.get_verdicts`), and overrules the built-in catalog exactly as it
    does everywhere else.

    `asof` defaults to the newest transaction rather than to today's date, so
    the answer depends only on the data handed in. `period` overrides
    `goal.period`, which is only useful for showing the same goal as weekly and
    monthly budgets side by side.
    """
    period = period or goal.period
    if period not in PERIODS_PER_YEAR:
        raise ValueError(f"period must be 'weekly' or 'monthly', got {period!r}")

    dates = [tx.date for tx in transactions]
    asof = asof or (max(dates) if dates else date.today())
    horizon = goal.periods(asof)
    currency = goal.target.currency
    lookback = lookback or DEFAULT_LOOKBACK[period]

    window = _window(dates, period, asof, lookback)
    committed_ids, committed_by_category = _commitments(transactions, series, verdicts, period)
    income, spend_by_category = _baselines(transactions, window, period, committed_ids)

    # Every category the user spends in, plus any that exist only as a
    # commitment — an annual insurance premium may not have landed inside the
    # lookback window at all, and leaving it out would overstate what is free.
    names = set(spend_by_category) | set(committed_by_category)
    lines: list[tuple[str, int, int]] = []  # (category, committed, discretionary)
    for name in names:
        committed_minor = committed_by_category.get(name, 0)
        # Clamp: a category whose refunds outweigh its purchases in a typical
        # period is not a source of savings, it is noise.
        discretionary = max(spend_by_category.get(name, 0), 0)
        if committed_minor or discretionary:
            lines.append((name, committed_minor, discretionary))

    committed_total = sum(c for _, c, _ in lines)
    discretionary_total = sum(d for _, _, d in lines)

    # Ceiling, not banker's rounding: rounding the per-period saving down by a
    # cent means missing the target by `horizon` cents, having followed the
    # budget exactly. The rounding should never be the reason a goal fails.
    required = -(-goal.target.minor // horizon)

    available = income - committed_total - required
    needed_cut = discretionary_total - available

    feasible = needed_cut <= discretionary_total
    shortfall = 0 if feasible else needed_cut - discretionary_total
    slack = max(-needed_cut, 0) if feasible else 0

    if needed_cut <= 0:
        # Already on course. Left at the baseline deliberately rather than
        # inflated to soak up the surplus: the user asked what they must do,
        # and the honest answer is "carry on".
        cuts = [0] * len(lines)
    elif feasible:
        weights = [d for _, _, d in lines]
        cuts = [m.minor for m in allocate(Money(needed_cut, currency), weights)]
    else:
        cuts = [d for _, _, d in lines]  # everything cuttable, and still not enough

    categories = [
        CategoryBudget(
            category=name,
            baseline=Money(committed_minor + discretionary, currency),
            committed=Money(committed_minor, currency),
            allowance=Money(committed_minor + discretionary - cut, currency),
        )
        for (name, committed_minor, discretionary), cut in zip(lines, cuts, strict=True)
    ]
    categories.sort(key=lambda c: (-c.baseline.minor, c.category))

    return BudgetPlan(
        goal=goal,
        period=period,
        periods=horizon,
        income=Money(income, currency),
        committed=Money(committed_total, currency),
        baseline=Money(committed_total + discretionary_total, currency),
        required=Money(required, currency),
        categories=categories,
        feasible=feasible,
        shortfall=Money(shortfall, currency),
        slack=Money(slack, currency),
        explanation=_explain(
            period=period,
            periods=horizon,
            income=Money(income, currency),
            committed=Money(committed_total, currency),
            discretionary=Money(discretionary_total, currency),
            required=Money(required, currency),
            cut=Money(max(needed_cut, 0), currency),
            shortfall=Money(shortfall, currency),
            slack=Money(slack, currency),
            feasible=feasible,
            observed=len(window),
        ),
        periods_observed=len(window),
    )


# -- how the baselines are built -----------------------------------------


def _sum(amounts: list[Money], currency: str) -> Money:
    result = Money.zero(currency)
    for amount in amounts:
        result = result + amount
    return result


def _category_of(transaction: Transaction) -> str:
    """The category to budget this row under.

    A category the user (or an importer) has already set wins; the rules are
    only consulted for rows nobody has placed yet.
    """
    return transaction.category or categorize(transaction)


def _window(dates: Sequence[date], period: Period, asof: date, lookback: int) -> list[date]:
    """The complete periods a baseline is measured over, oldest first.

    The period containing `asof` is deliberately excluded. It is almost always
    partial, and a half-finished month dragged into a median makes every
    baseline — and therefore every allowance — quietly too small.
    """
    if not dates:
        return []
    first = start_of_period(min(dates), period)
    cursor = start_of_period(asof, period)
    window: list[date] = []
    for _ in range(lookback):
        cursor = start_of_period(cursor - timedelta(days=1), period)
        if cursor < first:
            break
        window.append(cursor)
    window.reverse()
    return window


def _per_period_cost(series: RecurringSeries, period: Period) -> int:
    """What a recurring series costs per budgeting period, in cents.

    Amortised from the annual figure, so a quarterly insurance premium shows up
    as a third of itself every month. That is the number a budget needs: the
    money has to be set aside whether or not the bill lands this month.
    """
    annual = series.annualised  # already a magnitude
    if not annual:
        return 0
    return (annual * (Decimal(1) / Decimal(PERIODS_PER_YEAR[period]))).minor


def _commitments(
    transactions: Sequence[Transaction],
    series: Sequence[RecurringSeries] | None,
    verdicts: Mapping[str, str] | None,
    period: Period,
) -> tuple[set[str], dict[str, int]]:
    """Split out the spending that is already spoken for.

    Returns the transaction ids that belong to a commitment — so they can be
    kept out of the discretionary baseline rather than counted twice — and the
    per-period cost of those commitments by category.
    """
    committed_ids: set[str] = set()
    by_category: dict[str, int] = defaultdict(int)
    if not series:
        return committed_ids, dict(by_category)

    by_id = {tx.id: tx for tx in transactions}
    answers = dict(verdicts) if verdicts else None
    for candidate in series:
        # Inflows are not commitments; a salary is not a bill.
        if candidate.typical_amount.minor >= 0:
            continue
        if subscriptions.resolve(candidate.merchant, answers) not in COMMITTED_KINDS:
            continue
        cost = _per_period_cost(candidate, period)
        if cost <= 0:
            continue
        committed_ids.update(candidate.transaction_ids)
        by_category[_series_category(candidate, by_id)] += cost
    return committed_ids, dict(by_category)


def _series_category(series: RecurringSeries, by_id: Mapping[str, Transaction]) -> str:
    """Which budget line a commitment belongs on.

    Taken from the charges themselves by majority vote rather than from the
    merchant name, so a mis-normalised name cannot move rent out of Housing.
    """
    votes = Counter(
        _category_of(by_id[tx_id])
        for tx_id in series.transaction_ids
        if tx_id in by_id and _category_of(by_id[tx_id]) not in NON_SPENDING
    )
    return votes.most_common(1)[0][0] if votes else UNCATEGORIZED


def _baselines(
    transactions: Sequence[Transaction],
    window: Sequence[date],
    period: Period,
    committed_ids: set[str],
) -> tuple[int, dict[str, int]]:
    """Median per-period income, and median per-period discretionary spend by category.

    Periods in which a category saw no spending count as zero rather than being
    skipped. Otherwise a category touched once in six months would show a
    baseline of the whole charge every month, and the budget would hand out an
    allowance for a thing the user hardly ever buys.
    """
    if not window:
        return 0, {}

    live = set(window)
    income: dict[date, int] = dict.fromkeys(window, 0)
    spend: dict[str, dict[date, int]] = defaultdict(lambda: dict.fromkeys(window, 0))

    for tx in transactions:
        bucket = start_of_period(tx.date, period)
        if bucket not in live:
            continue
        category = _category_of(tx)
        if category == TRANSFER or tx.is_transfer:
            continue
        if category == INCOME:
            income[bucket] += tx.amount.minor
            continue
        if tx.id in committed_ids:
            continue
        # Stored as a magnitude, so a refund correctly reduces the category.
        spend[category][bucket] -= tx.amount.minor

    return (
        _median_minor(list(income.values())),
        {name: _median_minor(list(periods.values())) for name, periods in spend.items()},
    )


def _explain(
    *,
    period: Period,
    periods: int,
    income: Money,
    committed: Money,
    discretionary: Money,
    required: Money,
    cut: Money,
    shortfall: Money,
    slack: Money,
    feasible: bool,
    observed: int,
) -> str:
    """A sentence a person can argue with, which is the point of showing one."""
    each = "week" if period == "weekly" else "month"
    span = f"{periods} {each}{'s' if periods != 1 else ''}"
    ledger = (
        f"Income of {income.format()} a {each} less {committed.format()} of committed bills "
        f"leaves {(income - committed).format()}; the goal needs {required.format()} of that "
        f"put aside each {each}"
    )

    if not observed:
        return (
            f"No complete {each} of history to measure against, so there is no baseline to "
            f"budget from. Import at least one full {each} of transactions and try again. "
            f"The goal would need {required.format()} saved every {each} for {span}."
        )
    if not feasible:
        return (
            f"Not reachable. {ledger}, but only {discretionary.format()} of spending is "
            f"discretionary. Even at zero discretionary spending the goal falls "
            f"{shortfall.format()} short every {each} — {(shortfall * periods).format()} over "
            f"{span}. Extend the deadline, lower the target, or find "
            f"{shortfall.format()} a {each} of income."
        )
    if not cut:
        return (
            f"Already on course. {ledger}, and current spending leaves {slack.format()} a "
            f"{each} to spare. Keep spending as you are for {span}."
        )
    share = (Decimal(cut.minor) * 100 / Decimal(discretionary.minor)).quantize(Decimal("1"))
    return (
        f"Reachable. {ledger}, so spending has to come down by {cut.format()} a {each} — "
        f"{share}% of the {discretionary.format()} that is discretionary, spread across "
        f"categories in proportion to what each costs today. Committed bills of "
        f"{committed.format()} are left untouched."
    )


# -- mid-period tracking -------------------------------------------------


@dataclass(frozen=True, slots=True)
class CategoryProgress:
    """How one budget line is doing part way through a period."""

    category: str
    spent: Money
    committed: Money
    allowance: Money
    pace: Money  # what "on schedule" looks like today

    @property
    def remaining(self) -> Money:
        """What is left of the allowance. Negative once the line is overspent."""
        return self.allowance - self.spent

    @property
    def counted(self) -> Money:
        """Spending charged against the pace, treating commitments as already gone.

        A bill that has not left the account yet is not spare money. Counting
        only what has actually cleared would let three unpaid weeks of rent
        excuse a blown grocery budget, and the user would find that out on the
        1st.
        """
        return Money(max(self.spent.minor, self.committed.minor), self.spent.currency)

    @property
    def over(self) -> bool:
        """True when the whole period's allowance is already gone."""
        return self.spent.minor > self.allowance.minor

    @property
    def on_track(self) -> bool:
        return self.counted.minor <= self.pace.minor

    @property
    def fraction_used(self) -> Decimal:
        """Spent as a fraction of allowance, for a progress bar. >1 means over."""
        if not self.allowance:
            return Decimal(1) if self.spent else Decimal(0)
        return Decimal(self.spent.minor) / Decimal(self.allowance.minor)


@dataclass(frozen=True, slots=True)
class Progress:
    """A snapshot of a budget part way through the period it applies to."""

    period: Period
    period_start: date
    period_end: date  # exclusive
    asof: date
    elapsed_days: int
    period_days: int
    spent: Money
    counted: Money  # spent, with unpaid commitments treated as already gone
    allowance: Money
    pace: Money
    categories: list[CategoryProgress]
    on_track: bool

    @property
    def remaining(self) -> Money:
        return self.allowance - self.spent

    @property
    def over_budget(self) -> list[CategoryProgress]:
        """The lines to put in front of the user, worst overspend first."""
        return sorted(
            (c for c in self.categories if not c.on_track),
            key=lambda c: (c.counted - c.pace).minor,
            reverse=True,
        )


def progress(
    plan: BudgetPlan,
    transactions: Sequence[Transaction],
    period_start: date,
    *,
    asof: date | None = None,
) -> Progress:
    """Compare spending so far in one period against the plan's allowances.

    "On track" is measured against a *pace*, not against the whole allowance:
    two days into the month a user has spent almost nothing and is not yet
    virtuous, and on the last day they have spent nearly all of it and are not
    yet a failure.

    Committed spending is exempt from that pro-rating, in both directions.
    Rent leaves on the 1st, so pacing it would report every user as
    catastrophically over budget on the 2nd of the month; and a bill that has
    not gone out yet is not slack that excuses overspending somewhere else, so
    a commitment counts as spent from the start of the period either way. See
    `CategoryProgress.counted`.

    Spending in a category the plan has no line for is counted with an
    allowance of zero. It is real money and it is unbudgeted, and hiding it
    would make the totals disagree with the user's own bank statement.
    """
    start = start_of_period(period_start, plan.period)
    end = end_of_period(start, plan.period)
    period_days = (end - start).days
    today = asof or date.today()
    # Clamp, so asking about a period that has already finished reports it
    # complete rather than in some impossible future state.
    today = min(max(today, start), end - timedelta(days=1))
    elapsed = (today - start).days + 1

    currency = plan.income.currency
    spent: dict[str, int] = defaultdict(int)
    for tx in transactions:
        if not (start <= tx.date < end):
            continue
        category = _category_of(tx)
        if category in NON_SPENDING or tx.is_transfer:
            continue
        spent[category] -= tx.amount.minor

    fraction = Decimal(elapsed) / Decimal(period_days)
    lines: list[CategoryProgress] = []
    for budget in plan.categories:
        # Only the discretionary half is paced; see the docstring.
        pace = budget.committed + budget.discretionary * fraction
        lines.append(
            CategoryProgress(
                category=budget.category,
                spent=Money(max(spent.pop(budget.category, 0), 0), currency),
                committed=budget.committed,
                allowance=budget.allowance,
                pace=pace,
            )
        )
    for category, minor in spent.items():
        if minor <= 0:
            continue
        lines.append(
            CategoryProgress(
                category=category,
                spent=Money(minor, currency),
                committed=Money.zero(currency),
                allowance=Money.zero(currency),
                pace=Money.zero(currency),
            )
        )
    lines.sort(key=lambda c: (-c.spent.minor, c.category))

    total_counted = _sum([c.counted for c in lines], currency)
    total_pace = _sum([c.pace for c in lines], currency)
    return Progress(
        period=plan.period,
        period_start=start,
        period_end=end,
        asof=today,
        elapsed_days=elapsed,
        period_days=period_days,
        spent=_sum([c.spent for c in lines], currency),
        counted=total_counted,
        allowance=_sum([c.allowance for c in lines], currency),
        pace=total_pace,
        categories=lines,
        on_track=total_counted.minor <= total_pace.minor,
    )
