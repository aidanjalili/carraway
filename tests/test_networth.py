"""Tests for net worth reconstruction.

The interesting property under test is that the series is *inferred*: nothing
stores a historical balance, so every point behind today is the result of
walking transactions back out of a known current balance. Most of these tests
exist to hold that walk in the right direction and to keep debt behaving like
debt.
"""

import uuid
from datetime import date

from carraway.analysis.networth import (
    accounts_missing_balances,
    monthly_cashflow,
    normalise_balance,
    reconstruct,
    summarise,
)
from carraway.core.models import Account, AccountType, Transaction
from carraway.core.money import Money


def make_account(account_id, kind=AccountType.CHECKING, name="", closed=False) -> Account:
    return Account(id=account_id, name=name or account_id.title(), type=kind, closed=closed)


def make_tx(account_id, day: date, amount: str, description="Something", group="") -> Transaction:
    return Transaction(
        id=uuid.uuid4().hex,
        account_id=account_id,
        date=day,
        amount=Money.parse(amount),
        description=description,
        transfer_group=group,
    )


def nets(points) -> dict[date, Money]:
    """Net worth keyed by date, so a test can assert on the days it cares about."""
    return {p.date: p.net for p in points}


# -- the backwards walk --------------------------------------------------


def test_walks_backwards_from_the_current_balance():
    # $1,000 in chequing today. A $500 deposit landed on the 20th, so before it
    # there was $500, not $1,500 — the direction that is easy to invert.
    checking = make_account("chk")
    txs = [
        make_tx("chk", date(2026, 3, 10), "-100.00", "Groceries"),
        make_tx("chk", date(2026, 3, 20), "500.00", "Paycheck"),
    ]
    points = reconstruct(
        [checking],
        txs,
        {"chk": Money.parse("1000.00")},
        start=date(2026, 3, 1),
        granularity="daily",
        as_of=date(2026, 3, 31),
    )

    by_date = nets(points)
    assert by_date[date(2026, 3, 1)] == Money.parse("600.00")
    assert by_date[date(2026, 3, 9)] == Money.parse("600.00")
    assert by_date[date(2026, 3, 10)] == Money.parse("500.00")  # groceries taken out
    assert by_date[date(2026, 3, 19)] == Money.parse("500.00")
    assert by_date[date(2026, 3, 20)] == Money.parse("1000.00")  # paycheck arrives
    assert by_date[date(2026, 3, 31)] == Money.parse("1000.00")


def test_points_are_oldest_first_and_span_the_range():
    checking = make_account("chk")
    points = reconstruct(
        [checking],
        [make_tx("chk", date(2026, 3, 5), "-20.00")],
        {"chk": Money.parse("100.00")},
        start=date(2026, 3, 1),
        granularity="daily",
        as_of=date(2026, 3, 10),
    )

    assert [p.date for p in points] == sorted(p.date for p in points)
    assert points[0].date == date(2026, 3, 1)
    assert points[-1].date == date(2026, 3, 10)
    assert len(points) == 10


def test_start_defaults_to_the_earliest_transaction():
    checking = make_account("chk")
    txs = [make_tx("chk", date(2026, 3, 4), "-20.00"), make_tx("chk", date(2026, 3, 6), "-30.00")]
    points = reconstruct([checking], txs, {"chk": Money.parse("50.00")}, granularity="daily")

    assert points[0].date == date(2026, 3, 4)
    assert points[-1].date == date(2026, 3, 6)  # as_of defaults to the last transaction


# -- liabilities ---------------------------------------------------------


def test_credit_card_reduces_net_worth():
    accounts = [make_account("chk"), make_account("visa", AccountType.CREDIT_CARD)]
    balances = {"chk": Money.parse("2000.00"), "visa": Money.parse("-500.00")}
    point = reconstruct(accounts, [], balances, as_of=date(2026, 3, 31))[0]

    assert point.assets == Money.parse("2000.00")
    assert point.liabilities == Money.parse("500.00")  # stored positive, as an amount owed
    assert point.net == Money.parse("1500.00")


