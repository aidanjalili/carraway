"""Export a ledger to a spreadsheet the user can open in LibreOffice Calc.

An OpenDocument Spreadsheet is a ZIP package of XML, which the standard library
can write on its own. That matters here: the project takes no runtime
dependencies, and a spreadsheet exporter is not worth breaking that rule for.

Two things are easy to get wrong and both are handled deliberately below.

* The `mimetype` entry must be the **first** entry in the zip and must be
  **stored uncompressed**, otherwise the package is not identifiable as ODF and
  readers reject it before looking at any XML.
* Amounts must be written as ``office:value-type="float"``. A cell holding the
  string "-15.49" looks identical on screen and cannot be summed, which defeats
  the point of exporting to a spreadsheet at all. The number is rendered from
  `Money.decimal`, an exact Decimal, so no float ever touches the output.
"""

from __future__ import annotations

import csv
import re
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from ..analysis.categorize import UNCATEGORIZED
from ..core.models import Account, RecurringSeries, Transaction
from ..core.money import Money, exponent_for

MIMETYPE = "application/vnd.oasis.opendocument.spreadsheet"

# ODF 1.3 is what LibreOffice has written by default since 7.0 and what Excel's
# ODS reader is happiest with.
_ODF_VERSION = "1.3"

_NAMESPACES = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:number="urn:oasis:names:tc:opendocument:xmlns:datastyle:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"'
)

# A fixed timestamp keeps two exports of the same ledger byte-identical, so a
# user versioning their exports sees a diff only when the data actually changed.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# Everything XML 1.0 forbids outright, as opposed to merely needing escaping.
_ILLEGAL_XML = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")

_HEADER_STYLE = "ce-header"
_DATE_STYLE = "ce-date"


class ExportError(ValueError):
    """Raised when a ledger cannot be written to a spreadsheet."""


# -- XML plumbing --------------------------------------------------------


def _clean(value: str) -> str:
    """Drop characters XML 1.0 cannot represent at all.

    Bank descriptions occasionally carry a stray control byte from a fixed-width
    mainframe export. Escaping does not help — those code points are simply
    illegal in XML — so a reader would refuse the whole document over one row.
    """
    return _ILLEGAL_XML.sub("", value)


def _text(value: str) -> str:
    return escape(_clean(value))


def _attr(value: str) -> str:
    return quoteattr(_clean(value))


def _money_style(exponent: int) -> str:
    return f"ce-money-{exponent}"


def _string_cell(value: str, *, style: str = "") -> str:
    style_attr = f" table:style-name={_attr(style)}" if style else ""
    if not value:
        # An empty cell carries no value type; writing an empty string instead
        # makes Calc treat the column as text and refuse to sort it as numbers.
        return f"<table:table-cell{style_attr}/>"
    return (
        f'<table:table-cell{style_attr} office:value-type="string">'
        f"<text:p>{_text(value)}</text:p></table:table-cell>"
    )


def _money_cell(amount: Money) -> str:
    exponent = exponent_for(amount.currency)
    # `:f` on a Decimal is exact and never switches to scientific notation,
    # which office:value would not accept.
    value = f"{amount.decimal:f}"
    return (
        f'<table:table-cell table:style-name="{_money_style(exponent)}" '
        f'office:value-type="float" office:value="{value}">'
        f"<text:p>{value}</text:p></table:table-cell>"
    )


def _number_cell(value: int) -> str:
    return (
        f'<table:table-cell office:value-type="float" office:value="{value}">'
        f"<text:p>{value}</text:p></table:table-cell>"
    )


def _date_cell(when: date) -> str:
    iso = when.isoformat()
    return (
        f'<table:table-cell table:style-name="{_DATE_STYLE}" office:value-type="date" '
        f'office:date-value="{iso}"><text:p>{iso}</text:p></table:table-cell>'
    )


def _row(cells: Iterable[str]) -> str:
    return "<table:table-row>" + "".join(cells) + "</table:table-row>"


def _table(name: str, headers: Sequence[str], rows: Iterable[Sequence[str]]) -> Iterator[str]:
    """Yield one sheet, a chunk at a time so a large ledger never lands in memory."""
    yield f"<table:table table:name={_attr(name)}>"
    yield f'<table:table-column table:number-columns-repeated="{len(headers)}"/>'
    yield _row(_string_cell(h, style=_HEADER_STYLE) for h in headers)
    for cells in rows:
        yield _row(cells)
    yield "</table:table>"


