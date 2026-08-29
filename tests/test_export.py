"""An .ods is only useful if Calc can sum the money column, so that is the bar."""

import csv
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

import pytest

from carraway.core.models import Account, AccountType, RecurringSeries, Transaction
from carraway.core.money import Money
from carraway.exporters.ods import MIMETYPE, ExportError, export_csv, export_ods

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}

ACCOUNTS = [
    Account(id="acct1", name="Everyday Checking", type=AccountType.CHECKING),
    Account(id="acct2", name="Travel Card", type=AccountType.CREDIT_CARD),
]


def tx(
    id_: str,
    when: date,
    amount: str,
    description: str,
    *,
    account_id: str = "acct1",
    merchant: str = "",
    category: str = "",
    transfer_group: str = "",
) -> Transaction:
    return Transaction(
        id=id_,
        account_id=account_id,
        date=when,
        amount=Money.parse(amount),
        description=description,
        merchant=merchant or description,
        category=category,
        transfer_group=transfer_group,
    )


LEDGER = [
    tx("t1", date(2026, 1, 14), "-15.49", "NETFLIX.COM", category="Entertainment"),
    tx("t2", date(2026, 1, 15), "2400.00", "ACME CORP PAYROLL", category="Income"),
    tx("t3", date(2026, 1, 16), "-4.75", "BLUE BOTTLE", category="Dining"),
    tx("t4", date(2026, 1, 17), "-1200.00", "RENT", account_id="acct2", category="Housing"),
]

SERIES = [
    RecurringSeries(
        merchant="NETFLIX",
        account_id="acct1",
        cadence="monthly",
        typical_amount=Money.parse("-15.49"),
        occurrences=6,
        first_seen=date(2025, 8, 14),
        last_seen=date(2026, 1, 14),
        next_expected=date(2026, 2, 14),
        confidence=0.95,
        amount_varies=False,
    )
]


def read_content(path):
    with zipfile.ZipFile(path) as package:
        return ET.fromstring(package.read("content.xml"))


def sheets(root):
    return {
        table.get(f"{{{NS['table']}}}name"): table for table in root.iter(f"{{{NS['table']}}}table")
    }


def rows(table):
    """Every row of a sheet as a list of cell elements, header row included."""
    return [
        list(row.findall(f"{{{NS['table']}}}table-cell"))
        for row in table.findall(f"{{{NS['table']}}}table-row")
    ]


def cell_text(cell):
    node = cell.find(f"{{{NS['text']}}}p")
    return node.text if node is not None and node.text else ""


def test_mimetype_is_first_and_stored(tmp_path):
    # Get this wrong and no ODF reader will even try to parse the XML.
    path = export_ods(tmp_path / "ledger.ods", LEDGER)
    with zipfile.ZipFile(path) as package:
        entries = package.infolist()
        assert entries[0].filename == "mimetype"
        assert entries[0].compress_type == zipfile.ZIP_STORED
        assert package.read("mimetype").decode() == MIMETYPE
        assert package.testzip() is None
    # The magic must also sit as plain bytes near the start of the raw file.
    assert MIMETYPE.encode() in path.read_bytes()[:100]


def test_package_holds_the_required_parts(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER)
    with zipfile.ZipFile(path) as package:
        names = package.namelist()
        assert {"mimetype", "META-INF/manifest.xml", "content.xml"} <= set(names)
        manifest = ET.fromstring(package.read("META-INF/manifest.xml"))
        listed = {
            entry.get("{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}full-path")
            for entry in manifest
        }
        # Every part the manifest promises has to actually be in the package.
        assert listed - {"/"} <= set(names)


def test_expected_sheets_exist(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER, series=SERIES)
    names = list(sheets(read_content(path)))
    assert names == ["Transactions", "Categories", "Subscriptions"]


def test_subscriptions_sheet_is_omitted_without_series(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER)
    assert "Subscriptions" not in sheets(read_content(path))


def test_amounts_are_numeric_cells(tmp_path):
    # A string cell holding "-15.49" looks identical on screen and cannot be
    # summed, which would defeat the whole point of exporting a spreadsheet.
    path = export_ods(tmp_path / "ledger.ods", LEDGER)
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    amounts = [row[5] for row in body]
    for cell in amounts:
        assert cell.get(f"{{{NS['office']}}}value-type") == "float"
    assert [cell.get(f"{{{NS['office']}}}value") for cell in amounts] == [
        "-15.49",
        "2400.00",
        "-4.75",
        "-1200.00",
    ]