def test_card_balance_sign_is_normalised_in_both_directions():
    # SimpleFIN reports -791.76 for $791.76 owed; a statement says 791.76 for
    # the identical debt. Both must mean the same thing.
    visa = make_account("visa", AccountType.CREDIT_CARD)
    as_negative = reconstruct([visa], [], {"visa": Money.parse("-791.76")}, as_of=date(2026, 3, 1))
    as_positive = reconstruct([visa], [], {"visa": Money.parse("791.76")}, as_of=date(2026, 3, 1))

    assert as_negative[0].net == Money.parse("-791.76")
    assert as_negative[0].net == as_positive[0].net
    assert as_negative[0].liabilities == as_positive[0].liabilities == Money.parse("791.76")

    # And the same rule at the level it is actually applied.
    assert normalise_balance(visa, Money.parse("791.76")) == Money.parse("-791.76")
    assert normalise_balance(visa, Money.parse("-791.76")) == Money.parse("-791.76")


def test_asset_balance_sign_is_left_alone():
    # An overdrawn chequing account really is money owed, so it must not be
    # flipped to look like $120 of savings.
    checking = make_account("chk")
    assert normalise_balance(checking, Money.parse("-120.00")) == Money.parse("-120.00")

    point = reconstruct([checking], [], {"chk": Money.parse("-120.00")}, as_of=date(2026, 3, 1))[0]
    assert point.assets == Money.zero()
    assert point.liabilities == Money.parse("120.00")
    assert point.net == Money.parse("-120.00")


def test_paying_down_a_card_leaves_net_worth_unchanged():
    # $300 moves from chequing to a card. Assets fall by $300 and debt falls by
    # $300; the user is exactly as wealthy as they were an hour earlier.
    accounts = [make_account("chk"), make_account("visa", AccountType.CREDIT_CARD)]
    payment = [
        make_tx("chk", date(2026, 3, 15), "-300.00", "Visa payment", group="pay1"),
        make_tx("visa", date(2026, 3, 15), "300.00", "Payment thank you", group="pay1"),
    ]
    balances = {"chk": Money.parse("500.00"), "visa": Money.parse("-200.00")}
    points = reconstruct(
        accounts,
        payment,
        balances,
        start=date(2026, 3, 14),
        granularity="daily",
        as_of=date(2026, 3, 15),
    )

    before, after = points[0], points[-1]
    assert before.net == after.net == Money.parse("300.00")
    # The composition changed even though the total did not.
    assert before.assets == Money.parse("800.00")
    assert before.liabilities == Money.parse("500.00")
    assert after.assets == Money.parse("500.00")
    assert after.liabilities == Money.parse("200.00")


# -- transfers -----------------------------------------------------------


def test_transfers_move_balances_but_not_net_worth():
    accounts = [make_account("chk"), make_account("sav", AccountType.SAVINGS)]
    transfer = [
        make_tx("chk", date(2026, 3, 10), "-750.00", "Transfer to savings", group="mv1"),
        make_tx("sav", date(2026, 3, 10), "750.00", "Transfer from chequing", group="mv1"),
    ]
    balances = {"chk": Money.parse("1250.00"), "sav": Money.parse("5750.00")}
    points = reconstruct(
        accounts,
        transfer,
        balances,
        start=date(2026, 3, 9),
        granularity="daily",
        as_of=date(2026, 3, 10),
    )

    assert {p.net for p in points} == {Money.parse("7000.00")}
    # The individual accounts did move, which is why the transfer is kept in
    # the walk rather than filtered out of it.
    assert points[0].balances["chk"] == Money.parse("2000.00")
    assert points[0].balances["sav"] == Money.parse("5000.00")


# -- granularity ---------------------------------------------------------


def test_monthly_granularity_samples_month_ends():
    checking = make_account("chk")
    txs = [
        make_tx("chk", date(2026, 1, 15), "1000.00"),
        make_tx("chk", date(2026, 2, 15), "1000.00"),
        make_tx("chk", date(2026, 3, 15), "1000.00"),
    ]
    points = reconstruct(
        [checking],
        txs,
        {"chk": Money.parse("5000.00")},
        start=date(2026, 1, 1),
        granularity="monthly",
        as_of=date(2026, 4, 15),
    )

    assert [p.date for p in points] == [
        date(2026, 1, 1),
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 15),
    ]
    assert [p.net for p in points] == [
        Money.parse("2000.00"),
        Money.parse("3000.00"),
        Money.parse("4000.00"),
        Money.parse("5000.00"),
        Money.parse("5000.00"),
    ]