def _automatic_styles(exponents: Iterable[int]) -> str:
    """Number and date formats, so Calc displays what it is holding.

    Without a date format a date cell shows its serial number, which is
    technically correct and useless to read.
    """
    parts = [
        "<office:automatic-styles>",
        '<number:date-style style:name="N-date">',
        '<number:year number:style="long"/><number:text>-</number:text>',
        '<number:month number:style="long"/><number:text>-</number:text>',
        '<number:day number:style="long"/>',
        "</number:date-style>",
        f'<style:style style:name="{_DATE_STYLE}" style:family="table-cell" '
        'style:data-style-name="N-date"/>',
        f'<style:style style:name="{_HEADER_STYLE}" style:family="table-cell">'
        '<style:text-properties fo:font-weight="bold"/></style:style>',
    ]
    for exponent in sorted(exponents):
        # One format per currency exponent, so JPY does not display as "1234.00"
        # and KWD does not lose its third decimal place.
        parts.append(
            f'<number:number-style style:name="N-money-{exponent}">'
            f'<number:number number:decimal-places="{exponent}" '
            f'number:min-decimal-places="{exponent}" number:min-integer-digits="1" '
            'number:grouping="true"/></number:number-style>'
        )
        parts.append(
            f'<style:style style:name="{_money_style(exponent)}" style:family="table-cell" '
            f'style:data-style-name="N-money-{exponent}"/>'
        )
    parts.append("</office:automatic-styles>")
    return "".join(parts)


# -- the sheets ----------------------------------------------------------


def _categories_of(
    transactions: Sequence[Transaction], categories: Sequence[str] | None
) -> list[str]:
    """One category per transaction, in order.

    `categories` is the parallel list `analysis.categorize.categorize_all`
    returns, which is how a caller exports a categorisation that has not been
    saved to the database yet. Without it we fall back to what each row carries.
    """
    if categories is None:
        return [tx.category or UNCATEGORIZED for tx in transactions]
    if len(categories) != len(transactions):
        raise ExportError(
            f"Got {len(categories)} categories for {len(transactions)} transactions; "
            f"the two lists must line up row for row."
        )
    return [c or UNCATEGORIZED for c in categories]


def _transaction_rows(
    transactions: Sequence[Transaction],
    categories: Sequence[str],
    account_names: dict[str, str],
) -> Iterator[list[str]]:
    for tx, category in zip(transactions, categories, strict=True):
        yield [
            _date_cell(tx.date),
            _string_cell(account_names.get(tx.account_id, tx.account_id)),
            _string_cell(tx.description),
            _string_cell(tx.merchant),
            _string_cell(category),
            _money_cell(tx.amount),
        ]


def _category_rows(
    transactions: Sequence[Transaction], categories: Sequence[str]
) -> Iterator[list[str]]:
    """Spending per category, biggest first.

    Keyed by currency as well as category because Money refuses to add across
    currencies, and a multi-currency ledger must still export rather than raise.
    Transfers are left out: moving your own money between your own accounts is
    not spending, and counting it would double every payment.
    """
    totals: dict[tuple[str, str], list[int]] = {}
    for tx, category in zip(transactions, categories, strict=True):
        if tx.is_transfer or not tx.is_outflow:
            continue
        bucket = totals.setdefault((category, tx.amount.currency), [0, 0])
        bucket[0] += -tx.amount.minor
        bucket[1] += 1

    ordered = sorted(totals.items(), key=lambda item: item[1][0], reverse=True)
    for (category, currency), (minor, count) in ordered:
        yield [
            _string_cell(category),
            _string_cell(currency),
            _money_cell(Money(minor, currency)),
            _number_cell(count),
        ]


def _subscription_rows(series: Sequence[RecurringSeries]) -> Iterator[list[str]]:
    ordered = sorted(series, key=lambda s: s.annualised.minor, reverse=True)
    for entry in ordered:
        yield [
            _string_cell(entry.merchant),
            _string_cell(entry.cadence),
            _money_cell(abs(entry.typical_amount)),
            _money_cell(entry.annualised),
        ]


# Sections and subtotals, which is what makes a sheet read like something a
# person laid out rather than a table dump. Modelled on the workbook a real
# user kept by hand before this app existed: assets then liabilities then a
# net worth line; subscriptions split into monthly and yearly with a subtotal
# for each and a grand total under both.
_PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
_MONTHLY_CADENCES = ("weekly", "biweekly", "monthly")


