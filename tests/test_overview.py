"""Periods, and comparing one against the one before it.

Date arithmetic is where this kind of code goes wrong, and it goes wrong
quietly: an off-by-one in a comparison window does not crash, it just makes
every "up 12% on last month" a lie. So the month-end and year-end cases get
their own tests rather than being assumed.
"""

from datetime import date

import pytest

from carraway.analysis.overview import Movement, Period, preset, summarise
from carraway.core.models import Transaction
from carraway.core.money import Money


def _tx(when: str, amount: str, tid: str = "", *, transfer: bool = False) -> Transaction:
    return Transaction(
        id=tid or f"t{when}{amount}",
        account_id="a1",
        date=date.fromisoformat(when),
        amount=Money.parse(amount),
        description="x",
        merchant="X",
        # A transfer is one with a matched partner, not one filed under a
        # category called Transfer -- `is_transfer` reads transfer_group.
        transfer_group="pair" if transfer else None,
    )


# -- periods -------------------------------------------------------------


def test_a_period_counts_both_ends():
    assert Period(date(2026, 9, 1), date(2026, 9, 1)).days == 1
    assert Period(date(2026, 9, 1), date(2026, 9, 30)).days == 30


def test_the_period_before_ends_the_day_this_one_starts():
    now = Period(date(2026, 9, 10), date(2026, 9, 19))
    before = now.before()
    assert before.ends_on == date(2026, 9, 9)
    assert before.days == now.days
    assert before.starts_on == date(2026, 8, 31)


def test_this_month_is_compared_with_the_same_stretch_of_last_month():
    """Not with the tail end of it, which is what equal-length would give."""
    now, before = preset("This month", date(2026, 9, 15))
    assert (now.starts_on, now.ends_on) == (date(2026, 9, 1), date(2026, 9, 15))
    assert (before.starts_on, before.ends_on) == (date(2026, 8, 1), date(2026, 8, 15))


@pytest.mark.parametrize(
    ("today", "wanted_end"),
    [
        # 31 March has no counterpart in February; clamp rather than crash.
        (date(2026, 3, 31), date(2026, 2, 28)),
        (date(2026, 3, 30), date(2026, 2, 28)),
        (date(2024, 3, 31), date(2024, 2, 29)),  # a leap year
        (date(2026, 1, 31), date(2025, 12, 31)),  # across the year boundary
    ],
)
def test_the_month_before_clamps_to_a_day_that_exists(today, wanted_end):
    _, before = preset("This month", today)
    assert before.ends_on == wanted_end


def test_last_month_is_the_whole_month_and_the_whole_one_before():
    now, before = preset("Last month", date(2026, 9, 15))
    assert (now.starts_on, now.ends_on) == (date(2026, 8, 1), date(2026, 8, 31))
    assert (before.starts_on, before.ends_on) == (date(2026, 7, 1), date(2026, 7, 31))


def test_rolling_windows_compare_with_the_window_before():
    now, before = preset("Last 30 days", date(2026, 9, 15))
    assert now.days == before.days == 30
    assert before.ends_on == date(2026, 8, 16)
    assert now.starts_on == date(2026, 8, 17)


def test_this_year_is_compared_with_the_same_stretch_last_year():
    now, before = preset("This year", date(2026, 3, 4))
    assert (now.starts_on, now.ends_on) == (date(2026, 1, 1), date(2026, 3, 4))
    assert (before.starts_on, before.ends_on) == (date(2025, 1, 1), date(2025, 3, 4))


def test_a_leap_day_still_has_a_last_year():
    _, before = preset("This year", date(2024, 2, 29))
    assert before.ends_on == date(2023, 2, 28)


def test_all_time_has_nothing_to_compare_with():
    now, before = preset("All time", date(2026, 9, 15), date(2024, 5, 2))
    assert now.starts_on == date(2024, 5, 2)
    assert before is None


def test_an_unknown_preset_still_gives_a_sensible_screen():
    now, before = preset("nonsense", date(2026, 9, 15))
    assert now.starts_on == date(2026, 9, 1)
    assert before is not None


# -- summarising ---------------------------------------------------------


def test_only_transactions_inside_the_period_are_counted():
    txs = [_tx("2026-09-05", "-10.00"), _tx("2026-08-05", "-99.00")]
    got = summarise(txs, {}, Period(date(2026, 9, 1), date(2026, 9, 30)))
    assert abs(got.spent) == Money.parse("10.00")
    assert got.count == 1


def test_transfers_are_left_out_of_both_totals():
    """Money moved between your own accounts is not income and not spending."""
    txs = [
        _tx("2026-09-05", "-10.00"),
        _tx("2026-09-06", "-500.00", transfer=True),
        _tx("2026-09-07", "500.00", transfer=True),
    ]
    period = Period(date(2026, 9, 1), date(2026, 9, 30))
    got = summarise(txs, {}, period)
    assert abs(got.spent) == Money.parse("10.00")
    assert got.earned == Money.parse("0.00")
    assert got.count == 1


