"""Tests for price-change detection.

The critical case is not the subscription that goes up; it is the utility bill
that merely varies and must produce nothing at all.
"""

import uuid
from datetime import date, timedelta

from carraway.analysis import recurring
from carraway.analysis.price_changes import find_price_changes, summarise
from carraway.core.models import Transaction
from carraway.core.money import Money


def make_tx(day: date, amount: str, description: str, account="acct1") -> Transaction:
    return Transaction(
        id=uuid.uuid4().hex,
        account_id=account,
        date=day,
        amount=Money.parse(amount),
        description=description,
        merchant=recurring.normalise_merchant(description),
    )


def monthly(description: str, amounts: list[str], start: date, account="acct1"):
    """One charge a month from `start`, one per amount. Keep start.day <= 28."""
    out = []
    for i, amount in enumerate(amounts):
        month = start.month + i
        year = start.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        out.append(make_tx(date(year, month, start.day), amount, description, account))
    return out


def every(days: int, description: str, amounts: list[str], start: date, account="acct1"):
    out = []
    for i, amount in enumerate(amounts):
        out.append(make_tx(start + timedelta(days=days * i), amount, description, account))
    return out


def annually(description: str, amounts: list[str], start: date, account="acct1"):
    out = []
    for i, amount in enumerate(amounts):
        out.append(make_tx(start.replace(year=start.year + i), amount, description, account))
    return out


def test_clean_price_rise():
    txs = monthly(
        "NETFLIX.COM 866-579-7172 CA",
        ["-15.49"] * 3 + ["-17.99"] * 3,
        date(2025, 12, 14),
    )
    changes = find_price_changes(txs)

    assert len(changes) == 1
    change = changes[0]
    assert change.merchant == "Netflix"
    assert change.direction == "increase"
    assert change.old_amount == Money.parse("-15.49")
    assert change.new_amount == Money.parse("-17.99")
    assert change.changed_on == date(2026, 3, 14)
    assert change.transactions_before == 3
    assert change.transactions_after == 3
    assert change.confidence > 0.9


def test_price_decrease():
    txs = monthly("SPOTIFY USA", ["-14.99"] * 4 + ["-11.99"] * 4, date(2025, 9, 20))
    changes = find_price_changes(txs)

    assert len(changes) == 1
    assert changes[0].direction == "decrease"
    assert changes[0].old_amount == Money.parse("-14.99")
    assert changes[0].new_amount == Money.parse("-11.99")
    # Paying $3 a month less is $36 a year staying in the account.
    assert changes[0].annual_impact == Money.parse("36.00")


def test_two_changes_over_three_years():
    txs = monthly(
        "THE GYM MEMBERSHIP",
        ["-29.99"] * 8 + ["-34.99"] * 8 + ["-39.99"] * 8,
        date(2024, 1, 5),
    )
    changes = sorted(find_price_changes(txs), key=lambda c: c.changed_on)

    assert len(changes) == 2
    assert [c.old_amount.minor for c in changes] == [-2999, -3499]
    assert [c.new_amount.minor for c in changes] == [-3499, -3999]
    assert [c.changed_on for c in changes] == [date(2024, 9, 5), date(2025, 5, 5)]
    assert all(c.direction == "increase" for c in changes)


def test_variable_utility_bill_is_not_a_price_change():
    # The false positive that would make the feature useless. This bill swings
    # by a factor of three with the weather and has never changed rate; every
    # possible split of it is noise on both sides.
    amounts = [
        "-84.12",
        "-142.55",
        "-61.30",
        "-178.90",
        "-95.44",
        "-120.05",
        "-71.20",
        "-155.60",
        "-88.75",
        "-133.10",
        "-66.40",
        "-168.25",
    ]
    txs = monthly("CITY POWER AND LIGHT", amounts, date(2025, 1, 18))

    assert find_price_changes(txs) == []
    # ...and it is genuinely a recurring series, so it really did reach the
    # detector rather than being filtered out upstream.
    series = recurring.detect(txs)
    assert len(series) == 1 and series[0].amount_varies
    assert find_price_changes(txs, series=series) == []


def test_seasonal_bill_is_not_a_price_change():
    # Harder than the swinging bill above, and the one that fooled an earlier
    # version of this detector: a heating bill is genuinely *stable* either
    # side of the seasons, so a boundary between summer and winter is a clean
    # step by every test except the one that notices the old price came back.
    winter_and_summer = [
        "-40.00",
        "-45.00",
        "-60.00",
        "-95.00",
        "-140.00",
        "-165.00",
        "-160.00",
        "-130.00",
        "-90.00",
        "-58.00",
        "-44.00",
        "-41.00",
    ]
    txs = monthly("GAS COMPANY", winter_and_summer * 2, date(2024, 5, 15))
    assert find_price_changes(txs) == []


def test_a_price_that_comes_back_is_not_a_price_change():
    # An introductory rate ending and later returning. Real, but it is a bill
    # that alternates rather than a price that moved, and reporting the rise
    # without the fall would overstate what it costs the user.
    txs = monthly("PROMO SUB", ["-10.00"] * 4 + ["-12.00"] * 4 + ["-10.00"] * 4, date(2024, 1, 15))
    assert find_price_changes(txs) == []


def test_two_scattered_charges_do_not_establish_a_new_price():
    # Variable spending at a repeat merchant — food delivery, say. The last two
    # orders happen to be cheaper than the three before them, which is a step
    # by shape but not a price: nobody set these amounts. A level built from
    # fewer than three charges has to be exactly repeated to count.
    txs = monthly(
        "SOME DELIVERY APP",
        ["-42.10", "-51.75", "-37.60", "-28.40", "-26.95"],
        date(2026, 2, 23),
    )
    assert find_price_changes(txs) == []

    # Two charges *are* enough when they agree exactly, which is what makes an
    # annual subscription's price change visible at all: it cannot reach three
    # charges at the new price inside four years of history.
    yearly = annually(
        "DOMAIN RENEWAL", ["-99.00", "-99.00", "-119.00", "-119.00"], date(2022, 4, 3)
    )
    assert len(find_price_changes(yearly)) == 1