def _blank_row() -> list[str]:
    return [_string_cell("")]


def _section(title: str) -> list[str]:
    return [_string_cell(title, style=_HEADER_STYLE)]


def _monthly_equivalent(series: RecurringSeries) -> Money:
    """What a series costs in an average month, whatever its cadence.

    A quarterly premium is a third of itself each month; a biweekly charge is
    26 payments a year rather than 24. Comparing cadences without this is the
    error that makes a hand-kept spreadsheet wrong.
    """
    yearly = abs(series.typical_amount) * _PER_YEAR.get(series.cadence, 0)
    return Money(round(yearly.minor / 12), yearly.currency)


def _balance_rows(
    accounts: Sequence[Account], balances: dict[str, Money], *, liabilities: bool
) -> Iterator[list[str]]:
    """Assets or liabilities, with a total, laid out as a person would."""
    total_minor = 0
    currency = "USD"
    for account in sorted(accounts, key=lambda a: a.name.lower()):
        if account.type.is_liability != liabilities:
            continue
        balance = balances.get(account.id)
        if balance is None:
            yield [
                _string_cell(account.name),
                _string_cell(str(account.type)),
                _string_cell("no balance recorded"),
            ]
            continue
        shown = abs(balance) if liabilities else balance
        currency = shown.currency
        total_minor += shown.minor
        yield [
            _string_cell(account.name),
            _string_cell(str(account.type)),
            _money_cell(shown),
        ]

    yield _blank_row()
    yield [
        _string_cell("Total " + ("liabilities" if liabilities else "assets"), style=_HEADER_STYLE),
        _string_cell(""),
        _money_cell(Money(total_minor, currency)),
    ]


def _networth_rows(accounts: Sequence[Account], balances: dict[str, Money]) -> Iterator[list[str]]:
    """Assets, then liabilities, then the one line that matters."""
    yield from _section("Assets")
    assets = 0
    liabilities = 0
    currency = "USD"

    for account in sorted(accounts, key=lambda a: a.name.lower()):
        balance = balances.get(account.id)
        if balance is None or account.type.is_liability:
            continue
        currency = balance.currency
        assets += balance.minor
        yield [_string_cell(account.name), _string_cell(str(account.type)), _money_cell(balance)]

    yield [
        _string_cell("Total assets", style=_HEADER_STYLE),
        _string_cell(""),
        _money_cell(Money(assets, currency)),
    ]
    yield _blank_row()

    yield from _section("Liabilities")
    for account in sorted(accounts, key=lambda a: a.name.lower()):
        balance = balances.get(account.id)
        if balance is None or not account.type.is_liability:
            continue
        owed = abs(balance)
        liabilities += owed.minor
        yield [_string_cell(account.name), _string_cell(str(account.type)), _money_cell(owed)]

    yield [
        _string_cell("Total liabilities", style=_HEADER_STYLE),
        _string_cell(""),
        _money_cell(Money(liabilities, currency)),
    ]
    yield _blank_row()

    yield [
        _string_cell("NET WORTH", style=_HEADER_STYLE),
        _string_cell("assets less liabilities"),
        _money_cell(Money(assets - liabilities, currency)),
    ]

    missing = [a.name for a in accounts if a.id not in balances]
    if missing:
        yield _blank_row()
        yield [_string_cell("Not counted, no balance recorded: " + ", ".join(missing))]


