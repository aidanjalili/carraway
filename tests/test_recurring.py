"""Tests for subscription detection - Carraway's headline feature."""

import uuid
from datetime import date, timedelta

from carraway.analysis.recurring import _build_series, detect, normalise_merchant, stale
from carraway.core.models import Transaction
from carraway.core.money import Money


def make_tx(day: date, amount: str, description: str, account="acct1") -> Transaction:
    return Transaction(
        id=uuid.uuid4().hex,
        account_id=account,
        date=day,
        amount=Money.parse(amount),
        description=description,
        merchant=normalise_merchant(description),
    )


def monthly_series(description, amount, start: date, count: int, day_jitter=0):
    """Build `count` monthly charges, optionally wobbling the billing date."""
    out = []
    for i in range(count):
        month = start.month + i
        year = start.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        jitter = timedelta(days=(i % 3) * day_jitter)
        out.append(make_tx(date(year, month, start.day) + jitter, amount, description))
    return out


def test_detects_a_clean_monthly_subscription():
    txs = monthly_series("NETFLIX.COM 866-579-7172 CA", "-15.49", date(2026, 1, 14), 8)
    series = detect(txs)

    assert len(series) == 1
    found = series[0]
    assert found.cadence == "monthly"
    assert found.occurrences == 8
    assert found.typical_amount == Money.parse("-15.49")
    assert found.confidence > 0.85
    assert not found.amount_varies


def test_ignores_irregular_one_off_spending():
    # Coffee bought on scattered days is not a subscription, even though the
    # merchant repeats often.
    txs = [
        make_tx(date(2026, 1, d), "-4.75", "SQ *BLUE BOTTLE COFFEE")
        for d in (3, 4, 11, 12, 13, 27, 28)
    ]
    assert detect(txs) == []


def test_weekly_and_yearly_cadences():
    weekly = [
        make_tx(date(2026, 1, 5) + timedelta(days=7 * i), "-11.00", "THE GYM MEMBERSHIP")
        for i in range(10)
    ]
    yearly = [
        make_tx(date(2023, 3, 2), "-99.00", "DOMAIN RENEWAL LLC"),
        make_tx(date(2024, 3, 4), "-99.00", "DOMAIN RENEWAL LLC"),
        make_tx(date(2025, 3, 1), "-109.00", "DOMAIN RENEWAL LLC"),
        make_tx(date(2026, 3, 3), "-109.00", "DOMAIN RENEWAL LLC"),
    ]
    cadences = {s.merchant: s.cadence for s in detect(weekly + yearly)}
    assert cadences["The Gym Membership"] == "weekly"
    assert cadences["Domain Renewal"] == "yearly"  # LLC is not part of the identity


def test_variable_amount_bill_is_still_detected():
    # An electricity bill swings with usage but is obviously recurring.
    amounts = ["-84.12", "-142.55", "-61.30", "-178.90", "-95.44", "-120.05"]
    txs = [
        make_tx(date(2026, 1 + i, 18), amt, "CITY POWER AND LIGHT") for i, amt in enumerate(amounts)
    ]
    series = detect(txs)

    assert len(series) == 1
    assert series[0].cadence == "monthly"
    assert series[0].amount_varies is True


def test_income_excluded_unless_requested():
    paychecks = [
        make_tx(date(2026, 1, 2) + timedelta(days=14 * i), "2400.00", "ACME CORP PAYROLL")
        for i in range(8)
    ]
    assert detect(paychecks) == []

    found = detect(paychecks, include_inflows=True)
    assert len(found) == 1
    assert found[0].cadence == "biweekly"


def test_transfers_are_skipped():
    txs = monthly_series("TRANSFER TO SAVINGS", "-500.00", date(2026, 1, 5), 6)
    for tx in txs:
        tx.transfer_group = "grp1"
    assert detect(txs) == []


def test_predicts_the_next_charge_date():
    txs = monthly_series("SPOTIFY USA", "-11.99", date(2026, 1, 20), 6)
    found = detect(txs)[0]

    assert found.next_expected is not None
    assert found.next_expected > found.last_seen
    assert found.next_expected.day == 20


def test_annualised_cost():
    txs = monthly_series("NETFLIX.COM", "-15.49", date(2026, 1, 14), 6)
    assert detect(txs)[0].annualised == Money.parse("185.88")  # 15.49 * 12


def test_stale_series_are_flagged():
    txs = monthly_series("CANCELLED GYM", "-40.00", date(2025, 1, 8), 5)
    series = detect(txs)
    # Long after the last charge, the series should read as overdue.
    assert stale(series, date(2026, 8, 1)) == series
    # Right after the last charge, it should not.
    assert stale(series, series[0].last_seen) == []


