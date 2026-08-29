"""One place that owns the ledger the windows read from.

Views should never touch SQLite directly. They ask this object, which loads
once and hands out the same in-memory lists to everyone, so switching tabs is
instant and every screen agrees about the numbers it is showing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..analysis import budget as budget_mod
from ..analysis import categorize as cat
from ..analysis import guess as guess_mod
from ..analysis import networth as networth_mod
from ..analysis import price_changes, recurring, subscriptions, transfers
from ..analysis import spending as spending_mod
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
    price_changes: list = field(default_factory=list)
    balances: dict = field(default_factory=dict)
    manual: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    guesses: dict = field(default_factory=dict)  # transaction id -> Guess
    dismissed: list = field(default_factory=list)
    overrides: dict = field(default_factory=dict)
    user_rules: list = field(default_factory=list)
    categories_available: tuple = ()
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

        self.settings = db.all_settings(conn)
        self.balances = db.latest_balances(conn)
        self.manual = db.list_manual_subscriptions(conn)
        self.overrides = db.get_series_overrides(conn)
        self.user_rules = db.list_user_rules(conn)
        added, hidden = db.category_settings(conn)
        self.categories_available = cat.available_categories(added, hidden)
        self.verdicts = db.get_verdicts(conn)
        self.decided = db.get_verdict_dates(conn)
        # Inflows included so recurring income and person-to-person
        # payments are visible; the views split them out by kind.
        self.series = recurring.detect(self.transactions, include_inflows=True)
        self.series = subscriptions.apply_overrides(
            self.series + self.manual_series(), self.overrides
        )
        # A tracked entry carries its own kind, so it must not fall through
        # to the catalog and come back unknown.
        self.verdicts = {**subscriptions.manual_kinds(self.manual), **self.verdicts}
        self._split_dismissed()
        self.price_changes = price_changes.find_price_changes(self.transactions, series=self.series)
        # The user's rules outrank everything shipped, since they were
        # written while looking at the row that was wrong.
        assigned = cat.categorize_all(self.transactions, cat.rules_from(self.user_rules))
        self.categories = {
            tx.id: name for tx, name in zip(self.transactions, assigned, strict=True)
        }

        # Guessing is opt-in, and every guess stays marked as one. A guess the
        # user cannot tell apart from a rule match is worse than no guess.
        self.guesses = {}
        if self.setting("auto_categorize"):
            self.guesses = guess_mod.guess_all(self.transactions, assigned)
            for tx_id, found in self.guesses.items():
                self.categories[tx_id] = found.category
        conn.close()

    # -- derived views the screens ask for --------------------------------

    def manual_series(self) -> list[RecurringSeries]:
        return subscriptions.as_series(self.manual, self.series)

    def is_manual(self, series: RecurringSeries) -> bool:
        return subscriptions.is_manual(series)

    def add_manual(self, values: dict) -> None:
        conn = db.connect(self.path)
        db.add_manual_subscription(
            conn,
            values["merchant"],
            values["amount"],
            values["cadence"],
            kind=values.get("kind", "subscription"),
            paid_via=values.get("paid_via", ""),
            notes=values.get("notes", ""),
        )
        conn.close()
        self.manual = db.list_manual_subscriptions(db.connect(self.path))

    def remove_manual(self, series: RecurringSeries) -> bool:
        match = next((i for i in self.manual if str(i["merchant"]) == series.merchant), None)
        if match is None:
            return False
        conn = db.connect(self.path)
        db.remove_manual_subscription(conn, str(match["id"]))
        conn.close()
        self.manual = db.list_manual_subscriptions(db.connect(self.path))
        return True

    def spending_buckets(self, period: str = "monthly", *, include_guessed: bool = True) -> list:
        """Spending per period, with the computed categories rather than stored
        ones — nothing writes a category to the database."""
        names = [self.category_of(t, include_guessed=include_guessed) for t in self.transactions]
        return spending_mod.buckets(self.transactions, period=period, categories=names)

    def is_guessed(self, transaction_id: str) -> bool:
        return transaction_id in self.guesses

    def guess_reason(self, transaction_id: str) -> str:
        found = self.guesses.get(transaction_id)
        return found.reason if found else ""

    def add_rule(self, pattern: str, category: str) -> None:
        conn = db.connect(self.path)
        db.add_user_rule(conn, pattern, category)
        conn.close()
        self.load()

    def remove_rule(self, rule_id: str) -> None:
        conn = db.connect(self.path)
        db.remove_user_rule(conn, rule_id)
        conn.close()
        self.load()

    def add_category(self, name: str) -> None:
        conn = db.connect(self.path)
        db.add_user_category(conn, name)
        conn.close()
        self.load()

    def set_category_hidden(self, name: str, hidden: bool) -> None:
        conn = db.connect(self.path)
        db.hide_category(conn, name, hidden)
        conn.close()
        self.load()

    def rule_preview(self, pattern: str) -> int:
        """How many transactions a rule would match, before it is saved."""
        needle = pattern.strip().upper()
        if not needle:
            return 0
        return sum(1 for t in self.transactions if needle in t.description.upper())

    def setting(self, key: str):
        return self.settings.get(key, db.DEFAULT_SETTINGS.get(key))

    def save_setting(self, key: str, value) -> None:
        conn = db.connect(self.path)
        db.set_setting(conn, key, value)
        conn.close()
        self.settings[key] = value

    @property
    def excluded_accounts(self) -> set[str]:
        return set(self.setting("networth_excluded_accounts") or [])

    def networth_points(self, granularity: str = "monthly") -> list:
        """Reconstructed net worth history, or empty when no balance is known.

        Reconstruction walks transactions backwards from a balance the provider
        reported, so without at least one balance there is no anchor and the
        honest answer is nothing rather than a line starting at zero.
        """
        if not self.balances:
            return []
        # Excluded accounts are dropped from the inputs rather than subtracted
        # afterwards: a retirement account's transactions must not move the
        # line either, or the total and its shape disagree.
        excluded = self.excluded_accounts
        accounts = [a for a in self.accounts if a.id not in excluded]
        transactions = [t for t in self.transactions if t.account_id not in excluded]
        balances = {k: v for k, v in self.balances.items() if k not in excluded}
        if not balances:
            return []
        return networth_mod.reconstruct(accounts, transactions, balances, granularity=granularity)

    def accounts_without_balances(self) -> list[Account]:
        return networth_mod.accounts_missing_balances(self.accounts, self.balances)

    def budget_plan(self, goal, period: str = "monthly"):
        return budget_mod.plan(goal, self.transactions, series=self.series, period=period)

    def budget_progress(self, plan):
        """(spent, allowed, on track) for the period in progress, or None."""
        if plan is None or not plan.categories:
            return None
        start = budget_mod.start_of_period(date.today(), plan.period)
        report = budget_mod.progress(plan, self.transactions, start)
        spent = getattr(report, "spent", None)
        allowed = getattr(report, "allowance", None) or getattr(report, "allowed", None)
        if spent is None or allowed is None:
            return None
        return spent, allowed, bool(getattr(report, "on_track", True))

    def price_change_for(self, series: RecurringSeries):
        """The most recent price change for this merchant, if any."""
        matches = [c for c in self.price_changes if c.merchant.upper() == series.merchant.upper()]
        return max(matches, key=lambda c: c.changed_on) if matches else None

    def current_amount(self, series: RecurringSeries) -> Money:
        """What this series charges *now*, not its historical median.

        RecurringSeries.typical_amount is a median over the whole history,
        which is the right way to resist a one-off blip. After a price change
        it is the wrong number to show: eleven charges at $8.43 and four at
        $9.48 median to $8.43, so a view whose job is "what am I paying"
        would understate the bill the user is actually getting.
        """
        change = self.price_change_for(series)
        return change.new_amount if change is not None else series.typical_amount

    def current_annual(self, series: RecurringSeries) -> Money:
        """Annual cost at the current price."""
        change = self.price_change_for(series)
        if change is None:
            return series.annualised
        per_year = {
            "weekly": 52,
            "biweekly": 26,
            "monthly": 12,
            "quarterly": 4,
            "yearly": 1,
        }.get(series.cadence, 0)
        return abs(change.new_amount) * per_year if per_year else series.annualised

    def _split_dismissed(self) -> None:
        """Move dismissed series out of `series` and into `dismissed`.

        Dropped at the source rather than filtered in each view: a dismissed
        series is one the detector got wrong, and a total that quietly
        included it would be wrong in exactly the way the user just corrected.
        """
        everything = self.series + self.dismissed
        self.dismissed = [s for s in everything if self.kind_of(s) == subscriptions.DISMISSED]
        self.series = [s for s in everything if self.kind_of(s) != subscriptions.DISMISSED]

    def override_key(self, series: RecurringSeries) -> str:
        """The key a series' corrections are stored under.

        Corrections are keyed on the merchant as *detected*, but renaming a
        series changes the name every later lookup uses — so a renamed series
        could never be found again, and reset silently did nothing. Where the
        current name matches a stored display name, the original key is what
        comes back.
        """
        current = series.merchant.upper()
        if current in self.overrides:
            return current
        for key, correction in self.overrides.items():
            if str(correction.get("display_name") or "").upper() == current:
                return key
        return current

    def edit_series(self, series: RecurringSeries, **fields) -> None:
        """Correct one or more fields of a series, then reload so it takes."""
        conn = db.connect(self.path)
        db.set_series_override(conn, self.override_key(series), **fields)
        conn.close()
        self.load()

    def reset_series(self, series: RecurringSeries) -> None:
        """Discard every correction, returning the series to what was detected."""
        conn = db.connect(self.path)
        db.clear_series_override(conn, self.override_key(series))
        conn.close()
        self.load()

    def is_edited(self, series: RecurringSeries) -> bool:
        return self.override_key(series) in self.overrides

    def dismiss(self, series: RecurringSeries) -> None:
        """Mark a detected series as something the detector got wrong."""
        self.set_kind(series, subscriptions.DISMISSED)

    def restore(self, series: RecurringSeries) -> None:
        """Undo a dismissal, putting the series back to being unclassified."""
        conn = db.connect(self.path)
        db.clear_verdict(conn, series.merchant)
        conn.close()
        self.verdicts.pop(series.merchant.upper(), None)
        self._split_dismissed()

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
        # Re-split immediately, so dismissing something removes it from the
        # totals now rather than at the next reload.
        self._split_dismissed()

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

    def category_of(self, transaction: Transaction, *, include_guessed: bool = True) -> str:
        """A transaction's category, optionally ignoring guessed ones.

        With guesses excluded a guessed row reads as Uncategorized rather than
        vanishing: it is still money spent, and dropping it would quietly make
        every total smaller than the truth.
        """
        if not include_guessed and self.is_guessed(transaction.id):
            return cat.UNCATEGORIZED
        return self.categories.get(transaction.id, cat.UNCATEGORIZED)

    def spending_by_category(self, *, include_guessed: bool = True) -> list[tuple[str, Money, int]]:
        """(category, total spent, transaction count), biggest spend first."""
        amounts: dict[str, list[Money]] = {}
        counts: dict[str, int] = {}
        for tx in self.transactions:
            if not tx.is_outflow or tx.is_transfer:
                continue
            name = self.category_of(tx, include_guessed=include_guessed)
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
