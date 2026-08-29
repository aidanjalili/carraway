"""Import transactions from OFX/QFX statement files.

OFX exists in two mutually incompatible flavours, and banks are split between
them, so both are handled here:

* **OFX 1.x is SGML, not XML.** Leaf tags are opened and never closed
  (``<TRNAMT>-15.49``), the file opens with colon-separated headers such as
  ``OFXHEADER:100``, and no XML parser will touch it. Most US banks — and
  every QFX file Quicken has ever written — still look like this, so this is
  the path that matters most.
* **OFX 2.x is real XML**, and goes through ``xml.etree``.

Standard library only: the core of this project takes no runtime dependencies
(see docs/ARCHITECTURE.md), and pulling in ofxparse or lxml to read what is
ultimately a tag soup would trade that away cheaply.

FITID is the bank's own durable identifier for a transaction and is a far
better dedupe key than the description fingerprint that
``Transaction.signature`` computes. Persisting it needs a schema migration and
a change to that signature, which would reach well beyond this importer, so
for now the FITID is used only to drop duplicates *within* a single file. A
future migration should store it on the transaction and prefer it over the
fingerprint when it is present. It is deliberately *not* written to ``notes``:
that field belongs to the user, and an opaque bank identifier sitting in it
would be noise they then have to clear by hand.
"""

from __future__ import annotations

import html
import io
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

from ..analysis.recurring import normalise_merchant
from ..core.models import Transaction, assign_occurrences
from ..core.money import Money
from .csv_importer import ImportError_

_TAG = re.compile(r"<\s*(/?)\s*([A-Za-z0-9._]+)\s*>")
_STATEMENT = re.compile(r"<\s*(?:STMTRS|CCSTMTRS|INVSTMTRS|BANKTRANLIST)\b", re.IGNORECASE)
_CURDEF = re.compile(r"<\s*CURDEF\s*>\s*([A-Za-z]{3})", re.IGNORECASE)
_LEADING_DATE = re.compile(r"\s*(\d{8})")

# Aggregates whose closing tag must end an open <STMTTRN>. A 1.x transaction
# often has no </STMTTRN> of its own, and without this the balance and date
# fields that follow the transaction list would be read as part of the last
# transaction.
_TXN_TERMINATORS = {"STMTTRN", "BANKTRANLIST", "STMTRS", "CCSTMTRS", "INVSTMTRS", "OFX"}

# Fields a transaction can carry more than once, e.g. a <PAYEE> aggregate
# nested inside <STMTTRN> repeats <NAME>. The outermost occurrence is the one
# the bank meant, so the parsers keep the first value they see.


def parse_ofx_date(value: str) -> date:
    """Read the date out of an OFX timestamp.

    >>> parse_ofx_date("20260114120000.000[-8:PST]")
    datetime.date(2026, 1, 14)
    >>> parse_ofx_date("20260114")
    datetime.date(2026, 1, 14)

    The time and timezone suffix are dropped rather than converted, because a
    charge the bank posted at 20:00 [-8:PST] belongs to the day printed on the
    statement, not to the following day in UTC.
    """
    match = _LEADING_DATE.match(value or "")
    if not match:
        raise ImportError_(f"Unrecognised OFX date: {value!r}")
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError as exc:
        raise ImportError_(f"Unrecognised OFX date: {value!r}") from exc


def _parse_amount(raw: str, currency: str) -> Money:
    # OFX forbids thousands separators, so a comma in TRNAMT is a decimal
    # point (European exporters write "-15,49"). Money.parse() treats commas
    # as grouping, which would silently turn -15,49 into -1549.00.
    text = raw.strip()
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    return Money.parse(text, currency)


def _read(source: Path | str | io.StringIO) -> str:
    if not isinstance(source, (str, Path)):
        return source.read()
    data = Path(source).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 1.x headers commonly declare CHARSET:1252. Falling back keeps
        # accented merchant names readable instead of losing the whole file.
        return data.decode("cp1252", errors="replace")


