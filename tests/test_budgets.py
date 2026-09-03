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


# -- saying where a suggested figure came from ---------------------------


def _spend(day: str, amount: str = "-50.00", account: str = "a1") -> Transaction:
    return _tx(date.fromisoformat(day), amount, "Dining", account)


def test_the_basis_counts_months_with_data_not_months_in_the_window():
    """A ledger imported three weeks ago has six months of window and one
    month of evidence, and the second number is the one worth saying."""
    got = budgets.history_basis([_spend("2026-08-10")], asof=date(2026, 9, 15))
    assert got.months_with_data == 1
    assert got.months_examined == budgets.DEFAULT_LOOKBACK_MONTHS


def test_the_month_in_progress_never_counts_as_evidence():
    got = budgets.history_basis([_spend("2026-09-10")], asof=date(2026, 9, 15))
    assert got.months_with_data == 0
    assert "nothing to suggest" in got.describe()


def test_a_thin_history_says_so_rather_than_sounding_certain():
    thin = budgets.history_basis(
        [_spend("2026-07-10"), _spend("2026-08-10")], asof=date(2026, 9, 15)
    )
    assert thin.confident is False
    assert "little to go on" in thin.describe()

    thick = budgets.history_basis(
        [_spend(f"2026-0{month}-10") for month in range(3, 9)], asof=date(2026, 9, 15)
    )
    assert thick.confident is True
    assert "little to go on" not in thick.describe()


def test_the_description_names_the_months_and_the_one_left_out():
    got = budgets.history_basis(
        [_spend(f"2026-0{month}-10") for month in range(3, 9)], asof=date(2026, 9, 15)
    )
    said = got.describe()
    assert "6 complete months" in said
    assert "March–August 2026" in said
    assert "September" in said


def test_the_span_reads_the_way_a_person_would_say_it():
    one = budgets.history_basis([_spend("2026-08-10")], asof=date(2026, 9, 15))
    assert one.span == "August 2026"

    same_year = budgets.history_basis(
        [_spend("2026-07-10"), _spend("2026-08-10")], asof=date(2026, 9, 15)
    )
    assert same_year.span == "July–August 2026"

    across = budgets.history_basis(
        [_spend("2025-12-10"), _spend("2026-02-10")], asof=date(2026, 3, 5)
    )
    assert across.span == "December 2025 – February 2026"


def test_an_empty_ledger_has_no_span_and_no_confidence():
    got = budgets.history_basis([], asof=date(2026, 9, 15))
    assert got.span == ""
    assert got.confident is False
    assert got.first_month is None


def test_income_and_transfers_are_not_spending_history():
    """Otherwise a payday would count as a month of evidence about spending."""
    payday = _tx(date(2026, 8, 1), "4000.00", "Income")
    got = budgets.history_basis([payday], asof=date(2026, 9, 15))
    assert got.months_with_data == 0


def test_the_basis_respects_the_account_scope():
    """A budget watching one card should describe that card's history."""
    both = [_spend("2026-07-10", account="a1"), _spend("2026-08-10", account="a2")]
    assert budgets.history_basis(both, asof=date(2026, 9, 15)).months_with_data == 2
    narrowed = budgets.history_basis(both, asof=date(2026, 9, 15), accounts=["a2"])
    assert narrowed.months_with_data == 1
    assert narrowed.span == "August 2026"


def test_an_estimate_carries_its_reason():
    known = budgets.Estimate(Money.parse("4000.00"), "From your payslips.")
    assert known.known is True
    assert known.confident is True

    nothing = budgets.Estimate(Money.zero(), "Nothing found.", confident=False)
    assert nothing.known is False


# -- sharing the leftover when some categories are already committed -----


def _money(text: str) -> Money:
    return Money.parse(text)


def test_a_category_with_a_commitment_still_gets_an_allowance():
    """The bug this exists for, with the numbers it was found on.

    Shopping was budgeted $20.03 against $307.79 usually spent -- seven per
    cent -- because it happened to contain a $20 subscription, while Dining
    kept ninety-seven per cent for containing none. The totals added up and
    the plan was unusable.
    """
    weights = {"Shopping": _money("307.79"), "Dining": _money("903.07")}
    committed = {"Shopping": _money("20.03")}
    lines = budgets.split_with_commitments(_money("500.00"), weights, committed)
    allowances = {line.category: line.allowance for line in lines}

    assert allowances["Shopping"] > _money("20.03"), "the commitment swallowed the category"
    # Both squeezed by comparable amounts, rather than one bearing all of it.
    shopping_kept = allowances["Shopping"].minor / weights["Shopping"].minor
    dining_kept = allowances["Dining"].minor / weights["Dining"].minor
    assert abs(shopping_kept - dining_kept) < 0.25


