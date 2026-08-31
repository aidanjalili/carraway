"""Budgets set over a stretch of days, and how they are judged.

Distinct from test_budget.py, which covers deriving a repeating allowance from
a net-worth goal. This is the named, dated, saved kind.
"""

from datetime import date

import pytest

from carraway.analysis import budgets
from carraway.analysis.budgets import Budget, Envelope
from carraway.core.models import Transaction
from carraway.core.money import Money


def _tx(day: date, amount: str, category: str = "", account: str = "a1") -> Transaction:
    return Transaction(
        id=f"{day}-{amount}-{category}-{account}",
        account_id=account,
        date=day,
        amount=Money.parse(amount),
        description=category or "Something",
        category=category,
    )


def _september(**kw) -> Budget:
    return Budget(
        id="b1",
        name="September",
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 30),
        **kw,
    )


# -- the shape of a budget ------------------------------------------------


def test_a_window_counts_both_its_endpoints():
    # "1-30 September" is thirty days, not twenty-nine. An off-by-one here is
    # a whole day of spending.
    assert _september().days == 30
    assert (
        Budget(id="x", name="Trip", starts_on=date(2026, 9, 18), ends_on=date(2026, 9, 18)).days
        == 1
    )


def test_a_budget_cannot_end_before_it_starts():
    with pytest.raises(ValueError, match="cannot end"):
        Budget(id="x", name="Bad", starts_on=date(2026, 9, 30), ends_on=date(2026, 9, 1))


def test_the_total_is_every_envelope():
    budget = _september(
        envelopes=(
            Envelope("Dining", Money.parse("300")),
            Envelope("Travel", Money.parse("600")),
        )
    )
    assert budget.total == Money.parse("900")


def test_no_account_scope_means_every_account():
    # A card, a debit card and cash are all just ways of spending.
    assert _september().watches("anything") is True
    assert _september(accounts=("a1",)).watches("a1") is True
    assert _september(accounts=("a1",)).watches("a2") is False


# -- suggesting from history ----------------------------------------------


def _six_months(amount_per_month: str, category: str) -> list[Transaction]:
    return [_tx(date(2026, month, 10), amount_per_month, category) for month in range(3, 9)]


def test_a_suggestion_scales_a_monthly_median_to_the_window():
    # $300/month of dining, budgeted over a 30-day window, is about $300.
    txs = _six_months("-300.00", "Dining")
    lines = budgets.suggest(txs, date(2026, 9, 1), date(2026, 9, 30), asof=date(2026, 9, 1))
    dining = next(line for line in lines if line.category == "Dining")
    assert Money.parse("295") <= dining.allowance <= Money.parse("300")


def test_a_short_window_gets_a_proportionally_short_allowance():
    # The whole point of supporting arbitrary ranges: an eleven-day trip is
    # not a month, and budgeting it as one would be useless.
    txs = _six_months("-300.00", "Dining")
    lines = budgets.suggest(txs, date(2026, 9, 18), date(2026, 9, 28), asof=date(2026, 9, 1))
    dining = next(line for line in lines if line.category == "Dining")
    assert Money.parse("105") <= dining.allowance <= Money.parse("112")


def test_one_extravagant_month_does_not_set_the_budget():
    # The median exists for this: a budget built from a holiday is one nobody
    # can hit.
    txs = _six_months("-100.00", "Dining")
    txs.append(_tx(date(2026, 7, 20), "-2000.00", "Dining"))
    lines = budgets.suggest(txs, date(2026, 9, 1), date(2026, 9, 30), asof=date(2026, 9, 1))
    dining = next(line for line in lines if line.category == "Dining")
    assert dining.allowance < Money.parse("200")


def test_the_month_in_progress_is_not_used_as_a_baseline():
    # A budget set on the 3rd would otherwise come out at a tenth of the truth.
    txs = _six_months("-300.00", "Dining")
    txs.append(_tx(date(2026, 9, 2), "-20.00", "Dining"))
    lines = budgets.suggest(txs, date(2026, 10, 1), date(2026, 10, 31), asof=date(2026, 9, 3))
    dining = next(line for line in lines if line.category == "Dining")
    assert dining.allowance > Money.parse("250")


def test_a_category_seen_once_in_six_months_budgets_as_a_sixth():
    # Months with no spending are real zeros, not missing data. Dropping them
    # would make an occasional category look like a monthly one.
    txs = [_tx(date(2026, 5, 4), "-600.00", "Travel")]
    txs += _six_months("-100.00", "Dining")
    lines = budgets.suggest(txs, date(2026, 9, 1), date(2026, 9, 30), asof=date(2026, 9, 1))
    assert not any(line.category == "Travel" for line in lines)