def test_merchant_normalisation():
    blue_bottle = normalise_merchant("SQ *BLUE BOTTLE #402 SAN FRANCISCO CA")
    assert blue_bottle == "BLUE BOTTLE SAN FRANCISCO"
    assert normalise_merchant("NETFLIX.COM 866-579-7172 CA") == "NETFLIX"
    # All the ways one company appears across real statements must agree.
    assert normalise_merchant("NETFLIX, INC. 186-65797172 CA") == "NETFLIX"
    assert normalise_merchant("Netflix 1 8445052993 CA") == "NETFLIX"
    assert normalise_merchant("NETFLIX.COM NETFLIX.COM CA") == "NETFLIX"
    # Different noise around the same merchant must collapse to one key.
    a = normalise_merchant("POS DEBIT SQ *JOES PIZZA 05/14")
    b = normalise_merchant("SQ *JOES PIZZA #1123")
    assert a == b == "JOES PIZZA"


def test_per_purchase_order_codes_collapse_to_one_merchant():
    # Amazon stamps a different order code on every charge. If those survive
    # normalisation the merchant fragments into one group per purchase and no
    # pattern is ever found - so this is a detection bug, not a cosmetic one.
    codes = ["2K4LM9DR3", "9XQ2P1TTY", "7HH3KD00Z", "1PZ9QW4RM", "5TT8LN2XC", "3BB6VC9KD"]
    txs = [
        make_tx(date(2026, 1 + i, 26), "-8.99", f"AMZN Mktp US*{code} AMZN.COM/BILL")
        for i, code in enumerate(codes)
    ]
    assert len({t.merchant for t in txs}) == 1

    series = detect(txs)
    assert len(series) == 1
    assert series[0].cadence == "monthly"


def test_real_merchant_names_survive_order_code_stripping():
    # The order-code rule must not eat legitimate names that contain digits.
    assert normalise_merchant("7-ELEVEN 33412 SAN JOSE CA") == "7-ELEVEN SAN JOSE"
    assert normalise_merchant("MACYS EAST 0234") == "MACYS EAST"


def test_one_merchant_billing_several_things():
    # Found on real data: a letting agent takes rent every month and parking
    # and one-off fees under the same descriptor. Pooled together the gaps look
    # chaotic, the whole merchant scores below threshold, and a perfectly
    # regular $948 rent vanishes from the results entirely.
    rent = [
        make_tx(date(2026, month, 3), "-948.00", "MILL DISTRICT AP RENT") for month in range(1, 13)
    ]
    extras = [
        make_tx(day, amount, "MILL DISTRICT AP RENT")
        for day, amount in [
            (date(2026, 1, 4), "-45.00"),
            (date(2026, 1, 26), "-100.00"),
            (date(2026, 3, 2), "-1624.00"),
            (date(2026, 3, 25), "-45.00"),
            (date(2026, 5, 11), "-100.00"),
            (date(2026, 6, 19), "-45.00"),
            (date(2026, 8, 14), "-260.00"),
            (date(2026, 9, 27), "-45.00"),
            (date(2026, 11, 8), "-100.00"),
        ]
    ]
    # The merchant as a whole must genuinely fail, or this tests nothing.
    assert _build_series(rent + extras, "MILL DISTRICT AP RENT", "acct1", 3, 0.55) is None

    found = [s for s in detect(rent + extras) if s.typical_amount == Money.parse("-948.00")]
    assert len(found) == 1
    assert found[0].cadence == "monthly"
    assert found[0].occurrences == 12
    assert found[0].confidence > 0.8


def test_clustering_does_not_split_a_merchant_that_already_scores_well():
    # Netflix changed price partway through. The whole-merchant pass succeeds,
    # so clustering must never run and split it into two weaker series.
    txs = monthly_series("NETFLIX.COM", "-15.49", date(2026, 1, 14), 5)
    txs += monthly_series("NETFLIX.COM", "-17.99", date(2026, 6, 14), 5)

    series = detect(txs)
    assert len(series) == 1
    assert series[0].occurrences == 10


def test_usage_triggered_charges_are_still_rejected():
    # An E-ZPass auto-replenish is a fixed $10 every time, but it fires when
    # the balance runs low rather than on a schedule. Identical amounts must
    # not be enough on their own to call something recurring.
    days = [4, 6, 14, 169, 277, 472, 480, 528, 533, 574]
    txs = [
        make_tx(date(2025, 1, 1) + timedelta(days=offset), "-10.00", "E-ZPASS MA PPD")
        for offset in days
    ]
    assert detect(txs) == []


