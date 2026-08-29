"""Tests for goal-driven budgeting."""

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from carraway.analysis.budget import (
    Goal,
    allocate,
    end_of_period,
    plan,
    progress,
    start_of_period,
)
from carraway.core.models import RecurringSeries, Transaction
from carraway.core.money import Money

# Six complete months of history, and an "as of" inside the seventh. The
# current period is deliberately partial: the engine must not measure a
# baseline against it.
MONTHS = [date(2026, m, 1) for m in range(1, 7)]
ASOF = date(2026, 7, 15)

MONDAY = start_of_period(date(2026, 4, 6), "weekly")
WEEKS = [MONDAY + timedelta(days=7 * i) for i in range(13)]
WEEKLY_ASOF = WEEKS[-1] + timedelta(days=10)


def make_tx(
    day: date,
    amount: str,
    description: str,
    *,
    tx_id: str | None = None,
    category: str = "",
) -> Transaction:
    return Transaction(
        id=tx_id or f"{description}-{day.isoformat()}",
        account_id="acct1",
        date=day,
        amount=Money.parse(amount),
        description=description,
        category=category,
    )


def history(
    *,
    groceries: str = "-600.00",
    dining: str = "-300.00",
    shopping: str = "-200.00",
    income: str = "5000.00",
    months: list[date] | None = None,
) -> list[Transaction]:
    """A tidy synthetic year: $5,000 in, $3,065.99 out, every month the same.

    Categories are left unset on purpose, so the rules in `categorize` are
    exercised the way they would be on a fresh import.
    """
    months = MONTHS if months is None else months
    out: list[Transaction] = []
    for month in months:
        tag = month.isoformat()
        out += [
            make_tx(month, income, "ACME CORP PAYROLL DIRECT DEP"),
            make_tx(month, "-1800.00", "GREENFIELD APARTMENTS LEASING", tx_id=f"rent-{tag}"),
            make_tx(month.replace(day=5), "-150.00", "XCEL ENERGY BILLPAY", tx_id=f"power-{tag}"),
            make_tx(
                month.replace(day=8), "-15.99", "NETFLIX.COM 866-579-7172", tx_id=f"nflx-{tag}"
            ),
            make_tx(month.replace(day=10), groceries, "TRADER JOES #123 SAN JOSE CA"),
            make_tx(month.replace(day=12), dining, "CHIPOTLE 2481"),
            make_tx(month.replace(day=14), shopping, "TARGET 00023874 AUSTIN TX"),
        ]
    return out


def series_for(months: list[date] | None = None) -> list[RecurringSeries]:
    """The commitments hiding in `history`: rent, power and a subscription."""
    months = MONTHS if months is None else months
    specs = [
        ("Greenfield Apartments Leasing", "-1800.00", "rent"),
        ("Xcel Energy Billpay", "-150.00", "power"),
        ("Netflix", "-15.99", "nflx"),
    ]
    return [
        RecurringSeries(
            merchant=merchant,
            account_id="acct1",
            cadence="monthly",
            typical_amount=Money.parse(amount),
            occurrences=len(months),
            first_seen=months[0],
            last_seen=months[-1],
            next_expected=None,
            confidence=0.9,
            amount_varies=False,
            transaction_ids=[f"{prefix}-{m.isoformat()}" for m in months],
        )
        for merchant, amount, prefix in specs
    ]


# -- exact division ------------------------------------------------------


def test_allocation_never_loses_or_invents_a_cent():
    # The property that matters: whatever the split, the parts are the whole.
    rng = random.Random(20260829)
    for _ in range(400):
        amount = Money(rng.randint(0, 5_000_00))
        weights = [rng.randint(0, 900_00) for _ in range(rng.randint(1, 9))]
        parts = allocate(amount, weights)
        assert sum(p.minor for p in parts) == amount.minor
        assert all(p.minor >= 0 for p in parts)
        assert len(parts) == len(weights)


def test_allocation_is_proportional_not_equal():
    # $50 out of a $600 category and $50 out of a $60 one are different asks.
    big, small = allocate(Money(10_000), [600_00, 60_00])
    assert big.minor > small.minor
    assert big.minor == 9091 and small.minor == 909
    assert big.minor + small.minor == 10_000


def test_allocation_edge_cases():
    assert allocate(Money(100), []) == []
    # Nothing to weigh by: split as evenly as the cents allow, still exactly.
    even = allocate(Money(100), [0, 0, 0])
    assert [m.minor for m in even] == [34, 33, 33]
    # Signed amounts survive the round trip, which matters if a caller ever
    # allocates a shortfall rather than a cut.
    negative = allocate(Money(-100), [1, 1, 1])
    assert sum(m.minor for m in negative) == -100
    with pytest.raises(ValueError):
        allocate(Money(100), [1, -1])