def test_the_total_is_the_commitments_plus_the_leftover():
    weights = {"Shopping": _money("300.00"), "Dining": _money("900.00")}
    committed = {"Shopping": _money("20.00")}
    lines = budgets.split_with_commitments(_money("500.00"), weights, committed)
    total = sum(line.allowance.minor for line in lines)
    assert total == _money("520.00").minor


def test_a_category_that_is_only_commitment_draws_none_of_the_leftover():
    """Rent's cost is already covered; giving it a share would double-count."""
    weights = {"Rent/Mortgage": _money("900.00"), "Dining": _money("400.00")}
    committed = {"Rent/Mortgage": _money("900.00")}
    lines = budgets.split_with_commitments(_money("400.00"), weights, committed)
    allowances = {line.category: line.allowance for line in lines}
    assert allowances["Rent/Mortgage"] == _money("900.00")
    assert allowances["Dining"] == _money("400.00")


def test_a_commitment_larger_than_the_usual_spend_keeps_its_real_size():
    """What is actually owed beats what has lately been paid."""
    weights = {"Insurance": _money("83.23"), "Dining": _money("400.00")}
    committed = {"Insurance": _money("87.39")}
    lines = budgets.split_with_commitments(_money("200.00"), weights, committed)
    allowances = {line.category: line.allowance for line in lines}
    assert allowances["Insurance"] == _money("87.39")


def test_with_nothing_committed_it_is_an_ordinary_proportional_split():
    weights = {"Dining": _money("300.00"), "Travel": _money("100.00")}
    lines = budgets.split_with_commitments(_money("400.00"), weights, {})
    allowances = {line.category: line.allowance for line in lines}
    assert allowances["Dining"] == _money("300.00")
    assert allowances["Travel"] == _money("100.00")


def test_everything_committed_still_shares_the_leftover_somewhere():
    """A leftover has to land somewhere, or the budget silently loses money."""
    weights = {"Rent/Mortgage": _money("900.00"), "Utilities": _money("100.00")}
    committed = {"Rent/Mortgage": _money("900.00"), "Utilities": _money("100.00")}
    lines = budgets.split_with_commitments(_money("300.00"), weights, committed)
    total = sum(line.allowance.minor for line in lines)
    assert total == _money("1300.00").minor


def test_the_biggest_line_comes_first():
    weights = {"Fees": _money("3.00"), "Dining": _money("900.00")}
    lines = budgets.split_with_commitments(_money("400.00"), weights, {})
    assert lines[0].category == "Dining"


# -- what can change, and what cannot -----------------------------------


def test_the_squeeze_lands_only_on_what_can_change():
    """Saving more money is a decision to spend less on what you choose.

    Rent does not get smaller because the savings target grew, so the whole
    of the reduction has to fall on the discretionary lines.
    """
    weights = {"Rent/Mortgage": _money("900.00"), "Dining": _money("400.00")}
    committed = {"Rent/Mortgage": _money("900.00")}
    lines = {line.category: line for line in budgets.plan(_money("1100.00"), weights, committed)}

    assert lines["Rent/Mortgage"].allowance == _money("900.00")
    assert lines["Rent/Mortgage"].change == _money("0.00")
    assert lines["Dining"].allowance == _money("200.00")
    assert lines["Dining"].change == _money("-200.00")


def test_a_line_says_what_has_to_change_and_by_how_much():
    line = budgets.Line("Dining", _money("903.07"), _money("0.00"), _money("640.98"))
    assert line.change == _money("-262.09")
    assert line.percent_change == pytest.approx(-29.0, abs=0.5)
    assert line.locked is False


def test_rent_reads_as_locked_despite_the_two_estimates_disagreeing():
    """The commitment and the usual spend measure the same bill differently.

    A detected series against a median of what was paid differ by a few
    dollars, and without a tolerance rent came out "flexible" on the strength
    of a $13 gap -- putting the one line nobody can act on at the top of the
    list of things to change.
    """
    line = budgets.Line("Rent/Mortgage", _money("948.00"), _money("934.37"), _money("934.37"))
    assert line.locked is True