def test_dates_are_date_cells(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER)
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    first = body[0][0]
    assert first.get(f"{{{NS['office']}}}value-type") == "date"
    assert first.get(f"{{{NS['office']}}}date-value") == "2026-01-14"


def test_account_names_replace_ids_when_accounts_are_given(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER, accounts=ACCOUNTS)
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    assert [cell_text(row[1]) for row in body] == [
        "Everyday Checking",
        "Everyday Checking",
        "Everyday Checking",
        "Travel Card",
    ]


def test_unknown_account_id_falls_back_to_the_id(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER, accounts=ACCOUNTS[:1])
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    assert cell_text(body[3][1]) == "acct2"


def test_xml_hostile_description_round_trips(tmp_path):
    # Real bank descriptions contain these. Unescaped, one row corrupts the
    # whole document and nothing opens.
    nasty = "AT&T <PAYMENT> \"AUTOPAY\" '#1'"
    path = export_ods(tmp_path / "ledger.ods", [tx("t1", date(2026, 1, 14), "-9.99", nasty)])
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    assert cell_text(body[0][2]) == nasty


def test_sheet_content_survives_control_characters(tmp_path):
    # A stray control byte is illegal in XML outright; escaping cannot save it,
    # so it has to be dropped rather than allowed to poison the document.
    path = export_ods(
        tmp_path / "ledger.ods", [tx("t1", date(2026, 1, 14), "-1.00", "BAD\x00BYTE\x07")]
    )
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    assert cell_text(body[0][2]) == "BADBYTE"


def test_empty_ledger_still_produces_a_valid_file(tmp_path):
    path = export_ods(tmp_path / "empty.ods", [])
    with zipfile.ZipFile(path) as package:
        assert package.testzip() is None
    tables = sheets(read_content(path))
    assert set(tables) == {"Transactions", "Categories"}
    # Only the header row: a table with no rows at all is not valid ODF.
    assert len(rows(tables["Transactions"])) == 1


def test_categories_sheet_totals_outflows_only(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER)
    body = rows(sheets(read_content(path))["Categories"])[1:]
    totals = {cell_text(row[0]): row[2].get(f"{{{NS['office']}}}value") for row in body}
    assert totals == {"Housing": "1200.00", "Entertainment": "15.49", "Dining": "4.75"}
    # Income is an inflow, not spending, so it must not appear.
    assert "Income" not in totals
    # Biggest spend first, so the sheet answers "where does it go?" at a glance.
    assert [cell_text(row[0]) for row in body] == ["Housing", "Entertainment", "Dining"]
    counts = [row[3].get(f"{{{NS['office']}}}value") for row in body]
    assert counts == ["1", "1", "1"]


def test_transfers_are_excluded_from_spending(tmp_path):
    ledger = [
        tx(
            "t1",
            date(2026, 1, 14),
            "-500.00",
            "TO SAVINGS",
            category="Transfer",
            transfer_group="g1",
        ),
        tx("t2", date(2026, 1, 14), "-4.75", "BLUE BOTTLE", category="Dining"),
    ]
    path = export_ods(tmp_path / "ledger.ods", ledger)
    body = rows(sheets(read_content(path))["Categories"])[1:]
    assert [cell_text(row[0]) for row in body] == ["Dining"]


