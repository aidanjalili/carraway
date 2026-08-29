"""Venmo CSV import, where the hard part is that the file is not a plain table.

Every fixture below is synthetic: invented people, invented transaction ids.
The account holder throughout is "Jordan Mercer", and no test may ever see
that name in a description — the counterparty is the other side of the payment.
"""

import io

import pytest

from carraway.analysis.transfers import find_transfers
from carraway.core.models import Transaction
from carraway.core.money import Money
from carraway.importers.csv_importer import ImportError_
from carraway.importers.venmo import import_venmo, parse_venmo_amount

# A statement shaped the way Venmo writes one: a title line carrying the
# handle and period, the account holder's name, blank spacer lines, then the
# header. The opening- and closing-balance rows have no Transaction ID. Rows
# are wrapped in source only to stay inside the line limit; each entry below
# is one physical line of the file.
STATEMENT = "\n".join(
    [
        "Account Statement - (@jordan-mercer) - January 2026",
        "Jordan Mercer",
        "",
        "",
        ",Transaction ID,Datetime,Type,Status,Note,From,To,Amount (total),"
        "Amount (tip),Amount (tax),Amount (fee),Funding Source,Destination,"
        "Beginning Balance,Ending Balance,Statement Period Venmo Fees,"
        "Terminal Location,Year to Date Venmo Fees,Disclaimer",
        ",,,,,,,,,,,,,,$40.00,,,,,",
        ",4103829183,2026-01-14T18:32:05,Payment,Complete,dinner,"
        "Jordan Mercer,Alex Liang,- $25.00,,,,Venmo balance,,,,,,,",
        ",4104551027,2026-01-15T09:04:11,Payment,Complete,concert ticket,"
        "Priya Raman,Jordan Mercer,+ $10.00,,,,,Venmo balance,,,,,,",
        ",4106740318,2026-01-17T07:00:00,Standard Transfer,Complete,,"
        "Jordan Mercer,,- $150.00,,,,Venmo balance,"
        "Bluecrest Bank Personal Checking,,,,,,",
        ",4107992244,2026-01-18T12:15:00,Merchant Transaction,Complete,,"
        "Jordan Mercer,Ferndale Coffee Roasters,- $6.75,,,,Venmo balance,,,,,,,",
        ',4109110576,2026-01-19T20:41:33,Charge,Issued,"rent, february",'
        "Jordan Mercer,Marisol Vega,+ $900.00,,,,,,,,,,,",
        ",4110284915,2026-01-21T11:02:58,Payment,Cancelled,split cab,"
        "Jordan Mercer,Dev Okonkwo,- $18.40,,,,Venmo balance,,,,,,,",
        ",4111906633,2026-01-24T15:22:04,Refund,Complete,order 88213,"
        "Ferndale Coffee Roasters,Jordan Mercer,+ $6.75,,,,,Venmo balance,,,,,,",
        ",,,,,,,,,,,,,,,$25.60,$0.00,,$0.00,Venmo is a service of PayPal Inc.",
        "",
    ]
)

# Current exports label the column plain "ID", so the header has to be found
# from "Datetime" plus "Amount (total)" instead.
ID_COLUMN_STATEMENT = "\n".join(
    [
        "Account Statement - (@jordan-mercer) - February 2026",
        "Jordan Mercer",
        "",
        ",ID,Datetime,Type,Status,Note,From,To,Amount (total),Funding Source,Destination",
        ",5200114477,2026-02-03T08:11:00,Payment,Complete,groceries,"
        "Jordan Mercer,Sam Whitfield,- $42.18,Venmo balance,",
        "",
    ]
)

# Only the columns that carry meaning; every optional one is absent.
MINIMAL_STATEMENT = """Account Statement - (@jordan-mercer)

Transaction ID,Datetime,Type,Status,Note,From,To,Amount (total)
6001,2026-03-02T10:00:00,Payment,Complete,haircut,Jordan Mercer,Nina Alvarez,- $35.00
"""


