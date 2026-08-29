"""Work out what an account actually is from the name a bank reports.

Providers do not say. SimpleFIN's protocol has no account-type field at all, so
"Chase Freedom Unlimited" arrives indistinguishable from a chequing account —
and the difference matters, because a positive balance on a card means money
owed while the same number on a chequing account means money held.

The names are recognisable to a person, so the knowledge is written down here:
the card products US and UK issuers actually sell, plus the generic words that
give an account away. A catalogue can never be complete, which is why a balance
sign backs it up and why the user can always correct the result.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..core.models import AccountType

# Card product names, which is where a catalogue earns its keep: nothing in
# "Freedom Unlimited", "Sapphire Preferred" or "Quicksilver" says card.
_CARD_PRODUCTS: tuple[str, ...] = (
    # Chase
    "freedom",
    "sapphire",
    "slate",
    "ink business",
    "ink cash",
    "ink preferred",
    "amazon prime rewards",
    "united explorer",
    "united quest",
    "southwest rapid",
    "marriott bonvoy",
    "world of hyatt",
    "ihg rewards",
    "disney premier",
    "aeroplan",
    "instacart mastercard",
    # American Express
    "amex",
    "american express",
    "blue cash",
    "gold card",
    "green card",
    "delta skymiles",
    "hilton honors",
    "everyday preferred",
    "cash magnet",
    # Capital One
    "quicksilver",
    "savor",
    "venture",
    "ventureone",
    "spark",
    "platinum secured",
    # Citi
    "double cash",
    "custom cash",
    "premier",
    "rewards+",
    "diamond preferred",
    "aadvantage",
    "costco anywhere",
    # Discover / Wells Fargo / BoA / others
    "discover it",
    "active cash",
    "autograph",
    "reflect",
    "bilt",
    "customized cash",
    "unlimited cash",
    "travel rewards",
    "premium rewards",
    "cash preferred",
    "propel",
    "altitude go",
    "altitude reserve",
    "apple card",
    "prime visa",
    "sofi credit",
    "upgrade card",
    # UK
    "barclaycard",
    "amazon platinum",
    "nectar",
    "clarity card",
)

# Generic words. Weaker than a product name and checked after it, because
# "rewards checking" is a real chequing product and would otherwise read as a
# card purely for containing "rewards".
_GENERIC: list[tuple[tuple[str, ...], AccountType]] = [
    (("credit card", "creditcard", "chargecard", "charge card"), AccountType.CREDIT_CARD),
    (
        (
            "checking",
            "chequing",
            "current account",
            "everyday account",
            "college",
            "student checking",
        ),
        AccountType.CHECKING,
    ),
    (
        ("savings", "saver", "money market", "cd account", "certificate of deposit"),
        AccountType.SAVINGS,
    ),
    (
        (
            "mortgage",
            "home loan",
            "auto loan",
            "car loan",
            "student loan",
            "personal loan",
            "heloc",
            "line of credit",
        ),
        AccountType.LOAN,
    ),
    (
        (
            "brokerage",
            "investment",
            "roth",
            "traditional ira",
            "401k",
            "403b",
            "457b",
            "hsa",
            "529",
            "ira ",
            "pension",
            "annuity",
            "rollover",
            # A brokerage "cash management" account is a sweep account inside
            # an investment platform, not a chequing account.
            "cash management",
            "vanguard",
            "fidelity",
            "schwab",
            "etrade",
            "robinhood",
        ),
        AccountType.INVESTMENT,
    ),
    (("cash account", "wallet", "spending account"), AccountType.CASH),
]

# Card networks. Only consulted once nothing more specific matched: a debit
# card is also a Visa, so this is a hint rather than an answer.
_NETWORKS = ("visa", "mastercard", "master card", "amex", "discover", "diners club")

_DEBIT = re.compile(r"\bdebit\b")


def classify_account(name: str, balance: str | float | Decimal | None = None) -> AccountType:
    """Best guess at an account's type from its name, and its balance if known.

    >>> classify_account("Chase Freedom Unlimited (6550)")
    <AccountType.CREDIT_CARD: 'credit_card'>
    >>> classify_account("CHASE COLLEGE (6822)")
    <AccountType.CHECKING: 'checking'>
    >>> classify_account("CHASE SAVINGS (6571)")
    <AccountType.SAVINGS: 'savings'>
    """
    lowered = " ".join(name.lower().split())
    # Punctuation inside a plan name is meaningless but breaks matching:
    # banks write "401(K) PLAN" and "403(B)", not "401k". Collapsing it lets
    # one catalogue entry cover every spelling.
    flattened = re.sub(r"[()\[\].,/-]", "", lowered)

    # A named card product is the strongest signal there is.
    if any(product in lowered or product in flattened for product in _CARD_PRODUCTS):
        return AccountType.CREDIT_CARD

    for needles, kind in _GENERIC:
        if any(needle in lowered or needle in flattened for needle in needles):
            return kind

    # A card network name, unless the account says outright that it is a debit
    # card, in which case it is the chequing account behind it.
    if any(network in lowered for network in _NETWORKS):
        return AccountType.CHECKING if _DEBIT.search(lowered) else AccountType.CREDIT_CARD

    if "card" in lowered:
        return AccountType.CHECKING if _DEBIT.search(lowered) else AccountType.CREDIT_CARD

    # Nothing in the name says. A negative balance is the next best evidence:
    # an asset account sits at or above zero nearly always, a card below it
    # nearly always. Wrong for an overdrawn current account, which the user can
    # correct — better than calling every unrecognised account chequing.
    if balance is not None:
        try:
            if Decimal(str(balance)) < 0:
                return AccountType.CREDIT_CARD
        except (InvalidOperation, ValueError):
            pass
    return AccountType.CHECKING