def test_uncategorised_rows_get_a_label(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", [tx("t1", date(2026, 1, 14), "-1.00", "SHOP")])
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    assert cell_text(body[0][4]) == "Uncategorized"


def test_explicit_categories_override_what_the_rows_carry(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER, categories=["A", "B", "C", "D"])
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    assert [cell_text(row[4]) for row in body] == ["A", "B", "C", "D"]


def test_mismatched_category_list_is_rejected(tmp_path):
    with pytest.raises(ExportError):
        export_ods(tmp_path / "ledger.ods", LEDGER, categories=["A"])


def test_subscriptions_sheet_carries_annual_cost(tmp_path):
    path = export_ods(tmp_path / "ledger.ods", LEDGER, series=SERIES)
    body = rows(sheets(read_content(path))["Subscriptions"])[1:]
    assert cell_text(body[0][0]) == "NETFLIX"
    assert cell_text(body[0][1]) == "monthly"
    # Both figures are positive magnitudes: "costs you 15.49/mo, 185.88/yr".
    assert body[0][2].get(f"{{{NS['office']}}}value") == "15.49"
    assert body[0][3].get(f"{{{NS['office']}}}value") == "185.88"


def test_mixed_currencies_do_not_break_the_summary(tmp_path):
    # Money refuses to add across currencies; the summary must split rather
    # than raise, or a traveller cannot export at all.
    ledger = [
        tx("t1", date(2026, 1, 14), "-10.00", "CAFE", category="Dining"),
        Transaction(
            id="t2",
            account_id="acct1",
            date=date(2026, 1, 15),
            amount=Money.parse("-8.50", "EUR"),
            description="CAFE PARIS",
            category="Dining",
        ),
    ]
    path = export_ods(tmp_path / "ledger.ods", ledger)
    body = rows(sheets(read_content(path))["Categories"])[1:]
    assert {(cell_text(r[0]), cell_text(r[1])) for r in body} == {
        ("Dining", "USD"),
        ("Dining", "EUR"),
    }


def test_zero_decimal_currency_keeps_its_scale(tmp_path):
    ledger = [
        Transaction(
            id="t1",
            account_id="acct1",
            date=date(2026, 1, 14),
            amount=Money(-1250, "JPY"),
            description="RAMEN",
        )
    ]
    path = export_ods(tmp_path / "ledger.ods", ledger)
    body = rows(sheets(read_content(path))["Transactions"])[1:]
    assert body[0][5].get(f"{{{NS['office']}}}value") == "-1250"


def test_export_is_reproducible(tmp_path):
    # Same ledger, same bytes, so a user versioning exports sees real diffs only.
    first = export_ods(tmp_path / "a.ods", LEDGER).read_bytes()
    second = export_ods(tmp_path / "b.ods", LEDGER).read_bytes()
    assert first == second


def test_large_ledger_exports(tmp_path):
    ledger = [tx(f"t{i}", date(2026, 1, 1 + i % 28), "-1.99", f"MERCHANT {i}") for i in range(5000)]
    path = export_ods(tmp_path / "big.ods", ledger)
    body = rows(sheets(read_content(path))["Transactions"])
    assert len(body) == 5001  # header plus every row


# -- CSV fallback --------------------------------------------------------


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_csv_writes_parseable_values(tmp_path):
    rows_out = read_csv(export_csv(tmp_path / "ledger.csv", LEDGER))
    assert len(rows_out) == 4
    assert rows_out[0]["date"] == "2026-01-14"
    assert rows_out[0]["description"] == "NETFLIX.COM"
    assert rows_out[0]["category"] == "Entertainment"
    # Unformatted, so a spreadsheet reads the column as numbers.
    assert [r["amount"] for r in rows_out] == ["-15.49", "2400.00", "-4.75", "-1200.00"]
    assert rows_out[0]["currency"] == "USD"


def test_csv_quotes_commas_and_quotes(tmp_path):
    nasty = 'SQ *BLUE BOTTLE #402, SF "MAIN"'
    path = export_csv(tmp_path / "ledger.csv", [tx("t1", date(2026, 1, 14), "-4.75", nasty)])
    assert read_csv(path)[0]["description"] == nasty


def test_csv_handles_an_empty_ledger(tmp_path):
    path = export_csv(tmp_path / "empty.csv", [])
    assert read_csv(path) == []
    assert path.read_text(encoding="utf-8-sig").startswith("date,account_id")


@pytest.mark.skipif(shutil.which("libreoffice") is None, reason="LibreOffice not installed")
def test_libreoffice_opens_the_file(tmp_path):
    """Optional smoke test: the real reader is the only authority on validity."""
    path = export_ods(tmp_path / "ledger.ods", LEDGER, accounts=ACCOUNTS, series=SERIES)
    result = subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "csv", "--outdir", str(tmp_path), str(path)],
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert (tmp_path / "ledger.csv").exists()
