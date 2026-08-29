"""One place that owns the ledger the windows read from.

Views should never touch SQLite directly. They ask this object, which loads
once and hands out the same in-memory lists to everyone, so switching tabs is
instant and every screen agrees about the numbers it is showing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..analysis import categorize as cat
from ..analysis import recurring, subscriptions, transfers
from ..core import db
from ..core.models import Account, RecurringSeries, Transaction
from ..core.money import Money, total


@dataclass
class Ledger:
    """Everything the UI needs, loaded once and recomputed on demand."""

    path: Path
    accounts: list[Account] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    series: list[RecurringSeries] = field(default_factory=list)
    categories: dict[str, str] = field(default_factory=dict)  # transaction id -> category
    verdicts: dict[str, str] = field(default_factory=dict)  # merchant -> user's answer
    decided: dict[str, date] = field(default_factory=dict)  # merchant -> when answered

    def load(self) -> None:
        conn = db.connect(self.path)
        self.accounts = db.list_accounts(conn)
        self.transactions = db.list_transactions(conn)

        # Transfers are matched in memory rather than written, so opening the
        # app never mutates the user's data behind their back. The CLI's
        # `transfers --apply` remains the way to make it stick.
        pairs = transfers.find_transfers(self.transactions)
        transfers.apply_transfer_groups(self.transactions, pairs)

        self.verdicts = db.get_verdicts(conn)
        self.decided = db.get_verdict_dates(conn)
        # Inflows included so recurring income and person-to-person
        # payments are visible; the views split them out by kind.
        self.series = recurring.detect(self.transactions, include_inflows=True)
        assigned = cat.categorize_all(self.transactions)
        self.categories = {
            tx.id: name for tx, name in zip(self.transactions, assigned, strict=True)
        }
        conn.close()

    # -- derived views the screens ask for --------------------------------

    def set_kind(self, series: RecurringSeries, kind: str) -> None:
        """Store the user's answer and update what is already in memory.

        Writes straight through rather than deferring, because an answer the
        user gave and the app then lost would be worse than not asking.
        """
        conn = db.connect(self.path)
        db.set_verdict(conn, series.merchant, kind)
        conn.close()
        self.verdicts[series.merchant.upper()] = kind
        self.decided[series.merchant.upper()] = date.today()

    def kind_of(self, series: RecurringSeries) -> str:
        """What this series is. The user's own answer always wins.

        A cancellation that has charged again since is treated as out of date
        rather than wrong, so the merchant returns to the unclassified pile
        instead of silently staying off the books.
        """
        inflow = series.typical_amount.minor > 0
        kind = subscriptions.resolve(series.merchant, self.verdicts, is_inflow=inflow)
        when = self.decided.get(series.merchant.upper())
        if kind == subscriptions.CANCELLED and when and series.last_seen > when:
            return subscriptions.UNKNOWN
        return kind

    def series_by_kind(self, kind: str) -> list[RecurringSeries]:
        return [s for s in self.series if self.kind_of(s) == kind]

    def account_name(self, account_id: str) -> str:
        for account in self.accounts:
            if account.id == account_id:
                return account.name
        return "Unknown account"

    @property
    def active_series(self) -> list[RecurringSeries]:
        """Series whose next charge has not already come and gone."""
        overdue = {id(s) for s in recurring.stale(self.series, date.today())}
        return [s for s in self.series if id(s) not in overdue]

    @property
    def stale_series(self) -> list[RecurringSeries]:
        return recurring.stale(self.series, date.today())

    def monthly_cost(self, series: list[RecurringSeries]) -> Money:
        """What a set of series costs in an average month.

        Annualised then divided, because a biweekly charge is 26 payments a
        year rather than 24 — the error people most often make estimating this
        by hand, and worth being exact about.
        """
        yearly = total([s.annualised for s in series])
        return Money(round(yearly.minor / 12), yearly.currency)

    def spending_by_category(self) -> list[tuple[str, Money, int]]:
        """(category, total spent, transaction count), biggest spend first."""
        amounts: dict[str, list[Money]] = {}
        counts: dict[str, int] = {}
        for tx in self.transactions:
            if not tx.is_outflow or tx.is_transfer:
                continue
            name = self.categories.get(tx.id, cat.UNCATEGORIZED)
            # Brokerage and savings moves are categorised Transfer even when no
            # matching partner row was found, because the money is still the
            # user's. Counting them as spending inflates the chart and buries
            # the categories that represent money actually leaving.
            if name == cat.TRANSFER:
                continue
            amounts.setdefault(name, []).append(tx.amount)
            counts[name] = counts.get(name, 0) + 1
        rows = [(name, abs(total(items)), counts[name]) for name, items in amounts.items()]
        rows.sort(key=lambda r: -r[1].minor)
        return rows