def test_a_mostly_discretionary_line_is_not_locked():
    line = budgets.Line("Shopping", _money("307.79"), _money("20.03"), _money("224.28"))
    assert line.locked is False
    assert line.discretionary == _money("287.76")


def test_a_commitment_bigger_than_the_usual_spend_is_locked():
    line = budgets.Line("Insurance", _money("83.23"), _money("87.39"), _money("87.39"))
    assert line.locked is True
    assert line.discretionary == _money("0.00")


def test_locked_lines_sort_to_the_bottom():
    """The answer to "what do I change" is never among them."""
    weights = {"Rent/Mortgage": _money("900.00"), "Dining": _money("400.00")}
    committed = {"Rent/Mortgage": _money("900.00")}
    lines = budgets.plan(_money("1100.00"), weights, committed)
    assert [line.category for line in lines] == ["Dining", "Rent/Mortgage"]


def test_commitments_that_overrun_the_budget_do_not_go_negative():
    """Sharing out a negative leftover would hand every category a negative
    allowance, which is not a plan. The caller reports the shortfall."""
    weights = {"Rent/Mortgage": _money("900.00"), "Dining": _money("400.00")}
    committed = {"Rent/Mortgage": _money("900.00")}
    lines = {line.category: line for line in budgets.plan(_money("500.00"), weights, committed)}
    assert lines["Rent/Mortgage"].allowance == _money("900.00")
    assert lines["Dining"].allowance == _money("0.00")
    assert all(line.allowance.minor >= 0 for line in lines.values())


def test_the_totals_row_adds_up_the_lot():
    weights = {"Rent/Mortgage": _money("900.00"), "Dining": _money("400.00")}
    committed = {"Rent/Mortgage": _money("900.00")}
    lines = budgets.plan(_money("1100.00"), weights, committed)
    summary = budgets.totals(lines)
    assert summary.usual == _money("1300.00")
    assert summary.allowance == _money("1100.00")
    assert summary.change == _money("-200.00")


def test_the_subtotals_reconcile_with_the_total():
    """The flexible band, the locked band and the total must agree, or the
    screen shows two different answers to what looks like one question."""
    weights = {
        "Rent/Mortgage": _money("948.00"),
        "Insurance": _money("83.23"),
        "Dining": _money("903.07"),
    }
    committed = {"Rent/Mortgage": _money("934.37"), "Insurance": _money("87.39")}
    lines = budgets.plan(_money("1800.00"), weights, committed)

    flexible = budgets.totals([line for line in lines if not line.locked])
    locked = budgets.totals([line for line in lines if line.locked])
    everything = budgets.totals(lines)
    assert flexible.change.minor + locked.change.minor == everything.change.minor
    assert flexible.allowance.minor + locked.allowance.minor == everything.allowance.minor


def test_more_saving_means_a_smaller_allowance_every_time():
    """The screen is only useful if dragging the savings target moves this."""
    weights = {"Dining": _money("400.00"), "Travel": _money("200.00")}
    previous = None
    for budgeted in ("600.00", "450.00", "300.00", "150.00"):
        total = budgets.totals(budgets.plan(_money(budgeted), weights, {}))
        if previous is not None:
            assert total.allowance.minor < previous
        previous = total.allowance.minor


def test_subscriptions_cannot_be_changed_within_the_window():
    """Cancelling Netflix today does not refund this month's charge.

    And detection only finds the subscriptions it recognises, so the rest of
    the category would otherwise read as a discretionary choice: here $144
    is spent on subscriptions and only $101 of it was detected as recurring.
    """
    weights = {"Subscriptions": _money("144.35"), "Dining": _money("900.00")}
    committed = {"Subscriptions": _money("101.14")}
    lines = {line.category: line for line in budgets.plan(_money("1044.35"), weights, committed)}

    assert lines["Subscriptions"].locked is True
    assert lines["Subscriptions"].allowance == _money("144.35")
    assert lines["Subscriptions"].change == _money("0.00")
    # And the money it takes comes out of what is left to share.
    assert lines["Dining"].allowance == _money("900.00")


