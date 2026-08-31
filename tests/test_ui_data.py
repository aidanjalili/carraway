"""The Ledger the screens read from. No Qt needed — this is plain logic."""

from datetime import date

from carraway.core import db
from carraway.core.models import Account, AccountType, Transaction
from carraway.core.money import Money
from carraway.importers.csv_importer import import_csv
from carraway.ui.data import Ledger


def _statement(price_after: str) -> str:
    rows = ["Date,Description,Amount"]
    # Eleven months at one price, then four at another: a real price rise.
    for month in range(1, 12):
        rows.append(f"2025-{month:02d}-16,NETFLIX.COM 866-579-7172 CA,-8.43")
    for month in range(1, 5):
        rows.append(f"2026-{month:02d}-16,NETFLIX.COM 866-579-7172 CA,{price_after}")
    return "\n".join(rows) + "\n"


def _ledger(tmp_path, statement: str) -> Ledger:
    import io

    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Card", type=AccountType.CREDIT_CARD))
    txs, _ = import_csv(io.StringIO(statement), "a1")
    db.insert_transactions(conn, txs)
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def test_current_price_is_used_rather_than_the_median(tmp_path):
    # The median across a price change is the old price, because most of the
    # history sits before it. A view that answers "what am I paying" must not
    # quote a figure the user stopped paying months ago.
    ledger = _ledger(tmp_path, _statement("-9.48"))
    series = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())

    assert series.typical_amount == Money.parse("-8.43")  # the median
    assert ledger.current_amount(series) == Money.parse("-9.48")  # what they pay now
    assert ledger.current_annual(series) == Money.parse("113.76")  # 9.48 * 12


def test_a_series_with_no_price_change_is_left_alone(tmp_path):
    ledger = _ledger(tmp_path, _statement("-8.43"))
    series = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())

    assert ledger.price_change_for(series) is None
    assert ledger.current_amount(series) == series.typical_amount
    assert ledger.current_annual(series) == series.annualised


def test_transfers_are_matched_without_touching_the_database(tmp_path):
    # Opening the app must never rewrite the user's data behind their back.
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="chk", name="Checking", type=AccountType.CHECKING))
    db.upsert_account(conn, Account(id="crd", name="Card", type=AccountType.CREDIT_CARD))
    db.insert_transactions(
        conn,
        [
            Transaction(
                "t1",
                "chk",
                date(2026, 1, 25),
                Money.parse("-842.10"),
                "ONLINE PAYMENT TO CARD 4412",
            ),
            Transaction(
                "t2", "crd", date(2026, 1, 26), Money.parse("842.10"), "PAYMENT - THANK YOU"
            ),
        ],
    )
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()
    assert all(t.is_transfer for t in ledger.transactions)

    # In memory only: a fresh read still shows them ungrouped.
    assert all(not t.transfer_group for t in db.list_transactions(db.connect(path)))


def test_spending_by_category_excludes_money_that_only_moved(tmp_path):
    ledger = _ledger(tmp_path, _statement("-8.43"))
    names = {name for name, _, _ in ledger.spending_by_category()}
    assert "Transfer" not in names


def test_an_account_with_no_balance_is_reported_by_name(tmp_path):
    # accounts_missing_balances returns ids, and an id tells the user nothing.
    # This crashed the net worth screen the first time an account without a
    # recorded balance existed, which is any freshly imported one.
    import io

    from carraway.core.models import Account, AccountType
    from carraway.importers.csv_importer import import_csv

    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="a1", name="Everyday", type=AccountType.CHECKING))
    db.upsert_account(conn, Account(id="a2", name="No Balance Here", type=AccountType.CASH))
    txs, _ = import_csv(io.StringIO(_statement("-8.43")), "a1")
    db.insert_transactions(conn, txs)
    db.record_balance(conn, "a1", Money.parse("100.00"))
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()

    missing = ledger.accounts_without_balances()
    assert missing == ["a2"]
    by_id = {a.id: a.name for a in ledger.accounts}
    assert [by_id[i] for i in missing] == ["No Balance Here"]


