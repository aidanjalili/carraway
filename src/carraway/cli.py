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
from .analysis import recurring, subscriptions
from .core import db
from .core.models import Account, AccountType
from .core.money import Money, total


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
    from .importers.ofx_importer import import_ofx

    conn = db.connect(args.database)
    accounts = {a.id: a for a in db.list_accounts(conn)}
    if args.account not in accounts:
        print(f"Unknown account id {args.account!r}.", file=sys.stderr)
        print("Run 'carraway accounts' to list them.", file=sys.stderr)
        return 1

    # Dispatch on extension. OFX is structured and unambiguous where CSV is
    # guesswork, so it is always preferred when the file offers it.
    suffix = Path(args.file).suffix.lower()
    reader = import_ofx if suffix in (".ofx", ".qfx") else import_csv

    try:
        transactions, warnings = reader(
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


def cmd_transfers(args: argparse.Namespace) -> int:
    from .analysis import transfers

    conn = db.connect(args.database)
    all_tx = db.list_transactions(conn)
    if not all_tx:
        print("No transactions yet.")
        return 0

    pairs = transfers.find_transfers(all_tx, max_days=args.max_days)
    if not pairs:
        print(f"No transfers found among {len(all_tx)} transactions.")
        return 0

    accounts = {a.id: a.name for a in db.list_accounts(conn)}
    print(f"Found {len(pairs)} transfer pair(s):\n")
    for pair in pairs:
        out, inn = pair.outflow, pair.inflow
        print(f"  {abs(out.amount).format()}  {pair.confidence:.0%} confident")
        print(
            f"    out  {out.date}  {accounts.get(out.account_id, '?'):<18} {out.description[:44]}"
        )
        print(
            f"    in   {inn.date}  {accounts.get(inn.account_id, '?'):<18} {inn.description[:44]}"
        )
        print(f"    why  {pair.reason}")
        if pair.fee:
            print(f"    fee  {pair.fee.format()}")
        print()

    if not args.apply:
        print("Nothing written. Re-run with --apply to group these.")
        return 0

    marked = transfers.apply_transfer_groups(all_tx, pairs)
    written = db.update_transfer_groups(conn, all_tx)
    print(f"Grouped {marked} transactions ({written} rows written).")
    print("These are now excluded from spending totals and recurring detection.")
    return 0


def cmd_categorize(args: argparse.Namespace) -> int:
    from .analysis import categorize as cat

    conn = db.connect(args.database)
    all_tx = db.list_transactions(conn, account_id=args.account)
    if not all_tx:
        print("No transactions yet.")
        return 0

    assigned = cat.categorize_all(all_tx)

    # Report by spend rather than by count: what a category costs is the thing
    # worth looking at, and a long tail of tiny rows would otherwise dominate.
    by_category: dict[str, list[Money]] = {}
    counts: dict[str, int] = {}
    for tx, category in zip(all_tx, assigned, strict=True):
        counts[category] = counts.get(category, 0) + 1
        if tx.is_outflow and not tx.is_transfer:
            by_category.setdefault(category, []).append(tx.amount)

    rows = sorted(
        ((name, total(amounts), counts[name]) for name, amounts in by_category.items()),
        key=lambda r: r[1].minor,
    )
    print(f"{'CATEGORY':<18}{'SPENT':>13}{'TXNS':>7}")
    print("-" * 38)
    for name, spent, count in rows:
        print(f"{name:<18}{abs(spent).format():>13}{count:>7}")

    unknown = counts.get(cat.UNCATEGORIZED, 0)
    print(f"\n{len(all_tx) - unknown}/{len(all_tx)} categorised ({unknown} unmatched)")

    suggestions = cat.suggest_rules(all_tx)
    if suggestions:
        print("\nBiggest unmatched merchants - worth a rule each:")
        for s in suggestions[:8]:
            print(f"  {s.merchant[:34]:<36}{s.count:>4} txns")

    if not args.apply:
        print("\nNothing written. Re-run with --apply to save these categories.")
        return 0

    changed = db.update_categories(
        conn, [(tx.id, c) for tx, c in zip(all_tx, assigned, strict=True)]
    )
    print(f"\nSaved {changed} category assignment(s).")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Ask the user about recurring merchants the catalog cannot place.

    Deliberately not a wall of questions. Candidates are ordered by what they
    cost per year, so the first answer is always the most valuable one, and a
    session stops after `--limit`. Every answer is stored, so nothing is ever
    asked twice — that is what makes asking aggressively acceptable.
    """
    from .analysis import recurring

    conn = db.connect(args.database)
    transactions = db.list_transactions(conn)
    if not transactions:
        print("No transactions yet.")
        return 0

    verdicts = db.get_verdicts(conn)

    # Cast wider than the normal view on purpose: the whole point is to not
    # miss anything, and a series the detector is only 40% sure about is
    # exactly the kind a person can settle in one second.
    threshold = args.min_confidence if args.include_uncertain else recurring.MIN_CONFIDENCE
    series = recurring.detect(transactions, min_confidence=threshold)

    pending = [
        s
        for s in series
        if subscriptions.resolve(s.merchant, verdicts) == subscriptions.UNKNOWN
        and abs(s.annualised.minor) >= args.min_value * 100
    ]
    pending.sort(key=lambda s: -abs(s.annualised.minor))

    known = len(series) - len(pending)
    if not pending:
        print(f"Nothing to review. All {len(series)} recurring series are classified.")
        return 0

    if not sys.stdin.isatty():
        print(f"{len(pending)} merchant(s) need a decision. Run this in a terminal to answer:")
        for s in pending[: args.limit]:
            print(
                f"  {s.merchant} - {abs(s.typical_amount).format()} {s.cadence}, "
                f"{s.annualised.format()}/yr"
            )
        return 0

    batch = pending[: args.limit]
    print(f"{len(series)} recurring series - {known} already classified, {len(pending)} unknown.")
    print(f"Reviewing the {len(batch)} most expensive.\n")
    print("  [s] subscription   [b] bill   [h] habit / one-off   [enter] skip   [q] quit\n")

    answered = 0
    for index, item in enumerate(batch, start=1):
        print(f"({index}/{len(batch)}) {item.merchant}")
        print(
            f"    {abs(item.typical_amount).format()} {item.cadence}"
            f"  ·  {item.annualised.format()}/yr"
            f"  ·  seen {item.occurrences}x since {item.first_seen}"
            f"  ·  {item.confidence:.0%} confident"
        )
        try:
            answer = input("    > ").strip().lower()
        except EOFError:
            break
        if answer in ("q", "quit"):
            break
        kind = {
            "s": subscriptions.SUBSCRIPTION,
            "b": subscriptions.BILL,
            "h": subscriptions.HABIT,
        }.get(answer[:1] if answer else "")
        if kind is None:
            print("    skipped\n")
            continue
        db.set_verdict(conn, item.merchant, kind)
        answered += 1
        print(f"    saved as {kind}\n")

    remaining = len(pending) - answered
    print(f"Saved {answered} answer(s).")
    if remaining > 0:
        print(f"{remaining} still unclassified - run 'carraway review' again to continue.")
    return 0


def cmd_subscriptions(args: argparse.Namespace) -> int:
    """The cull list: recurring things that are actually cancellable."""
    from .analysis import recurring

    conn = db.connect(args.database)
    transactions = db.list_transactions(conn)
    if not transactions:
        print("No transactions yet.")
        return 0

    verdicts = db.get_verdicts(conn)
    series = recurring.detect(transactions)
    grouped: dict[str, list] = {}
    for item in series:
        grouped.setdefault(subscriptions.resolve(item.merchant, verdicts), []).append(item)

    order = [
        (subscriptions.SUBSCRIPTION, "Subscriptions", "cancellable"),
        (subscriptions.BILL, "Bills", "recurring, but not optional"),
        (subscriptions.HABIT, "Habits", "regular spending, not a commitment"),
        (subscriptions.UNKNOWN, "Unclassified", "run 'carraway review' to sort these"),
    ]
    for kind, heading, note in order:
        items = grouped.get(kind, [])
        if not items:
            continue
        items.sort(key=lambda s: -abs(s.annualised.minor))
        yearly = total([s.annualised for s in items])
        print(f"\n{heading} - {yearly.format()}/yr  ({note})")
        print("-" * 66)
        for item in items:
            flag = "*" if item.amount_varies else " "
            print(
                f"  {item.merchant[:30]:<32}{abs(item.typical_amount).format():>10}"
                f" {item.cadence:<10}{flag} {item.annualised.format():>11}/yr"
            )

    cancellable = grouped.get(subscriptions.SUBSCRIPTION, [])
    if cancellable:
        print(
            f"\n{len(cancellable)} cancellable subscription(s) costing "
            f"{total([s.annualised for s in cancellable]).format()}/yr."
        )
    unknown = grouped.get(subscriptions.UNKNOWN, [])
    if unknown:
        print(f"{len(unknown)} unclassified - 'carraway review' will ask about them.")
    return 0


def cmd_known(args: argparse.Namespace) -> int:
    """List, or mark, merchants the catalog recognises but detection cannot.

    Detection needs a pattern, and a yearly magazine bought once has exactly
    one data point. The charge is still in the ledger and the catalog still
    recognises the merchant, so the app can say "this looks like a
    subscription, I just cannot prove it recurs yet" instead of staying silent
    about money the user is definitely paying.
    """
    from .analysis import recurring

    conn = db.connect(args.database)
    transactions = db.list_transactions(conn)
    if not transactions:
        print("No transactions yet.")
        return 0

    verdicts = db.get_verdicts(conn)
    detected = {s.merchant.upper() for s in recurring.detect(transactions)}

    # Group by merchant so a magazine bought twice shows as one entry.
    seen: dict[str, list] = {}
    for tx in transactions:
        if tx.is_transfer or not tx.is_outflow:
            continue
        merchant = tx.merchant or recurring.normalise_merchant(tx.description)
        if not merchant or merchant.upper() in detected:
            continue
        if subscriptions.resolve(merchant, verdicts) != subscriptions.SUBSCRIPTION:
            continue
        seen.setdefault(merchant, []).append(tx)

    if not seen:
        print("Nothing recognised beyond what detection already found.")
        return 0

    print("Recognised as subscriptions, but not enough history to confirm a pattern:\n")
    rows = sorted(seen.items(), key=lambda kv: -abs(total([t.amount for t in kv[1]]).minor))
    for merchant, txs in rows:
        spent = abs(total([t.amount for t in txs]))
        last = max(t.date for t in txs)
        times = f"{len(txs)}x" if len(txs) > 1 else "once"
        print(f"  {merchant[:38]:<40}{spent.format():>10}  {times}, last {last}")

    print(
        f"\n{len(rows)} merchant(s). These are charges the catalog knows are "
        "subscriptions,\nbut which appear too few times to detect a cadence — "
        "typically annual\nrenewals in a short statement history."
    )
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

    p_transfers = sub.add_parser("transfers", help="find transfers between your own accounts")
    p_transfers.add_argument(
        "--apply", action="store_true", help="save the groupings (default is a dry run)"
    )
    p_transfers.add_argument(
        "--max-days",
        type=int,
        default=4,
        help="how far apart the two halves may post (default: %(default)s)",
    )
    p_transfers.set_defaults(func=cmd_transfers)

    p_categorize = sub.add_parser("categorize", help="categorise spending and show a breakdown")
    p_categorize.add_argument("--account", help="limit to one account id")
    p_categorize.add_argument(
        "--apply", action="store_true", help="save the categories (default is a dry run)"
    )
    p_categorize.set_defaults(func=cmd_categorize)

    p_subs = sub.add_parser(
        "subscriptions", help="recurring things split into subscriptions, bills and habits"
    )
    p_subs.set_defaults(func=cmd_subscriptions)

    p_known = sub.add_parser(
        "known", help="recognised subscriptions with too little history to detect"
    )
    p_known.set_defaults(func=cmd_known)

    p_review = sub.add_parser("review", help="answer what unrecognised recurring merchants are")
    p_review.add_argument(
        "--limit", type=int, default=10, help="how many to ask about (default: %(default)s)"
    )
    p_review.add_argument(
        "--include-uncertain",
        action="store_true",
        help="also ask about weaker patterns, so nothing is missed",
    )
    p_review.add_argument(
        "--min-confidence",
        type=float,
        default=0.35,
        help="threshold when --include-uncertain is set (default: %(default)s)",
    )
    p_review.add_argument(
        "--min-value",
        type=float,
        default=12.0,
        help="skip anything costing less than this per year (default: %(default)s)",
    )
    p_review.set_defaults(func=cmd_review)

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
