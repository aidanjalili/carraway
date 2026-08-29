"""The domain objects Carraway reasons about.

These are deliberately plain dataclasses with no database or UI awareness, so
the analysis code can be tested against hand-built objects with no I/O.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from .money import Money


class AccountType(StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    INVESTMENT = "investment"
    LOAN = "loan"
    CASH = "cash"

    @property
    def is_liability(self) -> bool:
        """True when a positive balance represents money owed, not money held."""
        return self in (AccountType.CREDIT_CARD, AccountType.LOAN)


@dataclass(slots=True)
class Account:
    """A single account at a single institution."""

    id: str
    name: str
    type: AccountType
    institution: str = ""
    currency: str = "USD"
    # Set by a sync provider (SimpleFIN etc.); empty for file-imported accounts.
    external_id: str = ""
    closed: bool = False


@dataclass(slots=True)
class Transaction:
    """One movement of money.

    Sign convention, applied consistently everywhere: **negative is money
    leaving you.** A $12.99 Netflix charge is -1299 minor units on both a
    checking account and a credit card. Card issuers usually export the
    opposite sign, so importers are responsible for normalising to this rule.
    """

    id: str
    account_id: str
    date: date
    amount: Money
    description: str
    # Cleaned-up merchant, e.g. "SQ *BLUE BOTTLE #402 SF" -> "Blue Bottle".
    merchant: str = ""
    category: str = ""
    notes: str = ""
    pending: bool = False
    # Marks the two halves of a transfer between your own accounts, so they can
    # be excluded from spending totals rather than double-counted.
    transfer_group: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def is_outflow(self) -> bool:
        return self.amount.minor < 0

    @property
    def is_transfer(self) -> bool:
        return bool(self.transfer_group)

    @property
    def signature(self) -> str:
        """Stable hash of the fields that identify a transaction.

        Used to recognise a row we have already imported. Banks rarely give a
        durable ID in CSV exports, so we fingerprint the immutable facts
        instead. Deliberately excludes category and notes, which the user
        edits after import and which must not create a duplicate on re-import.
        """
        raw = "|".join(
            [
                self.account_id,
                self.date.isoformat(),
                str(self.amount.minor),
                self.amount.currency,
                " ".join(self.description.split()).upper(),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class RecurringSeries:
    """A detected repeating charge, e.g. a subscription or a utility bill.

    This is the output of `carraway.analysis.recurring` and the feature the whole
    project is really built around.
    """

    merchant: str
    account_id: str
    cadence: str  # "weekly" | "biweekly" | "monthly" | "quarterly" | "yearly"
    typical_amount: Money
    occurrences: int
    first_seen: date
    last_seen: date
    next_expected: date | None
    confidence: float  # 0.0-1.0, how regular the series looks
    amount_varies: bool  # True for usage-based bills like electricity
    transaction_ids: list[str] = field(default_factory=list)

    @property
    def annualised(self) -> Money:
        """Roughly what this series costs over a year, for a 'you spend X/yr' view."""
        per_year = {
            "weekly": 52,
            "biweekly": 26,
            "monthly": 12,
            "quarterly": 4,
            "yearly": 1,
        }.get(self.cadence, 0)
        return abs(self.typical_amount) * per_year
