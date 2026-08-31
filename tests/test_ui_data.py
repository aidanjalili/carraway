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
