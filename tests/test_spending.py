"""Tests for spending aggregation.

The properties worth holding here are the ones a chart quietly gets wrong: a
period with no spending has to survive as a zero rather than vanish, money the
user only moved between their own accounts must never appear as spending, and
the slices of a pie have to add up to the total printed beside it.
"""

import uuid
from datetime import date

from carraway.analysis.categorize import categorize_all
from carraway.analysis.spending import (
    Bucket,
    CategoryChange,
    buckets,
    category_totals,
    compare,
    period_label,
    period_start,
    top_merchants,
)
from carraway.core.models import Transaction
from carraway.core.money import Money


def make_tx(
    day: date,
    amount: str,
    description="Something",
    category="",
    merchant="",
    group="",
    account_id="chk",
) -> Transaction:
    return Transaction(
        id=uuid.uuid4().hex,
        account_id=account_id,
        date=day,
        amount=Money.parse(amount),
        description=description,
        merchant=merchant,
        category=category,
        transfer_group=group,
    )


def labels(rows) -> list[str]:
    return [b.label for b in rows]


def totals(rows) -> list[Money]:
    return [b.total for b in rows]


# -- bucketing -----------------------------------------------------------


def test_monthly_buckets_are_oldest_first_and_labelled():
    txs = [
        make_tx(date(2026, 1, 6), "-25.00", category="Dining"),
        make_tx(date(2026, 1, 20), "-40.00", category="Groceries"),
        make_tx(date(2026, 2, 3), "-10.00", category="Dining"),
    ]
    rows = buckets(txs, period="monthly")

    assert labels(rows) == ["2026-01", "2026-02"]
    assert totals(rows) == [Money.parse("65.00"), Money.parse("10.00")]
    assert rows[0].start == date(2026, 1, 1)
    assert rows[0].end == date(2026, 2, 1)  # exclusive: the first day of the next period
    assert rows[0].last_day == date(2026, 1, 31)
    assert rows[0].by_category == {
        "Dining": Money.parse("25.00"),
        "Groceries": Money.parse("40.00"),
    }


def test_daily_buckets():
    txs = [
        make_tx(date(2026, 8, 3), "-12.00", category="Dining"),
        make_tx(date(2026, 8, 3), "-8.00", category="Dining"),
        make_tx(date(2026, 8, 5), "-30.00", category="Groceries"),
    ]
    rows = buckets(txs, period="daily")

    assert labels(rows) == ["2026-08-03", "2026-08-04", "2026-08-05"]
    assert totals(rows) == [Money.parse("20.00"), Money.zero(), Money.parse("30.00")]
    assert rows[0].end - rows[0].start == date(2026, 8, 4) - date(2026, 8, 3)


def test_weekly_buckets_start_on_monday():
    # 2026-08-03 is a Monday. A Sunday purchase belongs to the week that began
    # six days earlier, not to the one starting the next morning.
    txs = [
        make_tx(date(2026, 8, 3), "-10.00", category="Dining"),  # Monday
        make_tx(date(2026, 8, 9), "-20.00", category="Dining"),  # Sunday, same week
        make_tx(date(2026, 8, 10), "-40.00", category="Dining"),  # the next Monday
    ]
    rows = buckets(txs, period="weekly")

    assert [b.start for b in rows] == [date(2026, 8, 3), date(2026, 8, 10)]
    assert [b.end for b in rows] == [date(2026, 8, 10), date(2026, 8, 17)]
    assert totals(rows) == [Money.parse("30.00"), Money.parse("40.00")]
    assert labels(rows) == ["Aug 3-9", "Aug 10-16"]

    assert period_start(date(2026, 8, 9), "weekly") == date(2026, 8, 3)
    # A week across a month boundary names both months so it cannot be misread.
    assert period_label(date(2026, 9, 1), "weekly") == "Aug 31-Sep 6"


def test_yearly_buckets():
    txs = [
        make_tx(date(2024, 5, 1), "-100.00", category="Travel"),
        make_tx(date(2026, 2, 1), "-50.00", category="Travel"),
    ]
    rows = buckets(txs, period="yearly")

    assert labels(rows) == ["2024", "2025", "2026"]
    assert totals(rows) == [Money.parse("100.00"), Money.zero(), Money.parse("50.00")]
    assert rows[0].start == date(2024, 1, 1)
    assert rows[0].end == date(2025, 1, 1)


def test_unknown_period_is_rejected():
    try:
        buckets([], period="fortnightly")
    except ValueError as exc:
        assert "fortnightly" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected a ValueError for an unknown period")


# -- the empty periods ---------------------------------------------------


