"""SQLite storage.

Everything lives in one local file, which is the whole privacy pitch: there is
no server, no account, and nothing leaves the machine unless the user
explicitly configures a sync provider.

Schema changes go through `MIGRATIONS`, applied in order and tracked with
SQLite's built-in `user_version`. It is a deliberately boring mechanism, but it
means an old database always knows how to catch up.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from .models import Account, AccountType, Transaction
from .money import Money

MIGRATIONS: list[str] = [
    # v1 - accounts and transactions
    """
    CREATE TABLE accounts (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        type         TEXT NOT NULL,
        institution  TEXT NOT NULL DEFAULT '',
        currency     TEXT NOT NULL DEFAULT 'USD',
        external_id  TEXT NOT NULL DEFAULT '',
        closed       INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE transactions (
        id             TEXT PRIMARY KEY,
        account_id     TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        date           TEXT NOT NULL,          -- ISO-8601, sorts lexicographically
        amount_minor   INTEGER NOT NULL,       -- never a float; see core.money
        currency       TEXT NOT NULL DEFAULT 'USD',
        description    TEXT NOT NULL,
        merchant       TEXT NOT NULL DEFAULT '',
        category       TEXT NOT NULL DEFAULT '',
        notes          TEXT NOT NULL DEFAULT '',
        pending        INTEGER NOT NULL DEFAULT 0,
        transfer_group TEXT NOT NULL DEFAULT '',
        signature      TEXT NOT NULL           -- dedupe key; see Transaction.signature
    );

    CREATE INDEX idx_tx_account_date ON transactions(account_id, date);
    CREATE INDEX idx_tx_merchant     ON transactions(merchant);
    -- Enforces import idempotency: re-importing the same file is a no-op.
    CREATE UNIQUE INDEX idx_tx_signature ON transactions(account_id, signature);
    """,
    # v2 - tell apart real purchases that agree on every other field.
    # Two coffees at one shop for one price on one day used to collapse into a
    # single row on import. See Transaction.occurrence and assign_occurrences.
    """
    ALTER TABLE transactions ADD COLUMN occurrence INTEGER NOT NULL DEFAULT 0;
    """,
    # v3 - the user's own answer about what a merchant is. Asked once, in the
    # review flow, then never again. See analysis/subscriptions.py.
    """
    CREATE TABLE merchant_verdicts (
        merchant   TEXT PRIMARY KEY,   -- normalised merchant, uppercased
        kind       TEXT NOT NULL,      -- subscription | bill | habit
        decided_at TEXT NOT NULL
    );
    """,
    # v4 - observed balances. A provider reports only today's figure, so each
    # observation is kept: net worth history is reconstructed by walking
    # transactions back from a known balance, and that needs an anchor. Keeping
    # every observation also means a later sync corroborates the reconstruction
    # rather than replacing it.
    """
    CREATE TABLE balances (
        account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        observed_on  TEXT NOT NULL,      -- ISO-8601 date the provider reported
        amount_minor INTEGER NOT NULL,   -- never a float; see core.money
        currency     TEXT NOT NULL DEFAULT 'USD',
        PRIMARY KEY (account_id, observed_on)
    );

    CREATE INDEX idx_balances_account ON balances(account_id, observed_on);
    """,
    # v5 - subscriptions the user knows about but no detector can find.
    # Anything paid through another app or a family member reaches the
    # statement as the transfer, never as the merchant, so the service is
    # structurally invisible. Comparing a real user's own list against
    # detection showed this accounted for most of what was missing.
    """
    CREATE TABLE manual_subscriptions (
        id          TEXT PRIMARY KEY,
        merchant    TEXT NOT NULL,
        amount_minor INTEGER NOT NULL,   -- never a float; see core.money
        currency    TEXT NOT NULL DEFAULT 'USD',
        cadence     TEXT NOT NULL,       -- weekly | biweekly | monthly | quarterly | yearly
        kind        TEXT NOT NULL DEFAULT 'subscription',
        paid_via    TEXT NOT NULL DEFAULT '',  -- how it is paid; free text
        notes       TEXT NOT NULL DEFAULT '',
        active      INTEGER NOT NULL DEFAULT 1
    );
    """,
    # v6 - money to-dos. Disputes, reimbursements owed, accounts to close: the
    # things a ledger makes obvious but cannot act on. They live here rather
    # than in a notes app because they are answers to questions the data
    # raised, and they go stale the moment they are separated from it.
    """
    CREATE TABLE todos (
        id         TEXT PRIMARY KEY,
        task       TEXT NOT NULL,
        amount_minor INTEGER,            -- nullable: not every task has a figure
        currency   TEXT NOT NULL DEFAULT 'USD',
        source     TEXT NOT NULL DEFAULT '',  -- where it came from
        added_on   TEXT NOT NULL,
        done_on    TEXT
    );
    """,
    # v7 - user preferences. Kept in the database rather than a config file
    # because they are about this ledger — which accounts to leave out of net
    # worth, say — and would be meaningless beside a different one.
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL      -- JSON, so a setting can hold a list
    );
    """,
    # v8 - which categories were guessed rather than matched by a rule. A
    # guess that cannot be told apart from a certainty is worse than no guess,
    # so the flag travels with the row and the UI marks it.
    """
    ALTER TABLE transactions ADD COLUMN auto_categorized INTEGER NOT NULL DEFAULT 0;
    """,
    # v9 - corrections to a detected series. Detection infers an amount, a
    # cadence and a next date from history, and history is sometimes a poor
    # guide: a price rose last week, or the billing day moved. Every field is
    # nullable, so a correction to one leaves the rest inferred and keeps
    # improving as more charges arrive.
    """
    CREATE TABLE series_overrides (
        merchant      TEXT PRIMARY KEY,   -- uppercased, as verdicts are keyed
        display_name  TEXT,
        amount_minor  INTEGER,
        currency      TEXT,
        cadence       TEXT,
        next_expected TEXT,               -- ISO-8601
        note          TEXT,
        updated_on    TEXT NOT NULL
    );
    """,
    # v10 - the user's own categorisation rules, and their own categories. A
    # shipped list can never cover a local business or somebody's idea of what
    # a category is, and both were previously only editable in source.
    """
    CREATE TABLE user_rules (
        id       TEXT PRIMARY KEY,
        pattern  TEXT NOT NULL,          -- matched against the description
        category TEXT NOT NULL,
        added_on TEXT NOT NULL
    );

    CREATE TABLE user_categories (
        name    TEXT PRIMARY KEY,
        hidden  INTEGER NOT NULL DEFAULT 0   -- a built-in the user turned off
    );
    """,
    # v11 - when a tracked subscription first billed, so the app can work out
    # when the next charge is due. Without it a manual entry has a cadence but
    # no anchor, and cannot appear in Upcoming at all.
    """
    ALTER TABLE manual_subscriptions ADD COLUMN started_on TEXT;
    """,
    # v12 - which account pays for a tracked subscription. Distinct from the
    # free-text `paid_via`, which holds the things no account can represent
    # ("venmo to dad", "my mother's card"). A linked account is a reference
    # rather than a description: it survives the account being renamed, and
    # lets the subscription show the same account name every other screen
    # uses. Nullable, since neither field is required and most entries have
    # only one of the two.
    """
    ALTER TABLE manual_subscriptions ADD COLUMN paid_via_account TEXT;
    """,
    # v13 - budgets the user sets for a stretch of days, and the per-category
    # envelopes inside them. Separate from `analysis.budget`, which derives a
    # repeating allowance from a net-worth goal and stores nothing: this is a
    # thing you name, come back to next week, and check yourself against.
    #
    # `accounts` is a comma-separated list of account ids, empty meaning all
    # of them. A join table would be tidier, but this is a short scope list
    # read only alongside its budget and never queried across, so a table
    # would be structure without a use for it.
    """
    CREATE TABLE budgets (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        starts_on  TEXT NOT NULL,
        ends_on    TEXT NOT NULL,          -- inclusive
        accounts   TEXT NOT NULL DEFAULT '',
        currency   TEXT NOT NULL DEFAULT 'USD',
        -- The reasoning behind the numbers, when the user worked backwards
        -- from what they earn. Null when they simply typed the figures.
        income_minor  INTEGER,
        saving_minor  INTEGER,
        fixed_minor   INTEGER,
        created_on TEXT NOT NULL
    );

    CREATE TABLE budget_envelopes (
        budget_id  TEXT NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
        category   TEXT NOT NULL,
        amount_minor INTEGER NOT NULL,     -- a magnitude; see analysis.budgets
        PRIMARY KEY (budget_id, category)
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) a Carraway database and bring it up to date."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL keeps reads from blocking writes, which matters once a GUI is
    # refreshing charts while an import is running.
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any migrations the database has not seen. Returns the new version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index in range(version, len(MIGRATIONS)):
        conn.executescript(MIGRATIONS[index])
        # PRAGMA will not accept a bound parameter, and the value is a loop
        # index rather than user input, so interpolation is safe here.
        conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.commit()
    return len(MIGRATIONS)


def default_db_path() -> Path:
    """Follow the XDG spec so the file lands where a Linux user expects it."""
    import os

    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "carraway" / "carraway.db"


# -- accounts ------------------------------------------------------------


def upsert_account(conn: sqlite3.Connection, account: Account) -> None:
    conn.execute(
        """
        INSERT INTO accounts (id, name, type, institution, currency, external_id, closed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name        = excluded.name,
            type        = excluded.type,
            institution = excluded.institution,
            currency    = excluded.currency,
            external_id = excluded.external_id,
            closed      = excluded.closed
        """,
        (
            account.id,
            account.name,
            str(account.type),
            account.institution,
            account.currency,
            account.external_id,
            int(account.closed),
        ),
    )
    conn.commit()


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    rows = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
    return [
        Account(
            id=r["id"],
            name=r["name"],
            type=AccountType(r["type"]),
            institution=r["institution"],
            currency=r["currency"],
            external_id=r["external_id"],
            closed=bool(r["closed"]),
        )
        for r in rows
    ]


# -- transactions --------------------------------------------------------


def insert_transactions(
    conn: sqlite3.Connection, transactions: list[Transaction]
) -> tuple[int, int]:
    """Insert transactions, skipping ones already present.

    Returns `(inserted, skipped)`. Relies on the unique index over
    (account_id, signature) so that re-importing an overlapping statement
    cannot create duplicates.
    """
    inserted = 0
    for tx in transactions:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO transactions
                (id, account_id, date, amount_minor, currency, description,
                 merchant, category, notes, pending, transfer_group, occurrence, signature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tx.id,
                tx.account_id,
                tx.date.isoformat(),
                tx.amount.minor,
                tx.amount.currency,
                tx.description,
                tx.merchant,
                tx.category,
                tx.notes,
                int(tx.pending),
                tx.transfer_group,
                tx.occurrence,
                tx.signature,
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted, len(transactions) - inserted


def _row_to_transaction(r: sqlite3.Row) -> Transaction:
    return Transaction(
        id=r["id"],
        account_id=r["account_id"],
        date=date.fromisoformat(r["date"]),
        amount=Money(r["amount_minor"], r["currency"]),
        description=r["description"],
        merchant=r["merchant"],
        category=r["category"],
        notes=r["notes"],
        pending=bool(r["pending"]),
        transfer_group=r["transfer_group"],
        occurrence=r["occurrence"],
        auto_categorized=bool(r["auto_categorized"]),
    )


# -- what the user has told us a merchant is -----------------------------


def set_verdict(conn: sqlite3.Connection, merchant: str, kind: str) -> None:
    """Record the user's answer about a merchant, replacing any earlier one."""
    conn.execute(
        """
        INSERT INTO merchant_verdicts (merchant, kind, decided_at)
        VALUES (?, ?, ?)
        ON CONFLICT(merchant) DO UPDATE SET
            kind = excluded.kind,
            decided_at = excluded.decided_at
        """,
        (merchant.upper(), kind, date.today().isoformat()),
    )
    conn.commit()


def get_verdicts(conn: sqlite3.Connection) -> dict[str, str]:
    """Every stored answer, keyed by uppercased merchant."""
    return {
        r["merchant"]: r["kind"]
        for r in conn.execute("SELECT merchant, kind FROM merchant_verdicts")
    }


def get_verdict_dates(conn: sqlite3.Connection) -> dict[str, date]:
    """When each answer was given, keyed by uppercased merchant.

    Used to notice that a merchant marked cancelled has charged again since,
    which means the answer is out of date rather than wrong.
    """
    return {
        r["merchant"]: date.fromisoformat(r["decided_at"])
        for r in conn.execute("SELECT merchant, decided_at FROM merchant_verdicts")
    }


def clear_verdict(conn: sqlite3.Connection, merchant: str) -> int:
    """Forget one answer, so the review flow asks about it again."""
    cur = conn.execute("DELETE FROM merchant_verdicts WHERE merchant = ?", (merchant.upper(),))
    conn.commit()
    return cur.rowcount


# -- balances ------------------------------------------------------------


def record_balance(
    conn: sqlite3.Connection, account_id: str, amount: Money, observed_on: date | None = None
) -> None:
    """Store a balance observation, replacing any for the same day.

    Same-day replacement rather than accumulation: two syncs on one day are two
    readings of the same fact, and the later one is better.
    """
    conn.execute(
        """
        INSERT INTO balances (account_id, observed_on, amount_minor, currency)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(account_id, observed_on) DO UPDATE SET
            amount_minor = excluded.amount_minor,
            currency = excluded.currency
        """,
        (account_id, (observed_on or date.today()).isoformat(), amount.minor, amount.currency),
    )
    conn.commit()


def latest_balances(conn: sqlite3.Connection) -> dict[str, Money]:
    """The most recent balance seen for each account."""
    rows = conn.execute(
        """
        SELECT b.account_id, b.amount_minor, b.currency
        FROM balances b
        JOIN (
            SELECT account_id, MAX(observed_on) AS newest
            FROM balances GROUP BY account_id
        ) latest ON latest.account_id = b.account_id AND latest.newest = b.observed_on
        """
    ).fetchall()
    return {r["account_id"]: Money(r["amount_minor"], r["currency"]) for r in rows}


def latest_balance_dates(conn: sqlite3.Connection) -> dict[str, date]:
    """When each account's most recent balance was observed.

    The amount alone is not enough to say what an account holds *now*: a
    reading from July plus the transactions since is a different figure from
    the reading itself. Anything reconciling against a balance needs to know
    how old it is.
    """
    rows = conn.execute(
        """
        SELECT account_id, MAX(observed_on) AS observed_on
        FROM balances GROUP BY account_id
        """
    ).fetchall()
    return {r["account_id"]: date.fromisoformat(r["observed_on"]) for r in rows}


def balance_history(conn: sqlite3.Connection, account_id: str) -> list[tuple[date, Money]]:
    """Every balance observed for one account, oldest first."""
    rows = conn.execute(
        "SELECT observed_on, amount_minor, currency FROM balances "
        "WHERE account_id = ? ORDER BY observed_on",
        (account_id,),
    ).fetchall()
    return [
        (date.fromisoformat(r["observed_on"]), Money(r["amount_minor"], r["currency"]))
        for r in rows
    ]


# -- subscriptions the user tells us about --------------------------------


def add_manual_subscription(
    conn: sqlite3.Connection,
    merchant: str,
    amount: Money,
    cadence: str,
    *,
    kind: str = "subscription",
    paid_via: str = "",
    paid_via_account: str | None = None,
    notes: str = "",
    started_on: date | None = None,
) -> str:
    """Record a subscription detection cannot see. Returns its id.

    `paid_via_account` links the entry to an account in this ledger;
    `paid_via` describes a payment route that has no account here. They are
    alternatives, not a pair, and passing neither is normal.
    """
    import uuid

    subscription_id = uuid.uuid4().hex[:12]
    conn.execute(
        """
        INSERT INTO manual_subscriptions
            (id, merchant, amount_minor, currency, cadence, kind, paid_via, notes,
             active, started_on, paid_via_account)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            subscription_id,
            merchant,
            -abs(amount.minor),  # stored as an outflow, matching every other amount
            amount.currency,
            cadence,
            kind,
            paid_via,
            notes,
            started_on.isoformat() if started_on else None,
            paid_via_account or None,
        ),
    )
    conn.commit()
    return subscription_id


def list_manual_subscriptions(
    conn: sqlite3.Connection, *, active_only: bool = True
) -> list[dict[str, object]]:
    where = "WHERE active = 1" if active_only else ""
    rows = conn.execute(f"SELECT * FROM manual_subscriptions {where} ORDER BY merchant").fetchall()
    return [
        {
            "id": r["id"],
            "merchant": r["merchant"],
            "amount": Money(r["amount_minor"], r["currency"]),
            "cadence": r["cadence"],
            "kind": r["kind"],
            "paid_via": r["paid_via"],
            "paid_via_account": r["paid_via_account"] or "",
            "notes": r["notes"],
            "active": bool(r["active"]),
            "started_on": (date.fromisoformat(r["started_on"]) if r["started_on"] else None),
        }
        for r in rows
    ]


def remove_manual_subscription(conn: sqlite3.Connection, subscription_id: str) -> int:
    """Deactivate rather than delete.

    A cancelled subscription is still worth remembering — the user may have
    paid for it for years, and that history belongs in their totals. Use
    `delete_manual_subscription` for an entry that was simply a mistake.
    """
    cur = conn.execute(
        "UPDATE manual_subscriptions SET active = 0 WHERE id = ?", (subscription_id,)
    )
    conn.commit()
    return cur.rowcount


def set_manual_paid_via(
    conn: sqlite3.Connection,
    subscription_id: str,
    *,
    paid_via: str = "",
    paid_via_account: str | None = None,
) -> int:
    """Record how a tracked subscription is paid for. Returns rows changed.

    Both fields are written every time, so choosing an account clears a stale
    free-text note and vice versa — leaving the old one behind would show two
    contradictory answers to one question.
    """
    cur = conn.execute(
        "UPDATE manual_subscriptions SET paid_via = ?, paid_via_account = ? WHERE id = ?",
        (paid_via, paid_via_account or None, subscription_id),
    )
    conn.commit()
    return cur.rowcount


def delete_manual_subscription(conn: sqlite3.Connection, subscription_id: str) -> int:
    """Remove a tracked entry outright.

    For one added by mistake, where deactivating would leave a wrong row on
    the record forever. Nothing else references these, so there is nothing to
    orphan.
    """
    cur = conn.execute("DELETE FROM manual_subscriptions WHERE id = ?", (subscription_id,))
    conn.commit()
    return cur.rowcount


# -- budgets --------------------------------------------------------------


def save_budget(conn: sqlite3.Connection, budget) -> str:
    """Insert or replace a budget and its envelopes. Returns its id.

    Envelopes are deleted and rewritten rather than diffed: the whole set is
    always supplied together, and a leftover line from a previous version
    would silently keep counting against the user.
    """
    conn.execute(
        """
        INSERT INTO budgets
            (id, name, starts_on, ends_on, accounts, currency,
             income_minor, saving_minor, fixed_minor, created_on)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            starts_on = excluded.starts_on,
            ends_on = excluded.ends_on,
            accounts = excluded.accounts,
            currency = excluded.currency,
            income_minor = excluded.income_minor,
            saving_minor = excluded.saving_minor,
            fixed_minor = excluded.fixed_minor
        """,
        (
            budget.id,
            budget.name,
            budget.starts_on.isoformat(),
            budget.ends_on.isoformat(),
            ",".join(budget.accounts),
            budget.total.currency,
            budget.expected_income.minor if budget.expected_income else None,
            budget.savings_target.minor if budget.savings_target else None,
            budget.fixed_costs.minor if budget.fixed_costs else None,
            date.today().isoformat(),
        ),
    )
    conn.execute("DELETE FROM budget_envelopes WHERE budget_id = ?", (budget.id,))
    conn.executemany(
        "INSERT INTO budget_envelopes (budget_id, category, amount_minor) VALUES (?, ?, ?)",
        [(budget.id, e.category, abs(e.allowance.minor)) for e in budget.envelopes],
    )
    conn.commit()
    return budget.id


def list_budgets(conn: sqlite3.Connection) -> list:
    """Every saved budget, newest window first."""
    from ..analysis.budgets import Budget, Envelope

    rows = conn.execute("SELECT * FROM budgets ORDER BY starts_on DESC, name").fetchall()
    out = []
    for r in rows:
        envelopes = conn.execute(
            "SELECT category, amount_minor FROM budget_envelopes "
            "WHERE budget_id = ? ORDER BY amount_minor DESC, category",
            (r["id"],),
        ).fetchall()
        out.append(
            Budget(
                id=r["id"],
                name=r["name"],
                starts_on=date.fromisoformat(r["starts_on"]),
                ends_on=date.fromisoformat(r["ends_on"]),
                envelopes=tuple(
                    Envelope(e["category"], Money(e["amount_minor"], r["currency"]))
                    for e in envelopes
                ),
                accounts=tuple(a for a in r["accounts"].split(",") if a),
                expected_income=(
                    Money(r["income_minor"], r["currency"])
                    if r["income_minor"] is not None
                    else None
                ),
                savings_target=(
                    Money(r["saving_minor"], r["currency"])
                    if r["saving_minor"] is not None
                    else None
                ),
                fixed_costs=(
                    Money(r["fixed_minor"], r["currency"]) if r["fixed_minor"] is not None else None
                ),
            )
        )
    return out


def delete_budget(conn: sqlite3.Connection, budget_id: str) -> int:
    """Remove a budget and its envelopes. Returns rows removed."""
    # The cascade needs foreign keys on, which `connect` enables; deleting the
    # envelopes explicitly means this is right either way.
    conn.execute("DELETE FROM budget_envelopes WHERE budget_id = ?", (budget_id,))
    cur = conn.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
    conn.commit()
    return cur.rowcount


# -- money to-dos ---------------------------------------------------------


# -- settings ------------------------------------------------------------

# Defaults live here rather than being scattered through the UI, so a setting
# has one answer whether it has ever been written or not.
DEFAULT_SETTINGS: dict[str, object] = {
    # Account ids left out of net worth. Retirement and brokerage accounts are
    # the usual case: money you have but cannot spend, which makes the total
    # answer a different question from "how am I doing this month".
    "networth_excluded_accounts": [],
    # Off by default: a wrong category the user did not ask for is worse
    # than an honest "Uncategorized".
    "auto_categorize": False,
    # Whether guessed categories shape the breakdowns, or only the rows the
    # rules matched. On by default so turning guessing on has an effect,
    # but separable because "what am I sure about" is a real question.
    "include_guesses_in_totals": True,
    "networth_granularity": "monthly",
    "spending_granularity": "monthly",
    "spending_chart": "Pie",
    "budget_target": "5000",
    "budget_months": 6,
    "budget_period": "monthly",
    "theme": "system",
}


def get_setting(conn: sqlite3.Connection, key: str) -> object:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return DEFAULT_SETTINGS.get(key)


def set_setting(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def all_settings(conn: sqlite3.Connection) -> dict[str, object]:
    """Every setting, with defaults filled in for anything never written."""
    stored = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            stored[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            continue
    return stored


# -- corrections to a detected series -------------------------------------


def set_series_override(conn: sqlite3.Connection, merchant: str, **fields: object) -> None:
    """Record corrections for one merchant, merging with anything already set.

    Passing None for a field clears that correction and lets detection infer
    it again, which is how a user undoes a single edit without undoing all of
    them.
    """
    existing = get_series_overrides(conn).get(merchant.upper(), {})
    merged = {**existing, **fields}
    conn.execute(
        """
        INSERT INTO series_overrides
            (merchant, display_name, amount_minor, currency, cadence,
             next_expected, note, updated_on)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(merchant) DO UPDATE SET
            display_name  = excluded.display_name,
            amount_minor  = excluded.amount_minor,
            currency      = excluded.currency,
            cadence       = excluded.cadence,
            next_expected = excluded.next_expected,
            note          = excluded.note,
            updated_on    = excluded.updated_on
        """,
        (
            merchant.upper(),
            merged.get("display_name"),
            merged.get("amount_minor"),
            merged.get("currency"),
            merged.get("cadence"),
            merged.get("next_expected"),
            merged.get("note"),
            date.today().isoformat(),
        ),
    )
    conn.commit()


def get_series_overrides(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Every correction, keyed by uppercased merchant."""
    out: dict[str, dict[str, object]] = {}
    for row in conn.execute("SELECT * FROM series_overrides"):
        out[row["merchant"]] = {
            "display_name": row["display_name"],
            "amount_minor": row["amount_minor"],
            "currency": row["currency"],
            "cadence": row["cadence"],
            "next_expected": row["next_expected"],
            "note": row["note"],
        }
    return out


def clear_series_override(conn: sqlite3.Connection, merchant: str) -> int:
    """Drop every correction for a merchant, returning it to what was detected."""
    cur = conn.execute("DELETE FROM series_overrides WHERE merchant = ?", (merchant.upper(),))
    conn.commit()
    return cur.rowcount


# -- the user's own rules and categories ----------------------------------


def add_user_rule(conn: sqlite3.Connection, pattern: str, category: str) -> str:
    """Store a rule, returning the id. An identical one is never stored twice.

    Two rules with the same pattern and category behave as one, so a duplicate
    is only ever clutter in the list — and a double-fired button click should
    not produce it.
    """
    import uuid

    cleaned = pattern.strip()
    existing = conn.execute(
        "SELECT id FROM user_rules WHERE pattern = ? AND category = ?",
        (cleaned, category),
    ).fetchone()
    if existing:
        return str(existing["id"])

    rule_id = uuid.uuid4().hex[:8]
    conn.execute(
        "INSERT INTO user_rules (id, pattern, category, added_on) VALUES (?, ?, ?, ?)",
        (rule_id, cleaned, category, date.today().isoformat()),
    )
    conn.commit()
    return rule_id


def list_user_rules(conn: sqlite3.Connection) -> list[dict[str, str]]:
    return [
        {"id": r["id"], "pattern": r["pattern"], "category": r["category"]}
        for r in conn.execute("SELECT * FROM user_rules ORDER BY added_on, id")
    ]


def remove_user_rule(conn: sqlite3.Connection, rule_id: str) -> int:
    cur = conn.execute("DELETE FROM user_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount


def add_user_category(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO user_categories (name, hidden) VALUES (?, 0) "
        "ON CONFLICT(name) DO UPDATE SET hidden = 0",
        (name.strip(),),
    )
    conn.commit()


def hide_category(conn: sqlite3.Connection, name: str, hidden: bool = True) -> None:
    """Hide a category rather than delete it.

    Transactions may already be filed under it, and deleting the name would
    orphan them. Hidden ones stop being offered for new work.
    """
    conn.execute(
        "INSERT INTO user_categories (name, hidden) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET hidden = excluded.hidden",
        (name.strip(), int(hidden)),
    )
    conn.commit()


def category_settings(conn: sqlite3.Connection) -> tuple[list[str], set[str]]:
    """(categories the user added, names they have hidden)."""
    added: list[str] = []
    hidden: set[str] = set()
    for row in conn.execute("SELECT name, hidden FROM user_categories"):
        if row["hidden"]:
            hidden.add(row["name"])
        else:
            added.append(row["name"])
    return added, hidden


def add_todo(
    conn: sqlite3.Connection,
    task: str,
    *,
    amount: Money | None = None,
    source: str = "",
) -> str:
    import uuid

    todo_id = uuid.uuid4().hex[:8]
    conn.execute(
        "INSERT INTO todos (id, task, amount_minor, currency, source, added_on) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            todo_id,
            task,
            amount.minor if amount else None,
            amount.currency if amount else "USD",
            source,
            date.today().isoformat(),
        ),
    )
    conn.commit()
    return todo_id


def list_todos(conn: sqlite3.Connection, *, include_done: bool = False) -> list[dict[str, object]]:
    where = "" if include_done else "WHERE done_on IS NULL"
    rows = conn.execute(f"SELECT * FROM todos {where} ORDER BY added_on, id").fetchall()
    return [
        {
            "id": r["id"],
            "task": r["task"],
            "amount": Money(r["amount_minor"], r["currency"]) if r["amount_minor"] else None,
            "source": r["source"],
            "added_on": date.fromisoformat(r["added_on"]),
            "done_on": date.fromisoformat(r["done_on"]) if r["done_on"] else None,
        }
        for r in rows
    ]


def complete_todo(conn: sqlite3.Connection, todo_id: str) -> int:
    """Mark done rather than delete, so a finished dispute stays on the record."""
    cur = conn.execute(
        "UPDATE todos SET done_on = ? WHERE id = ? AND done_on IS NULL",
        (date.today().isoformat(), todo_id),
    )
    conn.commit()
    return cur.rowcount


def delete_transactions(conn: sqlite3.Connection, ids: list[str]) -> int:
    """Remove transactions by id. Returns how many rows went.

    Only used by duplicate removal, which shows the user exactly what it will
    delete first: nothing in Carraway deletes a transaction on its own.
    """
    removed = 0
    for tx_id in ids:
        removed += conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,)).rowcount
    conn.commit()
    return removed


def update_categories(
    conn: sqlite3.Connection,
    assignments: list[tuple[str, str]],
    *,
    guessed: set[str] | None = None,
) -> int:
    """Persist `(transaction_id, category)` pairs. Returns rows changed.

    Only writes rows whose category actually differs, so re-running
    categorisation over an unchanged ledger reports honestly that it changed
    nothing rather than claiming every row as an update.
    """
    changed = 0
    for tx_id, category in assignments:
        cur = conn.execute(
            "UPDATE transactions SET category = ?, auto_categorized = ? "
            "WHERE id = ? AND (category IS NOT ? OR auto_categorized IS NOT ?)",
            (
                category,
                int(bool(guessed and tx_id in guessed)),
                tx_id,
                category,
                int(bool(guessed and tx_id in guessed)),
            ),
        )
        changed += cur.rowcount
    conn.commit()
    return changed


def update_transfer_groups(conn: sqlite3.Connection, transactions: list[Transaction]) -> int:
    """Write each transaction's in-memory `transfer_group` back to the database.

    Takes the objects rather than ids because `apply_transfer_groups` stamps
    them in place, so the caller already holds the answer.
    """
    changed = 0
    for tx in transactions:
        if not tx.transfer_group:
            continue
        cur = conn.execute(
            "UPDATE transactions SET transfer_group = ? WHERE id = ? AND transfer_group = ''",
            (tx.transfer_group, tx.id),
        )
        changed += cur.rowcount
    conn.commit()
    return changed


def list_transactions(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    since: date | None = None,
    limit: int | None = None,
) -> list[Transaction]:
    clauses, params = [], []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if since:
        clauses.append("date >= ?")
        params.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM transactions {where} ORDER BY date DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_transaction(r) for r in conn.execute(sql, params).fetchall()]
