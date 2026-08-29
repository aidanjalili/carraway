"""Tests for subscription detection - Carraway's headline feature."""

import uuid
from datetime import date, timedelta

from carraway.analysis.recurring import detect, normalise_merchant, stale
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
    assert cadences["Domain Renewal Llc"] == "yearly"


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
    assert normalise_merchant("NETFLIX.COM 866-579-7172 CA") == "NETFLIX.COM"
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