def _by_date(transactions: list[Transaction], iso: str) -> Transaction:
    return next(tx for tx in transactions if tx.date.isoformat() == iso)


def test_full_statement_skips_preamble_and_trailer():
    txs, warnings = import_venmo(io.StringIO(STATEMENT), "venmo1")

    # Five complete rows out of seven; the balance rows top and bottom carry
    # no ID and are not movements of money, so they must not appear at all.
    assert [tx.date.isoformat() for tx in txs] == [
        "2026-01-14",
        "2026-01-15",
        "2026-01-17",
        "2026-01-18",
        "2026-01-24",
    ]
    assert all(tx.account_id == "venmo1" for tx in txs)
    # The only warnings are the two rows whose money never moved.
    assert len(warnings) == 2


def test_sign_is_parsed_in_both_directions():
    txs, _ = import_venmo(io.StringIO(STATEMENT), "venmo1")
    sent = _by_date(txs, "2026-01-14")
    received = _by_date(txs, "2026-01-15")

    # "- $25.00": a sign, a space, then the symbol. Money.parse alone chokes.
    assert sent.amount == Money.parse("-25.00")
    assert sent.amount.minor == -2500
    assert sent.is_outflow
    # "+ $10.00" must stay money coming in.
    assert received.amount.minor == 1000
    assert not received.is_outflow


def test_parse_venmo_amount_handles_grouping_and_bare_amounts():
    assert parse_venmo_amount("- $1,234.56").minor == -123456
    assert parse_venmo_amount("+ $1,234.56").minor == 123456
    # Not every row is written with a leading sign.
    assert parse_venmo_amount("$12.50").minor == 1250
    assert parse_venmo_amount("-25.00").minor == -2500
    with pytest.raises(ValueError):
        parse_venmo_amount("   ")


def test_amounts_are_preserved_exactly():
    txs, _ = import_venmo(io.StringIO(STATEMENT), "venmo1")
    assert [tx.amount.minor for tx in txs] == [-2500, 1000, -15000, -675, 675]
    assert all(isinstance(tx.amount.minor, int) for tx in txs)
    assert all(tx.amount.currency == "USD" for tx in txs)


def test_currency_is_honoured():
    txs, _ = import_venmo(io.StringIO(MINIMAL_STATEMENT), "venmo1", currency="EUR")
    assert txs[0].amount == Money(-3500, "EUR")


def test_description_uses_the_counterparty_not_the_account_holder():
    txs, _ = import_venmo(io.StringIO(STATEMENT), "venmo1")

    # Money out: the user is "From", so "To" is the person to name.
    assert _by_date(txs, "2026-01-14").description == "Alex Liang - dinner"
    # Money in: the sides swap.
    assert _by_date(txs, "2026-01-15").description == "Priya Raman - concert ticket"
    assert _by_date(txs, "2026-01-18").description == "Ferndale Coffee Roasters"
    assert _by_date(txs, "2026-01-24").description == "Ferndale Coffee Roasters - order 88213"
    assert not any("Jordan Mercer" in tx.description for tx in txs)


def test_merchant_is_the_counterparty_without_the_note():
    txs, _ = import_venmo(io.StringIO(STATEMENT), "venmo1")
    # Notes are written fresh each time; folding them into the merchant would
    # fragment one person across several groups and hide a recurring series.
    assert _by_date(txs, "2026-01-14").merchant == "ALEX LIANG"
    assert _by_date(txs, "2026-01-18").merchant == _by_date(txs, "2026-01-24").merchant


def test_standard_transfer_is_marked_as_a_transfer():
    txs, _ = import_venmo(io.StringIO(STATEMENT), "venmo1")
    transfer = _by_date(txs, "2026-01-17")

    assert transfer.description == "Transfer: Bluecrest Bank Personal Checking"
    assert transfer.amount.minor == -15000
    # No group id is invented here: only the matcher, which can see both
    # halves, is in a position to assign one.
    assert transfer.transfer_group == ""


