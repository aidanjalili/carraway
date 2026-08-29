"""Storage round-trips and, critically, import idempotency."""

import io
import uuid
from datetime import date

from carraway.core import db
from carraway.core.models import Account, AccountType, Transaction
from carraway.core.money import Money
from carraway.importers.csv_importer import import_csv

CSV = """Date,Description,Amount
2026-01-14,NETFLIX.COM,-15.49
2026-01-15,ACME PAYROLL,2400.00
"""


def make_db(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.upsert_account(conn, Account(id="acct1", name="Checking", type=AccountType.CHECKING))
    return conn


def test_account_round_trip(tmp_path):
    conn = make_db(tmp_path)
    accounts = db.list_accounts(conn)
    assert len(accounts) == 1
    assert accounts[0].name == "Checking"
    assert accounts[0].type is AccountType.CHECKING


def test_transaction_round_trip_preserves_exact_amount(tmp_path):
    conn = make_db(tmp_path)
    tx = Transaction(
        id=uuid.uuid4().hex,
        account_id="acct1",
        date=date(2026, 1, 14),
        amount=Money.parse("-15.49"),
        description="NETFLIX.COM",
    )
    db.insert_transactions(conn, [tx])

    loaded = db.list_transactions(conn)[0]
    assert loaded.amount == Money.parse("-15.49")
    assert loaded.date == date(2026, 1, 14)


def test_reimporting_the_same_file_is_a_no_op(tmp_path):
    # The most important storage guarantee: a user who imports overlapping
    # statements must not end up with doubled transactions.
    conn = make_db(tmp_path)

    first, _ = import_csv(io.StringIO(CSV), "acct1")
    inserted, skipped = db.insert_transactions(conn, first)
    assert (inserted, skipped) == (2, 0)

    second, _ = import_csv(io.StringIO(CSV), "acct1")
    inserted, skipped = db.insert_transactions(conn, second)
    assert (inserted, skipped) == (0, 2)
    assert len(db.list_transactions(conn)) == 2


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "test.db"
    db.connect(path).close()
    conn = db.connect(path)  # reopening must not re-run migrations
    assert db.migrate(conn) == len(db.MIGRATIONS)


def test_liability_account_types():
    assert AccountType.CREDIT_CARD.is_liability
    assert not AccountType.CHECKING.is_liability


def test_identical_same_day_purchases_are_both_kept(tmp_path):
    # Found on real data: two separate purchases at the same merchant, for the
    # same amount, on the same day agree on every field the dedupe fingerprint
    # reads, so the second was silently dropped. Losing a real transaction is
    # far worse than keeping a duplicate, because nothing surfaces the loss.
    conn = make_db(tmp_path)
    csv_text = (
        "Date,Description,Amount\n"
        "2026-01-14,BLUE BOTTLE COFFEE,-4.75\n"
        "2026-01-14,BLUE BOTTLE COFFEE,-4.75\n"
        "2026-01-14,BLUE BOTTLE COFFEE,-4.75\n"
    )
    txs, _ = import_csv(io.StringIO(csv_text), "acct1")
    assert [t.occurrence for t in txs] == [0, 1, 2]

    inserted, skipped = db.insert_transactions(conn, txs)
    assert (inserted, skipped) == (3, 0)
    assert len(db.list_transactions(conn)) == 3

    # Re-importing the same statement must still be a no-op.
    again, _ = import_csv(io.StringIO(csv_text), "acct1")
    assert db.insert_transactions(conn, again) == (0, 3)
    assert len(db.list_transactions(conn)) == 3