def test_the_locked_list_can_be_overridden():
    """It is a default, not a law: a different ledger may disagree."""
    weights = {"Subscriptions": _money("144.35"), "Dining": _money("900.00")}
    lines = {
        line.category: line for line in budgets.plan(_money("1044.35"), weights, {}, always=())
    }
    assert lines["Subscriptions"].locked is False


def test_a_locked_category_with_no_spending_is_not_invented():
    """Nothing to lock if the user never spends on it."""
    weights = {"Dining": _money("900.00")}
    lines = budgets.plan(_money("900.00"), weights, {})
    assert [line.category for line in lines] == ["Dining"]


# -- how much is left for the rest of the month, week, and today --------


def _running(spends, *, asof, starts, ends, allowance="3000.00"):
    """A budget with some spending in it, as of a given day."""
    from carraway.core.models import Transaction

    budget = budgets.Budget(
        id="b",
        name="Test",
        starts_on=starts,
        ends_on=ends,
        envelopes=(budgets.Envelope("Dining", _money(allowance)),),
    )
    txs = [
        Transaction(
            id=f"t{index}",
            account_id="a1",
            date=when,
            amount=_money(amount),
            description="X",
            merchant="X",
            category="Dining",
        )
        for index, (when, amount) in enumerate(spends)
    ]
    return budgets.status(budget, txs, asof=asof, categories={t.id: "Dining" for t in txs})


def test_what_is_left_per_day_falls_as_the_month_runs_out():
    from datetime import date

    starts, ends = date(2026, 9, 1), date(2026, 9, 30)
    early = _running([], asof=date(2026, 9, 1), starts=starts, ends=ends)
    late = _running([], asof=date(2026, 9, 28), starts=starts, ends=ends)
    assert early.days_left == 30
    assert late.days_left == 3
    assert late.daily_remaining > early.daily_remaining


def test_spending_today_is_counted_separately():
    from datetime import date

    asof = date(2026, 9, 15)
    state = _running(
        [(date(2026, 9, 14), "-40.00"), (asof, "-25.00")],
        asof=asof,
        starts=date(2026, 9, 1),
        ends=date(2026, 9, 30),
    )
    assert state.spent_today == _money("25.00")
    assert state.spent == _money("65.00")


def test_this_week_means_the_week_you_are_living_in():
    """Monday to Sunday, not a rolling seven days: "this week" means the one
    ending on Sunday to everybody who is not a computer."""
    from datetime import date

    # 2026-09-15 is a Tuesday, so the week began on Monday the 14th.
    asof = date(2026, 9, 15)
    state = _running(
        [
            (date(2026, 9, 11), "-100.00"),  # the Friday before: last week
            (date(2026, 9, 14), "-30.00"),  # Monday: this week
            (asof, "-20.00"),  # today
        ],
        asof=asof,
        starts=date(2026, 9, 1),
        ends=date(2026, 9, 30),
    )
    assert state.spent_this_week == _money("50.00")
    assert state.spent == _money("150.00")


def test_the_week_never_reaches_back_before_the_budget_began():
    from datetime import date

    # Budget starts Wednesday; asking on Thursday, the week is two days old.
    state = _running(
        [(date(2026, 9, 16), "-10.00")],
        asof=date(2026, 9, 17),
        starts=date(2026, 9, 16),
        ends=date(2026, 9, 30),
    )
    assert state.spent_this_week == _money("10.00")


def test_the_weekly_figure_is_this_weeks_share_not_the_whole_remainder():
    """Spending the month's entire remainder before Sunday is the thing this
    is meant to prevent."""
    from datetime import date

    state = _running([], asof=date(2026, 9, 1), starts=date(2026, 9, 1), ends=date(2026, 9, 30))
    assert state.weekly_remaining < state.remaining
    assert state.weekly_remaining.minor == state.daily_remaining.minor * state.days_left_this_week


def test_the_week_is_clipped_to_what_is_left_of_the_budget():
    from datetime import date

    # The 29th is a Tuesday; the week runs to Sunday but the budget ends on
    # the 30th, so there are two days left, not six.
    state = _running([], asof=date(2026, 9, 29), starts=date(2026, 9, 1), ends=date(2026, 9, 30))
    assert state.days_left == 2
    assert state.days_left_this_week == 2


