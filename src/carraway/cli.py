"""Command line interface.

The CLI exists so the core is usable and testable long before there is a GUI,
and it stays useful afterwards for scripting and debugging. Every command here
goes through the same functions the Qt interface will call.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from . import __version__
from .analysis import recurring, subscriptions
from .core import db
from .core.models import Account, AccountType
from .core.money import Money, total

# Payments a year, by cadence. A biweekly charge is 26 payments rather than
# 24, which is the error people most often make estimating an annual cost.
_PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}


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
    from .importers.venmo import import_venmo, looks_like_venmo

    conn = db.connect(args.database)
    accounts = {a.id: a for a in db.list_accounts(conn)}
    if args.account not in accounts:
        print(f"Unknown account id {args.account!r}.", file=sys.stderr)
        print("Run 'carraway accounts' to list them.", file=sys.stderr)
        return 1

    # Dispatch on extension. OFX is structured and unambiguous where CSV is
    # guesswork, so it is always preferred when the file offers it.
    suffix = Path(args.file).suffix.lower()
    if suffix in (".ofx", ".qfx"):
        reader = import_ofx
    elif suffix == ".csv" and looks_like_venmo(args.file):
        # Venmo's export is a CSV, but with preamble, trailer rows and its own
        # column names, so it is sniffed rather than left to the generic reader.
        reader = import_venmo
    else:
        reader = import_csv

    currency = accounts[args.account].currency
    try:
        if reader is import_venmo:
            # Venmo states the direction of every transaction explicitly, so
            # there is no ambiguous sign for --flip-sign to resolve.
            if args.flip_sign:
                print("--flip-sign does not apply to Venmo exports; ignoring.", file=sys.stderr)
            transactions, warnings = reader(args.file, args.account, currency=currency)
        else:
            transactions, warnings = reader(
                args.file, args.account, currency=currency, flip_sign=args.flip_sign
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
    decided = db.get_verdict_dates(conn)

    # Cast wider than the normal view on purpose: the whole point is to not
    # miss anything, and a series the detector is only 40% sure about is
    # exactly the kind a person can settle in one second.
    threshold = args.min_confidence if args.include_uncertain else recurring.MIN_CONFIDENCE
    # Inflows are included here even though the subscriptions view excludes
    # them: a recurring Zelle from a relative or a housemate's share of the
    # rent is exactly the kind of thing only the user can identify, and
    # never asking means never finding out.
    series = recurring.detect(transactions, min_confidence=threshold, include_inflows=True)

    def needs_answer(item) -> bool:
        inflow = item.typical_amount.minor > 0
        kind = subscriptions.resolve(item.merchant, verdicts, is_inflow=inflow)
        if kind == subscriptions.UNKNOWN:
            return True
        # A cancellation that has charged again since is out of date.
        when = decided.get(item.merchant.upper())
        return kind == subscriptions.CANCELLED and when is not None and item.last_seen > when

    pending = [
        s for s in series if needs_answer(s) and abs(s.annualised.minor) >= args.min_value * 100
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
    print(
        "  [s] subscription   [b] bill      [h] habit / one-off\n"
        "  [i] income         [c] cancelled  [enter] skip   [q] quit\n"
    )

    answered = 0
    for index, item in enumerate(batch, start=1):
        direction = "in " if item.typical_amount.minor > 0 else "out"
        hint = "  (person-to-person)" if subscriptions.is_person_to_person(item.merchant) else ""
        print(f"({index}/{len(batch)}) {item.merchant}{hint}")
        print(
            f"    {direction} {abs(item.typical_amount).format()} {item.cadence}"
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
            "i": subscriptions.INCOME,
            "c": subscriptions.CANCELLED,
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
    """Recurring money, split by what the user can actually do about it."""
    from .analysis import recurring

    conn = db.connect(args.database)
    transactions = db.list_transactions(conn)
    if not transactions:
        print("No transactions yet.")
        return 0

    verdicts = db.get_verdicts(conn)
    decided = db.get_verdict_dates(conn)
    series = recurring.detect(transactions, include_inflows=True)
    # Detected, but the next charge never came. Might be cancelled, might be a
    # bill that moved with the user; either way it is not current.
    overdue = {id(s) for s in recurring.stale(series, date.today())}

    grouped: dict[str, list] = {}
    revived: list[str] = []
    for item in series:
        inflow = item.typical_amount.minor > 0
        kind = subscriptions.resolve(item.merchant, verdicts, is_inflow=inflow)
        # A cancellation that has charged again since is out of date rather
        # than wrong, so the merchant goes back into the review queue.
        when = decided.get(item.merchant.upper())
        if kind == subscriptions.CANCELLED and when and item.last_seen > when:
            revived.append(item.merchant)
            kind = subscriptions.UNKNOWN
        grouped.setdefault(kind, []).append(item)

    order = [
        (subscriptions.SUBSCRIPTION, "Subscriptions", "cancellable"),
        (subscriptions.BILL, "Bills", "recurring, but not optional"),
        (subscriptions.INCOME, "Income", "money arriving on a schedule"),
        (subscriptions.HABIT, "Habits", "regular spending, not a commitment"),
        (subscriptions.CANCELLED, "Cancelled", "no longer paying"),
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
            stopped = "  (stopped?)" if id(item) in overdue else ""
            print(
                f"  {item.merchant[:30]:<32}{abs(item.typical_amount).format():>10}"
                f" {item.cadence:<10}{flag} {item.annualised.format():>11}/yr{stopped}"
            )

    cancellable = grouped.get(subscriptions.SUBSCRIPTION, [])
    if cancellable:
        live = [s for s in cancellable if id(s) not in overdue]
        print(
            f"\n{len(cancellable)} cancellable subscription(s); "
            f"{total([s.annualised for s in live]).format()}/yr still charging."
        )
        if len(live) != len(cancellable):
            print(
                f"  {len(cancellable) - len(live)} marked (stopped?) - the expected charge "
                "never arrived.\n  Answer [c] in 'carraway review' to confirm a cancellation."
            )
    stopped = grouped.get(subscriptions.CANCELLED, [])
    if stopped:
        print(f"Cancelled: {total([s.annualised for s in stopped]).format()}/yr no longer paid.")
    if revived:
        print(
            f"\n{len(revived)} merchant(s) marked cancelled have charged again: "
            + ", ".join(revived)
        )
        print("They are back in 'carraway review' to be answered afresh.")
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


def cmd_prices(args: argparse.Namespace) -> int:
    """What has quietly gone up in price."""
    from .analysis import price_changes, recurring

    conn = db.connect(args.database)
    transactions = db.list_transactions(conn)
    if not transactions:
        print("No transactions yet.")
        return 0

    series = recurring.detect(transactions, include_inflows=True)
    changes = price_changes.find_price_changes(
        transactions, series=series, include_inflows=args.include_income
    )
    if not changes:
        print(f"No price changes found across {len(series)} recurring series.")
        return 0

    print(f"{len(changes)} price change(s):\n")
    header = ["MERCHANT", "WAS", "NOW", "CHANGED", "PER YEAR", "CONF"]
    rows = [
        [
            c.merchant[:28],
            abs(c.old_amount).format(),
            abs(c.new_amount).format(),
            c.changed_on.isoformat(),
            ("+" if c.direction == "increase" else "-") + abs(c.annual_impact).format(),
            f"{c.confidence:.0%}",
        ]
        for c in changes
    ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    print(_fmt_row(header, widths))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        print(_fmt_row(row, widths))

    rises = [c for c in changes if c.direction == "increase"]
    if rises:
        yearly = total([abs(c.annual_impact) for c in rises])
        print(f"\nPrice rises are costing you {yearly.format()}/year more than before.")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Snapshot the database, or list and prune existing snapshots."""
    from .core import backup

    database = Path(args.database)
    if args.list:
        snapshots = backup.list_snapshots(database)
        if not snapshots:
            print("No snapshots yet.")
            return 0
        print(f"{len(snapshots)} snapshot(s) in {backup.backup_dir(database)}:")
        for path, taken, size in snapshots:
            print(f"  {taken}  {size / 1024:>8.0f} KB  {path.name}")
        return 0

    saved = backup.snapshot(database, tag=args.tag or "manual")
    if saved is None:
        print("Nothing to back up yet.")
        return 0
    print(f"Saved {saved}")
    print(f"Keeping the newest {backup.KEEP}; older ones are removed automatically.")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    """Install or remove a systemd user timer that syncs on its own.

    A user timer rather than cron: it survives reboots, runs without the user
    being logged in only if lingering is enabled, and `journalctl --user` shows
    what happened. Persistent=true means a machine that was off at the
    scheduled time catches up when it comes back, which is the whole point for
    someone syncing a bank rather than a server.
    """
    import shutil
    import subprocess

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    service = unit_dir / "carraway-sync.service"
    timer = unit_dir / "carraway-sync.timer"

    if args.remove:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "carraway-sync.timer"],
            capture_output=True,
            check=False,
        )
        removed = [p.name for p in (service, timer) if p.exists()]
        for path in (service, timer):
            path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
        print("Removed " + ", ".join(removed) if removed else "Nothing was scheduled.")
        return 0

    if not shutil.which("systemctl"):
        print("systemd is not available on this system.", file=sys.stderr)
        print("Schedule 'carraway sync simplefin' with cron instead.", file=sys.stderr)
        return 1

    executable = shutil.which("carraway") or str(Path(sys.argv[0]).resolve())
    unit_dir.mkdir(parents=True, exist_ok=True)
    service.write_text(
        "[Unit]\n"
        "Description=Carraway bank sync\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={executable} --database {args.database} sync simplefin --days 0 --link-all\n"
    )
    timer.write_text(
        "[Unit]\n"
        "Description=Sync Carraway on a schedule\n\n"
        "[Timer]\n"
        f"OnCalendar={args.when}\n"
        "# Catch up after the machine was off, rather than silently skipping a\n"
        "# run — a missed window is how a gap gets into the history.\n"
        "Persistent=true\n"
        "RandomizedDelaySec=1h\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "carraway-sync.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"Could not enable the timer: {result.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"Scheduled: {args.when}")
    print(f"  {service}")
    print(f"  {timer}")
    print("\nCheck it with:  systemctl --user list-timers carraway-sync.timer")
    print("See past runs:  journalctl --user -u carraway-sync.service")
    print("\nTo keep syncing while you are logged out, run:\n  loginctl enable-linger $USER")
    return 0


def cmd_networth(args: argparse.Namespace) -> int:
    from .analysis import networth

    conn = db.connect(args.database)
    accounts = db.list_accounts(conn)
    transactions = db.list_transactions(conn)
    balances = db.latest_balances(conn)
    if not balances:
        print("No account balances recorded yet.", file=sys.stderr)
        print("Net worth needs a starting point; run 'carraway sync simplefin'.", file=sys.stderr)
        return 1

    points = networth.reconstruct(accounts, transactions, balances, granularity=args.granularity)
    if not points:
        print("Not enough history to chart net worth yet.")
        return 0

    missing = networth.accounts_missing_balances(accounts, balances)
    print(f"{'DATE':<12}{'ASSETS':>14}{'OWED':>13}{'NET':>14}")
    print("-" * 53)
    for point in points[-args.limit :]:
        print(
            f"{point.date.isoformat():<12}{point.assets.format():>14}"
            f"{point.liabilities.format():>13}{point.net.format():>14}"
        )

    summary = networth.summarise(points)
    direction = "up" if summary.change.minor >= 0 else "down"
    print(f"\n{direction} {abs(summary.change).format()} since {summary.start}")
    if summary.percent_change is not None:
        print(f"  {summary.percent_change:+.1f}% over the period")
    else:
        # Undefined when the period began at or below zero: there is no
        # meaningful percentage change from nothing, or from debt.
        print("  (percentage change is undefined from a zero or negative start)")
    if summary.best_month:
        month, amount = summary.best_month
        print(f"  best month : {month}  {amount.format()}")
    if summary.worst_month:
        month, amount = summary.worst_month
        print(f"  worst month: {month}  {amount.format()}")
    if missing:
        print(f"\nExcluded, no balance known: {', '.join(a.name for a in missing)}")
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    from .analysis import budget as budget_mod
    from .analysis import recurring

    conn = db.connect(args.database)
    transactions = db.list_transactions(conn)
    if not transactions:
        print("No transactions yet.")
        return 0

    horizon = date.today() + timedelta(days=30 * args.months)
    goal = budget_mod.Goal(
        target=Money.parse(str(args.target)), horizon=horizon, period=args.period
    )
    series = recurring.detect(transactions, include_inflows=True)
    plan = budget_mod.plan(goal, transactions, series=series, period=args.period)

    verdict = "REACHABLE" if plan.feasible else "NOT REACHABLE"
    print(f"Goal: save {goal.target.format()} by {horizon}  [{verdict}]\n")
    print(plan.explanation)

    changed = [c for c in plan.categories if c.allowance != c.baseline]
    rows = changed or plan.categories
    if rows:
        heading = "Per-category allowances" + ("" if changed else " (no cuts needed)")
        print(f"\n{heading}, per {args.period[:-2]}:")
        print(f"  {'CATEGORY':<20}{'SPENDING NOW':>14}{'ALLOWED':>12}{'CHANGE':>10}")
        print("  " + "-" * 54)
        for item in rows:
            delta = item.allowance.minor - item.baseline.minor
            change = f"{delta / 100:+,.0f}" if delta else "-"
            print(
                f"  {item.category[:18]:<20}{item.baseline.format():>14}"
                f"{item.allowance.format():>12}{change:>10}"
            )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .analysis import categorize as cat
    from .analysis import recurring
    from .exporters.ods import export_csv, export_ods

    conn = db.connect(args.database)
    transactions = db.list_transactions(conn)
    if not transactions:
        print("Nothing to export yet.")
        return 0

    # Categories are computed rather than stored, so an export that read only
    # the saved column would file every row as Uncategorized.
    categories = cat.categorize_all(transactions)
    accounts = db.list_accounts(conn)
    series = recurring.detect(transactions, include_inflows=True)

    target = Path(args.file).expanduser()
    if args.format == "csv" or target.suffix.lower() == ".csv":
        written = export_csv(target, transactions, categories=categories)
    else:
        written = export_ods(
            target, transactions, accounts=accounts, series=series, categories=categories
        )

    size = written.stat().st_size
    print(f"Exported {len(transactions):,} transactions to {written} ({size / 1024:.0f} KB)")
    if written.suffix.lower() == ".ods":
        print("Open it with:  libreoffice --calc " + str(written))
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    """Find one charge imported twice from two sources, and optionally drop it."""
    from .analysis import duplicates
    from .core import backup

    conn = db.connect(args.database)
    transactions = db.list_transactions(conn)
    groups = duplicates.find_duplicates(transactions)
    if not groups:
        print(f"No cross-source duplicates found among {len(transactions):,} transactions.")
        return 0

    extra = total([g.wasted for g in groups])
    print(f"{len(groups)} duplicate(s), overstating your totals by {abs(extra).format()}:\n")
    for group in groups:
        print(f"  {group.keep.date}  {group.keep.amount.format():>11}")
        print(f"     keep : {group.keep.description[:60]}")
        for row in group.remove:
            print(f"     drop : {row.description[:60]}")

    if not args.apply:
        print("\nNothing removed. Re-run with --apply to delete the extra copies.")
        return 0

    # Deleting a real transaction is far worse than keeping a duplicate, so a
    # snapshot goes first and the user can always go back to it.
    saved = backup.snapshot(Path(args.database), tag="dedupe")
    if saved:
        print(f"\nBacked up to {saved.name}")

    doomed = [row.id for group in groups for row in group.remove]
    removed = db.delete_transactions(conn, doomed)
    print(f"Removed {removed} duplicate row(s).")
    return 0


def cmd_track(args: argparse.Namespace) -> int:
    """Record a subscription no detector can find, or list the ones recorded.

    Anything paid through Venmo, Zelle or PayPal reaches the statement as
    "VENMO PAYMENT", never as the service, so it cannot be detected at all.
    The only way for the app to know is to be told.
    """
    conn = db.connect(args.database)

    if args.remove:
        removed = db.remove_manual_subscription(conn, args.remove)
        print("Removed." if removed else f"No tracked subscription with id {args.remove!r}.")
        return 0

    if not args.merchant:
        tracked = db.list_manual_subscriptions(conn)
        if not tracked:
            print("Nothing tracked manually yet.")
            print("Add one with:  carraway track 'T-Mobile' 35 monthly --via 'venmo to dad'")
            return 0
        print(f"{'ID':<14}{'SERVICE':<26}{'AMOUNT':>10}  {'CADENCE':<10}PAID VIA")
        for item in tracked:
            print(
                f"{item['id']:<14}{str(item['merchant'])[:24]:<26}"
                f"{abs(item['amount']).format():>10}  {str(item['cadence']):<10}{item['paid_via']}"
            )
        yearly = total([abs(i["amount"]) * _PER_YEAR.get(str(i["cadence"]), 0) for i in tracked])
        print(f"\n{len(tracked)} tracked, {yearly.format()}/year")
        return 0

    if args.amount is None or not args.cadence:
        print("Give an amount and a cadence, e.g. carraway track 'AAA' 67 yearly", file=sys.stderr)
        return 1

    subscription_id = db.add_manual_subscription(
        conn,
        args.merchant,
        Money.parse(str(args.amount)),
        args.cadence,
        kind=args.kind,
        paid_via=args.via or "",
        notes=args.notes or "",
    )
    print(f"Tracking {args.merchant} at {Money.parse(str(args.amount)).format()} {args.cadence}")
    print(f"  id {subscription_id}; remove it with 'carraway track --remove {subscription_id}'")
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

    p_prices = sub.add_parser("prices", help="find recurring charges that changed price")
    p_prices.add_argument(
        "--include-income", action="store_true", help="also check recurring deposits"
    )
    p_prices.set_defaults(func=cmd_prices)

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

    # Sync lives in its own package so the main CLI never imports it, and
    # someone who only imports files pays nothing for code they never use.
    from .sync.cli import register as register_sync

    register_sync(sub, parser.get_default("database"))

    p_networth = sub.add_parser("networth", help="net worth over time")
    p_networth.add_argument(
        "--granularity",
        default="monthly",
        choices=["daily", "weekly", "monthly"],
        help="sampling interval (default: %(default)s)",
    )
    p_networth.add_argument(
        "--limit", type=int, default=24, help="how many points to show (default: %(default)s)"
    )
    p_networth.set_defaults(func=cmd_networth)

    p_budget = sub.add_parser("budget", help="work out what you can spend to hit a savings goal")
    p_budget.add_argument("target", help="how much to save, e.g. 5000")
    p_budget.add_argument(
        "--months", type=int, default=6, help="by when, in months (default: %(default)s)"
    )
    p_budget.add_argument(
        "--period",
        default="monthly",
        choices=["weekly", "monthly"],
        help="budget period (default: %(default)s)",
    )
    p_budget.set_defaults(func=cmd_budget)

    p_export = sub.add_parser("export", help="export to a spreadsheet for LibreOffice Calc")
    p_export.add_argument("file", help="output path, .ods or .csv")
    p_export.add_argument(
        "--format", choices=["ods", "csv"], help="override the format implied by the extension"
    )
    p_export.set_defaults(func=cmd_export)

    p_track = sub.add_parser(
        "track", help="record a subscription that never appears as itself on a statement"
    )
    p_track.add_argument("merchant", nargs="?", help="what it is, e.g. 'T-Mobile'")
    p_track.add_argument("amount", nargs="?", help="what it costs, e.g. 35")
    p_track.add_argument(
        "cadence",
        nargs="?",
        choices=["weekly", "biweekly", "monthly", "quarterly", "yearly"],
        help="how often it bills",
    )
    p_track.add_argument("--via", help="how it is paid, e.g. 'venmo to dad'")
    p_track.add_argument("--kind", default="subscription", choices=["subscription", "bill"])
    p_track.add_argument("--notes", help="anything worth remembering")
    p_track.add_argument("--remove", metavar="ID", help="stop tracking one")
    p_track.set_defaults(func=cmd_track)

    p_dedupe = sub.add_parser(
        "dedupe", help="find one charge imported twice from two different sources"
    )
    p_dedupe.add_argument(
        "--apply", action="store_true", help="delete the extra copies (default is a dry run)"
    )
    p_dedupe.set_defaults(func=cmd_dedupe)

    p_backup = sub.add_parser("backup", help="snapshot the database, or list snapshots")
    p_backup.add_argument("--list", action="store_true", help="list existing snapshots")
    p_backup.add_argument("--tag", help="label for this snapshot")
    p_backup.set_defaults(func=cmd_backup)

    p_schedule = sub.add_parser("schedule", help="sync automatically on a systemd timer")
    p_schedule.add_argument(
        "--when",
        default="weekly",
        help="systemd OnCalendar expression, e.g. daily or 'Mon *-*-* 09:00' "
        "(default: %(default)s)",
    )
    p_schedule.add_argument("--remove", action="store_true", help="remove the schedule")
    p_schedule.set_defaults(func=cmd_schedule)

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
