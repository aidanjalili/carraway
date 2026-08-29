"""OFX import, where the hard part is that half the files in the wild are not XML.

All fixtures below are synthetic: invented account numbers, invented merchants.
"""

import io

import pytest

from carraway.core.money import Money
from carraway.importers.csv_importer import ImportError_
from carraway.importers.ofx_importer import import_ofx

# OFX 1.x as a US bank actually emits it: colon-separated headers, leaf tags
# that are opened and never closed, and an SGML entity in a merchant name.
SGML_BANK = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20260201080000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>0<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM><BANKID>121000248<ACCTID>000123456789<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260101
<DTEND>20260131
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260114120000.000[-8:PST]
<TRNAMT>-15.49
<FITID>202601140001
<NAME>NETFLIX.COM 866-579-7172 CA
<MEMO>RECURRING PAYMENT
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260115
<TRNAMT>2400.00
<FITID>202601150002
<NAME>ACME CORP PAYROLL
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260116000000[-8:PST]
<TRNAMT>-12.34
<FITID>202601160003
<NAME>BARNES &amp; NOBLE #114 SAN FRANCISCO CA
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1234.56<DTASOF>20260131</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""

# OFX 2.x: real XML, every tag closed, and the fields in a different order.
XML_BANK = """<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="211" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <BANKMSGSRSV1><STMTTRNRS><STMTRS>
    <CURDEF>USD</CURDEF>
    <BANKACCTFROM><BANKID>121000248</BANKID><ACCTID>000123456789</ACCTID>
      <ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>
    <BANKTRANLIST>
      <DTSTART>20260101</DTSTART>
      <DTEND>20260131</DTEND>
      <STMTTRN>
        <TRNTYPE>DEBIT</TRNTYPE>
        <DTPOSTED>20260114120000.000[-8:PST]</DTPOSTED>
        <TRNAMT>-15.49</TRNAMT>
        <FITID>202601140001</FITID>
        <NAME>NETFLIX.COM 866-579-7172 CA</NAME>
        <MEMO>RECURRING PAYMENT</MEMO>
      </STMTTRN>
      <STMTTRN>
        <TRNTYPE>CREDIT</TRNTYPE>
        <DTPOSTED>20260115</DTPOSTED>
        <TRNAMT>2400.00</TRNAMT>
        <FITID>202601150002</FITID>
        <PAYEE><NAME>ACME CORP PAYROLL</NAME><CITY>SAN FRANCISCO</CITY>
          <STATE>CA</STATE></PAYEE>
      </STMTTRN>
    </BANKTRANLIST>
  </STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""

# A card statement. Note the issuer already signs charges negative here, which
# is the common case; --flip-sign exists for the ones that do not.
SGML_CARD = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>
<CURDEF>USD
<CCACCTFROM><ACCTID>XXXXXXXXXXXX4321</CCACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260118
<TRNAMT>-89.99
<FITID>CC20260118001
<NAME>ALPINE OUTFITTERS
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260125
<TRNAMT>200.00
<FITID>CC20260125002
<NAME>PAYMENT THANK YOU
</STMTTRN>
</BANKTRANLIST>
</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>
</OFX>
"""


def test_sgml_statement():
    txs, warnings = import_ofx(io.StringIO(SGML_BANK), "acct1")
    assert warnings == []
    assert len(txs) == 3
    assert txs[0].date.isoformat() == "2026-01-14"
    assert txs[0].amount == Money.parse("-15.49")
    assert txs[1].amount == Money.parse("2400.00")
    assert txs[0].description == "NETFLIX.COM 866-579-7172 CA - RECURRING PAYMENT"
    # Merchant comes from NAME only, so the per-charge MEMO cannot fragment it.
    assert txs[0].merchant == "NETFLIX"
    assert txs[2].description.startswith("BARNES & NOBLE")  # &amp; resolved
    # FITID is not smuggled into notes; notes belong to the user.
    assert txs[0].notes == ""


def test_xml_statement():
    txs, warnings = import_ofx(io.StringIO(XML_BANK), "acct1")
    assert warnings == []
    assert len(txs) == 2
    assert txs[0].amount == Money.parse("-15.49")
    assert txs[0].description == "NETFLIX.COM 866-579-7172 CA - RECURRING PAYMENT"
    # NAME nested in a <PAYEE> aggregate is still the payee.
    assert txs[1].description == "ACME CORP PAYROLL"
    assert txs[1].amount == Money.parse("2400.00")


def test_both_flavours_agree():
    sgml, _ = import_ofx(io.StringIO(SGML_BANK), "acct1")
    xml, _ = import_ofx(io.StringIO(XML_BANK), "acct1")
    assert sgml[0].signature == xml[0].signature


def test_credit_card_signs_are_left_alone():
    txs, _ = import_ofx(io.StringIO(SGML_CARD), "card1")
    # Negative is money leaving you on a card exactly as on a bank account.
    assert txs[0].amount == Money.parse("-89.99")
    assert txs[1].amount == Money.parse("200.00")


def test_flip_sign_for_issuers_that_export_charges_positive():
    txs, _ = import_ofx(io.StringIO(SGML_CARD), "card1", flip_sign=True)
    assert txs[0].amount == Money.parse("89.99")
    assert txs[1].amount == Money.parse("-200.00")