def test_net_is_what_came_in_less_what_went_out():
    txs = [_tx("2026-09-05", "-40.00"), _tx("2026-09-06", "100.00")]
    got = summarise(txs, {}, Period(date(2026, 9, 1), date(2026, 9, 30)))
    assert got.net == Money.parse("60.00")


def test_the_daily_burn_divides_by_the_period_not_the_data():
    """Ten days in, a month's budget is judged on ten days of spending."""
    txs = [_tx("2026-09-01", "-100.00")]
    got = summarise(txs, {}, Period(date(2026, 9, 1), date(2026, 9, 10)))
    assert got.daily_burn == Money.parse("10.00")


def test_movements_name_the_categories_that_changed_most():
    txs = [
        _tx("2026-09-05", "-300.00", "a"),
        _tx("2026-09-06", "-50.00", "b"),
        _tx("2026-08-05", "-100.00", "c"),
        _tx("2026-08-06", "-50.00", "d"),
    ]
    categories = {"a": "Dining", "b": "Transport", "c": "Dining", "d": "Transport"}
    got = summarise(
        txs,
        categories,
        Period(date(2026, 9, 1), date(2026, 9, 30)),
        Period(date(2026, 8, 1), date(2026, 8, 31)),
    )
    biggest = got.movements[0]
    assert biggest.category == "Dining"
    # Expressed as a magnitude: $200 *more* went out, which is a rise.
    assert biggest.change == Money.parse("200.00")
    assert biggest.rose is True
    assert biggest.percent == pytest.approx(200.0)


def test_a_category_that_did_not_move_is_not_news():
    txs = [_tx("2026-09-05", "-50.00", "a"), _tx("2026-08-05", "-50.00", "b")]
    got = summarise(
        txs,
        {"a": "Transport", "b": "Transport"},
        Period(date(2026, 9, 1), date(2026, 9, 30)),
        Period(date(2026, 8, 1), date(2026, 8, 31)),
    )
    assert got.movements == ()


def test_a_fall_counts_as_much_as_a_rise():
    txs = [_tx("2026-09-05", "-10.00", "a"), _tx("2026-08-05", "-300.00", "b")]
    got = summarise(
        txs,
        {"a": "Dining", "b": "Dining"},
        Period(date(2026, 9, 1), date(2026, 9, 30)),
        Period(date(2026, 8, 1), date(2026, 8, 31)),
    )
    assert got.movements[0].category == "Dining"
    assert got.movements[0].change == Money.parse("-290.00")  # less went out
    assert got.movements[0].rose is False
    assert got.movements[0].percent == pytest.approx(-96.667, abs=0.01)


def test_something_brand_new_has_no_percentage():
    """Dividing by a zero base would be an infinity, not an insight."""
    move = Movement("Travel", Money.parse("-400.00"), Money.parse("0.00"))
    assert move.is_new is True
    assert move.percent is None


def test_something_that_stopped_is_flagged():
    move = Movement("Gym", Money.parse("0.00"), Money.parse("-40.00"))
    assert move.is_gone is True


def test_with_no_comparison_there_are_no_movements():
    txs = [_tx("2026-09-05", "-10.00", "a")]
    got = summarise(txs, {"a": "Dining"}, Period(date(2026, 9, 1), date(2026, 9, 30)))
    assert got.movements == ()
    assert got.previous_net is None
    assert got.categories[0][0] == "Dining"


def test_an_empty_period_says_zero_rather_than_failing():
    got = summarise(
        [],
        {},
        Period(date(2026, 9, 1), date(2026, 9, 30)),
        Period(date(2026, 8, 1), date(2026, 8, 31)),
    )
    assert got.count == 0
    assert got.net == Money(0)
    assert got.movements == ()
    assert got.daily_burn == Money(0)


def test_the_daily_burn_is_comparable_across_unequal_periods():
    """Per day, not per period: a custom range and the window before it
    need not be the same length, and totals would not be comparable."""
    txs = [_tx("2026-09-01", "-100.00", "a"), _tx("2026-08-20", "-100.00", "b")]
    got = summarise(
        txs,
        {},
        Period(date(2026, 9, 1), date(2026, 9, 10)),  # 10 days
        Period(date(2026, 8, 12), date(2026, 8, 31)),  # 20 days
    )
    assert got.daily_burn == Money.parse("10.00")
    assert got.previous_daily_burn == Money.parse("5.00")


def test_the_previous_burn_keeps_the_ledgers_currency():
    txs = [
        Transaction(
            id="e",
            account_id="a1",
            date=date(2026, 8, 5),
            amount=Money.parse("-50.00", "EUR"),
            description="x",
            merchant="X",
        )
    ]
    got = summarise(
        txs,
        {},
        Period(date(2026, 9, 1), date(2026, 9, 30)),
        Period(date(2026, 8, 1), date(2026, 8, 31)),
    )
    assert got.previous_daily_burn is not None
    assert got.previous_daily_burn.currency == "EUR"


def test_with_no_previous_period_there_is_no_previous_burn():
    got = summarise([], {}, Period(date(2026, 9, 1), date(2026, 9, 30)))
    assert got.previous_daily_burn is None