def test_income_and_transfers_are_not_spending():
    txs = _six_months("-100.00", "Dining")
    txs += [_tx(date(2026, month, 1), "3000.00", "Income") for month in range(3, 9)]
    lines = budgets.suggest(txs, date(2026, 9, 1), date(2026, 9, 30), asof=date(2026, 9, 1))
    assert not any(line.category == "Income" for line in lines)


def test_a_suggestion_can_be_limited_to_certain_accounts():
    txs = _six_months("-100.00", "Dining")
    txs += [_tx(date(2026, month, 11), "-500.00", "Dining", account="a2") for month in range(3, 9)]
    both = budgets.suggest(txs, date(2026, 9, 1), date(2026, 9, 30), asof=date(2026, 9, 1))
    one = budgets.suggest(
        txs, date(2026, 9, 1), date(2026, 9, 30), asof=date(2026, 9, 1), accounts=["a1"]
    )
    assert both[0].allowance > one[0].allowance


# -- splitting a total ----------------------------------------------------


def test_a_split_sums_to_exactly_the_total():
    # Largest-remainder allocation: the parts must equal the whole, to the cent.
    weights = {
        "Dining": Money.parse("300"),
        "Groceries": Money.parse("400"),
        "Fun": Money.parse("55"),
    }
    lines = budgets.split(Money.parse("1000"), weights)
    assert sum(line.allowance.minor for line in lines) == Money.parse("1000").minor


def test_a_split_is_proportional_to_what_things_normally_cost():
    weights = {"Groceries": Money.parse("600"), "Coffee": Money.parse("60")}
    lines = {line.category: line.allowance for line in budgets.split(Money.parse("660"), weights)}
    assert lines["Groceries"] == Money.parse("600")
    assert lines["Coffee"] == Money.parse("60")


def test_splitting_with_no_history_yields_nothing_rather_than_guessing():
    assert budgets.split(Money.parse("500"), {}) == []


# -- working backwards from income ----------------------------------------


def test_spendable_is_income_less_saving_less_fixed():
    assert budgets.spendable(
        Money.parse("4000"), Money.parse("800"), Money.parse("1900")
    ) == Money.parse("1300")


def test_a_plan_that_does_not_fit_comes_back_negative():
    # Clamping to zero would read as "you may spend nothing" when the truth
    # is "this does not add up".
    assert budgets.spendable(Money.parse("2000"), Money.parse("800"), Money.parse("1900")).minor < 0


# -- how it is going ------------------------------------------------------


def _running_budget() -> Budget:
    return _september(
        envelopes=(
            Envelope("Dining", Money.parse("300")),
            Envelope("Travel", Money.parse("600")),
        )
    )


def test_pace_is_measured_against_elapsed_days():
    # Two days into September nobody has spent their month.
    budget = _running_budget()
    state = budgets.status(budget, [], asof=date(2026, 9, 15))
    assert state.elapsed_days == 15
    # Sixteen, not fifteen: the 15th through the 30th, today included.
    assert state.days_left == 16
    assert state.pace == Money.parse("450")  # half of $900


def test_spending_under_pace_is_on_track():
    budget = _running_budget()
    txs = [_tx(date(2026, 9, 5), "-100.00", "Dining")]
    state = budgets.status(budget, txs, asof=date(2026, 9, 15))
    assert state.spent == Money.parse("100")
    assert state.on_track is True
    assert state.remaining == Money.parse("800")


def test_spending_ahead_of_pace_is_not_on_track():
    budget = _running_budget()
    txs = [_tx(date(2026, 9, 5), "-700.00", "Travel")]
    state = budgets.status(budget, txs, asof=date(2026, 9, 10))
    assert state.on_track is False
    travel = next(line for line in state.lines if line.category == "Travel")
    assert travel.over is True
    assert travel.remaining.minor < 0


def test_what_is_left_per_remaining_day():
    # The number that changes a decision today: can this flight fit?
    budget = _running_budget()
    txs = [_tx(date(2026, 9, 5), "-300.00", "Travel")]
    state = budgets.status(budget, txs, asof=date(2026, 9, 20))
    assert state.remaining == Money.parse("600")
    # $600 across the 20th to the 30th inclusive.
    assert state.days_left == 11
    assert state.daily_remaining == Money.parse("54.54")


def test_spending_outside_the_window_does_not_count():
    budget = _running_budget()
    txs = [_tx(date(2026, 8, 31), "-500.00", "Dining"), _tx(date(2026, 10, 1), "-500.00", "Dining")]
    state = budgets.status(budget, txs, asof=date(2026, 9, 15))
    assert state.spent == Money.zero()