def _autopay_rows(
    series: Sequence[RecurringSeries],
    kinds: dict[str, str],
    paid_via: dict[str, str],
    account_names: dict[str, str] | None = None,
) -> Iterator[list[str]]:
    """Subscriptions and bills, split by cadence with a subtotal for each.

    Mirrors the layout of a hand-kept sheet: everything billed within a month
    together with its monthly total, everything billed yearly together with
    its annual total, then one figure for the year covering both.
    """
    monthly = [s for s in series if s.cadence in _MONTHLY_CADENCES]
    longer = [s for s in series if s.cadence not in _MONTHLY_CADENCES]
    currency = series[0].typical_amount.currency if series else "USD"

    def emit(group: Sequence[RecurringSeries], title: str) -> Iterator[list[str]]:
        if not group:
            return
        yield from _section(title)
        for item in sorted(group, key=lambda s: -abs(s.annualised.minor)):
            when = item.next_expected.isoformat() if item.next_expected else "—"
            # A detected series was charged to an account, and that is its
            # payment method. Only entries the user typed in need the note
            # they wrote, which is what paid_via holds.
            method = paid_via.get(item.merchant.upper()) or (
                (account_names or {}).get(item.account_id, "")
            )
            yield [
                _string_cell(item.merchant),
                _money_cell(abs(item.typical_amount)),
                _string_cell(item.cadence),
                _string_cell(when),
                _string_cell(method),
                _string_cell(kinds.get(item.merchant.upper(), "")),
                _money_cell(_monthly_equivalent(item)),
                _money_cell(abs(item.annualised)),
            ]
        yield _blank_row()

    yield from emit(monthly, "Billed monthly or more often")
    if monthly:
        per_month = sum(_monthly_equivalent(s).minor for s in monthly)
        yield [
            _string_cell("Total per month", style=_HEADER_STYLE),
            _string_cell(""),
            _string_cell(""),
            _string_cell(""),
            _string_cell(""),
            _string_cell(""),
            _money_cell(Money(per_month, currency)),
            _money_cell(Money(per_month * 12, currency)),
        ]
        yield _blank_row()

    yield from emit(longer, "Billed quarterly or yearly")
    if longer:
        per_year = sum(abs(s.annualised.minor) for s in longer)
        yield [
            _string_cell("Total per year", style=_HEADER_STYLE),
            _string_cell(""),
            _string_cell(""),
            _string_cell(""),
            _string_cell(""),
            _string_cell(""),
            _money_cell(Money(round(per_year / 12), currency)),
            _money_cell(Money(per_year, currency)),
        ]
        yield _blank_row()

    grand = sum(abs(s.annualised.minor) for s in series)
    yield [
        _string_cell("EVERYTHING, PER YEAR", style=_HEADER_STYLE),
        _string_cell(""),
        _string_cell(""),
        _string_cell(""),
        _string_cell(""),
        _string_cell(""),
        _money_cell(Money(round(grand / 12), currency)),
        _money_cell(Money(grand, currency)),
    ]


def _month_rows(
    transactions: Sequence[Transaction], categories: Sequence[str]
) -> Iterator[list[str]]:
    """Income, spending and the difference, one row a month."""
    from collections import defaultdict

    income: dict[str, int] = defaultdict(int)
    spending: dict[str, int] = defaultdict(int)
    currency = "USD"
    for tx, name in zip(transactions, categories, strict=True):
        if tx.is_transfer or name == "Transfer":
            continue
        key = f"{tx.date.year}-{tx.date.month:02d}"
        currency = tx.amount.currency
        if tx.is_outflow:
            spending[key] += abs(tx.amount.minor)
        else:
            income[key] += tx.amount.minor

    for key in sorted(set(income) | set(spending)):
        got, spent = income[key], spending[key]
        yield [
            _string_cell(key),
            _money_cell(Money(got, currency)),
            _money_cell(Money(spent, currency)),
            _money_cell(Money(got - spent, currency)),
        ]


def _content_xml(
    transactions: Sequence[Transaction],
    categories: Sequence[str],
    account_names: dict[str, str],
    series: Sequence[RecurringSeries] | None,
    accounts: Sequence[Account] | None = None,
    balances: dict[str, Money] | None = None,
    kinds: dict[str, str] | None = None,
    paid_via: dict[str, str] | None = None,
) -> Iterator[str]:
    exponents = {2}
    exponents.update(exponent_for(tx.amount.currency) for tx in transactions)
    if series:
        exponents.update(exponent_for(s.typical_amount.currency) for s in series)

    yield '<?xml version="1.0" encoding="UTF-8"?>'
    yield f'<office:document-content {_NAMESPACES} office:version="{_ODF_VERSION}">'
    yield _automatic_styles(exponents)
    yield "<office:body><office:spreadsheet>"

    if accounts and balances:
        yield from _table(
            "Net Worth",
            ["Account", "Type", "Amount"],
            _networth_rows(accounts, balances),
        )
    if series:
        yield from _table(
            "Auto-Pay",
            [
                "Service",
                "Price",
                "Per unit time",
                "Next billed",
                "Payment method",
                "Kind",
                "Per month",
                "Per year",
            ],
            _autopay_rows(series, kinds or {}, paid_via or {}, account_names),
        )
    yield from _table(
        "By Month",
        ["Month", "Income", "Spending", "Net"],
        _month_rows(transactions, categories),
    )
    yield from _table(
        "Transactions",
        ["Date", "Account", "Description", "Merchant", "Category", "Amount"],
        _transaction_rows(transactions, categories, account_names),
    )
    yield from _table(
        "Categories",
        ["Category", "Currency", "Total Spent", "Transactions"],
        _category_rows(transactions, categories),
    )

    yield "</office:spreadsheet></office:body></office:document-content>"