def test_a_cut_allocated_across_categories_sums_to_the_cut_exactly():
    result = plan(Goal(Money.parse("15000"), 6), history(), series=series_for(), asof=ASOF)
    total_cut = sum(line.cut.minor for line in result.categories)
    assert total_cut == result.cut.minor
    # And the allowances themselves add up to the plan's own total.
    assert sum(line.allowance.minor for line in result.categories) == result.allowance.minor
    assert result.baseline.minor - total_cut == result.allowance.minor


# -- periods -------------------------------------------------------------


def test_period_bounds():
    assert start_of_period(date(2026, 5, 14), "monthly") == date(2026, 5, 1)
    assert end_of_period(date(2026, 12, 3), "monthly") == date(2027, 1, 1)
    assert start_of_period(date(2026, 5, 14), "weekly") == date(2026, 5, 11)
    assert end_of_period(date(2026, 5, 14), "weekly") == date(2026, 5, 18)


def test_a_goal_accepts_a_deadline_or_a_count_of_periods():
    assert Goal(Money.parse("5000"), 6).periods() == 6
    assert Goal(Money.parse("5000"), date(2027, 1, 1)).periods(ASOF) == 6
    assert Goal(Money.parse("5000"), date(2026, 10, 5), "weekly").periods(WEEKS[0]) == 26
    with pytest.raises(ValueError):
        Goal(Money.parse("5000"), date(2026, 1, 1)).periods(ASOF)


# -- the plan ------------------------------------------------------------


def test_a_reachable_goal_produces_allowances_that_hit_it():
    goal = Goal(Money.parse("15000"), 6)
    result = plan(goal, history(), series=series_for(), asof=ASOF)

    assert result.feasible
    assert result.periods_observed == 6
    assert result.income == Money.parse("5000")
    assert result.committed == Money.parse("1965.99")  # rent + power + Netflix
    assert result.baseline == Money.parse("3065.99")
    assert result.required == Money.parse("2500")

    # The point of the whole exercise: follow this and the target is met.
    assert (result.income - result.allowance).minor >= result.required.minor
    assert result.projected.minor >= goal.target.minor

    allowances = {line.category: line.allowance for line in result.categories}
    assert allowances["Groceries"] == Money.parse("291.28")
    assert allowances["Dining"] == Money.parse("145.64")
    assert allowances["Shopping"] == Money.parse("97.09")
    assert "Reachable" in result.explanation


def test_a_goal_already_being_met_asks_for_no_cut():
    # $1,934.01 a month is already being saved; a $9,000/6mo goal needs $1,500.
    result = plan(Goal(Money.parse("9000"), 6), history(), series=series_for(), asof=ASOF)
    assert result.feasible
    assert result.cut == Money.zero()
    assert result.slack == Money.parse("434.01")
    assert all(line.allowance == line.baseline for line in result.categories)
    assert "Already on course" in result.explanation


def test_an_unreachable_goal_is_reported_not_rendered():
    # $30,000 in 6 months needs $5,000 a month saved out of $5,000 of income.
    result = plan(Goal(Money.parse("30000"), 6), history(), series=series_for(), asof=ASOF)

    assert not result.feasible
    # Exactly the committed spending, which is what makes it impossible.
    assert result.shortfall == Money.parse("1965.99")
    assert "Not reachable" in result.explanation
    assert "1,965.99" in result.explanation

    # Never a negative allowance, however impossible the goal.
    assert all(line.allowance.minor >= 0 for line in result.categories)
    # The best that could be done: nothing discretionary, commitments intact.
    for line in result.categories:
        assert line.allowance == line.committed
    assert result.allowance == result.committed


def test_committed_bills_are_never_proposed_as_a_saving():
    result = plan(Goal(Money.parse("15000"), 6), history(), series=series_for(), asof=ASOF)
    by_name = {line.category: line for line in result.categories}

    for name, amount in [
        ("Rent/Mortgage", "1800.00"),
        ("Utilities", "150.00"),
        ("Subscriptions", "15.99"),
    ]:
        line = by_name[name]
        assert line.committed == Money.parse(amount)
        assert line.allowance == line.baseline == Money.parse(amount)
        assert line.cut == Money.zero()
        assert line.is_fixed

    # ...and the cut lands entirely on the categories that can absorb it.
    assert all(by_name[name].cut.minor > 0 for name in ("Groceries", "Dining", "Shopping"))