def test_weekly_granularity_samples_week_ends():
    checking = make_account("chk")
    points = reconstruct(
        [checking],
        [make_tx("chk", date(2026, 1, 14), "-100.00")],
        {"chk": Money.parse("900.00")},
        start=date(2026, 1, 5),  # a Monday
        granularity="weekly",
        as_of=date(2026, 2, 3),
    )

    assert [p.date for p in points] == [
        date(2026, 1, 5),
        date(2026, 1, 11),  # Sundays close the week
        date(2026, 1, 18),
        date(2026, 1, 25),
        date(2026, 2, 1),
        date(2026, 2, 3),
    ]
    assert points[0].net == Money.parse("1000.00")
    assert points[1].net == Money.parse("1000.00")  # spending lands in the next week
    assert points[2].net == Money.parse("900.00")


def test_unknown_granularity_is_rejected():
    checking = make_account("chk")
    try:
        reconstruct([checking], [], {"chk": Money.zero()}, granularity="fortnightly")
    except ValueError as exc:
        assert "fortnightly" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected a ValueError for an unknown granularity")


# -- missing balances ----------------------------------------------------


def test_account_without_a_balance_is_excluded_and_reported():
    # Assuming zero would keep the shape of the mystery account's history while
    # putting every point a constant distance from the truth, so it is dropped
    # and named instead.
    accounts = [make_account("chk"), make_account("sav", AccountType.SAVINGS), make_account("old")]
    txs = [make_tx("old", date(2026, 3, 5), "-40.00"), make_tx("chk", date(2026, 3, 5), "-10.00")]
    balances = {"chk": Money.parse("100.00"), "sav": Money.parse("200.00")}

    assert accounts_missing_balances(accounts, balances) == ["old"]

    points = reconstruct(accounts, txs, balances, start=date(2026, 3, 4), granularity="daily")
    assert all(p.excluded == ("old",) for p in points)
    assert all("old" not in p.balances for p in points)
    # The excluded account's transaction did not leak into the total either.
    assert points[0].net == Money.parse("310.00")
    assert points[-1].net == Money.parse("300.00")


def test_closed_account_with_a_known_balance_still_contributes_history():
    # A closed account holds nothing today but held something last month, and
    # dropping it would carve a false cliff out of the chart.
    accounts = [make_account("chk"), make_account("gone", closed=True)]
    txs = [make_tx("gone", date(2026, 3, 10), "-800.00", "Closing withdrawal")]
    balances = {"chk": Money.parse("1000.00"), "gone": Money.zero()}
    points = reconstruct(accounts, txs, balances, start=date(2026, 3, 9), granularity="daily")

    assert points[0].net == Money.parse("1800.00")
    assert points[-1].net == Money.parse("1000.00")


# -- summarise -----------------------------------------------------------


def test_summarise_reports_change_and_percent():
    checking = make_account("chk")
    txs = [make_tx("chk", date(2026, 2, 15), "500.00", "Bonus")]
    points = reconstruct(
        [checking],
        txs,
        {"chk": Money.parse("1500.00")},
        start=date(2026, 2, 1),
        granularity="daily",
        as_of=date(2026, 2, 28),
    )
    result = summarise(points)

    assert result.start == date(2026, 2, 1)
    assert result.end == date(2026, 2, 28)
    assert result.start_net == Money.parse("1000.00")
    assert result.end_net == Money.parse("1500.00")
    assert result.change == Money.parse("500.00")
    assert result.percent_change == 50.0