def _tracked_ledger(tmp_path) -> Ledger:
    """A ledger with linked accounts and one tracked subscription on each route."""
    path = tmp_path / "paid.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="wf", name="Wells Fargo Card", type=AccountType.CREDIT_CARD))
    db.upsert_account(conn, Account(id="chk", name="Chase Checking", type=AccountType.CHECKING))
    db.upsert_account(
        conn, Account(id="old", name="Closed Card", type=AccountType.CREDIT_CARD, closed=True)
    )
    db.add_manual_subscription(conn, "Gym", Money.parse("29.54"), "monthly", paid_via_account="wf")
    db.add_manual_subscription(
        conn, "Phone", Money.parse("35.00"), "monthly", paid_via="venmo to dad"
    )
    db.add_manual_subscription(conn, "Domain", Money.parse("15.00"), "yearly")
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def _series_named(ledger: Ledger, name: str):
    return next(s for s in ledger.series if s.merchant == name)


def test_a_linked_account_is_shown_by_its_real_name(tmp_path):
    # The point of storing a reference rather than the text the user typed:
    # the subscription names the account the way every other screen does.
    ledger = _tracked_ledger(tmp_path)
    assert ledger.paid_with(_series_named(ledger, "Gym")) == "Wells Fargo Card"


def test_free_text_survives_for_routes_with_no_account(tmp_path):
    # "venmo to dad" is not an account and never will be. Forcing the answer
    # into a dropdown would make it unrecordable.
    ledger = _tracked_ledger(tmp_path)
    assert ledger.paid_with(_series_named(ledger, "Phone")) == "venmo to dad"


def test_saying_nothing_shows_nothing(tmp_path):
    ledger = _tracked_ledger(tmp_path)
    assert ledger.paid_with(_series_named(ledger, "Domain")) == ""


def test_a_detected_series_reports_the_account_it_was_found_in(tmp_path):
    # No user input needed: the charge landed somewhere, and that is the answer.
    ledger = _ledger(tmp_path, _statement("-8.43"))
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    assert ledger.paid_with(netflix) == "Card"


def test_choosing_an_account_clears_a_stale_note(tmp_path):
    # Both fields are written every time, so the row cannot end up claiming
    # two different answers to one question.
    ledger = _tracked_ledger(tmp_path)
    phone = _series_named(ledger, "Phone")
    assert ledger.set_paid_with(phone, {"paid_via_account": "chk"}) is True
    assert ledger.paid_with(_series_named(ledger, "Phone")) == "Chase Checking"
    entry = ledger.manual_entry(_series_named(ledger, "Phone"))
    assert entry["paid_via"] == ""


def test_a_detected_series_cannot_have_its_account_reassigned(tmp_path):
    # It is a fact from the statement, not a guess to correct.
    ledger = _ledger(tmp_path, _statement("-8.43"))
    netflix = next(s for s in ledger.series if "NETFLIX" in s.merchant.upper())
    assert ledger.set_paid_with(netflix, {"paid_via_account": "a1"}) is False


def test_payable_accounts_puts_cards_first_and_drops_closed_ones(tmp_path):
    ledger = _tracked_ledger(tmp_path)
    offered = ledger.payable_accounts
    assert [a.name for a in offered] == ["Wells Fargo Card", "Chase Checking"]


def _cash_ledger(tmp_path, *, observe: bool = True) -> Ledger:
    """A cash account whose imported history starts long after it opened."""
    path = tmp_path / "cash.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CASH))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id="t1",
                account_id="cash",
                date=date(2026, 6, 10),
                amount=Money.parse("-40.00"),
                description="Market",
            ),
            Transaction(
                id="t2",
                account_id="cash",
                date=date(2026, 8, 5),
                amount=Money.parse("-15.00"),
                description="Coffee",
            ),
        ],
    )
    if observe:
        # Observed after the first transaction and before the second, which is
        # the ordinary case: a statement reaches back further than the reading.
        db.record_balance(conn, "cash", Money.parse("200.00"), date(2026, 7, 1))
    conn.close()
    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def test_implied_balance_rolls_the_last_reading_forward(tmp_path):
    # $200 on 1 July, minus the $15 spent after it. The $40 spent in June is
    # already inside the $200 and must not be subtracted twice.
    ledger = _cash_ledger(tmp_path)
    assert ledger.implied_balance("cash") == Money.parse("185.00")


