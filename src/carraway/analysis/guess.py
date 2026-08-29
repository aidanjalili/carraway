"""Guess a category for a transaction the rules could not place.

The built-in rules match named merchants and clear keywords, which reaches
about 70% of a real ledger. The rest is local businesses no shipped list can
ever name — a particular pizza place, a regional utility, a corner shop.

This guesses at those from the shape of the description and what the user has
already agreed to, and every guess is marked as one. That marking is the whole
point: a guess indistinguishable from a certainty is worse than no guess,
because the user cannot know which figures to trust.

Two signals, cheapest first:

1. **What the user already did.** If they categorised one charge from this
   merchant, later charges belong in the same place. This is the strongest
   signal by a wide margin, and it improves as they use the app.
2. **Words in the description.** "PIZZA", "CAFE", "PHARMACY" are ordinary
   English and say what a business is.
Amount was tried as a third signal and dropped: a round $20 is a fantasy
football pool as readily as a subscription, and the guesses it produced were
wrong more often than right.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache

from ..core.models import Transaction
from .categorize import CATEGORIES, UNCATEGORIZED
from .recurring import normalise_merchant

# Words that name a kind of business rather than a particular one. Deliberately
# narrower than the rules engine's keyword list: this runs only after that has
# failed, so it can afford to be about vocabulary rather than merchants.
_WORD_HINTS: dict[str, tuple[str, ...]] = {
    "Dining": (
        "PIZZA",
        "PIZZERIA",
        "CAFE",
        "COFFEE",
        "ESPRESSO",
        "BAKERY",
        "DELI",
        "GRILL",
        "KITCHEN",
        "BISTRO",
        "TAVERN",
        "PUB",
        "BAR ",
        "BREWING",
        "BREWERY",
        "TAQUERIA",
        "SUSHI",
        "RAMEN",
        "NOODLE",
        "BURGER",
        "BBQ",
        "STEAKHOUSE",
        "CANTINA",
        "CREAMERY",
        "DONUT",
        "BAGEL",
        "SANDWICH",
        "RESTAURANT",
        "EATERY",
        "DINER",
        "CHOPHOUSE",
        "TRATTORIA",
    ),
    "Groceries": (
        "GROCERY",
        "GROCER",
        "MARKET",
        "FOODS",
        "SUPERMARKET",
        "PRODUCE",
        "BUTCHER",
        "FARMERS MKT",
        "MERCADO",
        "FOOD MART",
    ),
    "Health": (
        "PHARMACY",
        "DRUG",
        "MEDICAL",
        "DENTAL",
        "DENTIST",
        "CLINIC",
        "HOSPITAL",
        "OPTICAL",
        "OPTOMETR",
        "CHIROPRAC",
        "THERAPY",
        "PHYSICIAN",
        "URGENT CARE",
        "HEALTH",
    ),
    "Transport": (
        "PARKING",
        "GARAGE",
        "TRANSIT",
        "TAXI",
        "CAB ",
        "SHUTTLE",
        "TOLL",
        "FUEL",
        "GAS STATION",
        "SERVICE STATION",
        "AUTO REPAIR",
        "TIRE",
        "CAR WASH",
        "RAIL",
        "METRO",
        "SUBWAY STATION",
        "AIRPORT PARK",
    ),
    "Travel": (
        "HOTEL",
        "MOTEL",
        "INN ",
        "RESORT",
        "LODGE",
        "HOSTEL",
        "AIRLINE",
        "AIRWAYS",
        "AIRPORT",
        "TRAVEL",
        "TOURS",
        "CRUISE",
        "RENTAL CAR",
    ),
    "Shopping": (
        "HARDWARE",
        "SUPPLY",
        "BOUTIQUE",
        "OUTFITTERS",
        "APPAREL",
        "CLOTHING",
        "SHOES",
        "FURNITURE",
        "BOOKSTORE",
        "BOOKS",
        "GIFT",
        "STATIONERY",
        "DEPARTMENT STORE",
        "OUTLET",
    ),
    "Utilities": (
        "WATER",
        "SEWER",
        "ELECTRIC",
        "POWER",
        "ENERGY",
        "GAS COMPANY",
        "UTILITY",
        "UTILITIES",
        "SANITATION",
        "WASTE",
        "BROADBAND",
        "INTERNET",
    ),
    "Entertainment": (
        "CINEMA",
        "THEATRE",
        "THEATER",
        "MUSEUM",
        "GALLERY",
        "BOWLING",
        "ARCADE",
        "CONCERT",
        "TICKETS",
        "STADIUM",
        "GOLF",
        "CLIMBING",
    ),
    "Pets": ("VETERIN", "VET CLINIC", "PET ", "PETCARE", "GROOMING", "KENNEL"),
    "Education": (
        "UNIVERSITY",
        "COLLEGE",
        "SCHOOL",
        "TUITION",
        "ACADEMY",
        "INSTITUTE",
        "COURSE",
        "TUTOR",
        "LIBRARY",
    ),
    # Narrow on purpose. A bare "FEE" matched "ANYTIME FIT ABC CLUB FEES",
    # which is a gym membership, and "ATM" matched withdrawals, which are not
    # fees — the fee is its own line beside them.
    "Fees": (
        "SERVICE CHARGE",
        "INTEREST CHARGE",
        "LATE CHARGE",
        "LATE FEE",
        "OVERDRAFT",
        "ATM FEE",
        "FOREIGN TRANSACTION FEE",
        "ANNUAL FEE",
        "MAINTENANCE FEE",
        "WIRE FEE",
        "RETURNED ITEM",
    ),
}

# A guess needs to beat this to be worth showing at all.
MIN_CONFIDENCE = 0.45


@lru_cache(maxsize=512)
def _boundary(word: str) -> re.Pattern[str]:
    """A word matched on boundaries rather than as a bare substring."""
    return re.compile(rf"(?<![A-Z0-9]){re.escape(word.strip())}(?![A-Z0-9])")


@dataclass(frozen=True, slots=True)
class Guess:
    """A proposed category, and why."""

    category: str
    confidence: float
    reason: str


def _learned(transactions: list[Transaction]) -> dict[str, str]:
    """Merchant -> the category the user has most often given it.

    Only rows carrying a category the user actually accepted count, which is
    why this is empty on a fresh ledger and gets better with use.
    """
    votes: dict[str, Counter] = defaultdict(Counter)
    for tx in transactions:
        if not tx.category or tx.category == UNCATEGORIZED:
            continue
        if getattr(tx, "auto_categorized", False):
            # A guess must never train the next guess, or one mistake
            # propagates through the whole ledger unchallenged.
            continue
        key = (tx.merchant or normalise_merchant(tx.description)).upper()
        if key:
            votes[key][tx.category] += 1
    return {merchant: counter.most_common(1)[0][0] for merchant, counter in votes.items()}


def guess(transaction: Transaction, *, learned: dict[str, str] | None = None) -> Guess | None:
    """A category for a transaction the rules could not place, or None."""
    merchant = (transaction.merchant or normalise_merchant(transaction.description)).upper()
    description = transaction.description.upper()

    if learned:
        known = learned.get(merchant)
        if known:
            return Guess(known, 0.9, f"you filed {merchant.title()} under {known} before")

    for category, words in _WORD_HINTS.items():
        for word in words:
            if word in description or word in merchant:
                return Guess(category, 0.6, f'the description mentions "{word.strip().title()}"')

    # Nothing left worth guessing on. An earlier version treated a small round
    # amount as a likely subscription, which produced 167 guesses on a real
    # ledger and was wrong for most of them — a fantasy football pool and a
    # restaurant both charge $20. The amount of a charge says almost nothing
    # about its kind, and a confident wrong answer is the one thing this
    # module must not produce.
    return None


def guess_all(transactions: list[Transaction], categories: list[str]) -> dict[str, Guess]:
    """Guess for every row the rules left uncategorised.

    `categories` is what `categorize_all` returned, in the same order, so this
    only ever fills gaps and never overrides a rule that matched.
    """
    learned = _learned(transactions)
    out: dict[str, Guess] = {}
    for transaction, category in zip(transactions, categories, strict=True):
        if category != UNCATEGORIZED:
            continue
        found = guess(transaction, learned=learned)
        if found and found.confidence >= MIN_CONFIDENCE and found.category in CATEGORIES:
            out[transaction.id] = found
    return out
