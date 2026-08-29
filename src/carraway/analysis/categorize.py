"""Assign a spending category to a transaction.

Categorisation is the difference between a list of charges and an answer to
"where did the money go". This module is the *rules* half of the roadmap item;
a model learned from user corrections comes later, and the seams for it are
here already:

* Every decision is made by a `Rule` object, so a correction the user makes can
  be turned straight into a rule (see `suggest_rules`) instead of vanishing
  into a black box.
* `categorize_all` takes an optional `fallback`, which is where a learned
  classifier will be plugged in to handle what the rules miss — rules first,
  because a rule is inspectable and a user can argue with it.

Two design choices worth stating up front:

* Matching happens against the **normalised** merchant from `recurring.py`, not
  the raw description, so `SQ *BLUE BOTTLE #402 SF CA` and `BLUE BOTTLE COFFEE`
  land in the same place. Rules that specifically want the bank's raw text
  (a payment-processor prefix, say) opt in with `on="description"`.
* `categorize` never raises and never returns an empty string. A row we cannot
  place is `"Uncategorized"`, which is a visible, fixable state; guessing would
  quietly corrupt the spending totals the whole app is for.

The taxonomy is deliberately flat. Subcategories are easy to add and hard to
maintain — every one of them is another decision the user has to agree with —
so `CATEGORIES` stays at the level a person recognises on a bank statement.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from ..core.models import Transaction
from ..core.money import Money
from ..core.money import total as sum_money
from .recurring import normalise_merchant

# -- taxonomy ------------------------------------------------------------

GROCERIES = "Groceries"
DINING = "Dining"
TRANSPORT = "Transport"
TRAVEL = "Travel"
UTILITIES = "Utilities"
HOUSING = "Rent/Mortgage"
SUBSCRIPTIONS = "Subscriptions"
HEALTH = "Health"
SHOPPING = "Shopping"
ENTERTAINMENT = "Entertainment"
INSURANCE = "Insurance"
EDUCATION = "Education"
PETS = "Pets"
GIFTS = "Gifts/Charity"
FEES = "Fees"
TAXES = "Taxes"
INCOME = "Income"
TRANSFER = "Transfer"
UNCATEGORIZED = "Uncategorized"

# The order here is the order a UI should offer them in: everyday spending
# first, then the money-moving categories a user thinks about less often.
CATEGORIES: tuple[str, ...] = (
    GROCERIES,
    DINING,
    TRANSPORT,
    TRAVEL,
    UTILITIES,
    HOUSING,
    SUBSCRIPTIONS,
    HEALTH,
    SHOPPING,
    ENTERTAINMENT,
    INSURANCE,
    EDUCATION,
    PETS,
    GIFTS,
    FEES,
    TAXES,
    INCOME,
    TRANSFER,
    UNCATEGORIZED,
)

# -- rules ---------------------------------------------------------------

# Priority tiers. A user's own rule must always win, because being overruled by
# a shipped default is the fastest way to make someone stop trusting the app.
# Generic keyword rules ("COFFEE", "PHARMACY") sit *below* named merchants so
# that a broad guess can never displace something we actually recognise.
PRIORITY_GENERIC = 10
PRIORITY_BUILTIN = 100
PRIORITY_USER = 500

MatchField = Literal["merchant", "description"]
# Sign gate. Remember the convention: negative is money leaving you, so
# "inflow" means a positive amount.
Sign = Literal["any", "inflow", "outflow"]


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# normalise_merchant is pure but not cheap, and a bulk pass would otherwise
# call it once per rule per transaction. Memoising makes it once per distinct
# description instead.
@lru_cache(maxsize=8192)
def _normalise(description: str) -> str:
    return normalise_merchant(description)


@dataclass(frozen=True, slots=True)
class Rule:
    """One "if it looks like this, call it that" rule.

    `pattern` is a case-insensitive substring by default, or a regular
    expression when `regex=True`. It is tested against the normalised merchant
    unless `on="description"`, which is for patterns that live in the noise
    normalisation strips (processor prefixes, for instance).

    `sign` gates the rule on the direction of the money, which is how the same
    word means different things: `PAYROLL` on a positive amount is salary, and
    on a negative amount it is a business paying its staff.
    """

    pattern: str
    category: str
    priority: int = PRIORITY_BUILTIN
    regex: bool = False
    on: MatchField = "merchant"
    sign: Sign = "any"

    def matches(self, transaction: Transaction) -> bool:
        if self.sign == "inflow" and transaction.amount.minor <= 0:
            return False
        if self.sign == "outflow" and transaction.amount.minor >= 0:
            return False
        text = _text_for(transaction, self.on)
        if self.regex:
            return _compiled(self.pattern).search(text) is not None
        return self.pattern.upper() in text


def _text_for(transaction: Transaction, on: MatchField) -> str:
    """The haystack a rule is matched against."""
    if on == "description":
        return transaction.description.upper()
    # An importer or the user may already have set a clean merchant; trust it
    # over re-deriving one, exactly as recurring.detect does.
    if transaction.merchant:
        return transaction.merchant.upper()
    return _normalise(transaction.description)


def _many(
    category: str,
    *patterns: str,
    priority: int = PRIORITY_BUILTIN,
    regex: bool = False,
    on: MatchField = "merchant",
    sign: Sign = "any",
) -> list[Rule]:
    """Build a run of rules that differ only in their pattern."""
    return [Rule(p, category, priority=priority, regex=regex, on=on, sign=sign) for p in patterns]


# US-centric on purpose: the built-ins only have to cover the merchants a first
# import is likely to contain, and everything they miss is `suggest_rules`'
# job. A list like this can never be complete, which is the whole argument for
# the learned model later.
BUILTIN_RULES: list[Rule] = [
    # -- money moving between the user's own accounts, and income -------
    # Checked as ordinary rules rather than something special, so a user can
    # override a mis-flagged one like any other.
    *_many(
        TRANSFER,
        "TRANSFER TO",
        "TRANSFER FROM",
        "ONLINE TRANSFER",
        "ONLINE BANKING TRANSFER",
        "XFER",
        # Phrasings a real ledger turned up that the list above missed. A
        # balance transfer is $3,192 of the user's own money moving between
        # their own cards; counted as spending it is the single largest
        # purchase in the history and entirely fictional.
        "BALANCE TRANSFER",
        "ELECTRONIC TRANSFER",
        "INTERNAL TRANSFER",
        "ACCOUNT TRANSFER",
        "FUNDS TRANSFER",
        "WIRE TRANSFER",
        "TRANSFER TO SAVINGS",
        "TRANSFER TO CHECKING",
        "ZELLE",
        "VENMO",
        "CASH APP",
        "SQUARE CASH",
        "ATM WITHDRAWAL",
        "CREDIT CARD PAYMENT",
        "PAYMENT THANK YOU",
        "AUTOPAY PAYMENT",
        "EPAYMENT",
        "E-PAYMENT",
        "ROBINHOOD",
        "COINBASE",
        "VANGUARD",
        "FIDELITY",
        "CHARLES SCHWAB",
        "BETTERMENT",
        "WEALTHFRONT",
        "ACORNS",
    ),
    *_many(
        INCOME,
        "PAYROLL",
        "DIRECT DEP",
        "DIRECTDEP",
        "SALARY",
        "PAYCHECK",
        "GUSTO",
        "PAYCHEX",
        "SSA TREAS",
        "UNEMPLOYMENT",
        "DIVIDEND",
        "INTEREST PAID",
        "INTEREST EARNED",
        sign="inflow",
    ),
    # Short enough to appear inside real words, so anchor them.
    *_many(INCOME, r"\bADP\b", r"\bDEP\b.*PAYROLL", regex=True, sign="inflow"),
    # A refund from the tax authority is income; a payment to it is not.
    *_many(INCOME, "IRS TREAS", "TAX REF", "STATE TAX RFD", sign="inflow"),
    *_many(TAXES, "IRS USATAXPYMT", "TAX PAYMENT", "FRANCHISE TAX", sign="outflow"),
    # -- everyday spending ----------------------------------------------
    *_many(
        GROCERIES,
        "TRADER JOE",
        "WHOLE FOODS",
        "SAFEWAY",
        "KROGER",
        "PUBLIX",
        "ALDI",
        "WEGMANS",
        "SPROUTS",
        "VONS",
        "RALPHS",
        "ALBERTSONS",
        "FOOD LION",
        "HARRIS TEETER",
        "MEIJER",
        "WINCO",
        "STOP & SHOP",
        "GIANT EAGLE",
        "H-E-B",
        "HEB ",
        "COSTCO",
        "SAMS CLUB",
        "INSTACART",
        "GROCERY OUTLET",
        "SMITHS FOOD",
        "FRESH MARKET",
    ),
    *_many(
        DINING,
        "STARBUCKS",
        "DUNKIN",
        "BLUE BOTTLE",
        "PEETS",
        "PHILZ",
        "CHIPOTLE",
        "MCDONALD",
        "BURGER KING",
        "WENDYS",
        "TACO BELL",
        "PANERA",
        "SHAKE SHACK",
        "CHICK-FIL-A",
        "CHICK FIL A",
        "DOMINOS",
        "PAPA JOHNS",
        "PANDA EXPRESS",
        "FIVE GUYS",
        "POPEYES",
        "SWEETGREEN",
        "DOORDASH",
        "UBER EATS",
        "GRUBHUB",
        "POSTMATES",
        "SEAMLESS",
        "CAVIAR",
        "OLIVE GARDEN",
        "CHEESECAKE FACTORY",
        "IHOP",
        "DENNYS",
    ),
    # Toast and Square are restaurant/cafe point-of-sale systems, so their
    # prefix is itself evidence. It only survives on the raw description,
    # since normalisation exists precisely to strip it.
    *_many(DINING, "TST*", on="description"),
    *_many(
        TRANSPORT,
        "UBER",
        "LYFT",
        "CHEVRON",
        "EXXON",
        "ARCO",
        "TEXACO",
        "VALERO",
        "SUNOCO",
        "CIRCLE K",
        "SPEEDWAY",
        "FASTRAK",
        "E-ZPASS",
        "JIFFY LUBE",
        "AUTOZONE",
        "OREILLY AUTO",
        "DISCOUNT TIRE",
        "CALTRAIN",
        "METRO TRANSIT",
        "MTA ",
        "BART ",
        "CLIPPER",
    ),
    # Whole-word only: "MOBIL" is inside "T-MOBILE" and "SHELL" is inside a
    # dozen seafood restaurants.
    *_many(TRANSPORT, r"\bSHELL\b", r"\bMOBIL\b", regex=True),
    *_many(
        TRAVEL,
        "AIRBNB",
        "VRBO",
        "EXPEDIA",
        "BOOKING.COM",
        "PRICELINE",
        "KAYAK",
        "MARRIOTT",
        "HILTON",
        "HYATT",
        "AMTRAK",
        "DELTA AIR",
        "UNITED AIRLINES",
        "AMERICAN AIR",
        "SOUTHWEST AIR",
        "JETBLUE",
        "ALASKA AIR",
        "HERTZ",
        "ENTERPRISE RENT",
        "AVIS",
        "TSA PRE",
        "GLOBAL ENTRY",
    ),
    *_many(
        UTILITIES,
        "PG&E",
        "PGANDE",
        "COMCAST",
        "XFINITY",
        "SPECTRUM",
        "COX COMM",
        "CENTURYLINK",
        "GOOGLE FIBER",
        "VERIZON",
        "AT&T",
        "T-MOBILE",
        "DUKE ENERGY",
        "CON EDISON",
        "NATIONAL GRID",
        "WASTE MANAGEMENT",
        "REPUBLIC SERVICES",
        "SO CAL EDISON",
        "DOMINION ENERGY",
    ),
    *_many(
        HOUSING,
        "MORTGAGE",
        "PROPERTY MGMT",
        "PROPERTY MANAGEMENT",
        "APARTMENTS",
        "APARTMENT HOMES",
        "LEASING",
        "REALTY",
        "HOA ",
        "HOME LOAN",
    ),
    *_many(
        SUBSCRIPTIONS,
        "NETFLIX",
        "SPOTIFY",
        "HULU",
        "DISNEY PLUS",
        "DISNEYPLUS",
        "HBO MAX",
        "PEACOCK",
        "APPLE.COM",
        "ITUNES.COM",
        "YOUTUBEPREMIUM",
        "YOUTUBE PREMIUM",
        "AMAZON PRIME",
        "PRIME VIDEO",
        "ADOBE",
        "MICROSOFT 365",
        "DROPBOX",
        "NOTION",
        "GITHUB",
        "OPENAI",
        "ANTHROPIC",
        "CLAUDE.AI",
        "PATREON",
        "SUBSTACK",
        "AUDIBLE",
        "NYTIMES",
        "WSJ",
        "ICLOUD",
        "GOOGLE STORAGE",
    ),
    # Normalisation strips "+" as punctuation, which would erase the only thing
    # distinguishing Disney+ from Disneyland, so these read the raw text.
    *_many(SUBSCRIPTIONS, "DISNEY+", "PARAMOUNT+", on="description"),
    *_many(
        HEALTH,
        "CVS",
        "WALGREENS",
        "RITE AID",
        "LABCORP",
        "QUEST DIAGNOSTIC",
        "ONE MEDICAL",
        "KAISER",
        "ZOCDOC",
        "GOODRX",
        "PLANET FITNESS",
        "LA FITNESS",
        "EQUINOX",
        "24 HOUR FITNESS",
        "ORANGETHEORY",
        "PELOTON",
        "YMCA",
    ),
    *_many(
        SHOPPING,
        "AMAZON",
        "AMZN",
        "TARGET",
        "WALMART",
        "BEST BUY",
        "HOME DEPOT",
        "LOWES",
        "IKEA",
        "WAYFAIR",
        "ETSY",
        "EBAY",
        "MACYS",
        "NORDSTROM",
        "KOHLS",
        "TJ MAXX",
        "MARSHALLS",
        "ROSS STORES",
        "OLD NAVY",
        "UNIQLO",
        "ZARA",
        "H&M",
        "NIKE",
        "LULULEMON",
        "REI ",
        "SEPHORA",
        "ULTA",
        "ACE HARDWARE",
        "MICHAELS",
        "STAPLES",
        "OFFICE DEPOT",
        "SHEIN",
        "TEMU",
        "7-ELEVEN",
    ),
    *_many(
        ENTERTAINMENT,
        "AMC ",
        "REGAL CINEMA",
        "CINEMARK",
        "FANDANGO",
        "TICKETMASTER",
        "STUBHUB",
        "LIVE NATION",
        "EVENTBRITE",
        "STEAMGAMES",
        "PLAYSTATION",
        "XBOX",
        "NINTENDO",
        "TOPGOLF",
        "DAVE & BUSTER",
    ),
    *_many(
        INSURANCE,
        "GEICO",
        "STATE FARM",
        "PROGRESSIVE",
        "ALLSTATE",
        "USAA",
        "LIBERTY MUTUAL",
        "FARMERS INS",
        "NATIONWIDE INS",
        "LEMONADE INS",
        "BLUE CROSS",
        "BLUE SHIELD",
        "AETNA",
        "CIGNA",
        "UNITEDHEALTH",
    ),
    *_many(
        EDUCATION,
        "TUITION",
        "COURSERA",
        "UDEMY",
        "DUOLINGO",
        "CHEGG",
        "NAVIENT",
        "NELNET",
        "SALLIE MAE",
        "STUDENT LOAN",
        "MOSF",
        "BOOKSTORE",
    ),
    *_many(
        PETS,
        "CHEWY",
        "PETCO",
        "PETSMART",
        "BANFIELD",
        "ROVER.COM",
        "WAG LABS",
        "PET SUPPLIES",
    ),
    *_many(
        GIFTS,
        "GOFUNDME",
        "RED CROSS",
        "UNICEF",
        "ST JUDE",
        "SALVATION ARMY",
        "PLANNED PARENTHOOD",
        "WIKIMEDIA",
        "DONORSCHOOSE",
        "1-800-FLOWERS",
        "KIVA.ORG",
    ),
    # Checked before the transfer rules by priority, since "BALANCE TRANSFER
    # FEE" contains a transfer phrase but is money genuinely spent.
    *_many(
        FEES,
        "BALANCE TRANSFER FEE",
        "TRANSFER FEE",
        "WIRE FEE",
        priority=PRIORITY_USER,
    ),
    *_many(
        FEES,
        "OVERDRAFT",
        "NSF FEE",
        "ATM FEE",
        "SERVICE CHARGE",
        "MONTHLY MAINTENANCE",
        "MAINTENANCE FEE",
        "ANNUAL FEE",
        "LATE FEE",
        "FOREIGN TRANSACTION FEE",
        "WIRE FEE",
        "INTEREST CHARGE",
        "FINANCE CHARGE",
        "RETURNED ITEM",
    ),
    # -- generic keywords, deliberately outranked by everything above ----
    *_many(
        DINING,
        "COFFEE",
        "CAFE",
        "RESTAURANT",
        "PIZZA",
        "TAQUERIA",
        "SUSHI",
        "BAKERY",
        "DELI",
        "BREWING",
        "GRILL",
        "BISTRO",
        "KITCHEN",
        priority=PRIORITY_GENERIC,
    ),
    *_many(
        GROCERIES,
        r"\bMARKET\b",
        r"\bGROCER",
        r"\bSUPERMARKET\b",
        r"\bFOODS?\b",
        regex=True,
        priority=PRIORITY_GENERIC,
    ),
    *_many(
        UTILITIES,
        "ELECTRIC",
        "POWER",
        "WATER DIST",
        "WATER DEPT",
        "UTILIT",
        "GAS COMPANY",
        "ENERGY",
        "SANITATION",
        priority=PRIORITY_GENERIC,
    ),
    *_many(
        HEALTH,
        "PHARMACY",
        "DENTAL",
        "DENTIST",
        "MEDICAL",
        "CLINIC",
        "HOSPITAL",
        "ORTHO",
        "OPTOMETR",
        "PHYSICAL THERAPY",
        "WELLNESS",
        priority=PRIORITY_GENERIC,
    ),
    *_many(
        TRANSPORT,
        "PARKING",
        "TOLL",
        "GAS STATION",
        "FUEL",
        "TRANSIT",
        "AUTO PARTS",
        priority=PRIORITY_GENERIC,
    ),
    *_many(
        TRAVEL,
        "HOTEL",
        "MOTEL",
        "AIRLINE",
        "RESORT",
        "AIRPORT",
        "RENTAL CAR",
        priority=PRIORITY_GENERIC,
    ),
    *_many(HOUSING, r"\bRENT\b", regex=True, priority=PRIORITY_GENERIC),
    *_many(INSURANCE, "INSURANCE", "ASSURANCE", priority=PRIORITY_GENERIC),
    *_many(
        EDUCATION,
        "UNIVERSITY",
        "COLLEGE",
        "ACADEMY",
        "SCHOOL DIST",
        priority=PRIORITY_GENERIC,
    ),
    *_many(PETS, "VETERINAR", r"\bVET\b", "ANIMAL HOSPITAL", regex=True, priority=PRIORITY_GENERIC),
    *_many(GIFTS, "DONATION", "CHARITY", "FOUNDATION", priority=PRIORITY_GENERIC),
    *_many(
        ENTERTAINMENT,
        "CINEMA",
        "THEATRE",
        "THEATER",
        "MUSEUM",
        "BOWLING",
        priority=PRIORITY_GENERIC,
    ),
    # -- coverage widened after a real 2,200-transaction import -----------
    # Everything below is a national or multi-state chain, or a pattern that
    # generalises. Genuinely local merchants stay out: they belong in a user's
    # own rules, and baking one town's businesses into the defaults would make
    # the ruleset worse for everyone else.
    # Ahead of the dining rules, since "SUBWAY STATION" is transit and plain
    # "SUBWAY" is a sandwich shop.
    *_many(
        TRANSPORT,
        "SUBWAY STATION",
        "METRO STATION",
        "TRAIN STATION",
        priority=PRIORITY_USER,
    ),
    *_many(
        TRANSPORT,
        "KWIK TRIP",
        "CASEYS",
        "HOLIDAY STATIONSTORE",
        "SPEEDWAY",
        "CIRCLE K",
        "QUIKTRIP",
        "SHEETZ",
        "WAWA",
        "SUNOCO",
        "MARATHON PETRO",
        "PILOT TRAVEL",
        "MBTA",
        "VENTRA",
        "METRA",
        "SEPTA",
        "WMATA",
        "NJ TRANSIT",
        "CALTRAIN",
        "PARKMOBILE",
        "SPOTHERO",
        "E-ZPASS",
        "EZPASS",
        "FASTRAK",
    ),
    *_many(
        TRANSPORT,
        r"\bONSTREET\b",
        r"\bPARKING\b",
        r"\bTOLL(S)?\b",
        r"\bTRANSIT\b",
        regex=True,
        priority=PRIORITY_GENERIC,
    ),
    *_many(
        GROCERIES,
        "FAMILY FARE",
        "HY-VEE",
        "HYVEE",
        "WINN-DIXIE",
        "PUBLIX",
        "KROGER",
        "MEIJER",
        "ALDI",
        "WEGMANS",
        "GIANT EAGLE",
        "FOOD LION",
        "SAFEWAY",
        "ALBERTSONS",
        "SPROUTS",
        "FRESH THYME",
        "PIGGLY WIGGLY",
        "WOODMANS",
        "FESTIVAL FOODS",
        "CUB FOODS",
        "H MART",
        "STOP & SHOP",
    ),
    *_many(
        DINING,
        "CULVERS",
        "FIREHOUSE SUBS",
        "JIMMY JOHNS",
        "PORTILLO",
        "QDOBA",
        "FIVE GUYS",
        "SHAKE SHACK",
        "RAISING CANE",
        "WHATABURGER",
        "IN-N-OUT",
        "SONIC DRIVE",
        "ARBYS",
        "POPEYES",
        "DAIRY QUEEN",
        "JERSEY MIKE",
        "CANTEEN",
        "SNACK SODA",
        # Missing outright, and all obvious from a real ledger.
        "DOMINO",
        # No trailing space: normalisation strips the store number, so the
        # merchant arrives as bare "SUBWAY".
        "SUBWAY",
        "PAPA JOHN",
        "PIZZA HUT",
        "LITTLE CAESAR",
        "CHIPOTLE",
        "PANERA",
        "WENDY",
        "MCDONALD",
        "BURGER KING",
        "TACO BELL",
        "KFC",
        "CHICK-FIL-A",
        "DUNKIN",
        "CARIBOU COFFEE",
        "PEETS",
        "DINING HALL",
        "CAFETERIA",
        "FOOD HALL",
    ),
    *_many(
        DINING,
        r"\bTAVERN\b",
        r"\bBREW(ING|ERY)\b",
        r"\bCANTINA\b",
        r"\bBISTRO\b",
        r"\bDINER\b",
        r"\bTAQUERIA\b",
        r"\bSUSHI\b",
        r"\bVENDING\b",
        regex=True,
        priority=PRIORITY_GENERIC,
    ),
    *_many(
        SHOPPING,
        "MENARDS",
        "WAL-MART",
        "WALGREENS",
        "CVS",
        "DOLLAR GENERAL",
        "DOLLAR TREE",
        "IKEA",
        "HOME DEPOT",
        "LOWES",
        "REI ",
        "DICKS SPORTING",
        "ACE HARDWARE",
        "FLEET FARM",
        "TRACTOR SUPPLY",
        "HARBOR FREIGHT",
    ),
    *_many(
        UTILITIES,
        "XCEL ENERGY",
        "ALLIANT",
        "MADISON GAS",
        "WE ENERGIES",
        "DUKE ENERGY",
        "DOMINION ENERGY",
        "NATIONAL GRID",
        "CONED",
        "AMEREN",
        "CENTERPOINT",
    ),
    *_many(
        UTILITIES,
        # "UTL" is how several municipal billers abbreviate themselves.
        r"\bUTL\b",
        r"\bUTILIT(Y|IES)\b",
        r"\bWATER\s+(DEPT|UTILITY)\b",
        regex=True,
        priority=PRIORITY_GENERIC,
    ),
    *_many(
        TRAVEL,
        "WESTIN",
        "MARRIOTT",
        "HILTON",
        "HYATT",
        "SHERATON",
        "HAMPTON INN",
        "HOLIDAY INN",
        "BEST WESTERN",
        "DELTA AIR",
        "UNITED AIR",
        "SOUTHWEST AIR",
        "JETBLUE",
        "ALASKA AIR",
        "FRONTIER AIR",
        "SPIRIT AIR",
    ),
    # American Airlines bills from its Fort Worth headquarters; "AMERICAN"
    # alone would swallow far too much.
    Rule(r"^AMERICAN\b.*FORT WORTH", TRAVEL, regex=True),
    # A redeemed card reward is money arriving, not a purchase.
    *_many(
        INCOME,
        "CASH BACK REDEMPTION",
        "CASH REDEMPTION",
        "REWARD REDEMPTION",
        "REMOTE ONLINE DEPOSIT",
        "MOBILE DEPOSIT",
        "PAYABLES",
        sign="inflow",
    ),
    # Card autopay drawn from checking: a transfer, even when no partner row
    # was matched on the card side.
    *_many(
        TRANSFER,
        "CREDIT CRD AUTOPAY",
        "PAYMENT TO CHASE CARD",
        "CARD ENDING",
        "CARDMEMBER SERV",
        "BILL PAYMENT",
    ),
]


def _catalogue_rules() -> list[Rule]:
    """Category rules derived from the subscription catalogue.

    Those two lists were maintained separately, so a merchant recognised as a
    subscription could still come back Uncategorized — DigitalOcean was known
    to be a subscription and had no category at all. Anything the catalogue
    names is categorised from the same list, which also means adding a service
    there fixes both at once.

    Priority sits between the generic keywords and the named merchants: a
    specific built-in rule should still win, but a catalogue entry should beat
    a bare keyword match.
    """
    from .subscriptions import bill_names, subscription_names

    rules = [
        Rule(name, SUBSCRIPTIONS, priority=PRIORITY_BUILTIN - 10) for name in subscription_names()
    ]
    rules += [Rule(name, UTILITIES, priority=PRIORITY_BUILTIN - 20) for name in bill_names()]
    return rules


def _ordered(rules: Sequence[Rule]) -> list[Rule]:
    """Sort rules so the first match is the right one.

    Highest priority first; within a tier the longer pattern wins, because a
    longer pattern is the more specific claim — that is what makes
    `UBER EATS` beat `UBER` without needing hand-tuned priorities for every
    pair. Python's sort is stable, so a genuine tie keeps declaration order.
    """
    return sorted(rules, key=lambda r: (-r.priority, -len(r.pattern)))


_BUILTIN_ORDERED = _ordered(BUILTIN_RULES + _catalogue_rules())


def _resolve(rules: Sequence[Rule] | None, include_builtins: bool) -> list[Rule]:
    if not rules:
        return _BUILTIN_ORDERED if include_builtins else []
    return _ordered([*BUILTIN_RULES, *rules] if include_builtins else list(rules))


# -- the entry points ----------------------------------------------------


def matching_rule(
    transaction: Transaction,
    rules: Sequence[Rule] | None = None,
    *,
    include_builtins: bool = True,
) -> Rule | None:
    """The rule that decides this transaction, or None if nothing matches.

    Exposed so a UI can answer "why is this Dining?" with the actual rule,
    which is the property a learned model will not have and the reason rules
    come first.
    """
    for rule in _resolve(rules, include_builtins):
        if rule.matches(transaction):
            return rule
    return None


def categorize(
    transaction: Transaction,
    rules: Sequence[Rule] | None = None,
    *,
    include_builtins: bool = True,
) -> str:
    """Categorise one transaction, falling back to "Uncategorized".

    >>> from datetime import date
    >>> from carraway.core.models import Transaction
    >>> from carraway.core.money import Money
    >>> tx = Transaction("1", "acct", date(2026, 5, 14), Money(-475),
    ...                  "SQ *BLUE BOTTLE #402 SF CA")
    >>> categorize(tx)
    'Dining'

    `rules` are additional rules layered *over* the built-ins rather than
    replacing them, since a user rule is normally a correction to one merchant
    and not a wholesale rejection of the defaults. Pass
    `include_builtins=False` for the wholesale case.

    Any category already on the transaction is ignored on purpose: this stays a
    pure function of the rules, and whether to overwrite a human's choice is
    the caller's decision to make, not ours.
    """
    # Both halves of a matched transfer are money the user still has, so they
    # must never land in a spending category and skew every total.
    if transaction.is_transfer:
        return TRANSFER
    rule = matching_rule(transaction, rules, include_builtins=include_builtins)
    return rule.category if rule else UNCATEGORIZED


def categorize_all(
    transactions: Sequence[Transaction],
    rules: Sequence[Rule] | None = None,
    *,
    include_builtins: bool = True,
    fallback: Callable[[Transaction], str | None] | None = None,
) -> list[str]:
    """Categorise a batch, returning one category per transaction, in order.

    Returns a parallel list rather than a dict keyed by id, so it is still
    correct for transactions that have not been saved and given ids yet.

    `fallback` is consulted only for rows the rules leave uncategorised, and is
    the hook for the learned classifier: it may return a category name or None
    to accept "Uncategorized". A category it invents that is not in
    `CATEGORIES` is passed through untouched — inventing categories is a
    reasonable thing for a model to do, and second-guessing it here would hide
    the evidence.
    """
    ordered = _resolve(rules, include_builtins)
    out: list[str] = []
    for tx in transactions:
        if tx.is_transfer:
            out.append(TRANSFER)
            continue
        category = UNCATEGORIZED
        for rule in ordered:
            if rule.matches(tx):
                category = rule.category
                break
        if category == UNCATEGORIZED and fallback is not None:
            category = fallback(tx) or UNCATEGORIZED
        out.append(category)
    return out


# -- learning from what we missed ----------------------------------------


@dataclass(frozen=True, slots=True)
class RuleSuggestion:
    """A merchant we keep failing to categorise, offered to the user as a rule.

    The category is deliberately absent: we know *what* we cannot place, not
    what it is. The user supplies the category once and `as_rule` turns that
    single answer into something that categorises every future charge from the
    same merchant — which is the cheap, inspectable version of learning from
    corrections.
    """

    merchant: str  # the normalised key, and the proposed pattern
    count: int
    total: Money
    example: str  # one raw description, so the user can recognise it
    transaction_ids: list[str] = field(default_factory=list)

    def as_rule(self, category: str, *, priority: int = PRIORITY_USER) -> Rule:
        """Turn an accepted suggestion into a rule that outranks the built-ins."""
        return Rule(self.merchant, category, priority=priority)


def suggest_rules(
    transactions: Sequence[Transaction],
    rules: Sequence[Rule] | None = None,
    *,
    include_builtins: bool = True,
    limit: int = 10,
    min_count: int = 2,
) -> list[RuleSuggestion]:
    """Propose rules for the merchants a categorisation pass could not place.

    Ranked by transaction count first and total value second, because a rule
    earns its keep by how many rows it will catch: ten $4 coffees are more
    worth naming than one $400 mystery, and the single large charge is easier
    to spot by eye anyway.

    `min_count` exists because a merchant seen once is more likely a typo or a
    one-off than a gap in the ruleset worth writing a rule for.
    """
    assigned = categorize_all(transactions, rules, include_builtins=include_builtins)

    groups: dict[str, list[Transaction]] = defaultdict(list)
    for tx, category in zip(transactions, assigned, strict=True):
        if category != UNCATEGORIZED or tx.is_transfer:
            continue
        key = tx.merchant.upper() if tx.merchant else _normalise(tx.description)
        if not key:
            continue
        groups[key].append(tx)

    suggestions = [
        RuleSuggestion(
            merchant=key,
            count=len(txs),
            total=sum_money([t.amount for t in txs]),
            example=txs[0].description,
            transaction_ids=[t.id for t in txs],
        )
        for key, txs in groups.items()
        if len(txs) >= min_count
    ]
    suggestions.sort(key=lambda s: (-s.count, -abs(s.total.minor), s.merchant))
    return suggestions[:limit]


def rules_from(stored: list[dict[str, str]]) -> list[Rule]:
    """Turn the user's saved rules into Rules that outrank everything shipped.

    Matched against the raw description rather than the normalised merchant,
    because that is what the user is looking at when they write the rule: if
    they can see "PPD ID: 4760039224" on the row, a rule containing it should
    work.
    """
    return [
        Rule(
            item["pattern"],
            item["category"],
            priority=PRIORITY_USER + 100,
            on="description",
        )
        for item in stored
        if item.get("pattern") and item.get("category")
    ]


def available_categories(added: list[str], hidden: set[str]) -> tuple[str, ...]:
    """The category list to offer, with the user's edits applied.

    Hidden built-ins are dropped from what is offered but never rewritten on
    transactions already filed under them, which would silently move money
    between categories.
    """
    names = [name for name in CATEGORIES if name not in hidden]
    for name in added:
        if name not in names:
            names.insert(max(len(names) - 3, 0), name)
    return tuple(names)
