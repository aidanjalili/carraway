"""What every sync provider has to do.

Kept deliberately small. A provider's whole job is to hand back accounts and
transactions in Carraway's own shapes; everything after that — deduplication,
categorisation, recurring detection — is the same code that file imports go
through, so a synced ledger and an imported one behave identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from ..core.models import Account, Transaction


@dataclass(slots=True)
class SyncResult:
    """What one sync run produced, before anything is written to the database."""

    accounts: list[Account] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    # Provider-reported problems, e.g. one bank connection needing
    # reauthorisation while the rest succeeded. Never fatal on their own: a
    # partial sync is more useful than an aborted one.
    warnings: list[str] = field(default_factory=list)


class Provider(Protocol):
    """A source of accounts and transactions that is not a file."""

    name: str

    def fetch(self, *, since: date | None = None) -> SyncResult:
        """Retrieve accounts and transactions, optionally limited by date."""
        ...