def test_implied_balance_does_not_sum_every_transaction(tmp_path):
    # The bug this guards against: summing all history treats an unknown
    # opening balance as zero. On real data that was wrong by $4,928.
    ledger = _cash_ledger(tmp_path)
    assert ledger.implied_balance("cash") != Money.parse("-55.00")


def test_with_no_reading_at_all_the_transactions_are_all_there_is(tmp_path):
    ledger = _cash_ledger(tmp_path, observe=False)
    assert ledger.implied_balance("cash") == Money.parse("-55.00")


def test_typing_a_balance_records_it_without_touching_history(tmp_path):
    ledger = _cash_ledger(tmp_path)
    before = len(ledger.transactions)
    ledger.set_cash_balance("cash", Money.parse("240.00"), correction=False)
    assert ledger.balances["cash"] == Money.parse("240.00")
    assert len(ledger.transactions) == before  # declined, so nothing invented


def test_a_correction_line_makes_the_history_reach_the_typed_balance(tmp_path):
    # $185 implied, $240 actually there: $55 came in that was never recorded.
    ledger = _cash_ledger(tmp_path)
    gap = ledger.set_cash_balance("cash", Money.parse("240.00"), correction=True)
    assert gap == Money.parse("55.00")

    added = [t for t in ledger.transactions if t.description == "Cash adjustment"]
    assert len(added) == 1
    assert added[0].amount == Money.parse("55.00")
    assert added[0].date == date.today()


def test_a_correction_for_money_quietly_spent_is_negative(tmp_path):
    ledger = _cash_ledger(tmp_path)
    gap = ledger.set_cash_balance("cash", Money.parse("100.00"), correction=True)
    assert gap == Money.parse("-85.00")
    added = next(t for t in ledger.transactions if t.description == "Cash adjustment")
    assert added.amount.minor < 0


def test_correcting_twice_in_a_row_is_a_no_op_the_second_time(tmp_path):
    # After reconciling, the records agree, so there is nothing left to adjust.
    ledger = _cash_ledger(tmp_path)
    ledger.set_cash_balance("cash", Money.parse("240.00"), correction=True)
    gap = ledger.set_cash_balance("cash", Money.parse("240.00"), correction=True)
    assert gap == Money.zero()
    assert len([t for t in ledger.transactions if t.description == "Cash adjustment"]) == 1


def test_a_hand_entered_cash_transaction_lands_in_the_ledger(tmp_path):
    ledger = _cash_ledger(tmp_path)
    assert ledger.add_cash_transaction(
        "cash", date(2026, 8, 20), "Farmers market", Money.parse("-24.00")
    )
    assert any(t.description == "Farmers market" for t in ledger.transactions)


def test_the_same_cash_transaction_twice_is_recorded_once(tmp_path):
    ledger = _cash_ledger(tmp_path)
    args = ("cash", date(2026, 8, 20), "Farmers market", Money.parse("-24.00"))
    assert ledger.add_cash_transaction(*args) is True
    assert ledger.add_cash_transaction(*args) is False


def test_only_cash_accounts_accept_a_typed_balance(tmp_path):
    # Everything else is synced, and a typed figure would be overwritten by
    # the next refresh without saying so.
    ledger = _cash_ledger(tmp_path)
    assert ledger.is_cash_account("cash") is True
    assert ledger.is_cash_account("nope") is False
    assert ledger.is_cash_account(None) is False