def test_missing_closing_stmttrn_tag():
    ofx = SGML_BANK.replace("</STMTTRN>", "", 1)
    txs, warnings = import_ofx(io.StringIO(ofx), "acct1")
    assert warnings == []
    assert len(txs) == 3
    assert txs[0].amount == Money.parse("-15.49")
    assert txs[1].amount == Money.parse("2400.00")


def test_trailing_balance_is_not_absorbed_into_the_last_transaction():
    # </BANKTRANLIST> has to close an unterminated <STMTTRN>, or LEDGERBAL's
    # own fields would leak into it.
    ofx = SGML_BANK.replace("<NAME>BARNES &amp; NOBLE #114 SAN FRANCISCO CA\n</STMTTRN>", "")
    txs, _ = import_ofx(io.StringIO(ofx), "acct1")
    assert txs[2].amount == Money.parse("-12.34")
    assert "1234.56" not in txs[2].description


def test_bad_entries_warn_instead_of_aborting():
    ofx = SGML_BANK.replace("<DTPOSTED>20260115", "<DTPOSTED>not-a-date").replace(
        "<TRNAMT>-12.34", "<TRNAMT>not-a-number"
    )
    txs, warnings = import_ofx(io.StringIO(ofx), "acct1")
    assert len(txs) == 1  # the good entry survives
    assert len(warnings) == 2
    assert all("FITID" in w for w in warnings)


def test_missing_amount_is_skipped_with_a_warning():
    ofx = SGML_BANK.replace("<TRNAMT>2400.00\n", "")
    txs, warnings = import_ofx(io.StringIO(ofx), "acct1")
    assert len(txs) == 2
    assert len(warnings) == 1
    assert "TRNAMT" in warnings[0]


def test_repeated_fitid_within_one_file_is_dropped():
    ofx = SGML_BANK.replace("<FITID>202601150002", "<FITID>202601140001")
    txs, warnings = import_ofx(io.StringIO(ofx), "acct1")
    assert len(txs) == 2
    assert "duplicate FITID" in warnings[0]


def test_amounts_are_exact():
    ofx = (
        SGML_BANK.replace("<TRNAMT>-15.49", "<TRNAMT>-0.10")
        .replace("<TRNAMT>2400.00", "<TRNAMT>-0.20")
        .replace("<TRNAMT>-12.34", "<TRNAMT>1234567.89")
    )
    txs, _ = import_ofx(io.StringIO(ofx), "acct1")
    assert txs[0].amount.minor == -10
    assert txs[1].amount.minor == -20
    # The sum a float would render as -0.30000000000000004.
    assert (txs[0].amount + txs[1].amount).minor == -30
    assert txs[2].amount.minor == 123456789


def test_comma_decimal_separator_and_currency_warning():
    ofx = SGML_BANK.replace("<CURDEF>USD", "<CURDEF>EUR").replace(
        "<TRNAMT>-15.49", "<TRNAMT>-15,49"
    )
    txs, warnings = import_ofx(io.StringIO(ofx), "acct1", currency="EUR")
    # OFX forbids thousands separators, so the comma is a decimal point.
    assert txs[0].amount == Money.parse("-15.49", "EUR")
    assert warnings == []

    _, warnings = import_ofx(io.StringIO(ofx), "acct1")
    assert any("CURDEF EUR" in w for w in warnings)


def test_entry_with_neither_name_nor_memo_falls_back_to_type():
    ofx = SGML_BANK.replace("<NAME>ACME CORP PAYROLL\n", "")
    txs, _ = import_ofx(io.StringIO(ofx), "acct1")
    assert txs[1].description == "(CREDIT)"


def test_non_ofx_file_raises():
    with pytest.raises(ImportError_):
        import_ofx(io.StringIO("Date,Description,Amount\n2026-01-14,NETFLIX,-15.49\n"), "acct1")


def test_ofx_without_a_statement_raises():
    signon_only = (
        "OFXHEADER:100\nDATA:OFXSGML\n\n<OFX>\n"
        "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
        "</SONRS></SIGNONMSGSRSV1>\n</OFX>\n"
    )
    with pytest.raises(ImportError_):
        import_ofx(io.StringIO(signon_only), "acct1")


def test_empty_transaction_list_warns_rather_than_raising():
    ofx = "OFXHEADER:100\n\n<OFX><BANKMSGSRSV1><STMTRS><CURDEF>USD\n<BANKTRANLIST>\n"
    ofx += "<DTSTART>20260101\n<DTEND>20260131\n</BANKTRANLIST></STMTRS></BANKMSGSRSV1></OFX>\n"
    txs, warnings = import_ofx(io.StringIO(ofx), "acct1")
    assert txs == []
    assert warnings == ["statement contains no transactions"]


def test_sgml_body_under_an_xml_declaration_still_imports():
    # Exporters do ship this; failing outright would cost the user the file.
    ofx = '<?xml version="1.0" encoding="UTF-8"?>\n' + SGML_BANK[SGML_BANK.index("<OFX>") :]
    txs, warnings = import_ofx(io.StringIO(ofx), "acct1")
    assert len(txs) == 3
    assert any("read as SGML" in w for w in warnings)


def test_reading_from_a_path(tmp_path):
    path = tmp_path / "statement.qfx"
    path.write_text(SGML_BANK, encoding="utf-8")
    txs, warnings = import_ofx(path, "acct1")
    assert warnings == []
    assert len(txs) == 3