def test_quiet_months_appear_as_zero_buckets():
    # A chart that skips February re-spaces its own axis and tells the user
    # they spent money every month.
    txs = [
        make_tx(date(2026, 1, 10), "-100.00", category="Dining"),
        make_tx(date(2026, 4, 10), "-50.00", category="Dining"),
    ]
    rows = buckets(txs, period="monthly")

    assert labels(rows) == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert rows[1].total == Money.zero()
    assert rows[1].by_category == {}
    assert rows[1].is_empty
    assert not rows[0].is_empty


def test_quiet_weeks_appear_as_zero_buckets():
    txs = [
        make_tx(date(2026, 8, 3), "-10.00", category="Dining"),
        make_tx(date(2026, 8, 24), "-10.00", category="Dining"),
    ]
    rows = buckets(txs, period="weekly")

    assert labels(rows) == ["Aug 3-9", "Aug 10-16", "Aug 17-23", "Aug 24-30"]
    assert [b.is_empty for b in rows] == [False, True, True, False]
    # Consecutive buckets tile the timeline: no gap, no overlap.
    assert all(a.end == b.start for a, b in zip(rows, rows[1:], strict=False))


def test_a_range_with_no_transactions_is_all_zeros():
    # Given both bounds, "nothing here yet" is drawable; drawing nothing is not.
    rows = buckets([], period="monthly", start=date(2026, 1, 1), end=date(2026, 3, 31))

    assert labels(rows) == ["2026-01", "2026-02", "2026-03"]
    assert all(b.is_empty and b.total == Money.zero() for b in rows)


# -- what is not spending ------------------------------------------------


def test_transfers_are_excluded():
    # $500 moved to savings is not $500 spent.
    txs = [
        make_tx(date(2026, 1, 5), "-500.00", "Transfer to savings", group="mv1"),
        make_tx(
            date(2026, 1, 5), "500.00", "Transfer from chequing", group="mv1", account_id="sav"
        ),
        make_tx(date(2026, 1, 6), "-30.00", category="Dining"),
    ]
    rows = buckets(txs, period="monthly")

    assert rows[0].total == Money.parse("30.00")
    assert rows[0].by_category == {"Dining": Money.parse("30.00")}


def test_a_category_of_transfer_is_excluded_even_without_a_matched_half():
    # A card autopay often imports with only one side present. It is still the
    # user's own money moving, and the categoriser's verdict is trusted.
    txs = [
        make_tx(date(2026, 1, 5), "-800.00", "CHASE CREDIT CRD AUTOPAY"),
        make_tx(date(2026, 1, 6), "-30.00", "BLUE BOTTLE"),
    ]
    categories = categorize_all(txs)
    assert categories[0] == "Transfer"

    rows = buckets(txs, period="monthly", categories=categories)
    assert rows[0].total == Money.parse("30.00")
    assert category_totals(txs, categories=categories) == [("Dining", Money.parse("30.00"), 1)]


def test_income_is_excluded_by_default_and_included_when_asked():
    txs = [
        make_tx(date(2026, 1, 2), "2400.00", "Payroll", category="Income"),
        make_tx(date(2026, 1, 5), "-400.00", "Rent", category="Rent/Mortgage"),
    ]

    spending_only = buckets(txs, period="monthly")
    assert spending_only[0].total == Money.parse("400.00")
    assert "Income" not in spending_only[0].by_category

    with_income = buckets(txs, period="monthly", include_income=True)
    # Income subtracts, so the total becomes net outflow: $400 out, $2,400 in.
    assert with_income[0].total == Money.parse("-2000.00")
    assert with_income[0].by_category["Income"] == Money.parse("-2400.00")


def test_a_refund_only_nets_off_when_income_is_included():
    txs = [
        make_tx(date(2026, 1, 4), "-200.00", "Uniqlo", category="Shopping"),
        make_tx(date(2026, 1, 20), "200.00", "Uniqlo refund", category="Shopping"),
    ]

    assert buckets(txs, period="monthly")[0].total == Money.parse("200.00")

    netted = buckets(txs, period="monthly", include_income=True)[0]
    assert netted.total == Money.zero()
    # Zero because it was a wash, which is not the same as nothing happening.
    assert not netted.is_empty


# -- categories ----------------------------------------------------------


def test_categories_fall_back_to_the_transaction_then_uncategorized():
    txs = [
        make_tx(date(2026, 1, 4), "-10.00", category="Dining"),
        make_tx(date(2026, 1, 5), "-20.00"),  # no category anywhere
    ]
    by_category = buckets(txs, period="monthly")[0].by_category

    assert by_category == {"Dining": Money.parse("10.00"), "Uncategorized": Money.parse("20.00")}


def test_a_mismatched_categories_list_is_rejected():
    # Slipping by one would file every remaining purchase under its
    # neighbour's category and still look plausible on screen.
    txs = [make_tx(date(2026, 1, 4), "-10.00"), make_tx(date(2026, 1, 5), "-20.00")]
    try:
        buckets(txs, categories=["Dining"])
    except ValueError as exc:
        assert "parallel" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected a ValueError for a short categories list")


