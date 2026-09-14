"""One place that owns the ledger the windows read from.

Views should never touch SQLite directly. They ask this object, which loads
once and hands out the same in-memory lists to everyone, so switching tabs is
instant and every screen agrees about the numbers it is showing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
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
    expected: list = field(default_factory=list)  # money owed that has not landed
    # transaction id -> category chosen for that one row by hand
    category_overrides: dict = field(default_factory=dict)
    # budget id -> transaction ids taken out of that budget alone
    exclusions: dict = field(default_factory=dict)
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
        self.expected = db.list_expected_money(conn)
        self.exclusions = db.all_budget_exclusions(conn)
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

        # A category chosen for one row outranks everything else -- the rules,
        # the user's own rules, and the guesser. Applied last for that reason:
        # applied before the guesses, a hand-filed row with no matching rule
        # still read as Uncategorized to the guesser, which then overwrote the
        # user's answer with its own. A rule is a guess about a merchant; this
        # is a decision about a transaction.
        self.category_overrides = db.category_overrides(conn)
        for tx_id, name in self.category_overrides.items():
            if tx_id in self.categories:
                self.categories[tx_id] = name
                # No longer a guess, so it must not be marked or filtered as one.
                self.guesses.pop(tx_id, None)

        # Renamed categories. Stored references are rewritten when a rename
        # happens, but a built-in category's name comes from the code's own
        # rules on every load, so it has to be translated here as well --
        # after everything that can produce a category name, so none slips
        # through under the old one.
        renames = self.category_renames
        if renames:
            self.categories = {k: renames.get(v, v) for k, v in self.categories.items()}
            seen: set[str] = set()
            # Hidden is judged on the name after renaming as well as before.
            # Settings hides what it shows, which for a renamed built-in is
            # the new name -- but the built-in list still says the old one, so
            # checked only beforehand, hiding "Food" left Dining in the list
            # and translated it straight back to "Food".
            self.categories_available = tuple(
                n
                for n in (renames.get(name, name) for name in self.categories_available)
                if n not in hidden and not (n in seen or seen.add(n))
            )
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
            self._file_as_chosen_on_the_phone(conn, ready)
            conn.close()
            # The spends have to land before any count is reconciled, or the
            # difference is measured against a ledger that is missing them
            # and the correction swallows the very spends just collected.
            self.load()

        corrections = self._apply_counts(entries, by_name, unmatched)
        verdicts = self._apply_verdicts(entries)
        filed = self._apply_categorisations(entries)

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
            **verdicts,
            "categorised": filed,
        }

    def _file_as_chosen_on_the_phone(self, conn, spends: list[Transaction]) -> None:
        """Keep the category picked when a spend was typed on the phone.

        The phone asks for one with every spend, and it arrived on the row --
        where nothing reads it. Categories come from the rules, and a cash
        spend typed as "lunch with sam" matches none of them, so every spend
        logged from the phone landed in Uncategorized however carefully it
        had been filed. Stored as a choice made by hand, which is what it is,
        so the phone can still hand it back to the rules.

        Only a name this ledger offers. The phone's own list has drifted from
        the laptop's before, and a name nothing else uses would quietly start
        a category of its own.
        """
        offered = set(self.categories_available) - {cat.UNCATEGORIZED}
        renames = self.category_renames
        chosen = {
            tx.id: name
            for tx in spends
            if (name := renames.get(tx.category, tx.category)) in offered
        }
        if not chosen:
            return
        marks = ",".join("?" for _ in chosen)
        # Only rows that are really there. A spend collected twice is skipped
        # on insert under its first id, and a hand-set category pointing
        # at nothing would be clutter.
        stored = {
            r[0]
            for r in conn.execute(
                f"SELECT id FROM transactions WHERE id IN ({marks})", list(chosen)
            )
        }
        for tx_id in stored:
            db.set_category_override(conn, tx_id, chosen[tx_id])

    def _apply_categorisations(self, entries) -> int:
        """File transactions under the categories chosen on the phone.

        Oldest first, so a row changed twice on the way to the shops ends
        where it was left. A category for a transaction this ledger no longer
        has is dropped rather than retried, for the same reason as a budget
        verdict: nothing the user could do would make it apply later.
        """
        chosen = sorted(
            (e for e in entries if getattr(e, "is_categorisation", False)),
            key=lambda e: e.occurred_on,
        )
        if not chosen:
            return 0
        known = {tx.id for tx in self.transactions}
        latest: dict[str, str] = {}
        for entry in chosen:
            if entry.subject in known:
                latest[entry.subject] = entry.category
        if not latest:
            return 0
        conn = db.connect(self.path)
        for tx_id, category in latest.items():
            db.set_category_override(conn, tx_id, category or None)
        conn.close()
        self.load()
        return len(latest)

    def _apply_verdicts(self, entries) -> dict:
        """Apply "stop counting this" / "count it again" from the phone.

        Later verdicts win over earlier ones on the same transaction, which
        is what happens naturally when they are applied oldest first: a row
        toggled twice on the way to the shops ends where it was left.

        A verdict naming a transaction this ledger does not have is counted
        and dropped rather than left on the server. Unlike an entry with an
        unfamiliar account name, there is nothing the user could do to make
        it apply later -- the history the phone was reading only reaches back
        ninety days, and an id outside that is gone from its own point of
        view too. Leaving it would mean retrying it at every collection for
        ever.
        """
        verdicts = sorted(
            (e for e in entries if getattr(e, "is_verdict", False)),
            key=lambda e: e.occurred_on,
        )
        if not verdicts:
            return {}

        known = {tx.id for tx in self.transactions}
        budgets = {b.id for b in self.budgets}
        # Keyed by (scope, transaction): a row can be taken out of September
        # and left in the trip budget in one collection, and the two must not
        # overwrite each other.
        wanted: dict[tuple[str, str], bool] = {}
        # Transaction id -> the minor units that still count toward budgets.
        shares: dict[str, int] = {}
        unknown = 0
        for entry in verdicts:
            scope = getattr(entry, "scope", "") or ""
            if entry.subject not in known:
                unknown += 1
                continue
            # A verdict may carry the part that still counts. Zero means none
            # of it, which is what every verdict meant before shares existed,
            # so an older phone keeps saying exactly what it used to.
            #
            # A share and an every-budget verdict are two answers to the same
            # question -- how much of this counts -- so whichever came later
            # replaces the other. Kept in separate piles and applied shares
            # first, "take it out" followed by "count $20 of it" ended fully
            # excluded, and "count it again" followed by a share lost the
            # share: the older answer was applied last.
            if entry.excludes and getattr(entry, "amount", None) is not None:
                counted = abs(entry.amount.minor)
                if counted:
                    shares[entry.subject] = counted
                    wanted.pop(("", entry.subject), None)
                    continue
            if scope and scope not in budgets:
                # Named a budget this ledger no longer has. Same reasoning as
                # an unknown transaction: nothing the user could do would make
                # it apply later.
                unknown += 1
                continue
            wanted[(scope, entry.subject)] = entry.excludes
            if not scope:
                shares.pop(entry.subject, None)

        if shares:
            by_id = {tx.id: tx for tx in self.transactions}
            conn = db.connect(self.path)
            for tx_id, counted in shares.items():
                tx = by_id.get(tx_id)
                if tx is None:
                    continue
                whole = abs(tx.amount.minor)
                db.set_budget_share(
                    conn, tx_id, Money(max(whole - counted, 0), tx.amount.currency)
                )
            conn.close()
            self.load()

        if wanted:
            conn = db.connect(self.path)
            for scope in {s for s, _ in wanted}:
                for excluded in (True, False):
                    ids = [
                        tx_id
                        for (s, tx_id), flag in wanted.items()
                        if s == scope and flag is excluded
                    ]
                    if not ids:
                        continue
                    if scope:
                        db.set_budget_exclusion(conn, scope, ids, excluded)
                    else:
                        db.set_budget_excluded(conn, ids, excluded)
            conn.close()
            self.load()

        return {
            "excluded": sum(1 for flag in wanted.values() if flag),
            "included": sum(1 for flag in wanted.values() if not flag),
            "shared": len(shares),
            "unknown_verdicts": unknown,
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

    # -- the encrypted history ---------------------------------------------

    def vault_key(self) -> str | None:
        """The key that seals the history, or None if there is not one yet."""
        from ..sync import credentials

        return credentials.load("pocket_vault_key")

    def new_vault_key(self) -> str:
        """Mint a key and keep it. Shown once, typed into the phone once.

        Replacing an existing key makes every published history unreadable,
        which is the point of a rotation -- the caller is expected to have
        asked first.
        """
        from ..sync import credentials
        from ..sync.vault import new_key

        key = new_key()
        credentials.store("pocket_vault_key", key)
        return key

    def forget_vault_key(self) -> None:
        from ..sync import credentials

        credentials.delete("pocket_vault_key")

    def pocket_history(self, days: int = 90) -> dict:
        """The recent history, for reading on the phone.

        Everything in here is sealed before it leaves, so it can afford to be
        the real thing: dates, descriptions, amounts, categories and which
        account. That is what makes "what did I spend on Tuesday" answerable
        away from the laptop.

        Bounded to a window rather than the whole ledger. A phone showing
        three years of statements is not more useful than one showing three
        months, and every row is a row that has to be encrypted, sent, and
        stored on a box that does not need it.
        """
        from datetime import timedelta

        cutoff = date.today() - timedelta(days=days)
        names = {a.id: a.name for a in self.accounts}
        rows = [
            {
                # The ledger's own id, so a row read on the phone can be
                # acted on there and the answer applied to the right
                # transaction here. It rides inside the sealed blob like
                # everything else in this payload; the only place it appears
                # in the clear is on a verdict coming back, where it is the
                # one field there is.
                "id": tx.id,
                "date": tx.date.isoformat(),
                "description": tx.description,
                "amount": f"{tx.amount.decimal:.2f}",
                "category": self.category_of(tx),
                "account": names.get(tx.account_id, ""),
                # Set by hand rather than by a rule, so the phone can offer to
                # hand it back. Omitted otherwise, which is nearly every row.
                **({"filed": True} if tx.id in self.category_overrides else {}),
                "excluded": bool(getattr(tx, "budget_excluded", False)),
                # How much of this is held back, when only part of it counts.
                # Omitted when zero, which is nearly every row.
                **(
                    {"held": f"{Money(held).decimal:.2f}"}
                    if (
                        held := min(
                            abs(getattr(tx, "budget_excluded_minor", 0) or 0),
                            abs(tx.amount.minor),
                        )
                    )
                    and not getattr(tx, "budget_excluded", False)
                    else {}
                ),
                # Which budgets this row has been taken out of individually.
                # Omitted when empty, which is nearly every row -- ninety days
                # of statements would otherwise carry several hundred empty
                # lists through a PBKDF2 seal on every publish.
                **(
                    {"excluded_from": out}
                    if (out := self.excluded_from(tx.id))
                    else {}
                ),
            }
            for tx in self.transactions
            if tx.date >= cutoff and not tx.is_transfer
        ]
        rows.sort(key=lambda row: row["date"], reverse=True)
        return {"days": days, "transactions": rows}

    def sealed_history(self, days: int = 90) -> dict | None:
        """The history, encrypted. None when no key has been set up."""
        from ..sync.vault import seal

        key = self.vault_key()
        if not key:
            return None
        return seal(self.pocket_history(days), key).as_json()

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
                    # The id as well as the name: the phone sends a verdict
                    # naming a budget, and a name is not a handle.
                    "id": budget.id,
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
                    # Where you should be by now, and how far from it you
                    # actually are. "On track" is a yes or no; this is the
                    # number that says whether to worry, and by how much.
                    # Negative means spent more than the pace.
                    #
                    # The pace is not an even line: it steps on the days the
                    # known bills fall, so a month whose rent leaves on the 3rd
                    # does not read as a disaster on the 4th.
                    "pace": _wire(state.pace),
                    "ahead_by": _wire(
                        Money(state.pace.minor - state.spent.minor, state.spent.currency)
                    ),
                    # What the pace is made of, so the phone can say why it
                    # moved. A number that jumps a thousand dollars overnight
                    # needs to explain itself or it will not be believed.
                    # Both paces travel. The dynamic one leads on the phone,
                    # but the even line stays available underneath: the gap
                    # between them is the whole point, and a figure with
                    # nothing to compare it against explains nothing.
                    "even_pace": _wire(state.even_pace),
                    "even_ahead_by": _wire(
                        Money(state.even_pace.minor - state.spent.minor, state.spent.currency)
                    ),
                    # Money the user took out of budgeting by hand. Sent so the
                    # phone can say so too: a total that quietly omits a spend
                    # is how this screen would stop matching the bank.
                    "excluded": _wire(state.excluded),
                    "bills_due": _wire(state.scheduled_so_far),
                    "bills_total": _wire(state.scheduled_total),
                    "bills_to_come": _wire(
                        Money(
                            state.scheduled_total.minor - state.scheduled_so_far.minor,
                            state.scheduled_total.currency,
                        )
                    ),
                    "elapsed_days": state.elapsed_days,
                    "total_days": state.total_days,
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

        return {
            "budgets": lines,
            "summaries": summaries,
            # How the server should file the rows it fetches, so a category
            # muted on the phone means the same thing at both ends.
            #
            # Fingerprints, not merchant patterns. The first version of this
            # published the user's 89 rules in the clear -- "CROOKED PINT" ->
            # Dining -- which put a readable list of the places they go on a
            # machine that is meant to hold nothing readable. A hash of each
            # recent transaction answers the same question: the fetcher
            # hashes what it pulls, finds the category, and never learns a
            # name it did not already have in front of it.
            #
            # Only the recent window, because that is all the fetcher sees.
            "filing": self.fingerprint_categories(days=45),
            # What the phone may file a transaction under, starred first, so
            # its picker offers the same list as the laptop's rather than a
            # hardcoded one that drifts from categories the user has added or
            # hidden. Travels inside the sealed part like the rest.
            "categories": sorted(
                self.categories_available,
                key=lambda name: (name not in self.favourite_categories, name),
            ),
            "favourites": sorted(self.favourite_categories),
            # Net worth now, and what it becomes once everything on its way
            # lands -- the two figures the Net worth screen leads with, kept
            # apart the same way. Sealed with the rest: this is the single
            # most sensitive number the app produces.
            "networth": self.networth_summary(),
        }

    def fingerprint_categories(self, days: int = 45) -> dict:
        """{fingerprint: category} for recent transactions.

        The fingerprint is the same one the server computes to remember what
        it has already announced, so the two agree without either end sending
        a merchant name. Truncated to a window because the server only ever
        fetches recent rows, and a fingerprint for something it will never
        see is payload for nothing.
        """
        import hashlib
        from datetime import timedelta

        cutoff = date.today() - timedelta(days=days)
        names = {a.id: a.name for a in self.accounts}
        out: dict[str, str] = {}
        for tx in self.transactions:
            if tx.date < cutoff:
                continue
            category = self.category_of(tx)
            if not category or category == "Uncategorized":
                continue
            raw = "|".join(
                (
                    tx.date.isoformat(),
                    f"{tx.amount.decimal:.2f}",
                    tx.description,
                    names.get(tx.account_id, ""),
                )
            )
            out[hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]] = category
        return out

    def pocket_digest(self) -> str:
        """A fingerprint of what would be published, without encrypting it.

        Sealing costs 600,000 PBKDF2 rounds, so republishing on a timer would
        burn half a second and a network round trip every time whether or not
        anything had changed. Hashing the plaintext is cheap and answers the
        only question worth asking first: is this different from last time?
        """
        import hashlib
        import json

        payload = {"snapshot": self.pocket_snapshot()}
        if self.vault_key():
            payload["history"] = self.pocket_history()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def publish_to_pocket(self) -> str | None:
        """Send the summary and the history, encrypting what can be encrypted.

        The history has always been sealed. The *summary* used to travel in
        the clear on the grounds that it was only category names and figures
        -- but "$3,478 of $4,000 spent, $225 left in Dining" is a description
        of someone's month, and the server has no need of it: only the phone
        reads it.

        So it is sealed too, when there is a key. What stays readable is the
        one part the server has to act on: `filing`, a map of transaction
        fingerprints to category names, which is what lets the fetch timer
        decide whether a new charge is one the user asked to be told about.
        Hashes and generic category names, no merchants, no amounts.
        """
        from ..sync.vault import seal

        client = self.pocket_client()
        if client is None:
            return None

        snapshot = dict(self.pocket_snapshot())
        key = self.vault_key()
        payload: dict = {}
        if key:
            # Handed over whole, minus the part the server must read.
            filing = snapshot.pop("filing", {})
            payload = {"sealed": seal(snapshot, key).as_json(), "filing": filing}
        else:
            payload = snapshot

        sealed = self.sealed_history()
        if sealed is not None:
            payload["history"] = sealed
        return client.publish(payload)

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
        return budgets_mod.status(
            budget,
            self.transactions,
            asof=asof,
            categories=self.categories,
            # Without this the pace is a straight line, and a month whose bills
            # land on the 1st reads as a disaster on the 2nd.
            schedule=self.commitment_schedule(budget.starts_on, budget.ends_on),
            excluded_ids=self.exclusions.get(budget.id),
        )

    # -- taking a row out of one budget rather than all of them -------------

    #: Names the app's own logic depends on. Transfers are dropped from
    #: spending by the name "Transfer", income is found by "Income", and
    #: "Uncategorized" is what everything unmatched falls back to -- renaming
    #: any of them would quietly break the thing that looks for it.
    RESERVED_CATEGORIES = frozenset({"Income", "Transfer", "Uncategorized"})

    @property
    def category_renames(self) -> dict[str, str]:
        saved = self.setting("category_renames")
        return {str(k): str(v) for k, v in saved.items()} if isinstance(saved, dict) else {}

    def rename_category(self, old: str, new: str) -> str | None:
        """Rename a category everywhere. Returns why not, or None when done."""
        new = " ".join(str(new).split())
        if not new:
            return "A category needs a name."
        if old in self.RESERVED_CATEGORIES:
            return f"{old} is used by the app itself and cannot be renamed."
        if new in self.RESERVED_CATEGORIES:
            return f"{new} is reserved for the app's own use."
        if new == old:
            return None

        conn = db.connect(self.path)
        db.rename_category(conn, old, new)
        conn.close()

        # The alias, for built-in names that come from the code on every load.
        # Earlier renames that ended at `old` are pointed straight at `new`,
        # so renaming twice leaves one hop rather than a chain.
        renames = {k: (new if v == old else v) for k, v in self.category_renames.items()}
        original = next((k for k, v in self.category_renames.items() if v == old), old)
        renames[original] = new
        renames.pop(new, None)
        self.save_setting("category_renames", renames)

        favourites = self.favourite_categories
        if old in favourites:
            favourites.discard(old)
            favourites.add(new)
            self.save_setting(self.FAVOURITES_SETTING, sorted(favourites))

        self.load()
        return None

    def set_transaction_category(self, transaction_id: str, category: str | None) -> None:
        """File one transaction by hand, or put it back under the rules."""
        conn = db.connect(self.path)
        db.set_category_override(conn, transaction_id, category)
        conn.close()
        self.load()

    def budget_exclusions(self, budget_id: str) -> set[str]:
        return set(self.exclusions.get(budget_id) or ())

    def set_budget_exclusion(
        self, budget_id: str, transaction_ids: list[str], excluded: bool
    ) -> int:
        conn = db.connect(self.path)
        changed = db.set_budget_exclusion(conn, budget_id, transaction_ids, excluded)
        conn.close()
        self.load()
        return changed

    def excluded_from(self, transaction_id: str) -> list[str]:
        """The ids of every budget this transaction has been taken out of."""
        return sorted(
            budget_id
            for budget_id, ids in self.exclusions.items()
            if transaction_id in ids
        )

    def suggest_envelopes(self, starts_on: date, ends_on: date, accounts=None):
        """What this window costs at the user's usual rate, per category.

        Handed the commitment schedule as well as the history, so a bill is
        counted in the window it actually lands in rather than smeared across
        every window at a daily rate. See `budgets.suggest`.
        """
        return budgets_mod.suggest(
            self.transactions,
            starts_on,
            ends_on,
            categories=self.categories,
            accounts=accounts,
            scheduled_monthly=self.committed_by_category(),
            scheduled_in_window=self.committed_by_category(starts_on, ends_on),
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
        # A series whose expected charge never arrived is not a commitment.
        # committed_by_category has skipped these since the day a quarterly
        # subscription last charged in May 2025 was found still claiming $16 a
        # month; this function was left counting it, so the headline fixed-cost
        # figure and the per-category breakdown disagreed by exactly that
        # amount. Money the budget reserves for a subscription that stopped
        # billing is money the user is told they cannot spend.
        stale = {id(series) for series in self.stale_series}
        minor = 0
        for series in self.series:
            if self.kind_of(series) not in budget_mod.COMMITTED_KINDS:
                continue
            if id(series) in stale:
                continue
            amount = self.current_amount(series)
            if amount.minor >= 0:
                continue
            minor += abs(amount.minor) * per_year.get(series.cadence, 0) // 12
        return Money(minor)

    def series_category(self, series) -> str:
        """Which category a recurring series' money lands in.

        One definition, used by everything that needs it. It existed in three
        places at once and they had already drifted apart -- the per-category
        breakdown skipped stale series while the headline figure counted them,
        and the two disagreed by exactly the cost of a subscription cancelled
        sixteen months earlier. A single answer cannot drift from itself.

        Charges vote when there are any, so a mis-normalised merchant name
        cannot move rent out of Rent/Mortgage. A tracked entry has no charges,
        so it says for itself; one that says nothing falls back to
        Subscriptions, where somebody would look for it.
        """
        from collections import Counter

        by_id = {tx.id: tx for tx in self.transactions}
        votes = Counter(
            self.category_of(by_id[tx_id])
            for tx_id in getattr(series, "transaction_ids", ()) or ()
            if tx_id in by_id
        )
        if votes:
            return votes.most_common(1)[0][0]
        return self.tracked_category(series) or "Subscriptions"

    def commitment_schedule(self, start: date, end: date) -> list:
        """Every charge already expected between two dates, with its date.

        A budget's pace is a straight line without this, which is wrong in any
        month that contains bills: rent leaving on the 1st is not overspending
        on the 2nd. See analysis.budgets.Commitment.

        Stale series are left out for the same reason they are left out of the
        committed totals -- a charge that stopped arriving is not expected.
        """
        stale = {id(series) for series in self.stale_series}

        out = []
        for series in self.series:
            if self.kind_of(series) not in budget_mod.COMMITTED_KINDS:
                continue
            if id(series) in stale:
                continue
            amount = self.current_amount(series)
            if amount.minor >= 0:
                continue
            category = self.series_category(series)
            for when in self.occurrences(series, start, end):
                out.append(budgets_mod.Commitment(due=when, category=category, amount=amount))
        out.sort(key=lambda c: (c.due, c.category))
        return out

    def occurrences(self, series, start: date, end: date) -> list[date]:
        """Every date this series is expected to charge between two dates.

        One definition, because two callers needing "when does this bill" is
        exactly how the committed figures drifted apart before.
        """
        step_days = {"weekly": 7, "biweekly": 14}
        step_months = {"monthly": 1, "quarterly": 3, "yearly": 12}
        when = getattr(series, "next_expected", None)
        if when is None:
            return []
        cadence = getattr(series, "cadence", "")

        # The charges that already happened inside the window count too. The
        # next expected date is always after the latest charge, so counting
        # only forward from it lost a bill the moment it was paid: a budget
        # for this month stopped expecting the rent that left on the 1st, its
        # pace went back to a straight line for that line, and on the 14th
        # the month read as $400 behind for nothing but paying rent on time.
        # Suggestions for "This month" left the rent out altogether.
        found = self._charged_before(series, when, start, end)
        anchor = when
        # A prediction can sit in the past; roll it forward rather than
        # dropping it, exactly as the Upcoming screen does.
        for step in range(1, 401):  # a hard stop: never spin on a bad cadence
            if when > end:
                break
            if when >= start:
                found.append(when)
            if cadence in step_days:
                when = when + timedelta(days=step_days[cadence])
            elif cadence in step_months:
                # Counted from the first date every time, not from the last
                # one. Stepping from the last, a bill on the 30th was clamped
                # to the 28th by February and stayed there for good, so from
                # March on the budget's pace stepped two days before the
                # Upcoming screen said the charge would land.
                when = _add_months(anchor, step_months[cadence] * step)
            else:
                break  # an unknown cadence charges once, as far as we know
        return found

    def _charged_before(self, series, upcoming: date, start: date, end: date) -> list[date]:
        """When this series charged inside the window, before `upcoming`.

        A detected series has the real charges, so those dates are used as
        they are: a bill that landed on the 31st of last month belongs to last
        month, whatever its usual day. A tracked entry has none, so its dates
        are counted back from the next one, no further than when it started.
        """
        ids = getattr(series, "transaction_ids", None) or ()
        if ids:
            wanted = set(ids)
            return sorted(
                tx.date
                for tx in self.transactions
                if tx.id in wanted and start <= tx.date <= end and tx.date < upcoming
            )

        step_days = {"weekly": 7, "biweekly": 14}
        step_months = {"monthly": 1, "quarterly": 3, "yearly": 12}
        cadence = getattr(series, "cadence", "")
        began = getattr(series, "first_seen", None) or start
        earlier: list[date] = []
        for step in range(1, 401):
            if cadence in step_days:
                when = upcoming - timedelta(days=step_days[cadence] * step)
            elif cadence in step_months:
                when = _add_months(upcoming, -step_months[cadence] * step)
            else:
                break
            if when < start or when < began:
                break
            if when <= end:
                earlier.append(when)
        return sorted(earlier)

    def tracked_category(self, series) -> str:
        """What a manually tracked entry says it buys, or "" if it says nothing.

        Detection-backed series take their category from their own charges by
        majority vote. A tracked entry has no charges, so this is the only
        place it can come from.
        """
        wanted = (getattr(series, "merchant", "") or "").casefold()
        for entry in self.manual:
            if str(entry.get("merchant", "")).casefold() == wanted:
                return str(entry.get("category") or "").strip()
        return ""

    def committed_by_category(
        self, start: date | None = None, end: date | None = None
    ) -> dict[str, Money]:
        """What commitments cost, split by the category they land in.

        Given a window, a charge counts only if one actually falls inside it.
        The monthly-rate view answers "what do my commitments cost on average";
        it is the wrong answer to "what must this September hold", where a
        Costco membership that renews next September is nothing at all rather
        than a twelfth of itself. Reserving a slice of every annual bill in
        every month makes each month look poorer than it is, and the big hits
        are watched on the Upcoming screen where they can be seen coming.
        """
        if start is not None and end is not None:
            per_category: dict[str, int] = {}
            for commitment in self.commitment_schedule(start, end):
                owed = abs(commitment.amount.minor)
                per_category[commitment.category] = (
                    per_category.get(commitment.category, 0) + owed
                )
            return {name: Money(minor) for name, minor in per_category.items() if minor > 0}
        return self._committed_monthly_rate()

    def _committed_monthly_rate(self) -> dict[str, Money]:
        """What commitments cost per month, split by the category they land in.

        Needed so that "work backwards" does not budget for rent twice: the
        user's fixed-costs figure already covers it, so rent must be given its
        real allowance rather than a proportional share of what is left over.

        The category comes from the charges themselves by majority vote, not
        from the merchant name, so a mis-normalised name cannot move rent out
        of Housing.
        """
        per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
        # A series whose expected charge never arrived is not a commitment.
        # One quarterly subscription last charged in May 2025 and overdue ever
        # since was still being counted as $16 a month of fixed costs, which
        # is money the budget then refused to let the user spend on anything
        # else. Detection already knows these are stale; this had not asked.
        stale = {id(series) for series in self.stale_series}
        out: dict[str, int] = {}
        for series in self.series:
            if self.kind_of(series) not in budget_mod.COMMITTED_KINDS:
                continue
            if id(series) in stale:
                continue
            amount = self.current_amount(series)
            if amount.minor >= 0:
                continue

            # A tracked entry has no charges to vote, so it says for itself
            # what it buys. Without that every one of them landed in
            # Subscriptions, which is the same mistake the Subscriptions
            # *category* invites everywhere else: it describes how the money is
            # billed rather than what it is for, so a gym membership and a
            # Costco card both vanished into one label. Entries with nothing
            # set still fall back to Subscriptions, which is where someone
            # would look for an uncategorised recurring charge.
            category = self.series_category(series)
            monthly = abs(amount.minor) * per_year.get(series.cadence, 0) // 12
            out[category] = out.get(category, 0) + monthly
        return {name: Money(minor) for name, minor in out.items() if minor > 0}

    def commitments_in(
        self, category: str, start: date | None = None, end: date | None = None
    ) -> list[dict]:
        """The individual things making up a category's committed figure.

        A total is not an explanation. "$49.28 of Uncategorized is committed"
        invites exactly one question -- committed to *what* -- and until this
        existed the only way to answer it was to go and read the
        Subscriptions screen with the figure held in your head.
        """
        per_year = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
        stale = {id(series) for series in self.stale_series}

        found: list[dict] = []
        for series in self.series:
            if self.kind_of(series) not in budget_mod.COMMITTED_KINDS:
                continue
            if id(series) in stale:
                continue
            amount = self.current_amount(series)
            if amount.minor >= 0:
                continue
            landed = self.series_category(series)
            if landed != category:
                continue
            # With a window, only what actually bills inside it, and for the
            # amount it will actually bill. A yearly membership renewing next
            # September does not belong in this September's list, and a twelfth
            # of it is a figure that appears on no statement.
            if start is not None and end is not None:
                when = self.occurrences(series, start, end)
                if not when:
                    continue
                in_window = Money(abs(amount.minor) * len(when))
            else:
                in_window = Money(abs(amount.minor) * per_year.get(series.cadence, 0) // 12)
            found.append(
                {
                    "series": series,
                    "merchant": series.merchant,
                    "amount": abs(amount),
                    "cadence": series.cadence,
                    "kind": self.kind_of(series),
                    "monthly": in_window,
                    "next": series.next_expected,
                }
            )
        found.sort(key=lambda item: -item["monthly"].minor)
        return found

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

    @property
    def current_balances(self) -> dict[str, Money]:
        """What each account holds now, as far as this ledger can tell.

        The same as `balances` for every account a bank reports on. A cash
        account's reading is only as new as the last time somebody counted,
        and every spend typed since -- on this screen or from the phone -- is
        a movement the reading does not include. Net worth used the raw
        reading, so a $10 lunch logged the day after a count moved nothing:
        not the total, not the banner over the account, not the phone.

        Only cash is rolled forward. A synced reading arrives in the same
        response as the transactions it covers, so nothing can be newer than
        it -- except that the feed dates transactions in UTC and the reading
        is stamped with the local date, so an evening sync can file a charge
        under tomorrow. Rolling those forward would count them twice.
        """
        out = dict(self.balances)
        for account_id in out:
            if self.is_cash_account(account_id):
                rolled = self.implied_balance(account_id)
                if rolled is not None:
                    out[account_id] = rolled
        return out

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

    # -- categories the user cares about most -------------------------------

    #: Kept in settings rather than in `user_categories`, whose rows double as
    #: "a category the user added" -- favouriting a built-in one there would
    #: have made it appear twice in every list that offers categories.
    FAVOURITES_SETTING = "favourite_categories"

    @property
    def favourite_categories(self) -> set[str]:
        saved = self.setting(self.FAVOURITES_SETTING)
        return {str(name) for name in saved} if isinstance(saved, list) else set()

    def set_favourite_category(self, name: str, favourite: bool) -> None:
        favourites = self.favourite_categories
        if favourite:
            favourites.add(name)
        else:
            favourites.discard(name)
        self.save_setting(self.FAVOURITES_SETTING, sorted(favourites))

    def is_favourite(self, name: str) -> bool:
        return name in self.favourite_categories

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
        balances = {k: v for k, v in self.current_balances.items() if k not in excluded}
        if not balances:
            return []
        return networth_mod.reconstruct(accounts, transactions, balances, granularity=granularity)

    # -- money that has not landed yet -------------------------------------

    def networth_summary(self) -> dict | None:
        """Net worth today and once expected money lands, for the phone.

        None when there is no balance to work from, rather than zeros: a
        phone showing "$0.00 net worth" would be stating something false.
        """
        points = self.networth_points("daily")
        if not points:
            return None
        latest = points[-1]
        counted = self.counted_expected_money()
        arriving = sum(e.amount.minor for e in counted if e.amount.minor > 0)
        leaving = sum(-e.amount.minor for e in counted if e.amount.minor < 0)
        projected = latest.net.minor + arriving - leaving
        currency = latest.net.currency
        by_id = {a.id: a.name for a in self.accounts}
        return {
            "as_of": latest.date.isoformat(),
            "net": _wire(latest.net),
            "assets": _wire(latest.assets),
            "owed": _wire(latest.liabilities),
            "arriving": _wire(Money(arriving, currency)),
            "leaving": _wire(Money(leaving, currency)),
            "projected": _wire(Money(projected, currency)),
            "pending_items": len(counted),
            # Said on the phone as on the laptop, or a figure that leaves out a
            # retirement account reads as the whole picture.
            "not_counted": sorted(by_id.get(i, i) for i in self.excluded_accounts),
        }

    def expected_money(self) -> list:
        """Everything outstanding, soonest first."""
        return list(self.expected)

    def counted_expected_money(self) -> list:
        """The entries that belong in *this* net worth figure.

        An entry against an account the user has taken out of net worth is
        left out too. Counting it would move a total that account is not part
        of, which is the same mistake as subtracting an excluded balance
        afterwards instead of dropping it from the inputs.
        """
        excluded = self.excluded_accounts
        return [
            e for e in self.expected_money() if not (e.account_id and e.account_id in excluded)
        ]

    def expected_total(self) -> Money:
        """The net of what is coming and going but has not arrived."""
        counted = self.counted_expected_money()
        currency = counted[0].amount.currency if counted else "USD"
        return Money(sum(e.amount.minor for e in counted), currency)

    def add_expected_money(
        self,
        description: str,
        amount: Money,
        *,
        expected_on=None,
        account_id: str = "",
        note: str = "",
    ) -> str:
        conn = db.connect(self.path)
        entry_id = db.add_expected_money(
            conn,
            description,
            amount,
            expected_on=expected_on,
            account_id=account_id,
            note=note,
        )
        conn.close()
        self.load()
        return entry_id

    def update_expected_money(
        self,
        entry_id: str,
        description: str,
        amount: Money,
        *,
        expected_on=None,
        account_id: str = "",
        note: str = "",
    ) -> bool:
        conn = db.connect(self.path)
        changed = db.update_expected_money(
            conn,
            entry_id,
            description,
            amount,
            expected_on=expected_on,
            account_id=account_id,
            note=note,
        )
        conn.close()
        self.load()
        return bool(changed)

    def delete_expected_money(self, entry_id: str) -> bool:
        conn = db.connect(self.path)
        removed = db.delete_expected_money(conn, entry_id)
        conn.close()
        self.load()
        return bool(removed)

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
        charged_since = (
            kind == subscriptions.CANCELLED
            and when is not None
            and series.last_seen > when
            # Only a series with real charges behind it. A tracked entry the
            # user typed has no observations, so `as_series` fills last_seen
            # with its start date -- or, when there is none, with *today*,
            # recomputed on every load. Against a decision made yesterday
            # that date is always newer, so a cancelled entry with no start
            # date came back to the unclassified pile every single day, and
            # answering it again changed nothing. Five of them had been
            # answered at least three times.
            and not subscriptions.is_manual(series)
        )
        return subscriptions.UNKNOWN if charged_since else kind

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


def _add_months(when: date, months: int) -> date:
    """Same day-of-month, `months` later, clamped to the month's length.

    The 31st plus one month is the 28th, 29th or 30th depending on where you
    land; naive arithmetic raises instead, and a bill that falls on the 31st is
    not a rare thing.
    """
    import calendar

    month_index = when.month - 1 + months
    year = when.year + month_index // 12
    month = month_index % 12 + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