def _manifest_xml(entries: Sequence[str]) -> str:
    files = "".join(
        f'<manifest:file-entry manifest:full-path="{name}" manifest:media-type="text/xml"/>'
        for name in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        f'manifest:version="{_ODF_VERSION}">'
        f'<manifest:file-entry manifest:full-path="/" manifest:version="{_ODF_VERSION}" '
        f'manifest:media-type="{MIMETYPE}"/>'
        f"{files}</manifest:manifest>"
    )


def _styles_xml() -> str:
    # Empty, but present: Excel's ODS reader expects the part to exist even
    # when every style the document uses is an automatic one in content.xml.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-styles {_NAMESPACES} office:version="{_ODF_VERSION}">'
        "<office:styles/><office:automatic-styles/><office:master-styles/>"
        "</office:document-styles>"
    )


def _meta_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
        f'office:version="{_ODF_VERSION}">'
        "<office:meta><meta:generator>Carraway</meta:generator></office:meta>"
        "</office:document-meta>"
    )


# -- public API ----------------------------------------------------------


def export_ods(
    path: Path | str,
    transactions: Sequence[Transaction],
    *,
    accounts: Sequence[Account] | None = None,
    series: Sequence[RecurringSeries] | None = None,
    categories: Sequence[str] | None = None,
    balances: dict[str, Money] | None = None,
    kinds: dict[str, str] | None = None,
    paid_via: dict[str, str] | None = None,
) -> Path:
    """Write `transactions` to an .ods workbook at `path` and return that path.

    Five sheets, laid out the way a person keeping this by hand would: Net
    Worth (assets, then liabilities, then the difference), Auto-Pay
    (subscriptions and bills split by cadence, each with a subtotal), By Month
    (income against spending), Transactions, and Categories.

    `accounts` and `balances` are needed for the Net Worth sheet and it is
    skipped without them, since assets with no figures beside them are not
    worth a page. `kinds` and `paid_via` are keyed by uppercased merchant and
    only annotate the Auto-Pay sheet.
    """
    target = Path(path)
    resolved = _categories_of(transactions, categories)
    account_names = {a.id: a.name for a in accounts or ()}

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
        # First entry, and stored rather than deflated, so the ODF magic sits at
        # a fixed offset in the raw bytes. Compress it and nothing will open it.
        mimetype = zipfile.ZipInfo("mimetype", date_time=_ZIP_TIMESTAMP)
        mimetype.compress_type = zipfile.ZIP_STORED
        package.writestr(mimetype, MIMETYPE)

        for name, body in (
            ("META-INF/manifest.xml", _manifest_xml(["content.xml", "styles.xml", "meta.xml"])),
            ("styles.xml", _styles_xml()),
            ("meta.xml", _meta_xml()),
        ):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, body)

        content = zipfile.ZipInfo("content.xml", date_time=_ZIP_TIMESTAMP)
        content.compress_type = zipfile.ZIP_DEFLATED
        with package.open(content, "w") as stream:
            chunks = _content_xml(
                transactions,
                resolved,
                account_names,
                series,
                accounts,
                balances,
                kinds,
                paid_via,
            )
            for chunk in chunks:
                stream.write(chunk.encode("utf-8"))

    return target


def export_csv(
    path: Path | str,
    transactions: Sequence[Transaction],
    *,
    categories: Sequence[str] | None = None,
) -> Path:
    """Write `transactions` to a flat CSV at `path` and return that path.

    Calc opens these too, and some people would rather have one plain file than
    a workbook. Amounts are written unformatted — no symbol, no thousands
    separator — because that is what a spreadsheet can parse back as a number.
    """
    target = Path(path)
    resolved = _categories_of(transactions, categories)

    # A BOM, because Excel otherwise reads a UTF-8 CSV in the local code page
    # and mangles every non-ASCII merchant name. Calc is happy either way.
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["date", "account_id", "description", "merchant", "category", "amount", "currency"]
        )
        for tx, category in zip(transactions, resolved, strict=True):
            writer.writerow(
                [
                    tx.date.isoformat(),
                    tx.account_id,
                    tx.description,
                    tx.merchant,
                    category,
                    f"{tx.amount.decimal:f}",
                    tx.amount.currency,
                ]
            )
    return target