def test_category_totals_are_ordered_and_exact():
    txs = [
        make_tx(date(2026, 1, 4), "-12.34", category="Dining"),
        make_tx(date(2026, 1, 5), "-45.67", category="Groceries"),
        make_tx(date(2026, 1, 6), "-0.99", category="Dining"),
        make_tx(date(2026, 1, 7), "-89.01", category="Rent/Mortgage"),
    ]
    rows = category_totals(txs)

    assert rows == [
        ("Rent/Mortgage", Money.parse("89.01"), 1),
        ("Groceries", Money.parse("45.67"), 1),
        ("Dining", Money.parse("13.33"), 2),
    ]
    # The pie's slices add up to the number printed beside it, to the cent.
    whole = buckets(txs, period="monthly")[0].total
    assert sum(m.minor for _, m, _ in rows) == whole.minor == 14801


def test_category_totals_ties_break_deterministically():
    txs = [
        make_tx(date(2026, 1, 4), "-20.00", category="Zebra"),
        make_tx(date(2026, 1, 5), "-20.00", category="Alpha"),
        make_tx(date(2026, 1, 6), "-10.00", category="Beta"),
        make_tx(date(2026, 1, 7), "-10.00", category="Beta"),
    ]
    # Equal totals order by count, then by name, so a chart's colours hold still.
    assert [c for c, _, _ in category_totals(txs)] == ["Beta", "Alpha", "Zebra"]


def test_bucket_total_equals_the_sum_of_its_categories():
    txs = [
        make_tx(date(2026, 3, day), f"-{day}.33", category=f"Cat{day % 3}") for day in range(1, 29)
    ]
    for bucket in buckets(txs, period="weekly"):
        assert bucket.total.minor == sum(m.minor for m in bucket.by_category.values())


# -- merchants -----------------------------------------------------------


def test_top_merchants_ranks_by_spend_and_respects_the_limit():
    txs = [
        make_tx(date(2026, 1, 4), "-4.00", merchant="Blue Bottle"),
        make_tx(date(2026, 1, 5), "-6.00", merchant="Blue Bottle"),
        make_tx(date(2026, 1, 6), "-95.00", merchant="Trader Joe's"),
        make_tx(date(2026, 1, 7), "-3.00", merchant="Philz"),
    ]
    rows = top_merchants(txs)

    assert rows == [
        ("Trader Joe's", Money.parse("95.00"), 1),
        ("Blue Bottle", Money.parse("10.00"), 2),
        ("Philz", Money.parse("3.00"), 1),
    ]
    assert top_merchants(txs, limit=1) == rows[:1]
    assert top_merchants(txs, limit=0) == []


def test_top_merchants_groups_one_shop_into_one_row():
    # Shouted by the bank, title-cased by a user edit, and buried in processor
    # noise: still one coffee shop.
    txs = [
        make_tx(date(2026, 1, 4), "-4.00", "SQ *BLUE BOTTLE #402 SF"),
        make_tx(date(2026, 1, 5), "-6.00", "whatever", merchant="BLUE BOTTLE"),
        make_tx(date(2026, 1, 6), "-5.00", "whatever", merchant="Blue Bottle"),
    ]
    assert top_merchants(txs) == [("Blue Bottle", Money.parse("15.00"), 3)]


def test_top_merchants_excludes_a_transfer_the_categoriser_caught():
    # The half of a card autopay that imports on its own has no transfer_group
    # to hide it, and it is big enough to own the top of the table.
    txs = [
        make_tx(date(2026, 1, 5), "-800.00", "CHASE CREDIT CRD AUTOPAY"),
        make_tx(date(2026, 1, 6), "-30.00", "BLUE BOTTLE"),
    ]
    categories = categorize_all(txs)

    assert top_merchants(txs)[0][0] == "Chase Credit Crd Autopay"  # without the labels
    assert top_merchants(txs, categories=categories) == [("Blue Bottle", Money.parse("30.00"), 1)]


def test_top_merchants_excludes_transfers_and_income():
    txs = [
        make_tx(date(2026, 1, 2), "2400.00", "Payroll", merchant="Acme Payroll"),
        make_tx(date(2026, 1, 3), "-500.00", "To savings", merchant="Savings", group="mv1"),
        make_tx(date(2026, 1, 4), "-4.00", merchant="Blue Bottle"),
    ]
    assert top_merchants(txs) == [("Blue Bottle", Money.parse("4.00"), 1)]


# -- date ranges ---------------------------------------------------------