def test_spending_after_today_does_not_count_yet():
    # A budget is judged as of a day, not against its whole window.
    budget = _running_budget()
    txs = [_tx(date(2026, 9, 25), "-500.00", "Dining")]
    state = budgets.status(budget, txs, asof=date(2026, 9, 10))
    assert state.spent == Money.zero()


def test_spending_on_an_unwatched_account_does_not_count():
    budget = _september(envelopes=(Envelope("Dining", Money.parse("300")),), accounts=("a1",))
    txs = [_tx(date(2026, 9, 5), "-50.00", "Dining", account="a2")]
    assert budgets.status(budget, txs, asof=date(2026, 9, 15)).spent == Money.zero()


def test_spending_on_any_account_counts_by_default():
    # Card, debit or cash: money spent is money spent.
    budget = _running_budget()
    txs = [
        _tx(date(2026, 9, 5), "-50.00", "Dining", account="card"),
        _tx(date(2026, 9, 6), "-30.00", "Dining", account="cash"),
    ]
    assert budgets.status(budget, txs, asof=date(2026, 9, 15)).spent == Money.parse("80")


def test_unbudgeted_spending_is_reported_rather_than_hidden():
    # It is real money that left the account. Hiding it would make this screen
    # disagree with the bank statement.
    budget = _running_budget()
    txs = [_tx(date(2026, 9, 5), "-75.00", "Alcohol")]
    state = budgets.status(budget, txs, asof=date(2026, 9, 15))
    alcohol = next(line for line in state.lines if line.category == "Alcohol")
    assert alcohol.unbudgeted is True
    assert state.spent == Money.parse("75")


def test_a_budget_that_has_not_started_reports_nothing_elapsed():
    budget = _running_budget()
    state = budgets.status(budget, [], asof=date(2026, 8, 20))
    assert state.started is False
    assert state.elapsed_days == 0
    assert state.pace == Money.zero()


def test_a_finished_budget_reports_itself_complete():
    budget = _running_budget()
    txs = [_tx(date(2026, 9, 5), "-100.00", "Dining")]
    state = budgets.status(budget, txs, asof=date(2026, 11, 1))
    assert state.finished is True
    assert state.elapsed_days == 30
    assert state.days_left == 0
    assert state.daily_remaining is None
    assert state.spent == Money.parse("100")


def test_refunds_reduce_the_category_they_land_in():
    budget = _running_budget()
    txs = [_tx(date(2026, 9, 5), "-100.00", "Dining"), _tx(date(2026, 9, 6), "40.00", "Dining")]
    state = budgets.status(budget, txs, asof=date(2026, 9, 15))
    assert state.spent == Money.parse("60")


def test_the_worst_overspend_is_listed_first():
    budget = _running_budget()
    txs = [
        _tx(date(2026, 9, 2), "-290.00", "Dining"),
        _tx(date(2026, 9, 2), "-610.00", "Travel"),
    ]
    state = budgets.status(budget, txs, asof=date(2026, 9, 3))
    assert [line.category for line in state.overspent] == ["Travel", "Dining"]


# -- storage --------------------------------------------------------------


def test_a_budget_survives_a_round_trip(tmp_path):
    from carraway.core import db

    original = Budget(
        id="b1",
        name="September",
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 30),
        envelopes=(
            Envelope("Travel", Money.parse("600")),
            Envelope("Dining", Money.parse("300")),
        ),
        accounts=("a1", "a2"),
        expected_income=Money.parse("4000"),
        savings_target=Money.parse("800"),
        fixed_costs=Money.parse("1900"),
    )
    conn = db.connect(tmp_path / "b.db")
    db.save_budget(conn, original)
    (loaded,) = db.list_budgets(conn)
    conn.close()

    assert loaded == original


def test_saving_twice_replaces_rather_than_accumulates(tmp_path):
    # A leftover envelope from a previous version would silently keep counting.
    from carraway.core import db

    conn = db.connect(tmp_path / "b.db")
    db.save_budget(conn, _september(envelopes=(Envelope("Travel", Money.parse("600")),)))
    db.save_budget(conn, _september(envelopes=(Envelope("Dining", Money.parse("300")),)))
    (loaded,) = db.list_budgets(conn)
    conn.close()

    assert [e.category for e in loaded.envelopes] == ["Dining"]
    assert loaded.total == Money.parse("300")


def test_a_budget_with_no_reasoning_stores_no_reasoning(tmp_path):
    # Typing the figures directly is a normal way to make a budget, and the
    # income/saving/fixed fields must not come back as zeros pretending to be
    # an answer the user never gave.
    from carraway.core import db

    conn = db.connect(tmp_path / "b.db")
    db.save_budget(conn, _september(envelopes=(Envelope("Dining", Money.parse("300")),)))
    (loaded,) = db.list_budgets(conn)
    conn.close()

    assert loaded.expected_income is None
    assert loaded.savings_target is None
    assert loaded.accounts == ()