def test_without_series_everything_looks_cuttable():
    # The contrast that shows what `series` buys: told nothing about the rent,
    # the engine has no way to know it is not negotiable.
    result = plan(Goal(Money.parse("15000"), 6), history(), asof=ASOF)
    rent = result.for_category("Rent/Mortgage")
    assert rent is not None
    assert rent.committed == Money.zero()
    assert rent.cut.minor > 0
    assert result.committed == Money.zero()


def test_committed_charges_are_not_counted_twice():
    # Rent appears both as a series and as six transactions. If the series
    # cost were added to a baseline that still contained those transactions,
    # the housing line would read $3,600 a month.
    result = plan(Goal(Money.parse("15000"), 6), history(), series=series_for(), asof=ASOF)
    rent = result.for_category("Rent/Mortgage")
    assert rent is not None
    assert rent.baseline == Money.parse("1800.00")
    assert rent.discretionary == Money.zero()
    assert result.baseline == Money.parse("3065.99")


def test_the_cut_is_proportional_to_what_each_category_costs():
    result = plan(Goal(Money.parse("15000"), 6), history(), series=series_for(), asof=ASOF)
    cuts = {line.category: line for line in result.categories}
    groceries, dining, shopping = cuts["Groceries"], cuts["Dining"], cuts["Shopping"]

    # Ordered by size, not shared out equally.
    assert groceries.cut.minor > dining.cut.minor > shopping.cut.minor
    equal_share = result.cut.minor // 3
    assert groceries.cut.minor != equal_share

    # Every discretionary category gives up the same *share* of itself, to
    # within the odd cent largest-remainder has to place somewhere.
    fractions = [line.cut_fraction for line in (groceries, dining, shopping)]
    assert max(fractions) - min(fractions) < Decimal("0.001")


def test_weekly_and_monthly_budgets_are_both_supported():
    weekly_history: list[Transaction] = []
    for week in WEEKS:
        weekly_history += [
            make_tx(week, "1000.00", "ACME CORP PAYROLL DIRECT DEP"),
            make_tx(week + timedelta(days=1), "-150.00", "TRADER JOES #123 SAN JOSE CA"),
            make_tx(week + timedelta(days=3), "-50.00", "CHIPOTLE 2481"),
        ]

    result = plan(Goal(Money.parse("9000"), 10, "weekly"), weekly_history, asof=WEEKLY_ASOF)
    assert result.period == "weekly"
    assert result.periods == 10
    assert result.periods_observed == 13  # a quarter of weeks, not six of them
    assert result.income == Money.parse("1000.00")
    assert result.required == Money.parse("900.00")
    assert result.feasible

    allowances = {line.category: line.allowance for line in result.categories}
    assert allowances["Groceries"] == Money.parse("75.00")
    assert allowances["Dining"] == Money.parse("25.00")
    assert "week" in result.explanation


def test_the_period_may_be_overridden_for_the_same_goal():
    # The same six-month target, shown as a weekly budget.
    monthly = plan(Goal(Money.parse("6000"), 6), history(), series=series_for(), asof=ASOF)
    weekly = plan(
        Goal(Money.parse("6000"), 26), history(), series=series_for(), period="weekly", asof=ASOF
    )
    assert monthly.period == "monthly"
    assert weekly.period == "weekly"
    # A monthly rent amortised across weeks: $21,600 a year over 52 weeks.
    rent = weekly.for_category("Rent/Mortgage")
    assert rent is not None
    assert rent.committed == Money.parse("415.38")


# -- baselines -----------------------------------------------------------


def test_one_extravagant_month_does_not_inflate_the_budget():
    # A median, not a mean: the mean of five $300 months and one $2,000 month
    # is $583.33, and a budget built on that is one nobody needs to follow.
    txs = history(months=MONTHS[:5]) + history(dining="-2000.00", months=MONTHS[5:])
    result = plan(Goal(Money.parse("15000"), 6), txs, series=series_for(), asof=ASOF)
    dining = result.for_category("Dining")
    assert dining is not None
    assert dining.baseline == Money.parse("300.00")


def test_occasional_spending_is_averaged_over_the_periods_it_missed():
    # Three $60 haircuts in six months is a $30-a-month habit, not a $60 one.
    # Periods with no spending in a category have to count as zero, or a
    # once-a-quarter charge would earn a full monthly allowance.
    txs = history()
    for month in MONTHS[:3]:
        txs.append(make_tx(month.replace(day=20), "-60.00", "MIDTOWN ANIMAL HOSPITAL"))
    result = plan(Goal(Money.parse("15000"), 6), txs, series=series_for(), asof=ASOF)
    pets = result.for_category("Pets")
    assert pets is not None
    assert pets.baseline == Money.parse("30.00")