def _looks_like_xml(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<?xml") or bool(re.search(r'OFXHEADER\s*=\s*"2', text))


def _local(tag: str) -> str:
    """Tag name without any namespace, uppercased. 2.x files are usually
    namespace-free, but a few exporters wrap the document anyway."""
    return tag.rpartition("}")[2].upper()


def _xml_transactions(text: str) -> list[dict[str, str]]:
    # Start at <OFX> so the <?xml?> and <?OFX?> processing instructions, which
    # some exporters write malformed, never reach the parser.
    start = text.find("<OFX")
    root = ET.fromstring(text[start:] if start >= 0 else text)

    blocks: list[dict[str, str]] = []
    for node in root.iter():
        if _local(node.tag) != "STMTTRN":
            continue
        fields: dict[str, str] = {}
        for child in node.iter():
            if child is node:
                continue
            value = (child.text or "").strip()
            tag = _local(child.tag)
            if value and tag not in fields:
                fields[tag] = value
        blocks.append(fields)
    return blocks


def _sgml_transactions(text: str) -> list[dict[str, str]]:
    """Pull <STMTTRN> blocks out of an SGML body without an XML parser.

    A leaf value is whatever sits between its opening tag and the next tag, so
    banks that do close their leaf tags parse identically to banks that do not.
    """
    matches = list(_TAG.finditer(text))
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for index, match in enumerate(matches):
        tag = match.group(2).upper()
        if match.group(1):
            if tag in _TXN_TERMINATORS and current is not None:
                blocks.append(current)
                current = None
            continue
        if tag == "STMTTRN":
            if current is not None:
                blocks.append(current)
            current = {}
            continue
        if current is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = html.unescape(text[match.end() : end]).strip()
        if value and tag not in current:
            current[tag] = value

    if current is not None:
        blocks.append(current)
    return blocks


def _describe(name: str, memo: str, trntype: str) -> str:
    if name and memo and memo.upper() not in name.upper():
        return f"{name} - {memo}"
    if name or memo:
        return name or memo
    # Some banks send fee and ATM entries with neither NAME nor MEMO; the type
    # is at least something the user can recognise on their statement.
    kind = trntype.strip().upper()
    return f"({kind})" if kind else "(no description)"


def import_ofx(
    source: Path | str | io.StringIO,
    account_id: str,
    *,
    currency: str = "USD",
    flip_sign: bool = False,
) -> tuple[list[Transaction], list[str]]:
    """Parse an OFX/QFX statement into Transactions.

    Returns `(transactions, warnings)`. A malformed transaction produces a
    warning rather than aborting the import, because one bad entry in a
    2,000-transaction file should not cost the user the whole file.

    TRNAMT already follows Carraway's convention — negative is money leaving
    you — on both bank and credit-card statements, so it is used as written.
    `flip_sign` is there for the minority of issuers that export charges as
    positive anyway.
    """
    text = _read(source)
    upper = text.upper()
    if "<OFX" not in upper and "OFXHEADER" not in upper:
        raise ImportError_("File does not look like OFX/QFX: no <OFX> element or OFX header")
    if not _STATEMENT.search(text):
        raise ImportError_(
            "OFX file contains no statement (<STMTRS>, <CCSTMTRS> or <BANKTRANLIST>)"
        )

    warnings: list[str] = []
    declared = _CURDEF.search(text)
    if declared and declared.group(1).upper() != currency.upper():
        # Not fatal: the caller may be deliberately relabelling. Silence would
        # be worse, because the amounts would carry the wrong currency forever.
        warnings.append(
            f"file declares CURDEF {declared.group(1).upper()} but importing as {currency.upper()}"
        )

    if _looks_like_xml(text):
        try:
            blocks = _xml_transactions(text)
        except ET.ParseError as exc:
            # Exporters do ship SGML bodies under an XML declaration. The
            # tolerant reader copes with those, so degrade instead of failing.
            warnings.append(f"declared as XML but did not parse ({exc}); read as SGML")
            blocks = _sgml_transactions(text)
    else:
        blocks = _sgml_transactions(text)

    transactions: list[Transaction] = []
    seen_fitids: set[str] = set()

    for index, fields in enumerate(blocks, start=1):
        fitid = fields.get("FITID", "")
        label = f"transaction {index}" + (f" (FITID {fitid})" if fitid else "")
        try:
            when = parse_ofx_date(fields.get("DTPOSTED", ""))
            raw_amount = fields.get("TRNAMT", "").strip()
            if not raw_amount:
                warnings.append(f"{label}: no TRNAMT, skipped")
                continue
            amount = _parse_amount(raw_amount, currency)
            if flip_sign:
                amount = -amount
            if fitid and fitid in seen_fitids:
                # Statements that overlap two periods in one file repeat
                # entries verbatim; the bank's own id is proof they are the same.
                warnings.append(f"{label}: duplicate FITID, skipped")
                continue

            name = " ".join(fields.get("NAME", "").split())
            memo = " ".join(fields.get("MEMO", "").split())
            description = _describe(name, memo, fields.get("TRNTYPE", ""))

            transactions.append(
                Transaction(
                    id=uuid.uuid4().hex,
                    account_id=account_id,
                    date=when,
                    amount=amount,
                    description=description,
                    # From NAME alone where possible: MEMO often carries a
                    # per-charge reference that would fragment one merchant
                    # into many groups and hide a recurring series.
                    merchant=normalise_merchant(name or memo or description),
                )
            )
            if fitid:
                seen_fitids.add(fitid)
        except (ValueError, TypeError) as exc:
            warnings.append(f"{label}: {exc}")

    assign_occurrences(transactions)
    if not transactions and not warnings:
        warnings.append("statement contains no transactions")
    return transactions, warnings