def test_deleting_a_budget_takes_its_envelopes_with_it(tmp_path):
    from carraway.core import db

    conn = db.connect(tmp_path / "b.db")
    db.save_budget(conn, _september(envelopes=(Envelope("Dining", Money.parse("300")),)))
    assert db.delete_budget(conn, "b1") == 1
    assert db.list_budgets(conn) == []
    left = conn.execute("SELECT COUNT(*) FROM budget_envelopes").fetchone()[0]
    conn.close()
    assert left == 0


# -- budgets that argue with each other -----------------------------------


def _month() -> Budget:
    return Budget(
        id="month",
        name="September",
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 30),
        envelopes=(Envelope("Travel", Money.parse("400")), Envelope("Dining", Money.parse("300"))),
    )


def _trip(travel: str = "600") -> Budget:
    return Budget(
        id="trip",
        name="Late Sept trip",
        starts_on=date(2026, 9, 18),
        ends_on=date(2026, 9, 28),
        envelopes=(Envelope("Travel", Money.parse(travel)),),
    )


def test_budgets_that_never_overlap_do_not_clash():
    august = Budget(id="aug", name="August", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31))
    assert budgets.clashes(_month(), [august]) == []


def test_a_trip_inside_a_month_is_reported_as_overlapping():
    (clash,) = budgets.clashes(_trip("100"), [_month()])
    assert clash.contained is True
    assert clash.overlap_days == 11
    assert clash.contradicts is False  # $100 fits inside the month's $400


def test_allowing_more_than_the_month_does_is_a_contradiction():
    # Every day of the trip is a day of September, so every dollar it permits
    # also counts against September. $600 of travel cannot fit in $400.
    (clash,) = budgets.clashes(_trip("600"), [_month()])
    assert clash.contradicts is True
    assert clash.tighter == (("Travel", Money.parse("600"), Money.parse("400")),)
    assert "only allows $400.00 for Travel" in budgets.describe_clashes([clash])


def test_a_partial_overlap_claims_no_contradiction():
    # The spending could land in the days they do not share, so asserting a
    # conflict would be a false alarm.
    straddling = Budget(
        id="x",
        name="Late Sept into Oct",
        starts_on=date(2026, 9, 25),
        ends_on=date(2026, 10, 5),
        envelopes=(Envelope("Travel", Money.parse("5000")),),
    )
    (clash,) = budgets.clashes(straddling, [_month()])
    assert clash.contained is False
    assert clash.contradicts is False
    assert "counts against both" in budgets.describe_clashes([clash])


def test_budgets_watching_different_accounts_do_not_clash():
    # They are watching different money, so the same dollar is never counted
    # twice and neither constrains the other.
    mine = Budget(
        id="m",
        name="My card",
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 30),
        envelopes=(Envelope("Dining", Money.parse("900")),),
        accounts=("a1",),
    )
    theirs = Budget(
        id="t",
        name="Joint card",
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 30),
        envelopes=(Envelope("Dining", Money.parse("100")),),
        accounts=("a2",),
    )
    assert budgets.clashes(mine, [theirs]) == []


def test_an_unscoped_budget_shares_accounts_with_a_scoped_one():
    # "All accounts" includes a1, so these do watch the same money.
    scoped = Budget(
        id="t",
        name="Card only",
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 30),
        envelopes=(Envelope("Dining", Money.parse("100")),),
        accounts=("a1",),
    )
    assert len(budgets.clashes(_month(), [scoped])) == 1


def test_a_nested_budget_exceeding_the_total_contradicts_even_without_shared_categories():
    inner = Budget(
        id="i",
        name="Splurge",
        starts_on=date(2026, 9, 5),
        ends_on=date(2026, 9, 10),
        envelopes=(Envelope("Alcohol", Money.parse("5000")),),
    )
    (clash,) = budgets.clashes(inner, [_month()])
    assert clash.total_exceeds is True
    assert clash.contradicts is True
    assert "allows more in total" in budgets.describe_clashes([clash])


def test_a_budget_does_not_clash_with_itself():
    assert budgets.clashes(_month(), [_month()]) == []


def test_contradictions_are_listed_before_mere_overlaps():
    harmless = Budget(
        id="h",
        name="Harmless",
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 30),
        envelopes=(Envelope("Travel", Money.parse("9000")),),
    )
    found = budgets.clashes(_trip("600"), [harmless, _month()])
    assert [c.other.name for c in found] == ["September", "Harmless"]


def test_nothing_to_say_when_there_are_no_clashes():
    assert budgets.describe_clashes([]) == ""