def test_a_category_touched_once_does_not_become_a_monthly_allowance():
    txs = history() + [make_tx(MONTHS[2].replace(day=9), "-900.00", "DELTA AIR LINES")]
    result = plan(Goal(Money.parse("15000"), 6), txs, series=series_for(), asof=ASOF)
    assert result.for_category("Travel") is None
    assert result.baseline == Money.parse("3065.99")


def test_the_partial_current_period_is_excluded_from_the_baseline():
    # Two days into July the user has bought nothing. That is not a $0 budget.
    txs = history() + [make_tx(date(2026, 7, 2), "-12.00", "CHIPOTLE 2481")]
    result = plan(Goal(Money.parse("15000"), 6), txs, series=series_for(), asof=ASOF)
    dining = result.for_category("Dining")
    assert dining is not None
    assert dining.baseline == Money.parse("300.00")


def test_a_stored_category_beats_the_rules():
    txs = history()
    for tx in txs:
        if "CHIPOTLE" in tx.description:
            tx.category = "Entertainment"
    result = plan(Goal(Money.parse("15000"), 6), txs, series=series_for(), asof=ASOF)
    assert result.for_category("Dining") is None
    entertainment = result.for_category("Entertainment")
    assert entertainment is not None
    assert entertainment.baseline == Money.parse("300.00")


def test_transfers_are_not_spending():
    txs = history()
    for month in MONTHS:
        txs.append(make_tx(month.replace(day=18), "-2000.00", "ONLINE TRANSFER TO SAVINGS"))
    result = plan(Goal(Money.parse("15000"), 6), txs, series=series_for(), asof=ASOF)
    assert result.baseline == Money.parse("3065.99")
    assert result.for_category("Transfer") is None


def test_empty_history_admits_it_has_nothing_to_go_on():
    result = plan(Goal(Money.parse("5000"), 6), [], asof=ASOF)
    assert result.categories == []
    assert result.periods_observed == 0
    assert result.income == Money.zero()
    assert result.baseline == Money.zero()
    assert not result.feasible
    assert result.shortfall == result.required == Money.parse("833.34")
    assert "No complete month of history" in result.explanation


def test_a_goal_needing_more_than_the_whole_income_is_infeasible_not_negative():
    result = plan(Goal(Money.parse("60000"), 6), history(), series=series_for(), asof=ASOF)
    assert not result.feasible
    assert result.shortfall == Money.parse("6965.99")  # $10,000 needed, $3,034.01 free
    assert all(line.allowance.minor >= 0 for line in result.categories)


# -- mid-period progress -------------------------------------------------


def budget_plan():
    return plan(Goal(Money.parse("15000"), 6), history(), series=series_for(), asof=ASOF)


def test_progress_is_on_track_when_spending_is_paced():
    result = budget_plan()
    spending = [
        make_tx(date(2026, 7, 1), "-1800.00", "GREENFIELD APARTMENTS LEASING"),
        make_tx(date(2026, 7, 10), "-100.00", "TRADER JOES #123 SAN JOSE CA"),
        make_tx(date(2026, 7, 12), "-40.00", "CHIPOTLE 2481"),
    ]
    tracked = progress(result, spending, date(2026, 7, 1), asof=date(2026, 7, 16))

    assert tracked.period_start == date(2026, 7, 1)
    assert tracked.period_end == date(2026, 8, 1)
    assert tracked.elapsed_days == 16
    assert tracked.period_days == 31
    assert tracked.on_track
    assert tracked.over_budget == []
    assert tracked.spent == Money.parse("1940.00")
    assert tracked.remaining.minor > 0

    groceries = next(c for c in tracked.categories if c.category == "Groceries")
    assert groceries.spent == Money.parse("100.00")
    assert groceries.allowance == Money.parse("291.28")
    assert groceries.on_track and not groceries.over


def test_rent_paid_on_the_first_is_not_an_overspend():
    # Pro-rating a commitment would report every user as wildly over budget on
    # the 2nd of the month, which is the fastest way to make a UI ignorable.
    result = budget_plan()
    rent = [make_tx(date(2026, 7, 1), "-1800.00", "GREENFIELD APARTMENTS LEASING")]
    tracked = progress(result, rent, date(2026, 7, 1), asof=date(2026, 7, 2))
    housing = next(c for c in tracked.categories if c.category == "Rent/Mortgage")
    assert housing.spent == housing.allowance
    assert housing.on_track
    assert tracked.on_track