def test_what_is_left_today_takes_off_what_has_gone_already():
    from datetime import date

    asof = date(2026, 9, 15)
    state = _running([(asof, "-20.00")], asof=asof, starts=date(2026, 9, 1), ends=date(2026, 9, 30))
    assert state.left_today.minor == state.daily_remaining.minor - _money("20.00").minor


def test_a_closed_budget_has_nothing_left_for_today():
    from datetime import date

    state = _running([], asof=date(2026, 10, 5), starts=date(2026, 9, 1), ends=date(2026, 9, 30))
    assert state.finished is True
    assert state.daily_remaining is None
    assert state.left_today is None
    assert state.weekly_remaining is None
    assert state.days_left_this_week == 0


def test_overspending_leaves_a_negative_figure_rather_than_a_comforting_zero():
    from datetime import date

    state = _running(
        [(date(2026, 9, 2), "-4000.00")],
        asof=date(2026, 9, 15),
        starts=date(2026, 9, 1),
        ends=date(2026, 9, 30),
    )
    assert state.remaining.minor < 0
    assert state.daily_remaining.minor < 0


# -- what a trip costs the month it sits inside -------------------------


def _window(name, starts, ends, **lines):
    from datetime import date as _d

    return budgets.Budget(
        id=name.lower(),
        name=name,
        starts_on=_d.fromisoformat(starts),
        ends_on=_d.fromisoformat(ends),
        envelopes=tuple(budgets.Envelope(c, _money(a)) for c, a in lines.items()),
    )


def test_a_trip_says_what_it_leaves_the_rest_of_the_month():
    """The question is "can I afford this", and "it contradicts your month"
    does not answer it. The arithmetic does."""
    month = _window("September", "2026-09-01", "2026-09-30", Travel="600.00", Dining="740.00")
    trip = _window("Trip", "2026-09-20", "2026-09-22", Travel="800.00", Dining="200.00")

    made = budgets.impact(trip, month)
    assert made is not None
    assert made.fits is True
    assert made.days == 3
    assert made.other_days == 27
    assert made.left_for_the_rest == _money("340.00")
    assert "27 days" in made.describe()


def test_a_trip_that_does_not_fit_says_how_short():
    month = _window("September", "2026-09-01", "2026-09-30", Travel="600.00")
    trip = _window("Trip", "2026-09-20", "2026-09-22", Travel="900.00")

    made = budgets.impact(trip, month)
    assert made.fits is False
    assert "short" in made.describe()


def test_the_squeeze_is_stated_per_day_because_that_is_what_changes():
    month = _window("September", "2026-09-01", "2026-09-30", Dining="3000.00")
    trip = _window("Trip", "2026-09-10", "2026-09-12", Dining="900.00")

    made = budgets.impact(trip, month)
    assert made.normal_per_day == _money("100.00")
    assert made.per_day_after == _money("77.77")  # 2100 over 27 days
    assert made.squeeze.minor > 0


def test_a_budget_that_only_overlaps_is_not_measured():
    """Its spending could land in the days they do not share, so the
    arithmetic would be a guess dressed as an answer."""
    month = _window("September", "2026-09-01", "2026-09-30", Travel="600.00")
    straddle = _window("Trip", "2026-09-28", "2026-10-04", Travel="500.00")
    assert budgets.impact(straddle, month) is None


def test_budgets_watching_different_accounts_do_not_eat_each_other():
    from datetime import date as _d

    month = budgets.Budget(
        id="m",
        name="September",
        starts_on=_d(2026, 9, 1),
        ends_on=_d(2026, 9, 30),
        envelopes=(budgets.Envelope("Travel", _money("600.00")),),
        accounts=("card",),
    )
    trip = budgets.Budget(
        id="t",
        name="Trip",
        starts_on=_d(2026, 9, 20),
        ends_on=_d(2026, 9, 22),
        envelopes=(budgets.Envelope("Travel", _money("800.00")),),
        accounts=("cash",),
    )
    assert budgets.impact(trip, month) is None


def test_a_budget_never_reports_eating_into_itself():
    month = _window("September", "2026-09-01", "2026-09-30", Travel="600.00")
    assert budgets.impacts(month, [month]) == []


def test_the_tightest_squeeze_is_reported_first():
    tight = _window("Tight", "2026-09-01", "2026-09-30", Travel="900.00")
    loose = _window("Loose", "2026-09-01", "2026-09-30", Travel="9000.00")
    trip = _window("Trip", "2026-09-20", "2026-09-22", Travel="800.00")
    found = budgets.impacts(trip, [loose, tight])
    assert found[0].outer.name == "Tight"


