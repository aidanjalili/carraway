"""CSV import has to cope with the fact that no two banks agree on a format."""

import io

import pytest

from carraway.core.money import Money
from carraway.importers.csv_importer import ImportError_, import_csv

SIGNED_AMOUNT_CSV = """Date,Description,Amount
2026-01-14,NETFLIX.COM 866-579-7172 CA,-15.49
2026-01-15,ACME CORP PAYROLL,2400.00
2026-01-16,"SQ *BLUE BOTTLE #402, SF",-4.75
"""

DEBIT_CREDIT_CSV = """Transaction Date;Payee;Debit;Credit
14/01/2026;NETFLIX;15.49;
15/01/2026;PAYROLL;;2400.00
"""


def test_signed_amount_column():
    txs, warnings = import_csv(io.StringIO(SIGNED_AMOUNT_CSV), "acct1")
    assert warnings == []
    assert len(txs) == 3
    assert txs[0].amount == Money.parse("-15.49")
    assert txs[1].amount == Money.parse("2400.00")
    assert txs[0].merchant == "NETFLIX"


def test_separate_debit_credit_columns_and_semicolons():
    txs, _ = import_csv(io.StringIO(DEBIT_CREDIT_CSV), "acct1")
    assert len(txs) == 2
    # Debits must come out negative regardless of how the bank wrote them.
    assert txs[0].amount == Money.parse("-15.49")
    assert txs[1].amount == Money.parse("2400.00")


def test_flip_sign_for_credit_card_exports():
    txs, _ = import_csv(io.StringIO(SIGNED_AMOUNT_CSV), "acct1", flip_sign=True)
    assert txs[0].amount == Money.parse("15.49")


def test_bad_rows_warn_instead_of_aborting():
    csv_text = "Date,Description,Amount\n2026-01-14,GOOD,-1.00\nnot-a-date,BAD,-2.00\n"
    txs, warnings = import_csv(io.StringIO(csv_text), "acct1")
    assert len(txs) == 1  # the good row survives
    assert len(warnings) == 1


def test_missing_columns_raise():
    with pytest.raises(ImportError_):
        import_csv(io.StringIO("Foo,Bar\n1,2\n"), "acct1")


def test_signature_is_stable_and_ignores_user_edits():
    txs_a, _ = import_csv(io.StringIO(SIGNED_AMOUNT_CSV), "acct1")
    txs_b, _ = import_csv(io.StringIO(SIGNED_AMOUNT_CSV), "acct1")
    # Same row imported twice must fingerprint identically, or dedupe fails.
    assert txs_a[0].signature == txs_b[0].signature
    txs_b[0].category = "Entertainment"
    txs_b[0].notes = "shared with roommate"
    assert txs_a[0].signature == txs_b[0].signature


def test_html_entities_in_descriptions_are_decoded():
    # Seen on a real export: one row wrote "C &amp; S" where every other row
    # wrote "C & S", which split one vendor into two merchants.
    csv_text = (
        "Date,Description,Amount\n"
        "2026-01-14,CTLP*C &amp; S VENDING COM,-2.50\n"
        "2026-01-15,CTLP*C & S VENDING COM,-2.50\n"
    )
    txs, _ = import_csv(io.StringIO(csv_text), "acct1")
    assert txs[0].description == "CTLP*C & S VENDING COM"
    assert txs[0].merchant == txs[1].merchant