def test_progress_flags_a_category_running_hot():
    result = budget_plan()
    spending = [make_tx(date(2026, 7, 2), "-400.00", "TRADER JOES #123 SAN JOSE CA")]
    tracked = progress(result, spending, date(2026, 7, 1), asof=date(2026, 7, 3))

    assert not tracked.on_track
    groceries = next(c for c in tracked.categories if c.category == "Groceries")
    assert not groceries.on_track
    assert groceries.over  # the whole month's allowance, gone on the 2nd
    assert groceries.remaining.minor < 0
    assert groceries.fraction_used > 1
    assert [c.category for c in tracked.over_budget] == ["Groceries"]


def test_unbudgeted_spending_counts_against_you():
    result = budget_plan()
    spending = [make_tx(date(2026, 7, 4), "-500.00", "DELTA AIR LINES 006")]
    tracked = progress(result, spending, date(2026, 7, 5), asof=date(2026, 7, 6))

    travel = next(c for c in tracked.categories if c.category == "Travel")
    assert travel.allowance == Money.zero()
    assert travel.spent == Money.parse("500.00")
    assert not travel.on_track
    assert not tracked.on_track


def test_progress_normalises_the_period_and_clamps_the_date():
    result = budget_plan()
    # Any day in the period identifies it, and a date past the end reports the
    # period as finished rather than as some impossible future.
    tracked = progress(result, history(), date(2026, 7, 19), asof=date(2027, 1, 1))
    assert tracked.period_start == date(2026, 7, 1)
    assert tracked.asof == date(2026, 7, 31)
    assert tracked.elapsed_days == tracked.period_days == 31
    assert tracked.spent == Money.zero()  # the history stops in June


def test_an_unpaid_bill_is_not_slack_for_overspending_elsewhere():
    # Mid-month the rent has not gone out yet. Counting only what has cleared
    # would let $1,800 of pending commitment excuse a blown grocery budget,
    # and the user would discover that on the 1st.
    result = budget_plan()
    spending = [make_tx(date(2026, 7, 3), "-450.00", "TRADER JOES #123 SAN JOSE CA")]
    tracked = progress(result, spending, date(2026, 7, 1), asof=date(2026, 7, 4))

    assert tracked.spent == Money.parse("450.00")
    assert tracked.counted == Money.parse("2415.99")  # + the commitments still due
    assert not tracked.on_track

    housing = next(c for c in tracked.categories if c.category == "Rent/Mortgage")
    assert housing.spent == Money.zero()
    assert housing.counted == Money.parse("1800.00")
    assert housing.on_track  # the rent is not late, it is just not paid yet


def test_it_works_on_series_that_detection_actually_found():
    # The end-to-end path: whatever recurring.detect returns is what a caller
    # will hand over, including the regular habits it also picks up.
    from carraway.analysis.recurring import detect

    txs = history()
    found = detect(txs)
    assert {s.merchant for s in found} >= {"Greenfield Apartments Leasing", "Netflix"}

    result = plan(Goal(Money.parse("15000"), 6), txs, series=found, asof=ASOF)
    assert result.committed == Money.parse("1965.99")

    # Groceries recur just as reliably as the rent and are still cuttable:
    # a habit is not a commitment.
    groceries = result.for_category("Groceries")
    assert groceries is not None
    assert groceries.committed == Money.zero()
    assert groceries.cut.minor > 0


def test_a_user_verdict_overrules_the_catalog():
    # The gym is a subscription to the catalog, but this user has decided it
    # is a bill they are not giving up.
    txs = history()
    for month in MONTHS:
        txs.append(make_tx(month.replace(day=6), "-89.00", "EQUINOX", tx_id=f"gym-{month}"))
    gym = RecurringSeries(
        merchant="Equinox",
        account_id="acct1",
        cadence="monthly",
        typical_amount=Money.parse("-89.00"),
        occurrences=6,
        first_seen=MONTHS[0],
        last_seen=MONTHS[-1],
        next_expected=None,
        confidence=0.9,
        amount_varies=False,
        transaction_ids=[f"gym-{m}" for m in MONTHS],
    )
    goal = Goal(Money.parse("15000"), 6)

    default = plan(goal, txs, series=[*series_for(), gym], asof=ASOF)
    assert default.for_category("Health").committed == Money.parse("89.00")

    overruled = plan(
        goal, txs, series=[*series_for(), gym], verdicts={"EQUINOX": "habit"}, asof=ASOF
    )
    health = overruled.for_category("Health")
    assert health.committed == Money.zero()
    assert health.cut.minor > 0
