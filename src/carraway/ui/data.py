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
from ..analysis import budgets as budgets_mod
from ..analysis import categorize as cat
from ..analysis import guess as guess_mod
from ..analysis import networth as networth_mod
from ..analysis import price_changes, recurring, subscriptions, transfers
from ..analysis import spending as spending_mod
from ..core import db
from ..core.models import Account, AccountType, RecurringSeries, Transaction
from ..core.money import Money, total


def _squash(text: str) -> str:
    """Letters and digits only, lowercased, with any parenthetical dropped.

    "Patreon (recklessben)" and "Patreon* Membership Internet CA" have to meet
    somewhere, and the bracketed note is the user's own aside rather than part
    of the merchant's name.
    """
    import re

    without_asides = re.sub(r"\([^)]*\)", " ", text)
    return "".join(ch for ch in without_asides.lower() if ch.isalnum())


def _wire(amount) -> str | None:
    """An amount as the wire wants it: a decimal string, or null.

    Never a float. The phone parses these back with Number() only for
    display; every figure that matters is computed on this side.
    """
    return None if amount is None else f"{amount.decimal:.2f}"


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
    balance_dates: dict = field(default_factory=dict)  # account id -> when last observed
    manual: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    guesses: dict = field(default_factory=dict)  # transaction id -> Guess
    dismissed: list = field(default_factory=list)
    overrides: dict = field(default_factory=dict)
    user_rules: list = field(default_factory=list)
    budgets: list = field(default_factory=list)
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
        self.balance_dates = db.latest_balance_dates(conn)
        self.manual = db.list_manual_subscriptions(conn)
        self.overrides = db.get_series_overrides(conn)
        self.user_rules = db.list_user_rules(conn)
        self.budgets = db.list_budgets(conn)
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
            paid_via_account=values.get("paid_via_account") or None,
            notes=values.get("notes", ""),
            started_on=values.get("started_on"),
        )
        conn.close()
        # A full reload rather than just refreshing `manual`: the series list
        # is built once during load, so updating the tracked entries alone
        # left the new subscription invisible until the app restarted.
        self.load()

    @property
    def payable_accounts(self) -> list[Account]:
        """Accounts worth offering as the thing that pays for a subscription.

        Closed ones are dropped, since nothing new bills to them. Every open
        one is kept, including the unlikely ones: ruling out a brokerage
        account would be a guess, and the cost of being wrong is a user who
        cannot record the truth. Cards first, because an autopay is far more
        often on a card than out of a savings account, and the top of a
        dropdown is where the common answer belongs.
        """
        order = {
            AccountType.CREDIT_CARD: 0,
            AccountType.CHECKING: 1,
            AccountType.CASH: 2,
            AccountType.SAVINGS: 3,
            AccountType.LOAN: 4,
            AccountType.INVESTMENT: 5,
        }
        return sorted(
            (a for a in self.accounts if not a.closed),
            key=lambda a: (order.get(a.type, 9), a.name.lower()),
        )

    # -- the phone inbox ---------------------------------------------------

    def pocket_client(self):
        """A client for the configured inbox, or None if none is set up."""
        from ..sync import credentials
        from ..sync.pocket import PocketClient

        url = self.setting("pocket_url")
        if not url:
            return None
        token = credentials.load("pocket_token")
        if not token:
            return None
        return PocketClient(str(url), token)

    def pair_pocket(self, pairing_url: str) -> str:
        """Redeem a pairing link so this computer can use the inbox.

        Returns where the secret ended up, so Settings can say so plainly
        rather than implying a safety the fallback store does not have.
        """
        from ..sync import credentials
        from ..sync.pocket import redeem

        base, token = redeem(pairing_url)
        where = credentials.store("pocket_token", token)
        self.save_setting("pocket_url", base)
        return where

    def unpair_pocket(self) -> None:
        """Forget the inbox on this end.

        This does not revoke the device on the server -- that needs a working
        token, which is exactly what is being thrown away. Settings revokes
        first and calls this second.
        """
        from ..sync import credentials

        credentials.delete("pocket_token")
        self.save_setting("pocket_url", "")

    @property
    def pocket_configured(self) -> bool:
        return self.pocket_client() is not None

    def collect_from_pocket(self) -> dict:
        """Bring in everything typed on the phone. Returns what happened.

        Written to the local database *before* the server is told, so a
        connection dropping between the two leaves entries to be collected
        again rather than gone. Carraway refuses to import the same
        transaction twice, so arriving twice is survivable; never arriving
        is not.
        """
        from ..sync.pocket import to_transactions

        client = self.pocket_client()
        if client is None:
            return {"configured": False, "added": 0, "unmatched": []}

        entries = client.pending()
        if not entries:
            return {"configured": True, "added": 0, "skipped": 0, "unmatched": []}

        by_name = {a.name: a.id for a in self.accounts}
        ready, unmatched = to_transactions(entries, by_name)
        added = skipped = 0
        if ready:
            conn = db.connect(self.path)
            added, skipped = db.insert_transactions(conn, ready)
            conn.close()
            # The spends have to land before any count is reconciled, or the
            # difference is measured against a ledger that is missing them
            # and the correction swallows the very spends just collected.
            self.load()

        corrections = self._apply_counts(entries, by_name, unmatched)

        # Only claim what was actually stored. An entry naming an account
        # this ledger does not have stays on the server, so it is not lost
        # while the user works out what to call it.
        stored = {e.id for e in entries} - {e.id for e in unmatched}
        client.claim(sorted(stored))

        self.load()
        return {
            "corrections": corrections,
            "configured": True,
            "added": added,
            "skipped": skipped,
            "unmatched": [e.description for e in unmatched],
        }

    def _apply_counts(self, entries, by_name: dict, unmatched: list) -> list[dict]:
        """Turn "this is what is in my wallet" into a correction, per count.

        The phone can only say what it can see -- a total in a pocket. What
        that means depends on everything this ledger already knows, so the
        subtraction happens here and nowhere else.

        Counts are applied oldest first, so two in one collection settle in
        the order they were made rather than the order they arrived.
        """
        lowered = {name.lower(): account_id for name, account_id in by_name.items()}
        made: list[dict] = []

        for entry in sorted(
            (e for e in entries if getattr(e, "is_count", False)),
            key=lambda e: e.occurred_on,
        ):
            account_id = lowered.get(entry.account.lower())
            if account_id is None or not self.is_cash_account(account_id):
                # A count only means anything for an account whose balance
                # nobody else reports. Left on the server rather than guessed.
                unmatched.append(entry)
                continue
            gap = self.set_cash_balance(account_id, entry.amount, correction=True)
            made.append(
                {
                    "account": self.account_name(account_id),
                    "counted": entry.amount,
                    "correction": gap,
                }
            )
        return made

    def pocket_snapshot(self) -> dict:
        """The small summary the phone shows.

        Two parts. A headline per live budget -- what is left for the rest of
        it, for the rest of this week, and for today -- because "can I buy
        this now" is the question actually being asked while standing in a
        shop, and a monthly figure does not answer it. Then the categories
        underneath, for when the answer is "on what".

        Still deliberately the least that answers it: category names and
        figures. No merchants, no transactions, no account names, nothing
        that says where the money is or how to reach it. That constraint is
        what lets the server hold this at all.
        """
        summaries: list[dict] = []
        lines: list[dict] = []

        for budget in self.budgets:
            state = self.budget_status(budget)
            if state.finished or not state.started:
                continue

            summaries.append(
                {
                    "name": budget.name,
                    "allowance": _wire(state.allowance),
                    "spent": _wire(state.spent),
                    "remaining": _wire(state.remaining),
                    "days_left": state.days_left,
                    "spent_today": _wire(state.spent_today),
                    "spent_this_week": _wire(state.spent_this_week),
                    "left_today": _wire(state.left_today),
                    "left_this_week": _wire(state.left_this_week),
                    "per_day": _wire(state.daily_remaining),
                    "on_track": state.on_track,
                }
            )

            for line in state.lines:
                if line.unbudgeted:
                    continue
                per_day = (
                    Money(line.remaining.minor // state.days_left, line.remaining.currency)
                    if state.days_left > 0
                    else None
                )
                lines.append(
                    {
                        "category": line.category,
                        "allowance": _wire(line.allowance),
                        "spent": _wire(line.spent),
                        "remaining": _wire(line.remaining),
                        "note": (
                            f"{budget.name} · {state.days_left} days left"
                            + (f" · {per_day.format()}/day" if per_day else "")
                        ),
                    }
                )

        return {"budgets": lines, "summaries": summaries}

    def publish_to_pocket(self) -> str | None:
        """Send the summary. Returns when the server stored it, or None."""
        client = self.pocket_client()
        if client is None:
            return None
        return client.publish(self.pocket_snapshot())

    # -- budgets the user sets and comes back to ---------------------------

    def save_budget(self, budget) -> None:
        conn = db.connect(self.path)
        db.save_budget(conn, budget)
        conn.close()
        self.load()

    def delete_budget(self, budget_id: str) -> bool:
        conn = db.connect(self.path)
        removed = db.delete_budget(conn, budget_id)
        conn.close()
        self.load()
        return bool(removed)

    def budget_by_id(self, budget_id: str):
        return next((b for b in self.budgets if b.id == budget_id), None)

    def budget_status(self, budget, *, asof: date | None = None):
        """How a budget is doing, judged with the user's own categorisations.

        Passing `self.categories` matters: it already has the user's rules and
        any guesses applied, so the budget agrees with what the Spending screen
        shows rather than re-deriving categories from the built-in rules.
        """
        return budgets_mod.status(budget, self.transactions, asof=asof, categories=self.categories)

    def suggest_envelopes(self, starts_on: date, ends_on: date, accounts=None):
        """What this window costs at the user's usual rate, per category."""
        return budgets_mod.suggest(
            self.transactions,
            starts_on,
            ends_on,
            categories=self.categories,
            accounts=accounts,
        )

    def spending_weights(self, accounts=None) -> dict:
        """Median monthly spend per category, for splitting a total in proportion."""
        return budgets_mod.monthly_baselines(
            self.transactions, categories=self.categories, accounts=accounts
        )

    def history_basis(self, accounts=None):
        """How much history the suggestions rest on, so a screen can say so."""
        return budgets_mod.history_basis(
            self.transactions, categories=self.categories, accounts=accounts
        )

    def income_estimate(self):
        """What the app thinks you make each month, and why it thinks it.

        Only recurring income counts, because that is the part that can be
        relied on next month. A one-off deposit is real money but budgeting
        against it plans to receive it again.
        """
        series = [s for s in self.series if self.kind_of(s) == subscriptions.INCOME]
        amount = self.typical_monthly_income()
        if not amount.minor:
            return budgets_mod.Estimate(
                amount,
                "No recurring income found yet, so there is nothing to offer — "
                "type what you expect to make.",
                confident=False,
            )
        names = sorted(s.merchant for s in series)
        shown = ", ".join(names[:2]) + (f" and {len(names) - 2} more" if len(names) > 2 else "")
        subject = "the one thing" if len(names) == 1 else f"the {len(names)} things"
        return budgets_mod.Estimate(
            amount,
            f"The monthly rate of {subject} you have marked as income ({shown}).",
        )

    def fixed_costs_estimate(self):
        """What is already spoken for each month, and where that comes from.

        Bills and subscriptions only. A habit is discretionary spending, and
        the whole point of the question is to separate the two.
        """
        committed = [s for s in self.series if self.kind_of(s) in budget_mod.COMMITTED_KINDS]
        amount = self.committed_per_month()
        if not amount.minor:
            return budgets_mod.Estimate(
                amount,
                "Nothing is classified as a bill or subscription yet, so there is "
                "nothing to add up.",
                confident=False,
            )
        subject = (
            "the one bill Carraway knows about"
            if len(committed) == 1
            else f"the {len(committed)} bills and subscriptions Carraway knows about"
        )
        return budgets_mod.Estimate(
            amount,
            f"The monthly rate of {subject}. Habits are left out — those are "
            "spending, and they belong in the table below.",
        )

    def committed_per_month(self) -> Money:
        """What recurring bills and subscriptions cost in a typical month.

        Offered as the starting figure for "my fixed costs are…", since the
        app already knows: it is exactly the series the user has classified as
        bills or subscriptions, at their monthly rate. A habit is not counted
        — that is discretionary spending, and the whole point of the question
        is to separate the two.
        """
        per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
        minor = 0
        for series in self.series:
            if self.kind_of(series) not in budget_mod.COMMITTED_KINDS:
                continue
            amount = self.current_amount(series)
            if amount.minor >= 0:
                continue
            minor += abs(amount.minor) * per_year.get(series.cadence, 0) // 12
        return Money(minor)

    def committed_by_category(self) -> dict[str, Money]:
        """What commitments cost per month, split by the category they land in.

        Needed so that "work backwards" does not budget for rent twice: the
        user's fixed-costs figure already covers it, so rent must be given its
        real allowance rather than a proportional share of what is left over.

        The category comes from the charges themselves by majority vote, not
        from the merchant name, so a mis-normalised name cannot move rent out
        of Housing.
        """
        from collections import Counter

        per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
        by_id = {tx.id: tx for tx in self.transactions}
        out: dict[str, int] = {}
        for series in self.series:
            if self.kind_of(series) not in budget_mod.COMMITTED_KINDS:
                continue
            amount = self.current_amount(series)
            if amount.minor >= 0:
                continue
            votes = Counter(
                self.category_of(by_id[tx_id]) for tx_id in series.transaction_ids if tx_id in by_id
            )
            # A tracked entry has no transactions to vote, so it has no
            # category of its own; those land in Subscriptions, which is where
            # the user would look for them.
            category = votes.most_common(1)[0][0] if votes else "Subscriptions"
            monthly = abs(amount.minor) * per_year.get(series.cadence, 0) // 12
            out[category] = out.get(category, 0) + monthly
        return {name: Money(minor) for name, minor in out.items() if minor > 0}

    def typical_monthly_income(self) -> Money:
        """Recurring income at its monthly rate, as a starting figure."""
        per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
        minor = 0
        for series in self.series:
            if self.kind_of(series) != subscriptions.INCOME:
                continue
            amount = self.current_amount(series)
            if amount.minor <= 0:
                continue
            minor += amount.minor * per_year.get(series.cadence, 0) // 12
        return Money(minor)

    # -- cash accounts, which no bank feed can tell us about ---------------

    def is_cash_account(self, account_id: str | None) -> bool:
        """True for an account whose balance only a person can know.

        Everything else is synced or imported from a statement, and a figure
        typed over one of those would be silently replaced on the next
        refresh. Cash is the one kind where the user is the only source.
        """
        if account_id is None:
            return False
        return any(a.id == account_id and a.type is AccountType.CASH for a in self.accounts)

    def implied_balance(self, account_id: str) -> Money | None:
        """What the records say the account holds now, or None if they say nothing.

        The last observed balance rolled forward by everything since it, not
        the sum of every transaction: an imported statement rarely reaches
        back to the day the account opened, so summing it all silently treats
        an unknown opening balance as zero. On real data that was wrong by
        $4,928 — the whole balance that existed before the first imported row.

        "Since" means strictly after, because a recorded balance is a closing
        figure that already includes its own day. Counting same-day
        transactions on top of it double-counts them: on real data two
        transfers landing on the reading date would have inflated the balance
        by $137.
        """
        observed = self.balance_dates.get(account_id)
        base = self.balances.get(account_id)
        moves = [t for t in self.transactions if t.account_id == account_id]
        if base is None or observed is None:
            # Never observed. The transactions are all there is, and treating
            # the opening balance as zero is the only assumption available —
            # which is exactly what a correction line is for.
            return total([t.amount for t in moves]) if moves else None
        since = [t for t in moves if t.date > observed]
        return Money(base.minor + sum(t.amount.minor for t in since), base.currency)

    def set_cash_balance(self, account_id: str, amount: Money, correction: bool = False) -> Money:
        """Record what the user says an account holds. Returns the correction made.

        The balance is always recorded, so net worth is right either way. The
        correction line is optional and separate: it exists so the *history*
        adds up to the same figure, which is what Spending and the category
        totals read. Declining it leaves a knowingly incomplete history rather
        than inventing a transaction the user did not agree to.
        """
        implied = self.implied_balance(account_id)
        gap = Money(amount.minor - implied.minor, amount.currency) if implied else amount

        conn = db.connect(self.path)
        if correction and gap.minor:
            db.insert_transactions(conn, [self._correction(account_id, gap)])
        db.record_balance(conn, account_id, amount, date.today())
        conn.close()
        self.load()
        return gap

    def _correction(self, account_id: str, gap: Money) -> Transaction:
        """A transaction standing in for movements that were never recorded."""
        import uuid

        return Transaction(
            id=uuid.uuid4().hex,
            account_id=account_id,
            date=date.today(),
            amount=gap,
            # Named so it is obvious in the ledger that a person adjusted this
            # rather than a bank reporting it.
            description="Cash adjustment",
            merchant="Cash adjustment",
        )

    def add_cash_transaction(
        self, account_id: str, when: date, description: str, amount: Money
    ) -> bool:
        """Record a movement the user knows about. False if it was a duplicate."""
        import uuid

        transaction = Transaction(
            id=uuid.uuid4().hex,
            account_id=account_id,
            date=when,
            amount=amount,
            description=description,
            merchant=recurring.normalise_merchant(description),
        )
        conn = db.connect(self.path)
        inserted, _ = db.insert_transactions(conn, [transaction])
        conn.close()
        self.load()
        return bool(inserted)

    def rename_account(self, account_id: str, name: str) -> bool:
        """Change an account's display name, leaving its history alone."""
        account = next((a for a in self.accounts if a.id == account_id), None)
        if account is None:
            return False
        conn = db.connect(self.path)
        db.upsert_account(
            conn,
            Account(
                id=account.id,
                name=name,
                type=account.type,
                institution=account.institution,
                currency=account.currency,
                external_id=account.external_id,
                closed=account.closed,
            ),
        )
        conn.close()
        self.load()
        return True

    def manual_entry(self, series: RecurringSeries) -> dict | None:
        """The stored row behind a tracked series, or None if it was detected.

        Matched on merchant, which is what `as_series` copies across. Detected
        series never match, because nothing put them in the table.
        """
        return next((i for i in self.manual if str(i["merchant"]) == series.merchant), None)

    def paid_with(self, series: RecurringSeries) -> str:
        """Which card, account or route this charge comes out of.

        Order matters, and it is most-specific-thing-the-user-said first:

        1. a correction made on this series,
        2. what they typed when they tracked it by hand,
        3. the account the charge actually landed in.

        The statement comes last because it cannot answer the question being
        asked. "Who pays for this" is not "where did it land" -- a charge can
        land on a card that somebody else settles.

        Putting the account above the tracked entry was a real bug. A detected
        series can share a merchant with a tracked one that detection then
        suppresses as a duplicate, and the tracked entry is where its "paid
        with" is stored -- so editing it saved correctly, the account won the
        lookup, and the edit appeared to do nothing at all.
        """
        override = self.paid_with_override(series)
        if override:
            return override
        entry = self.manual_entry(series)
        if entry:
            account_id = entry.get("paid_via_account")
            if account_id:
                return self.account_name(str(account_id))
            typed = str(entry.get("paid_via") or "")
            if typed:
                return typed
        if series.account_id:
            return self.account_name(series.account_id)
        return ""

    def paid_with_override(self, series: RecurringSeries) -> str:
        """What the user said pays for this, or empty if they never said."""
        fields = self.overrides.get(series.merchant.upper(), {})
        account_id = fields.get("paid_via_account")
        if account_id:
            return self.account_name(str(account_id))
        return str(fields.get("paid_via") or "")

    def paid_with_is_corrected(self, series: RecurringSeries) -> bool:
        """Whether the shown value is the user's, rather than the statement's."""
        return bool(self.paid_with_override(series))

    def set_paid_with(self, series: RecurringSeries, choice: dict) -> bool:
        """Record how this is paid for. Works for tracked and detected alike.

        A tracked entry keeps its answer on the entry itself. A detected one
        gets an override, so the account the charge actually landed in stays
        underneath and `clear_paid_with` can restore it.

        Which of the two is decided by the series in front of the user, not by
        whether a tracked entry of the same name happens to exist. Deciding it
        the other way sent the edit to a row that was not the one on screen.
        """
        entry = self.manual_entry(series) if self.is_manual(series) else None
        conn = db.connect(self.path)
        if entry is not None:
            db.set_manual_paid_via(
                conn,
                str(entry["id"]),
                paid_via=choice.get("paid_via", ""),
                paid_via_account=choice.get("paid_via_account") or None,
            )
        else:
            db.set_series_override(
                conn,
                series.merchant,
                paid_via=choice.get("paid_via") or None,
                paid_via_account=choice.get("paid_via_account") or None,
            )
        conn.close()
        self.load()
        return True

    def set_billed_on(self, series: RecurringSeries, when) -> bool:
        """Set when a tracked entry last billed, so a next charge can be shown.

        Only tracked entries: a detected series already has real charges to
        count forward from, and overriding those with a typed date would make
        the projection worse, not better.
        """
        entry = self.manual_entry(series)
        if entry is None:
            return False
        conn = db.connect(self.path)
        db.set_manual_started_on(conn, str(entry["id"]), when)
        conn.close()
        self.load()
        return True

    def suggest_billed_on(self, series: RecurringSeries):
        """The most recent charge that looks like this entry, or None.

        For an entry typed in by hand, the date it last billed is usually
        sitting in the statements already -- it simply was not matched to the
        entry, which is why detection did not cover it in the first place.

        The matching is deliberately strict. A loose match on any long word
        offered "Ava Hollis - Apostle island plus food" as the last charge for
        "SoundCloud Plus", and prefilling a date that confidently wrong is
        worse than offering nothing: the user would accept it, and every
        future charge would be projected from a number out of thin air. So
        the entry's name must appear whole in the description.
        """
        needle = _squash(series.merchant)
        if len(needle) < 4:
            return None
        best = None
        for tx in self.transactions:
            if tx.is_transfer or not tx.is_outflow:
                continue
            if needle not in _squash(tx.description):
                continue
            if best is None or tx.date > best:
                best = tx.date
        return best

    def billed_on(self, series: RecurringSeries):
        """The anchor date on a tracked entry, or None if never set."""
        entry = self.manual_entry(series)
        return entry.get("started_on") if entry else None

    def clear_paid_with(self, series: RecurringSeries) -> bool:
        """Drop a correction and go back to what the statement says."""
        if not self.paid_with_is_corrected(series):
            return False
        conn = db.connect(self.path)
        db.set_series_override(conn, series.merchant, paid_via=None, paid_via_account=None)
        conn.close()
        self.load()
        return True

    def delete_manual(self, series: RecurringSeries) -> bool:
        """Remove a tracked entry outright, for one added by mistake."""
        match = self.manual_entry(series)
        if match is None:
            return False
        conn = db.connect(self.path)
        db.delete_manual_subscription(conn, str(match["id"]))
        conn.close()
        self.load()
        return True

    def remove_manual(self, series: RecurringSeries) -> bool:
        match = self.manual_entry(series)
        if match is None:
            return False
        conn = db.connect(self.path)
        db.remove_manual_subscription(conn, str(match["id"]))
        conn.close()
        self.load()
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