def test_yearly_subscription_detected_from_two_charges():
    # An annual subscription cannot reach three charges inside a two-year
    # statement history, so requiring three made every yearly magazine, domain
    # and insurance renewal structurally invisible. Real data had five such
    # magazines, none of them detected.
    txs = [
        make_tx(date(2024, 12, 30), "-121.98", "INST XFER CONDE NAST WEB"),
        make_tx(date(2025, 12, 30), "-121.97", "INST XFER CONDE NAST WEB"),
    ]
    series = detect(txs)

    assert len(series) == 1
    assert series[0].cadence == "yearly"
    assert series[0].occurrences == 2
    assert series[0].next_expected == date(2026, 12, 30)


def test_two_short_interval_charges_are_still_a_coincidence():
    # The relaxation is only for long cadences. Two charges a month apart, or a
    # week apart, remain exactly the coincidence they always were.
    monthly = [
        make_tx(date(2026, 1, 14), "-15.49", "SOME SHOP"),
        make_tx(date(2026, 2, 14), "-15.49", "SOME SHOP"),
    ]
    assert detect(monthly) == []

    weekly = [
        make_tx(date(2026, 1, 5), "-11.00", "ANOTHER SHOP"),
        make_tx(date(2026, 1, 12), "-11.00", "ANOTHER SHOP"),
    ]
    assert detect(weekly) == []


def test_two_charge_series_scores_below_a_well_evidenced_one():
    # A single interval is real evidence but thin, and the confidence figure
    # should say so rather than presenting one gap as proof.
    thin = detect(
        [
            make_tx(date(2024, 3, 2), "-99.00", "DOMAIN RENEWAL"),
            make_tx(date(2025, 3, 2), "-99.00", "DOMAIN RENEWAL"),
        ]
    )
    thick = detect([make_tx(date(2023 + i, 3, 2), "-99.00", "OTHER RENEWAL") for i in range(4)])
    assert thin and thick
    assert thin[0].confidence < thick[0].confidence


def test_refunds_are_not_part_of_the_series_they_reverse():
    # Real data: three quarterly charges, then three refunds on the day the
    # user cancelled. Pooled into one series the median amount is $0.00 and a
    # genuine subscription disappears behind a zero.
    charges = [
        make_tx(date(2024, 11, 4), "-48.93", "MOJOCH.COM LONDON"),
        make_tx(date(2025, 2, 4), "-48.93", "MOJOCH.COM LONDON"),
        make_tx(date(2025, 5, 5), "-48.93", "MOJOCH.COM LONDON"),
    ]
    refunds = [make_tx(date(2025, 7, 10), "48.93", "MOJOCH.COM LONDON") for _ in range(3)]

    series = detect(charges + refunds, include_inflows=True)
    spending = [s for s in series if s.typical_amount.minor < 0]
    assert len(spending) == 1
    assert spending[0].typical_amount == Money.parse("-48.93")
    assert spending[0].occurrences == 3


def test_a_monthly_charge_keeps_its_day_of_month():
    from carraway.analysis.recurring import advance

    # Adding 30 days walks a charge backwards through the year — the 30th
    # becomes the 29th, then the 28th, and is four days wrong within six
    # months. Calendar arithmetic holds the day instead.
    when = date(2026, 8, 30)
    for _ in range(5):
        when = advance(when, "monthly", 30)
        assert when.day == 30, when


def test_a_short_month_clamps_without_losing_the_day_after():
    from carraway.analysis.recurring import advance

    # A charge on the 31st has to become the 28th in February and then go
    # back to the 31st, rather than staying on the 28th forever.
    assert advance(date(2026, 1, 31), "monthly", 31) == date(2026, 2, 28)
    assert advance(date(2026, 2, 28), "monthly", 31) == date(2026, 3, 31)


def test_projecting_forward_lands_on_or_after_today():
    from carraway.analysis.recurring import project_from

    started = date(2026, 8, 30)
    today = date(2026, 11, 15)
    # Rolled forward one period at a time, so the calendar rules apply at
    # every step rather than only the last.
    assert project_from(started, "monthly", today) == date(2026, 11, 30)
    assert project_from(started, "yearly", today) == date(2027, 8, 30)
    # A start date in the future is already the next occurrence.
    assert project_from(date(2027, 1, 1), "monthly", today) == date(2027, 1, 1)


def test_projection_terminates_on_an_unknown_cadence():
    from carraway.analysis.recurring import project_from

    # An unrecognised cadence must not spin: the loop is bounded.
    assert project_from(date(2020, 1, 1), "fortnightly-ish", date(2026, 1, 1))
