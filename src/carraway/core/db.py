"""SQLite storage.

Everything lives in one local file, which is the whole privacy pitch: there is
no server, no account, and nothing leaves the machine unless the user
explicitly configures a sync provider.

Schema changes go through `MIGRATIONS`, applied in order and tracked with
SQLite's built-in `user_version`. It is a deliberately boring mechanism, but it
means an old database always knows how to catch up.
"""

from __future__ import annotations

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


def update_categories(conn: sqlite3.Connection, assignments: list[tuple[str, str]]) -> int:
    """Persist `(transaction_id, category)` pairs. Returns rows changed.

    Only writes rows whose category actually differs, so re-running
    categorisation over an unchanged ledger reports honestly that it changed
    nothing rather than claiming every row as an update.
    """
    changed = 0
    for tx_id, category in assignments:
        cur = conn.execute(
            "UPDATE transactions SET category = ? WHERE id = ? AND category IS NOT ?",
            (category, tx_id, category),
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