def test_a_trip_filling_the_whole_window_leaves_no_other_days():
    """Dividing by zero other days would be the obvious way to crash this."""
    month = _window("September", "2026-09-01", "2026-09-30", Travel="600.00")
    same = _window("Same", "2026-09-01", "2026-09-30", Travel="500.00")
    made = budgets.impact(same, month)
    assert made.other_days == 0
    assert made.per_day_after == _money("0.00")
    assert made.describe()


# -- pace that knows when the bills land -----------------------------------


def _rent_month() -> tuple[Budget, list]:
    """A budget whose money is mostly a single bill due on the 3rd."""
    budget = _september(
        envelopes=[
            Envelope(category="Rent/Mortgage", allowance=Money.parse("900")),
            Envelope(category="Dining", allowance=Money.parse("300")),
        ]
    )
    schedule = [
        budgets.Commitment(
            due=date(2026, 9, 3), category="Rent/Mortgage", amount=Money.parse("-900")
        )
    ]
    return budget, schedule


def test_pace_steps_on_the_day_a_bill_is_due():
    # Three days into September, a flat pace expects 3/30 of $1,200 -- $120.
    # But $900 of rent was always leaving on the 3rd, so paying it is not
    # overspending, and a budget that says otherwise is training the user to
    # ignore it every month.
    budget, schedule = _rent_month()
    asof = date(2026, 9, 3)

    flat = budgets.status(budget, [], asof=asof)
    aware = budgets.status(budget, [], asof=asof, schedule=schedule)

    assert flat.pace == Money.parse("120")
    assert aware.pace == Money.parse("930")  # $900 rent + 3/30 of $300 dining
    assert aware.scheduled_so_far == Money.parse("900")


def test_a_bill_not_yet_due_does_not_inflate_the_pace():
    # The other half of the same idea: on the 2nd the rent has not gone out, so
    # the pace must not pretend it has and quietly excuse real overspending.
    budget, schedule = _rent_month()
    aware = budgets.status(budget, [], asof=date(2026, 9, 2), schedule=schedule)

    assert aware.scheduled_so_far == Money.zero()
    assert aware.pace == Money.parse("20")  # 2/30 of the $300 that is discretionary


def test_bill_aware_and_flat_pace_agree_on_the_last_day():
    # By the end every bill has landed, so the staircase has to arrive at the
    # same place as the straight line. A pace that finished anywhere else would
    # mean the budget could never be spent exactly.
    budget, schedule = _rent_month()
    asof = date(2026, 9, 30)

    flat = budgets.status(budget, [], asof=asof)
    aware = budgets.status(budget, [], asof=asof, schedule=schedule)

    assert aware.pace == flat.pace == Money.parse("1200")


def test_no_schedule_leaves_the_pace_exactly_as_it_was():
    budget, _ = _rent_month()
    asof = date(2026, 9, 10)
    assert budgets.status(budget, [], asof=asof).pace == budgets.status(
        budget, [], asof=asof, schedule=[]
    ).pace


def test_a_bill_larger_than_its_envelope_cannot_pace_past_the_allowance():
    # Otherwise a line whose bill exceeds what was budgeted for it reports a
    # pace above its own allowance, and then calls genuinely overspent money
    # "on track" -- the one thing this screen must never do.
    budget = _september(
        envelopes=[Envelope(category="Rent/Mortgage", allowance=Money.parse("500"))]
    )
    schedule = [
        budgets.Commitment(
            due=date(2026, 9, 1), category="Rent/Mortgage", amount=Money.parse("-900")
        )
    ]
    state = budgets.status(budget, [], asof=date(2026, 9, 5), schedule=schedule)

    assert state.pace == Money.parse("500")
    line = state.lines[0]
    assert line.pace.minor <= line.allowance.minor


def test_a_commitment_outside_the_window_is_somebody_elses_month():
    budget, _ = _rent_month()
    schedule = [
        budgets.Commitment(
            due=date(2026, 8, 3), category="Rent/Mortgage", amount=Money.parse("-900")
        )
    ]
    state = budgets.status(budget, [], asof=date(2026, 9, 3), schedule=schedule)
    assert state.scheduled_so_far == Money.zero()
