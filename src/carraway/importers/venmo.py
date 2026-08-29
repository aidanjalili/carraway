"""Import transactions from a Venmo CSV account statement.

Venmo retired its Developer/Payouts API, so the only supported way to get
personal transaction history out is the statement export at
venmo.com -> Statements -> Download CSV. That export is capped at 90 days,
which means a user with a year of history has *several overlapping files*
rather than one, and correct deduplication is a requirement rather than a
nicety.

The file itself is not a plain CSV table:

* It opens with **preamble junk** — an "Account Statement" title line, the
  account holder's name, and one or two blank lines — so the header row is
  not line 1 and has to be located by content.
* It closes with **trailer rows**: a closing-balance summary that has no
  Transaction ID and no date. Venmo also writes an opening-balance row
  immediately below the header. Both are recognised by the empty ID.
* Amounts are written ``- $25.00`` / ``+ $10.00`` — sign, space, symbol,
  number. ``Money.parse`` copes with ``$`` and thousands separators but not
  with a sign detached from its digits, so the sign is normalised here.

Venmo's own sign convention already matches Carraway's — money you send is
negative, money you receive is positive — so amounts are used as written.

The "Transaction ID" is Venmo's durable identifier for a payment and is a far
better dedupe key than the description fingerprint that
``Transaction.signature`` computes, which matters more here than anywhere else
because the 90-day cap guarantees overlapping exports. Persisting it needs a
schema migration and a change to that signature, which would reach well beyond
this importer, so for now the ID is used only to drop duplicates *within* a
single file. A future migration should store it on the transaction and prefer
it over the fingerprint when it is present. It is deliberately *not* written to
``notes``: that field belongs to the user, and an opaque identifier sitting in
it would be noise they then have to clear by hand.

Standard library only: the core of this project takes no runtime dependencies
(see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import csv
import html
import io
import re
import uuid
from datetime import date
from pathlib import Path

from ..analysis.recurring import normalise_merchant
from ..core.models import Transaction, assign_occurrences
from ..core.money import Money
from .csv_importer import ImportError_

# Column names as Venmo writes them, lowercased. Everything except the ID, the
# datetime and the total is optional: exports differ between years and between
# personal and business accounts, and a missing "Terminal Location" is no
# reason to refuse the file.
_ID_HEADERS = ("transaction id", "id")
_DATETIME_HEADER = "datetime"
_AMOUNT_HEADER = "amount (total)"

# Money moving between the user's Venmo balance and their own bank card or
# account, rather than to another person.
_TRANSFER_TYPES = frozenset({"standard transfer", "instant transfer"})

_SIGN = re.compile(r"^([+-])\s*(.+)$")


def _norm(cell: str) -> str:
    """Collapse whitespace and lowercase, for matching header cells by name."""
    return " ".join(cell.split()).lower()


def _clean(value: str) -> str:
    # Same reason as the CSV importer: one row writing "C &amp; S" where the
    # rest write "C & S" would otherwise split one counterparty into two.
    return " ".join(html.unescape(value).split())


def parse_venmo_datetime(value: str) -> date:
    """Read the calendar date out of a Venmo ISO-8601 timestamp.

    >>> parse_venmo_datetime("2026-01-14T18:32:05")
    datetime.date(2026, 1, 14)
    >>> parse_venmo_datetime("2026-01-14")
    datetime.date(2026, 1, 14)

    The clock time is dropped rather than converted. Venmo stamps in the
    account's own timezone, and a payment sent at 23:40 belongs to the day the
    user remembers sending it, not to the next day in UTC.
    """
    text = " ".join((value or "").split())
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ImportError_(f"Unrecognised Venmo datetime: {value!r}") from exc


def parse_venmo_amount(raw: str, currency: str = "USD") -> Money:
    """Parse a Venmo amount such as "- $25.00", where the sign stands apart.

    >>> parse_venmo_amount("- $25.00").minor
    -2500
    >>> parse_venmo_amount("+ $10.00").minor
    1000
    >>> parse_venmo_amount("$1,234.56").minor
    123456

    ``Money.parse`` strips currency symbols and grouping commas but hands
    "- $25.00" to Decimal as "- 25.00", which is not a number. Splitting the
    leading sign off first is the whole trick, and getting it wrong would
    silently import every payment sent as income.
    """
    text = " ".join((raw or "").split())
    if not text:
        raise ValueError("Cannot parse an empty amount")

    match = _SIGN.match(text)
    if not match:
        return Money.parse(text, currency)
    # An explicit sign is Venmo's statement of direction, so it wins outright.
    amount = abs(Money.parse(match.group(2), currency))
    return -amount if match.group(1) == "-" else amount


def _read(source: Path | str | io.StringIO) -> str:
    if isinstance(source, (str, Path)):
        return Path(source).read_text(encoding="utf-8-sig")
    return source.read()


def _find_header(rows: list[list[str]]) -> int:
    """Index of the real header row, past the title and account-holder lines.

    Matched by content rather than by position because the preamble is not a
    fixed number of lines: it carries the statement period and the account
    holder's name, and blank lines come and go between exports.
    """
    for index, row in enumerate(rows):
        cells = {_norm(cell) for cell in row}
        if "transaction id" in cells:
            return index
        # Older and current exports label the column plain "ID", so fall back
        # to the pair of columns no other file in this codebase's world has.
        if _DATETIME_HEADER in cells and any(c.startswith("amount (total") for c in cells):
            return index
    raise ImportError_(
        "File does not look like a Venmo statement: no header row with "
        "'Transaction ID', or 'Datetime' and 'Amount (total)'"
    )


def _columns(header: list[str]) -> dict[str, int]:
    """Map header name -> column index, so nothing depends on column order.

    Venmo's first column is unnamed and its column set drifts between exports,
    so positions are never assumed. The first occurrence of a name wins.
    """
    columns: dict[str, int] = {}
    for index, cell in enumerate(header):
        name = _norm(cell)
        if name and name not in columns:
            columns[name] = index
    return columns


def _cell(row: list[str], index: int | None) -> str:
    """One field, tolerating short rows and absent optional columns."""
    if index is None or index >= len(row):
        return ""
    return _clean(row[index])


def _counterparty(kind: str, outgoing: bool, fields: dict[str, str]) -> str:
    """The other party to the payment — never the user themselves.

    Which of From/To that is depends on the direction of the money: on a
    payment the user sent, the user *is* "From" and the counterparty is "To";
    on one they received it is the other way round. Reading the direction off
    the sign means the importer never needs to know the account holder's name,
    which is only ever available as free text in the preamble.

    Transfers are the exception. Both ends belong to the user, so "From"/"To"
    hold the user's own name, and the bank named in Destination (money out) or
    Funding Source (money in) is the only informative side.
    """
    if kind in _TRANSFER_TYPES:
        return fields["destination"] if outgoing else fields["funding source"]
    return fields["to"] if outgoing else fields["from"]


# A note long enough to name a service rather than label a coffee. Short
# notes ("gas", "food") describe an occasion; longer ones tend to name the
# thing being paid for.
_MEANINGFUL_NOTE = 6


def _merchant_key(counterparty: str, note: str, description: str) -> str:
    """The merchant name a recurring series should be grouped and shown under.

    Paying one person for several different things is normal — a phone bill, a
    streaming subscription, dinner — so the counterparty alone is not enough to
    identify what recurs, and a view that says "Ali Jalili, $35 monthly" tells
    the user nothing they did not already know.

    Folding the note in gives "ALI JALILI T MOBILE PAYMENT" instead. Ad-hoc
    notes are all different and so form no series, which is the correct
    outcome; a repeated note is exactly the signal that something recurs.
    Punctuation and case are normalised away, so "T-Mobile Payment" and "T
    mobile payment" are one series and a price change between them is visible.
    """
    if counterparty and len(note.strip()) >= _MEANINGFUL_NOTE:
        # Hyphens are flattened in the note specifically. A note is free text
        # someone typed, and the same person writes "T-Mobile Payment" one
        # month and "T mobile payment" the next; left alone that is two series
        # of one subscription. Bank descriptors are machine-generated and
        # consistent, so the shared normaliser leaves their hyphens alone.
        flattened = note.replace("-", " ")
        return normalise_merchant(f"{counterparty} {flattened}")
    return normalise_merchant(counterparty or description)


def _describe(kind: str, counterparty: str, note: str, transfer: bool) -> str:
    parts = [part for part in (counterparty, note) if part]
    if transfer:
        # carraway.analysis.transfers pairs the two halves of a transfer once
        # the bank side is imported too, but it gates on transfer wording in
        # the description and deliberately does not treat "Venmo" as such —
        # Venmo moves money to other people as often as between your own
        # accounts. This prefix is what lets the matcher see the pair, and it
        # is why no transfer_group is invented here: only the matcher, which
        # can see both halves, is in a position to assign one.
        return "Transfer: " + (" - ".join(parts) if parts else kind or "Venmo")
    if parts:
        return " - ".join(parts)
    return f"({kind})" if kind else "(no description)"


def import_venmo(
    source: Path | str | io.StringIO,
    account_id: str,
    *,
    currency: str = "USD",
) -> tuple[list[Transaction], list[str]]:
    """Parse a Venmo CSV statement into Transactions.

    Returns `(transactions, warnings)`. A malformed row produces a warning
    rather than aborting the import, because a single bad row in a 90-day
    export should not cost the user the whole file.

    Amounts keep the sign Venmo wrote, which already follows Carraway's
    convention that negative is money leaving you.
    """
    text = _read(source)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ImportError_("CSV appears to be empty")

    header_index = _find_header(rows)
    columns = _columns(rows[header_index])

    id_col = next((columns[name] for name in _ID_HEADERS if name in columns), None)
    datetime_col = columns.get(_DATETIME_HEADER)
    amount_col = next(
        (index for name, index in columns.items() if name.startswith("amount (total")), None
    )
    if datetime_col is None or amount_col is None:
        raise ImportError_(
            f"Venmo statement is missing a required column "
            f"('Datetime', 'Amount (total)'): {rows[header_index]}"
        )

    transactions: list[Transaction] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    if id_col is None:
        warnings.append("no Transaction ID column: duplicates within the file cannot be detected")

    for line_no, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        tx_id = _cell(row, id_col)
        # The opening- and closing-balance summary rows Venmo brackets the
        # transactions with carry no ID, and neither do blank spacer lines.
        # Neither is a movement of money, so both go without a warning.
        if id_col is not None and not tx_id:
            continue
        if not any(cell.strip() for cell in row):
            continue

        label = f"line {line_no}" + (f" (ID {tx_id})" if tx_id else "")
        try:
            fields = {
                name: _cell(row, columns.get(name))
                for name in (
                    "type",
                    "status",
                    "note",
                    "from",
                    "to",
                    "funding source",
                    "destination",
                )
            }

            status = fields["status"]
            # An absent Status column is not evidence that nothing happened;
            # only a stated status other than Complete is. Cancelled, failed
            # and pending money never left the account.
            if status and status.lower() != "complete":
                warnings.append(f"{label}: status {status!r}, skipped")
                continue

            if tx_id and tx_id in seen_ids:
                # Consecutive 90-day exports overlap, and users concatenate
                # them; Venmo's own id is proof two rows are one payment.
                warnings.append(f"{label}: duplicate Transaction ID, skipped")
                continue

            when = parse_venmo_datetime(_cell(row, datetime_col))
            raw_amount = _cell(row, amount_col)
            if not raw_amount:
                warnings.append(f"{label}: no amount, skipped")
                continue
            # The tip/tax/fee columns are components of the total, not extras
            # alongside it, so the total is the only figure that is spent.
            amount = parse_venmo_amount(raw_amount, currency)

            kind = fields["type"]
            transfer = kind.lower() in _TRANSFER_TYPES
            counterparty = _counterparty(kind.lower(), amount.minor < 0, fields)
            description = _describe(kind, counterparty, fields["note"], transfer)

            transactions.append(
                Transaction(
                    id=uuid.uuid4().hex,
                    account_id=account_id,
                    date=when,
                    amount=amount,
                    description=description,
                    merchant=_merchant_key(counterparty, fields["note"], description),
                )
            )
            if tx_id:
                seen_ids.add(tx_id)
        except (ValueError, TypeError) as exc:
            warnings.append(f"{label}: {exc}")

    assign_occurrences(transactions)
    if not transactions and not warnings:
        warnings.append("statement contains no transactions")
    return transactions, warnings


def looks_like_venmo(source: Path | str) -> bool:
    """True if this CSV is a Venmo statement rather than a bank export.

    Venmo's export is a .csv like any other, so the import command sniffs it
    instead of asking the user which reader to use. Reuses the same header
    detection the importer relies on, so the two can never disagree about what
    counts as a Venmo file.
    """
    try:
        text = _read(source)
        rows = list(csv.reader(io.StringIO(text)))
        _find_header(rows)
    except (ImportError_, OSError, UnicodeDecodeError, csv.Error):
        return False
    return True


def statement_balance(source: Path | str | io.StringIO) -> tuple[date, Money] | None:
    """The closing balance a statement reports, and the day it applies to.

    Venmo brackets its transactions with an opening and a closing balance row,
    neither of which carries a transaction id. That closing figure is the only
    place a Venmo balance appears at all — there is no API to ask — so it is
    what makes a Venmo account countable towards net worth.

    Returns None when the file carries no closing figure, which is not an
    error: the transactions are still perfectly good.
    """
    try:
        text = _read(source)
        rows = list(csv.reader(io.StringIO(text)))
        header_index = _find_header(rows)
    except (ImportError_, OSError, UnicodeDecodeError, csv.Error):
        return None

    columns = _columns(rows[header_index])
    ending = columns.get("ending balance")
    if ending is None:
        return None

    closing: Money | None = None
    for row in rows[header_index + 1 :]:
        raw = _cell(row, ending).strip()
        if not raw:
            continue
        try:
            closing = parse_venmo_amount(raw, "USD")
        except (ValueError, TypeError):
            continue

    if closing is None:
        return None

    # Dated to the last transaction in the file rather than today: a statement
    # downloaded months later still describes the balance at its own period
    # end, and dating it now would put a stale figure at the front of the
    # net worth history.
    latest = date.min
    datetime_column = columns.get(_DATETIME_HEADER)
    if datetime_column is not None:
        for row in rows[header_index + 1 :]:
            stamp = _cell(row, datetime_column).strip()
            if not stamp:
                continue
            try:
                latest = max(latest, parse_venmo_datetime(stamp))
            except ImportError_:
                continue
    return (latest if latest != date.min else date.today()), abs(closing)
