"""Import transactions from bank CSV exports.

There is no CSV standard among banks, so this module does two things: guess the
column mapping from the header row, and let the user override that guess when
the heuristics fail. Getting file import genuinely right is what makes Carraway
usable without paying a bank-aggregation provider.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..analysis.recurring import normalise_merchant
from ..core.models import Transaction
from ..core.money import Money

# Header names seen in the wild, lowercased. Order matters: earlier entries win.
_DATE_HEADERS = ["transaction date", "posted date", "post date", "date", "trans date"]
_DESC_HEADERS = ["description", "payee", "name", "merchant", "memo", "details"]
_AMOUNT_HEADERS = ["amount", "transaction amount", "value"]
_DEBIT_HEADERS = ["debit", "withdrawal", "withdrawals", "money out"]
_CREDIT_HEADERS = ["credit", "deposit", "deposits", "money in"]

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%b %d, %Y",
    "%d.%m.%Y",
]


class ImportError_(ValueError):
    """Raised when a CSV cannot be interpreted as transactions."""


@dataclass(slots=True)
class ColumnMap:
    """Which CSV columns hold which fields.

    Banks express amounts one of two ways: a single signed `amount` column, or
    separate `debit`/`credit` columns. Both are supported.
    """

    date: str
    description: str
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None
    # Set when the file is from a credit card that exports charges as positive.
    flip_sign: bool = False


def _find(headers: list[str], candidates: list[str]) -> str | None:
    lowered = {h.strip().lower(): h for h in headers}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    # Fall back to a substring match for headers like "Amount (USD)".
    for candidate in candidates:
        for low, original in lowered.items():
            if candidate in low:
                return original
    return None


def guess_columns(headers: list[str]) -> ColumnMap:
    """Infer a ColumnMap from a header row, raising if the essentials are missing."""
    date_col = _find(headers, _DATE_HEADERS)
    desc_col = _find(headers, _DESC_HEADERS)
    if not date_col:
        raise ImportError_(f"No date column found in headers: {headers}")
    if not desc_col:
        raise ImportError_(f"No description column found in headers: {headers}")

    amount_col = _find(headers, _AMOUNT_HEADERS)
    debit_col = _find(headers, _DEBIT_HEADERS)
    credit_col = _find(headers, _CREDIT_HEADERS)
    if not amount_col and not (debit_col or credit_col):
        raise ImportError_(f"No amount, debit or credit column found in: {headers}")

    return ColumnMap(
        date=date_col,
        description=desc_col,
        amount=amount_col,
        debit=debit_col,
        credit=credit_col,
    )


def parse_date(value: str) -> date:
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ImportError_(f"Unrecognised date format: {value!r}") from exc


def _row_amount(row: dict[str, str], mapping: ColumnMap, currency: str) -> Money | None:
    """Read one row's amount, normalising to 'negative means money out'."""
    if mapping.amount:
        raw = (row.get(mapping.amount) or "").strip()
        if not raw:
            return None
        amount = Money.parse(raw, currency)
    else:
        debit = (row.get(mapping.debit) or "").strip() if mapping.debit else ""
        credit = (row.get(mapping.credit) or "").strip() if mapping.credit else ""
        if debit:
            amount = -abs(Money.parse(debit, currency))
        elif credit:
            amount = abs(Money.parse(credit, currency))
        else:
            return None
    return -amount if mapping.flip_sign else amount


def import_csv(
    source: Path | str | io.StringIO,
    account_id: str,
    *,
    mapping: ColumnMap | None = None,
    currency: str = "USD",
    flip_sign: bool = False,
) -> tuple[list[Transaction], list[str]]:
    """Parse a CSV into Transactions.

    Returns `(transactions, warnings)`. A malformed row produces a warning
    rather than aborting the import, because a single bad row in a 2,000-row
    export should not cost the user the whole file.
    """
    if isinstance(source, (str, Path)):
        text = Path(source).read_text(encoding="utf-8-sig")
    else:
        text = source.read()

    # Sniff the delimiter; some European exports use semicolons.
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ImportError_("CSV appears to be empty (no header row)")

    headers = [h for h in reader.fieldnames if h]
    mapping = mapping or guess_columns(headers)
    mapping.flip_sign = flip_sign or mapping.flip_sign

    transactions: list[Transaction] = []
    warnings: list[str] = []

    for line_no, row in enumerate(reader, start=2):
        try:
            when = parse_date(row.get(mapping.date) or "")
            amount = _row_amount(row, mapping, currency)
            if amount is None:
                warnings.append(f"line {line_no}: no amount, skipped")
                continue
            description = " ".join((row.get(mapping.description) or "").split())
            if not description:
                description = "(no description)"

            tx = Transaction(
                id=uuid.uuid4().hex,
                account_id=account_id,
                date=when,
                amount=amount,
                description=description,
                merchant=normalise_merchant(description),
            )
            transactions.append(tx)
        except (ValueError, TypeError) as exc:
            warnings.append(f"line {line_no}: {exc}")

    if not transactions and not warnings:
        warnings.append("no data rows found below the header")
    return transactions, warnings