def test_a_date_range_narrows_every_view():
    txs = [
        make_tx(date(2026, 1, 15), "-100.00", category="Dining", merchant="Old"),
        make_tx(date(2026, 2, 10), "-30.00", category="Dining", merchant="Kept"),
        make_tx(date(2026, 3, 15), "-70.00", category="Dining", merchant="New"),
    ]
    window = {"start": date(2026, 2, 1), "end": date(2026, 2, 28)}

    rows = buckets(txs, period="monthly", **window)
    assert labels(rows) == ["2026-02"]
    assert rows[0].total == Money.parse("30.00")

    assert category_totals(txs, **window) == [("Dining", Money.parse("30.00"), 1)]
    assert top_merchants(txs, **window) == [("Kept", Money.parse("30.00"), 1)]


def test_range_bounds_are_inclusive_on_both_ends():
    txs = [
        make_tx(date(2026, 2, 1), "-10.00", category="Dining"),
        make_tx(date(2026, 2, 28), "-10.00", category="Dining"),
    ]
    rows = buckets(txs, period="monthly", start=date(2026, 2, 1), end=date(2026, 2, 28))

    assert rows[0].total == Money.parse("20.00")


def test_a_range_wider_than_the_data_still_spans_the_range():
    txs = [make_tx(date(2026, 2, 10), "-30.00", category="Dining")]
    rows = buckets(txs, period="monthly", start=date(2026, 1, 1), end=date(2026, 4, 30))

    assert labels(rows) == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert totals(rows) == [Money.zero(), Money.parse("30.00"), Money.zero(), Money.zero()]


# -- compare -------------------------------------------------------------


def test_compare_reports_the_movement_per_category():
    january, february = buckets(
        [
            make_tx(date(2026, 1, 4), "-100.00", category="Dining"),
            make_tx(date(2026, 1, 5), "-200.00", category="Groceries"),
            make_tx(date(2026, 2, 4), "-220.00", category="Dining"),
            make_tx(date(2026, 2, 5), "-150.00", category="Groceries"),
        ],
        period="monthly",
    )
    result = compare(january, february)

    assert result.change == Money.parse("70.00")
    assert result.percent_change == round(70 / 300 * 100, 2)

    moves = {c.category: c for c in result.categories}
    assert moves["Dining"].change == Money.parse("120.00")  # "Dining is up $120"
    assert moves["Dining"].percent_change == 120.0
    assert moves["Groceries"].change == Money.parse("-50.00")
    assert moves["Groceries"].percent_change == -25.0
    # Biggest mover first, by size of the change rather than its direction.
    assert [c.category for c in result.categories] == ["Dining", "Groceries"]


def test_compare_percent_from_zero_is_undefined_not_infinite():
    # A category that did not exist last month is not an infinite increase;
    # "Pets +inf%" is a bug on screen.
    empty = Bucket(date(2026, 1, 1), date(2026, 2, 1), "2026-01", Money.zero(), {})
    spent = Bucket(
        date(2026, 2, 1),
        date(2026, 3, 1),
        "2026-02",
        Money.parse("40.00"),
        {"Pets": Money.parse("40.00")},
    )
    result = compare(empty, spent)

    assert result.change == Money.parse("40.00")
    assert result.percent_change is None
    assert result.categories[0].category == "Pets"
    assert result.categories[0].before == Money.zero()
    assert result.categories[0].percent_change is None


def test_compare_reports_a_category_that_stopped():
    a = Bucket(
        date(2026, 1, 1),
        date(2026, 2, 1),
        "2026-01",
        Money.parse("60.00"),
        {"Pets": Money.parse("60.00")},
    )
    b = Bucket(date(2026, 2, 1), date(2026, 3, 1), "2026-02", Money.zero(), {})
    result = compare(a, b)

    assert result.change == Money.parse("-60.00")
    assert result.percent_change == -100.0
    assert result.categories == [
        CategoryChange("Pets", Money.parse("60.00"), Money.zero(), Money.parse("-60.00"), -100.0)
    ]


def test_compare_of_two_identical_buckets_is_all_zero():
    bucket = Bucket(
        date(2026, 1, 1),
        date(2026, 2, 1),
        "2026-01",
        Money.parse("10.00"),
        {"Dining": Money.parse("10.00")},
    )
    result = compare(bucket, bucket)

    assert result.change == Money.zero()
    assert result.percent_change == 0.0
    assert result.categories[0].change == Money.zero()


# -- empty input ---------------------------------------------------------


def test_empty_input_produces_empty_output():
    assert buckets([]) == []
    assert category_totals([]) == []
    assert top_merchants([]) == []


def test_input_with_nothing_spendable_produces_empty_output():
    # Transfers and income only: there is no range of spending to bucket.
    txs = [
        make_tx(date(2026, 1, 2), "2400.00", "Payroll", category="Income"),
        make_tx(date(2026, 1, 3), "-500.00", "To savings", group="mv1"),
    ]
    assert buckets(txs) == []
    assert category_totals(txs) == []