def test_renaming_an_account_keeps_its_history(tmp_path):
    ledger = _cash_ledger(tmp_path)
    assert ledger.rename_account("cash", "Pocket money") is True
    assert ledger.account_name("cash") == "Pocket money"
    assert len([t for t in ledger.transactions if t.account_id == "cash"]) == 2
    assert ledger.balances["cash"] == Money.parse("200.00")


def test_a_reading_already_includes_its_own_day(tmp_path):
    # A recorded balance is a closing figure. Counting same-day transactions
    # on top of it double-counts them — on real Venmo data two transfers
    # dated on the reading date would have inflated the balance by $137.
    path = tmp_path / "sameday.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CASH))
    db.insert_transactions(
        conn,
        [
            Transaction(
                id="s1",
                account_id="cash",
                date=date(2026, 8, 29),
                amount=Money.parse("97.00"),
                description="Transfer in",
            )
        ],
    )
    db.record_balance(conn, "cash", Money.parse("1008.47"), date(2026, 8, 29))
    conn.close()

    ledger = Ledger(path=path)
    ledger.load()
    assert ledger.implied_balance("cash") == Money.parse("1008.47")
    assert ledger.implied_balance("cash") != Money.parse("1105.47")


# -- budgets --------------------------------------------------------------


def _budget_ledger(tmp_path) -> Ledger:
    """Six months of steady spending across two accounts."""
    path = tmp_path / "budget.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="card", name="Card", type=AccountType.CREDIT_CARD))
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CASH))
    rows = []
    for month in range(2, 8):
        rows.append(
            Transaction(
                id=f"d{month}",
                account_id="card",
                date=date(2026, month, 9),
                amount=Money.parse("-200.00"),
                description="RESTAURANT",
                category="Dining",
            )
        )
        rows.append(
            Transaction(
                id=f"c{month}",
                account_id="cash",
                date=date(2026, month, 12),
                amount=Money.parse("-60.00"),
                description="MARKET",
                category="Groceries",
            )
        )
    db.insert_transactions(conn, rows)
    conn.close()
    ledger = Ledger(path=path)
    ledger.load()
    return ledger


def test_a_ledger_starts_with_no_budgets(tmp_path):
    assert _budget_ledger(tmp_path).budgets == []


def test_a_saved_budget_comes_back_on_the_ledger(tmp_path):
    from carraway.analysis.budgets import Budget, Envelope

    ledger = _budget_ledger(tmp_path)
    ledger.save_budget(
        Budget(
            id="b1",
            name="August",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            envelopes=(Envelope("Dining", Money.parse("250")),),
        )
    )
    assert [b.name for b in ledger.budgets] == ["August"]
    assert ledger.budget_by_id("b1").total == Money.parse("250")

    assert ledger.delete_budget("b1") is True
    assert ledger.budgets == []


def test_suggestions_can_be_narrowed_to_some_accounts(tmp_path):
    ledger = _budget_ledger(tmp_path)
    every = {e.category for e in ledger.suggest_envelopes(date(2026, 8, 1), date(2026, 8, 31))}
    card = {
        e.category
        for e in ledger.suggest_envelopes(date(2026, 8, 1), date(2026, 8, 31), accounts=["card"])
    }
    assert "Groceries" in every
    assert "Groceries" not in card
    assert "Dining" in card


def test_budget_status_uses_the_ledgers_own_categories(tmp_path):
    # Not the built-in rules: the budget must agree with what Spending shows.
    from carraway.analysis.budgets import Budget, Envelope

    ledger = _budget_ledger(tmp_path)
    budget = Budget(
        id="b1",
        name="July",
        starts_on=date(2026, 7, 1),
        ends_on=date(2026, 7, 31),
        envelopes=(Envelope("Dining", Money.parse("250")),),
    )
    state = ledger.budget_status(budget, asof=date(2026, 7, 31))
    dining = next(line for line in state.lines if line.category == "Dining")
    assert dining.spent == Money.parse("200")
    assert dining.on_track is True
