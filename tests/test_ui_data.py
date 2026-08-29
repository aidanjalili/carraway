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
