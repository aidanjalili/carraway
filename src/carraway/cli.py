"""Command line interface.

The CLI exists so the core is usable and testable long before there is a GUI,
and it stays useful afterwards for scripting and debugging. Every command here
goes through the same functions the Qt interface will call.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date
from pathlib import Path

from . import __version__
from .analysis import recurring
from .core import db
from .core.models import Account, AccountType
from .core.money import total


def _fmt_row(cells: list[str], widths: list[int]) -> str:
    return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()


def cmd_accounts(args: argparse.Namespace) -> int:
    conn = db.connect(args.database)
    if args.add:
        account = Account(
            id=uuid.uuid4().hex[:12],
            name=args.add,
            type=AccountType(args.type),
            institution=args.institution or "",
        )
        db.upsert_account(conn, account)
        print(f"Added {account.type} account {account.name!r} (id {account.id})")
        return 0

    accounts = db.list_accounts(conn)
    if not accounts:
        print("No accounts yet. Add one with:\n  carraway accounts --add 'Checking'")
        return 0

    print(f"{'ID':<14}{'NAME':<26}{'TYPE':<14}INSTITUTION")
    for a in accounts:
        print(f"{a.id:<14}{a.name:<26}{str(a.type):<14}{a.institution}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .importers.csv_importer import ImportError_, import_csv

    conn = db.connect(args.database)
    accounts = {a.id: a for a in db.list_accounts(conn)}
    if args.account not in accounts:
        print(f"Unknown account id {args.account!r}.", file=sys.stderr)
        print("Run 'carraway accounts' to list them.", file=sys.stderr)
        return 1

    try:
        transactions, warnings = import_csv(
            args.file,
            args.account,
            currency=accounts[args.account].currency,
            flip_sign=args.flip_sign,
        )
    except ImportError_ as exc:
        print(f"Could not read {args.file}: {exc}", file=sys.stderr)
        return 1

    inserted, skipped = db.insert_transactions(conn, transactions)
    print(f"Imported {inserted} transaction(s) from {Path(args.file).name}")
    if skipped:
        print(f"  {skipped} already present, skipped")
    for warning in warnings[:10]:
        print(f"  warning: {warning}", file=sys.stderr)
    if len(warnings) > 10:
        print(f"  ... and {len(warnings) - 10} more warnings", file=sys.stderr)
    return 0


def cmd_recurring(args: argparse.Namespace) -> int:
    conn = db.connect(args.database)
    transactions = db.list_transactions(conn, account_id=args.account)
    if not transactions:
        print("No transactions yet. Import a CSV first:")
        print("  carraway import statement.csv --account <id>")
        return 0

    series = recurring.detect(
        transactions,
        min_confidence=args.min_confidence,
        include_inflows=args.include_income,
    )
    if not series:
        print(
            f"No recurring charges found in {len(transactions)} transactions.\n"
            "Detection needs at least 3 charges from the same merchant."
        )
        return 0

    print(f"Found {len(series)} recurring series in {len(transactions)} transactions:\n")
    header = ["MERCHANT", "CADENCE", "AMOUNT", "NEXT", "SEEN", "CONF"]
    rows = [
        [
            s.merchant[:30],
            s.cadence + ("*" if s.amount_varies else ""),
            abs(s.typical_amount).format(),
            s.next_expected.isoformat() if s.next_expected else "-",
            str(s.occurrences),
            f"{s.confidence:.0%}",
        ]
        for s in series
    ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    print(_fmt_row(header, widths))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        print(_fmt_row(row, widths))

    yearly = total([s.annualised for s in series])
    print(f"\nTotal annualised: {yearly.format()}/year")
    if any(s.amount_varies for s in series):
        print("* amount varies between charges")

    overdue = recurring.stale(series, date.today())
    if overdue:
        print(f"\n{len(overdue)} series look overdue (possibly cancelled):")
        for s in overdue:
            print(f"  {s.merchant} - expected {s.next_expected}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    conn = db.connect(args.database)
    transactions = db.list_transactions(conn, account_id=args.account)
    if not transactions:
        print("No transactions yet.")
        return 0

    outflows = [t.amount for t in transactions if t.is_outflow and not t.is_transfer]
    inflows = [t.amount for t in transactions if not t.is_outflow and not t.is_transfer]
    spent, earned = total(outflows), total(inflows)
    dates = [t.date for t in transactions]

    print(f"Transactions : {len(transactions)}")
    print(f"Date range   : {min(dates)} to {max(dates)}")
    print(f"Money in     : {earned.format()}")
    print(f"Money out    : {abs(spent).format()}")
    print(f"Net          : {(earned + spent).format()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carraway",
        description="Carraway - a local-first, open source money manager.",
    )
    parser.add_argument("--version", action="version", version=f"carraway {__version__}")
    parser.add_argument(
        "--database",
        default=str(db.default_db_path()),
        help="path to the Carraway database (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_accounts = sub.add_parser("accounts", help="list or create accounts")
    p_accounts.add_argument("--add", metavar="NAME", help="create an account with this name")
    p_accounts.add_argument(
        "--type",
        default="checking",
        choices=[t.value for t in AccountType],
        help="account type when using --add (default: %(default)s)",
    )
    p_accounts.add_argument("--institution", help="bank or provider name")
    p_accounts.set_defaults(func=cmd_accounts)

    p_import = sub.add_parser("import", help="import transactions from a CSV export")
    p_import.add_argument("file", help="path to the CSV file")
    p_import.add_argument("--account", required=True, help="account id to import into")
    p_import.add_argument(
        "--flip-sign",
        action="store_true",
        help="invert amounts, for card exports that list charges as positive",
    )
    p_import.set_defaults(func=cmd_import)

    p_recurring = sub.add_parser("recurring", help="find subscriptions and recurring bills")
    p_recurring.add_argument("--account", help="limit to one account id")
    p_recurring.add_argument(
        "--min-confidence",
        type=float,
        default=recurring.MIN_CONFIDENCE,
        help="detection threshold, 0-1 (default: %(default)s)",
    )
    p_recurring.add_argument(
        "--include-income",
        action="store_true",
        help="also detect recurring deposits such as paychecks",
    )
    p_recurring.set_defaults(func=cmd_recurring)

    p_summary = sub.add_parser("summary", help="show totals across imported data")
    p_summary.add_argument("--account", help="limit to one account id")
    p_summary.set_defaults(func=cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
