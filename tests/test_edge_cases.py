"""Edge cases across the money-critical paths.

Not features -- the awkward inputs. A budget screen that divides by zero,
a wallet count against an account with no history, a period with one day in
it. These are the shapes that turn up once on a real ledger and are never
seen again, which is exactly why they are worth pinning.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from carraway.analysis import budgets
from carraway.analysis.overview import Period, summarise
from carraway.core.money import CurrencyMismatch, Money


def _m(text: str) -> Money:
    return Money.parse(text)


# -- the budget plan ----------------------------------------------------


def test_a_plan_with_nothing_to_budget_hands_out_nothing():
    lines = budgets.plan(_m("0.00"), {"Dining": _m("400.00")}, {})
    assert all(line.allowance.minor == 0 for line in lines)


def test_a_plan_with_no_categories_is_empty_rather_than_an_error():
    assert budgets.plan(_m("500.00"), {}, {}) == []
    assert budgets.totals([]).allowance == Money(0)


def test_a_category_with_no_history_does_not_divide_by_zero():
    lines = budgets.plan(_m("500.00"), {"New thing": _m("0.00")}, {})
    assert all(line.allowance.minor >= 0 for line in lines)


def test_a_line_with_no_usual_spend_reports_no_percentage():
    line = budgets.Line("New", _m("0.00"), _m("0.00"), _m("50.00"))
    assert line.percent_change is None


def test_every_allowance_stays_at_or_above_zero_however_tight():
    """A negative allowance is not a plan, and would be saved as one."""
    weights = {"Rent/Mortgage": _m("900.00"), "Dining": _m("400.00"), "Fees": _m("3.00")}
    committed = {"Rent/Mortgage": _m("900.00")}
    for budgeted in ("0.00", "1.00", "500.00", "900.00", "901.00", "5000.00"):
        lines = budgets.plan(_m(budgeted), weights, committed)
        assert all(line.allowance.minor >= 0 for line in lines), budgeted


def test_the_split_never_loses_or_invents_a_cent():
    """Largest-remainder, so the envelopes sum to exactly the total."""
    weights = {f"C{n}": Money(n * 37 + 11) for n in range(1, 18)}
    for total_minor in (1, 7, 99, 100, 12_345, 999_999):
        lines = budgets.split(Money(total_minor), weights)
        assert sum(line.allowance.minor for line in lines) == total_minor, total_minor


def test_a_plan_keeps_the_ledgers_currency():
    weights = {"Dining": Money.parse("400.00", "EUR")}
    lines = budgets.plan(Money.parse("300.00", "EUR"), weights, {})
    assert all(line.allowance.currency == "EUR" for line in lines)
    assert budgets.totals(lines).allowance.currency == "EUR"


def test_mixing_currencies_is_refused_rather_than_silently_wrong():
    with pytest.raises(CurrencyMismatch):
        Money.parse("10.00", "USD") + Money.parse("10.00", "EUR")


# -- the overview period ------------------------------------------------


def test_a_single_day_period_does_not_divide_by_zero():
    day = Period(date(2026, 9, 2), date(2026, 9, 2))
    assert day.days == 1
    got = summarise([], {}, day, day.before())
    assert got.daily_burn == Money(0)


def test_a_period_that_has_not_started_still_answers():
    future = Period(date(2030, 1, 1), date(2030, 1, 31))
    got = summarise([], {}, future, future.before())
    assert got.count == 0
    assert got.net == Money(0)


# -- what a budget says about itself ------------------------------------


def test_days_left_never_goes_negative_on_a_budget_that_has_closed():
    """A closed budget has no days left, not a negative number of them --
    which would divide the wrong way in "how much a day is left"."""
    budget = budgets.Budget(
        id="b",
        name="Past",
        starts_on=date.today() - timedelta(days=60),
        ends_on=date.today() - timedelta(days=30),
        envelopes=(budgets.Envelope("Dining", _m("100.00")),),
    )
    state = budgets.status(budget, [])
    assert state.finished is True
    assert state.days_left == 0


def test_a_budget_of_one_day_counts_that_day():
    """Both ends inclusive: "1-30 September" is thirty days, and today counts
    as a day you can still spend in."""
    today = date.today()
    budget = budgets.Budget(
        id="b",
        name="Today",
        starts_on=today,
        ends_on=today,
        envelopes=(budgets.Envelope("Dining", _m("100.00")),),
    )
    state = budgets.status(budget, [])
    assert state.total_days == 1
    assert state.days_left == 1


def test_a_budget_that_has_not_begun_has_all_its_days_left():
    start = date.today() + timedelta(days=10)
    budget = budgets.Budget(
        id="b",
        name="Later",
        starts_on=start,
        ends_on=start + timedelta(days=9),
        envelopes=(budgets.Envelope("Dining", _m("100.00")),),
    )
    state = budgets.status(budget, [])
    assert state.started is False
    assert state.days_left == state.total_days == 10


# -- the wallet correction, which writes to the ledger ------------------


def _cash(tmp_path, *, balance: str | None = "100.00", spends=()):
    from carraway.core import db
    from carraway.core.models import Account, AccountType, Transaction
    from carraway.ui.data import Ledger

    path = tmp_path / "cash.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CASH))
    anchor = date.today() - timedelta(days=10)
    if balance is not None:
        db.record_balance(conn, "cash", Money.parse(balance), anchor)
    db.insert_transactions(
        conn,
        [
            Transaction(
                id=f"s{index}",
                account_id="cash",
                date=anchor + timedelta(days=1 + index),
                amount=Money.parse(amount),
                description="SPEND",
                merchant="SPEND",
            )
            for index, amount in enumerate(spends)
        ],
    )
    conn.close()
    ledger = Ledger(path)
    ledger.load()
    return ledger


def test_a_correction_lands_exactly_on_the_counted_figure(tmp_path):
    ledger = _cash(tmp_path, spends=("-30.00",))
    assert ledger.implied_balance("cash") == _m("70.00")
    ledger.set_cash_balance("cash", _m("55.00"), correction=True)
    assert ledger.implied_balance("cash") == _m("55.00")


def test_counting_twice_does_not_compound(tmp_path):
    """The second count is measured against the first, not against the
    original -- otherwise correcting twice doubles the adjustment."""
    ledger = _cash(tmp_path, spends=("-30.00",))
    ledger.set_cash_balance("cash", _m("55.00"), correction=True)
    ledger.set_cash_balance("cash", _m("55.00"), correction=True)
    assert ledger.implied_balance("cash") == _m("55.00")
    adjustments = [t for t in ledger.transactions if t.description == "Cash adjustment"]
    assert len(adjustments) == 1, "a second identical count invented another line"


def test_counting_an_empty_wallet_is_allowed(tmp_path):
    ledger = _cash(tmp_path, spends=("-30.00",))
    ledger.set_cash_balance("cash", _m("0.00"), correction=True)
    assert ledger.implied_balance("cash") == _m("0.00")


def test_a_count_against_an_account_with_no_history_sets_it(tmp_path):
    """Nothing to reconcile against, so the count is simply the truth."""
    ledger = _cash(tmp_path, balance=None)
    ledger.set_cash_balance("cash", _m("42.00"), correction=True)
    assert ledger.implied_balance("cash") == _m("42.00")


def test_declining_the_correction_leaves_the_history_alone(tmp_path):
    """The balance is still recorded, so net worth is right; the history is
    knowingly incomplete rather than carrying an invented transaction."""
    ledger = _cash(tmp_path, spends=("-30.00",))
    ledger.set_cash_balance("cash", _m("55.00"), correction=False)
    assert [t for t in ledger.transactions if t.description == "Cash adjustment"] == []


def test_a_correction_is_never_written_for_a_gap_of_nothing(tmp_path):
    ledger = _cash(tmp_path, spends=("-30.00",))
    gap = ledger.set_cash_balance("cash", _m("70.00"), correction=True)
    assert gap == _m("0.00")
    assert [t for t in ledger.transactions if t.description == "Cash adjustment"] == []