def test_percent_change_is_undefined_from_a_negative_start():
    # Debt of $1,000 paid down to $200 is an $800 improvement. Expressed as a
    # percentage of -$1,000 it reads "-80%", which says the opposite.
    visa = make_account("visa", AccountType.CREDIT_CARD)
    txs = [make_tx("visa", date(2026, 2, 10), "800.00", "Payment")]
    points = reconstruct(
        [visa],
        txs,
        {"visa": Money.parse("-200.00")},
        start=date(2026, 2, 1),
        granularity="daily",
        as_of=date(2026, 2, 28),
    )
    result = summarise(points)

    assert result.start_net == Money.parse("-1000.00")
    assert result.end_net == Money.parse("-200.00")
    assert result.change == Money.parse("800.00")
    assert result.percent_change is None


def test_percent_change_is_undefined_from_a_zero_start():
    checking = make_account("chk")
    txs = [make_tx("chk", date(2026, 2, 10), "500.00", "First paycheck")]
    points = reconstruct(
        [checking],
        txs,
        {"chk": Money.parse("500.00")},
        start=date(2026, 2, 1),
        granularity="daily",
        as_of=date(2026, 2, 28),
    )
    result = summarise(points)

    assert result.start_net == Money.zero()
    assert result.percent_change is None  # never inf, never nan
    assert result.change == Money.parse("500.00")


def test_summarise_finds_the_best_and_worst_months():
    checking = make_account("chk")
    txs = [
        make_tx("chk", date(2026, 1, 20), "2000.00", "Good month"),
        make_tx("chk", date(2026, 2, 20), "-3000.00", "Bad month"),
        make_tx("chk", date(2026, 3, 20), "100.00", "Quiet month"),
    ]
    points = reconstruct(
        [checking],
        txs,
        {"chk": Money.parse("4100.00")},
        start=date(2026, 1, 1),
        granularity="monthly",
        as_of=date(2026, 3, 31),
    )
    result = summarise(points)

    assert result.best_month == ("2026-01", Money.parse("2000.00"))
    assert result.worst_month == ("2026-02", Money.parse("-3000.00"))


# -- cashflow ------------------------------------------------------------


def test_monthly_cashflow_splits_income_from_spending():
    txs = [
        make_tx("chk", date(2026, 1, 2), "2400.00", "Payroll"),
        make_tx("chk", date(2026, 1, 5), "-1800.00", "Rent"),
        make_tx("chk", date(2026, 1, 9), "-200.00", "Groceries"),
    ]
    rows = monthly_cashflow(txs)

    assert rows == [
        ("2026-01", Money.parse("2400.00"), Money.parse("2000.00"), Money.parse("400.00"))
    ]


def test_monthly_cashflow_excludes_transfers():
    # $500 moved to savings is neither income nor spending; counting it as
    # either turns a budget into fiction.
    txs = [
        make_tx("chk", date(2026, 1, 2), "2400.00", "Payroll"),
        make_tx("chk", date(2026, 1, 3), "-500.00", "To savings", group="mv1"),
        make_tx("sav", date(2026, 1, 3), "500.00", "From chequing", group="mv1"),
    ]
    month, income, spending, net = monthly_cashflow(txs)[0]

    assert income == Money.parse("2400.00")
    assert spending == Money.zero()
    assert net == Money.parse("2400.00")


def test_monthly_cashflow_fills_empty_months():
    # A gap in the chart reads as "no data"; a real month with no activity is
    # an informative zero and should be drawn as one.
    txs = [
        make_tx("chk", date(2026, 1, 2), "100.00"),
        make_tx("chk", date(2026, 4, 2), "-100.00"),
    ]
    rows = monthly_cashflow(txs)

    assert [r[0] for r in rows] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert rows[1] == ("2026-02", Money.zero(), Money.zero(), Money.zero())
    assert rows[3][2] == Money.parse("100.00")


# -- empty input ---------------------------------------------------------


def test_empty_inputs_produce_empty_output():
    assert reconstruct([], [], {}) == []
    # Accounts with no balances at all leaves nothing that can be reconstructed.
    assert reconstruct([make_account("chk")], [], {}) == []
    assert monthly_cashflow([]) == []


def test_summarise_of_an_empty_series():
    result = summarise([])

    assert result.start is None
    assert result.end is None
    assert result.change == Money.zero()
    assert result.percent_change is None
    assert result.best_month is None
    assert result.worst_month is None