def test_percentage_floor_ignores_trivial_drift():
    # 11 cents on $15.49 is tax or FX rounding, not a price change.
    txs = monthly("NEWS SUBSCRIPTION", ["-15.49"] * 4 + ["-15.60"] * 4, date(2025, 6, 10))
    assert find_price_changes(txs) == []


def test_absolute_floor_ignores_small_change_on_a_cheap_charge():
    # 75 cents clears 3% of $9.00 but is below the dollar floor.
    txs = monthly("TINY APP", ["-9.00"] * 4 + ["-9.75"] * 4, date(2025, 6, 10))
    assert find_price_changes(txs) == []


def test_annual_impact_uses_the_cadence():
    """The same $2.50 rise costs a different amount depending on the rhythm."""
    monthly_txs = monthly("MONTHLY BOX", ["-10.00"] * 4 + ["-12.50"] * 4, date(2025, 6, 12))
    weekly_txs = every(7, "WEEKLY BOX", ["-10.00"] * 4 + ["-12.50"] * 4, date(2026, 1, 5))
    yearly_txs = annually("YEARLY BOX", ["-10.00"] * 2 + ["-12.50"] * 2, date(2022, 4, 3))

    assert find_price_changes(monthly_txs)[0].cadence == "monthly"
    assert find_price_changes(monthly_txs)[0].annual_impact == Money.parse("-30.00")
    assert find_price_changes(weekly_txs)[0].cadence == "weekly"
    assert find_price_changes(weekly_txs)[0].annual_impact == Money.parse("-130.00")
    assert find_price_changes(yearly_txs)[0].cadence == "yearly"
    assert find_price_changes(yearly_txs)[0].annual_impact == Money.parse("-2.50")


def test_sign_convention_paying_more_is_an_increase():
    """The easiest thing in this module to get backwards."""
    txs = monthly("INSURANCE CO", ["-120.00"] * 3 + ["-145.00"] * 3, date(2025, 8, 22))
    change = find_price_changes(txs)[0]

    assert change.direction == "increase"
    # Both amounts stay negative: they are still money leaving the account.
    assert change.old_amount.minor < 0 and change.new_amount.minor < 0
    assert change.new_amount.minor < change.old_amount.minor
    # And the impact keeps the same convention: $300 a year more leaving.
    assert change.annual_impact == Money.parse("-300.00")
    assert change.annual_impact.minor < 0


def test_sign_convention_a_raise_on_income_is_also_an_increase():
    txs = every(
        14,
        "ACME CORP PAYROLL",
        ["2400.00"] * 4 + ["2650.00"] * 4,
        date(2026, 1, 2),
    )
    assert find_price_changes(txs) == []  # inflows are opt-in, as in recurring.detect

    change = find_price_changes(txs, include_inflows=True)[0]
    assert change.direction == "increase"
    assert change.cadence == "biweekly"
    assert change.annual_impact == Money.parse("6500.00")  # 250 * 26, money arriving


def test_series_too_short_to_judge():
    # A step needs two charges either side; one charge at the new price could
    # just as easily be a proration.
    txs = monthly("NETFLIX.COM", ["-15.49"] * 3 + ["-17.99"], date(2026, 1, 14))
    assert find_price_changes(txs) == []

    txs = monthly("NETFLIX.COM", ["-15.49", "-17.99"], date(2026, 1, 14))
    assert find_price_changes(txs) == []


def test_series_supply_the_cadence_and_confine_the_search():
    subscription = monthly("NETFLIX.COM", ["-15.49"] * 3 + ["-17.99"] * 3, date(2025, 12, 14))
    # Four coffees at four prices, at scattered intervals: a real step by
    # amount, but nothing that recurs, so passing series must exclude it.
    coffee = [
        make_tx(date(2026, 1, d), amt, "SQ *BLUE BOTTLE COFFEE")
        for d, amt in ((3, "-4.75"), (4, "-4.75"), (19, "-9.50"), (21, "-9.50"))
    ]
    txs = subscription + coffee

    series = recurring.detect(txs)
    changes = find_price_changes(txs, series=series)

    assert [c.merchant for c in changes] == ["Netflix"]
    assert changes[0].cadence == "monthly"
    assert changes[0].annual_impact == Money.parse("-30.00")

    # Without the series to lean on, the coffee is fair game — which is why
    # callers should pass one.
    assert len(find_price_changes(txs)) == 2


def test_ordered_by_annual_impact():
    small = monthly("SMALL SUB", ["-5.00"] * 4 + ["-7.00"] * 4, date(2025, 6, 3))
    large = monthly("BIG SUB", ["-50.00"] * 4 + ["-70.00"] * 4, date(2025, 6, 4))
    changes = find_price_changes(small + large)

    assert [c.merchant for c in changes] == ["Big Sub", "Small Sub"]
    assert changes[0].annual_impact == Money.parse("-240.00")
    assert changes[1].annual_impact == Money.parse("-24.00")


def test_summarise_reads_as_english():
    txs = monthly("NETFLIX.COM", ["-15.49"] * 3 + ["-17.99"] * 3, date(2025, 12, 14))
    text = summarise(find_price_changes(txs))

    assert text == "Netflix: $15.49 -> $17.99 monthly from 2026-03-14 (+$30.00/yr)"
    assert summarise([]) == "No price changes detected."