def test_transfer_wording_lets_the_matcher_pair_the_bank_side():
    txs, _ = import_venmo(io.StringIO(STATEMENT), "venmo1")
    bank_side = Transaction(
        id="bank-1",
        account_id="checking1",
        date=_by_date(txs, "2026-01-17").date,
        amount=Money(15000, "USD"),
        description="VENMO CASHOUT",
    )

    pairs = find_transfers([*txs, bank_side])
    assert [pair.inflow.id for pair in pairs] == ["bank-1"]
    assert pairs[0].outflow.description.startswith("Transfer:")


def test_incomplete_statuses_are_skipped_with_a_named_warning():
    _, warnings = import_venmo(io.StringIO(STATEMENT), "venmo1")
    joined = " | ".join(warnings)
    assert "'Issued'" in joined
    assert "'Cancelled'" in joined
    assert "4109110576" in joined  # the warning names the row it dropped


def test_duplicate_transaction_id_within_one_file_is_dropped():
    doubled = STATEMENT + (
        ",4103829183,2026-01-14T18:32:05,Payment,Complete,dinner,"
        "Jordan Mercer,Alex Liang,- $25.00,,,,Venmo balance,,,,,,,\n"
    )
    txs, warnings = import_venmo(io.StringIO(doubled), "venmo1")

    # Overlapping 90-day exports repeat rows verbatim; the id proves they are
    # the same payment.
    assert len(txs) == 5
    assert any("duplicate Transaction ID" in w for w in warnings)


def test_two_identical_payments_on_one_day_both_survive():
    # Same person, same amount, same note, same day, but different ids: two
    # real coffees, not one row imported twice.
    text = MINIMAL_STATEMENT + (
        "6002,2026-03-02T16:40:00,Payment,Complete,haircut,Jordan Mercer,Nina Alvarez,- $35.00\n"
    )
    txs, warnings = import_venmo(io.StringIO(text), "venmo1")
    assert len(txs) == 2
    assert warnings == []
    assert txs[0].signature != txs[1].signature


def test_header_is_found_when_the_id_column_is_named_id():
    txs, warnings = import_venmo(io.StringIO(ID_COLUMN_STATEMENT), "venmo1")
    assert warnings == []
    assert len(txs) == 1
    assert txs[0].description == "Sam Whitfield - groceries"
    assert txs[0].amount.minor == -4218


def test_missing_optional_columns_are_tolerated():
    txs, warnings = import_venmo(io.StringIO(MINIMAL_STATEMENT), "venmo1")
    assert warnings == []
    assert txs[0].description == "Nina Alvarez - haircut"


def test_a_bad_row_warns_instead_of_aborting_the_file():
    text = MINIMAL_STATEMENT + (
        "6003,not-a-date,Payment,Complete,bad row,Jordan Mercer,Nina Alvarez,- $1.00\n"
    )
    txs, warnings = import_venmo(io.StringIO(text), "venmo1")
    assert len(txs) == 1  # the good row survives
    assert len(warnings) == 1
    assert "6003" in warnings[0]


def test_non_venmo_csv_raises():
    with pytest.raises(ImportError_):
        import_venmo(io.StringIO("Date,Description,Amount\n2026-01-14,NETFLIX,-15.49\n"), "venmo1")
    with pytest.raises(ImportError_):
        import_venmo(io.StringIO(""), "venmo1")


def test_reimporting_the_same_file_fingerprints_identically():
    # The 90-day export cap means users import overlapping files; dedupe rests
    # on the signature being stable across runs.
    first, _ = import_venmo(io.StringIO(STATEMENT), "venmo1")
    second, _ = import_venmo(io.StringIO(STATEMENT), "venmo1")
    assert [tx.signature for tx in first] == [tx.signature for tx in second]
