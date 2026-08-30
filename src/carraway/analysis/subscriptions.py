"""Tell a subscription from a bill from a habit.

`recurring.detect` answers "does this repeat?". That is not the same question
as "is this a subscription?", and real data made the difference obvious: a
weekly corner-shop visit and a monthly takeaway order both repeat convincingly,
and neither is something you can cancel.

Three kinds, because the user's next action differs for each:

* **subscription** — a service billing on a schedule. Cancellable. This is the
  list someone actually wants to look at.
* **bill** — rent, utilities, insurance, loan payments. Recurring and real, but
  cancelling is not the move; these belong in a budget, not a cull list.
* **habit** — periodic spending at an ordinary merchant. Worth knowing about,
  but it is not a commitment.

A catalog can never be complete, so anything it does not recognise is left
`unknown` and put to the user once. Their answer is stored and never asked
again — see `core.db.set_verdict`.
"""

from __future__ import annotations

import contextlib
import re
from datetime import date
from functools import lru_cache
from typing import Literal

from ..core.models import RecurringSeries

# At this length a catalog name is specific enough that finding it inside a
# longer run of characters is a match rather than a coincidence. Chosen so
# "DASHPASS" qualifies and "AWS", "MAX" and "BOX" do not.
_UNAMBIGUOUS_LENGTH = 7

Kind = Literal["subscription", "bill", "habit", "income", "cancelled", "dismissed", "unknown"]

SUBSCRIPTION: Kind = "subscription"
BILL: Kind = "bill"
HABIT: Kind = "habit"
INCOME: Kind = "income"
# Something the user has told us they have stopped paying for. Kept visible
# rather than deleted — knowing you cancelled a $195/yr magazine is useful —
# but excluded from what the app says you currently pay.
CANCELLED: Kind = "cancelled"
# Detection was wrong: this is not a recurring thing at all. Distinct from
# `cancelled`, which means it was real and has stopped — a dismissed series
# never should have been listed, so it is hidden rather than counted as
# something the user no longer pays. Stored rather than deleted, because the
# detector will find the same pattern again on the next import and the user
# should not have to dismiss it twice.
DISMISSED: Kind = "dismissed"
UNKNOWN: Kind = "unknown"

# Kinds a user may assign in the review flow.
ANSWERABLE: tuple[Kind, ...] = (
    SUBSCRIPTION,
    BILL,
    HABIT,
    INCOME,
    CANCELLED,
    DISMISSED,
)

# Kinds that represent money the user actually deals with. A dismissed
# series is not one of them and must never reach a total.
COUNTED: tuple[Kind, ...] = (SUBSCRIPTION, BILL, HABIT, INCOME, CANCELLED, UNKNOWN)

# Matched against the normalised merchant on word boundaries. Names are held in
# the form normalisation leaves them in: uppercase, no ".COM", no corporate
# suffix.
#
# An entry must identify a company on its own. Ordinary English words do not:
# "BOX" matched "LIQUOR BOX", and "MAX" matched an office supply shop, so those
# are spelled out ("BOX.COM", "HBO MAX") even though it costs a few real
# matches. A false positive here is worse than a miss, because the review flow
# exists to catch misses and nothing catches a confident wrong answer.
_SUBSCRIPTIONS: tuple[str, ...] = (
    # streaming video
    "NETFLIX",
    "HULU",
    "DISNEY",
    "HBO MAX",
    "HBO",
    "PARAMOUNT",
    "PEACOCK",
    "APPLE TV",
    "STARZ",
    "SHOWTIME",
    "CRUNCHYROLL",
    "MUBI",
    "CURIOSITYSTREAM",
    "BRITBOX",
    "PLUTO TV",
    "FUBO",
    "SLING",
    "YOUTUBE TV",
    "PHILO",
    "DAZN",
    # music and audio
    "SPOTIFY",
    "APPLE MUSIC",
    "TIDAL",
    "DEEZER",
    "PANDORA",
    "SIRIUSXM",
    "AUDIBLE",
    "YOUTUBE PREMIUM",
    "SOUNDCLOUD",
    "BANDCAMP",
    # cloud, hosting and developer billing
    "ICLOUD",
    "GOOGLE ONE",
    "GOOGLE STORAGE",
    "DROPBOX",
    "BOX.COM",
    "MEGA.NZ",
    "DIGITALOCEAN",
    "LINODE",
    "VULTR",
    "HETZNER",
    "AWS",
    "AMAZON WEB SERVICES",
    "GOOGLE CLOUD",
    "AZURE",
    "HEROKU",
    "VERCEL",
    "NETLIFY",
    "RENDER",
    "CLOUDFLARE",
    "FASTLY",
    "SUPABASE",
    "PLANETSCALE",
    "MONGODB",
    "REDIS",
    "GITHUB",
    "GITLAB",
    "BITBUCKET",
    "NPMJS",
    "DOCKER",
    "JETBRAINS",
    "NAMECHEAP",
    "GODADDY",
    "GANDI",
    "HOVER",
    "PORKBUN",
    "DNSIMPLE",
    # AI and API billing
    "ANTHROPIC",
    "OPENAI",
    "CLAUDE",
    "CHATGPT",
    "MIDJOURNEY",
    "REPLICATE",
    "HUGGING FACE",
    "PERPLEXITY",
    "CURSOR",
    "COPILOT",
    "ELEVENLABS",
    # software and productivity
    "ADOBE",
    "MICROSOFT 365",
    "OFFICE 365",
    "NOTION",
    "FIGMA",
    "CANVA",
    "1PASSWORD",
    "LASTPASS",
    "BITWARDEN",
    "DASHLANE",
    "NORDVPN",
    "EXPRESSVPN",
    "PROTON",
    "MULLVAD",
    "TAILSCALE",
    "EVERNOTE",
    "TODOIST",
    "SLACK",
    "ZOOM",
    "GRAMMARLY",
    "SETAPP",
    "BACKBLAZE",
    "CARBONITE",
    "MALWAREBYTES",
    "MCAFEE",
    "NORTON",
    "SQUARESPACE",
    "WIX",
    "WORDPRESS",
    "SHOPIFY",
    "MAILCHIMP",
    "SUBSTACK",
    "PATREON",
    "MEDIUM",
    "GHOST",
    # news, magazines and reading. Magazines are worth naming individually:
    # they bill yearly, so they are easy to forget and easy to miss, and the
    # publisher's name on a statement is often nothing like the title.
    "THEATLANT",
    "THE FREE PRESS",
    "CONDE NAST",
    "CONDENAST",
    "HEARST",
    "MEREDITH",
    "TIME MAGAZINE",
    "NATGEO",
    "NATIONAL GEOGRAPHIC",
    "SMITHSONIAN MAG",
    "NEW YORKER",
    "WIRED",
    "VOGUE",
    "ESQUIRE",
    "HARPERS",
    "BON APPETIT",
    "ROLLING STONE",
    "SPORTS ILLUSTRATED",
    "SCIENTIFIC AMERICAN",
    "NEWSWEEK",
    "FORBES",
    "BLOOMBERG",
    "BARRONS",
    "THE NATION",
    "HARVARD BUSINESS",
    "READERS DIGEST",
    "CONSUMER REPORTS",
    "NEW YORK TIMES",
    "NYTIMES",
    "WASHINGTON POST",
    "WALL STREET JOURNAL",
    "THE ATLANTIC",
    "THE ECONOMIST",
    "FINANCIAL TIMES",
    "GUARDIAN",
    "KINDLE UNLIMITED",
    "SCRIBD",
    "BLINKIST",
    # fitness and wellbeing
    "PLANET FITNESS",
    "ANYTIME FIT",
    "LA FITNESS",
    "EQUINOX",
    "CLASSPASS",
    "PELOTON",
    "STRAVA",
    "WHOOP",
    "CALM",
    "HEADSPACE",
    "NOOM",
    "WEIGHT WATCHERS",
    "YMCA",
    "CROSSFIT",
    "ORANGETHEORY",
    "LIFE TIME",
    # delivery, retail and gaming memberships
    "AMAZON PRIME",
    "DASHPASS",
    "UBER ONE",
    "INSTACART",
    "GRUBHUB PLUS",
    "WALMART+",
    "COSTCO MEMBERSHIP",
    "SAMS CLUB MEMBERSHIP",
    "BJS MEMBERSHIP",
    "XBOX GAME PASS",
    "PLAYSTATION PLUS",
    "NINTENDO SWITCH ONLINE",
    "STEAM",
    "EA PLAY",
    "UBISOFT",
    "ROBLOX",
    "TWITCH",
    # phone and connectivity, which behave like subscriptions
    "T-MOBILE",
    "VERIZON WIRELESS",
    "AT&T WIRELESS",
    "MINT MOBILE",
    "GOOGLE FI",
    "VISIBLE",
    "CRICKET WIRELESS",
    "BOOST MOBILE",
    "TRACFONE",
    "US MOBILE",
    "TELLO",
    "MEET MOBILE",
)

# Recurring, but cancelling is not the user's next move.
_BILLS: tuple[str, ...] = (
    "RENT",
    "MORTGAGE",
    "PROPERTY MANAGEMENT",
    "LEASING",
    "APARTMENT",
    "ELECTRIC",
    "ENERGY",
    "POWER",
    "GAS COMPANY",
    "WATER",
    "SEWER",
    "UTILITY",
    "UTL",
    "WASTE",
    "RECYCLING",
    "SANITATION",
    "INSURANCE",
    "USAA",
    "GEICO",
    "PROGRESSIVE",
    "STATE FARM",
    "ALLSTATE",
    "STUDENT LOAN",
    "LOAN PAYMENT",
    "MORTGAGE PAYMENT",
    "NELNET",
    "SALLIE MAE",
    "COMCAST",
    "XFINITY",
    "SPECTRUM",
    "COX COMMUNICATIONS",
    "CENTURYLINK",
    "FRONTIER COMM",
    "VERIZON",
    "AT&T",
    "T-MOBILE",
    "US CELLULAR",
    "ALLIANT",
    "XCEL",
    "MADISON GAS",
    "WE ENERGIES",
    "DUKE ENERGY",
    "NATIONAL GRID",
    "CONED",
    "AMEREN",
    "CENTERPOINT",
    "DOMINION",
    "CHILDCARE",
    "DAYCARE",
    "TUITION",
    "HOA",
    "STORAGE",
)


# Words that mark a periodical even when the publisher is unknown. Applied
# only after the named catalogs miss, since a bare "PRESS" or "MEDIA" appears
# in plenty of businesses that are not subscriptions.
_PERIODICAL_HINTS = re.compile(r"\b(MAGAZINE|MAGAZINES|SUBSCRIPTION|SUBSCR|PERIODICAL)\b")


@lru_cache(maxsize=4)
def _ordered(
    names: tuple[str, ...],
) -> tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...]:
    """Catalog names longest-first, each as a word-boundary pattern.

    Longest first so "AMAZON PRIME" wins over a hypothetical "AMAZON".

    Word boundaries matter more than they look: plain substring matching had
    "AWS" match inside "MATT LAWS", "MAX" inside "OFFICEMAX" and "BOX" inside
    "LIQUOR BOX", so a Zelle payment to a person was reported as a cloud
    subscription. Short entries are the ones people most want in a catalog and
    the ones most likely to appear inside unrelated words.
    """
    ordered = []
    for name in sorted(names, key=len, reverse=True):
        flat_name = _flatten(name)
        # \b does not fire next to punctuation like "+" or ".", so anchor on a
        # non-word character instead of relying on it at both ends.
        #
        # Long names need no boundaries at all: processors run words together
        # in both directions ("DOORDASHDASHPASS", "SPOTIFYUSA"), and a name of
        # this length is specific enough that appearing inside a longer run is
        # a real match rather than an accident. Short names keep both
        # boundaries, which is the whole point of the rule.
        if len(name) >= _UNAMBIGUOUS_LENGTH:
            pattern = re.compile(re.escape(name))
            flat_pattern = re.compile(re.escape(flat_name))
        else:
            pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(name)}(?![A-Z0-9])")
            flat_pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(flat_name)}(?![A-Z0-9])")
        ordered.append((name, pattern, flat_pattern))
    return tuple(ordered)


# Money arriving on a schedule: salary, benefits, dividends, refunds. Only
# ever consulted for inflows, so "PAYROLL" on an outgoing charge (a business
# paying its own staff) is not mistaken for the user's income.
_INCOME_HINTS = re.compile(
    r"\b(PAYROLL|DIRECT\s?DEP(OSIT)?|SALARY|PAYCHECK|DIVIDEND|INTEREST\s+PAID"
    r"|TAX\s+REF(UND)?|REIMBURSE\w*|CASH\s?BACK|REDEMPTION|PENSION|ANNUITY"
    r"|SOCIAL\s+SECURITY|UNEMPLOYMENT|BENEFIT|REBATE|SETTLEMENT|ROYALT\w+)\b"
)

# A person-to-person payment rail. Whether one of these is income, a bill split
# with a housemate, or a habit depends entirely on who is at the other end, and
# only the user knows that — so a recurring one is surfaced for review rather
# than guessed at.
_P2P_RAILS = re.compile(r"\b(ZELLE|VENMO|CASH\s?APP|SQUARE\s?CASH|PAYPAL|WISE|REVOLUT)\b")


def is_person_to_person(merchant: str) -> bool:
    """True for Zelle, Venmo and friends, whichever direction the money went.

    >>> is_person_to_person("Zelle Payment From Ali Jalili")
    True
    >>> is_person_to_person("Netflix")
    False
    """
    return _P2P_RAILS.search(merchant.upper()) is not None


def _matches(merchant: str, names: tuple[str, ...]) -> str | None:
    upper = merchant.upper()
    # Also matched against a punctuation-flattened form, because a catalogue
    # cannot list every spelling: "T-MOBILE" has to meet "T MOBILE PAYMENT"
    # from a hand-written note and "T MOBILE" from a bank descriptor.
    flat = _flatten(merchant)
    for name, pattern, flat_pattern in _ordered(names):
        if pattern.search(upper) or flat_pattern.search(flat):
            return name
    return None


def classify(merchant: str, *, is_inflow: bool = False) -> Kind:
    """What kind of recurring thing this merchant is, from the catalog alone.

    >>> classify("Netflix")
    'subscription'
    >>> classify("Madison Gas El Billpay Ppd")
    'bill'
    >>> classify("Down Town Tobacco Northfield")
    'unknown'
    >>> classify("Epic Systems Cor Payroll Ppd", is_inflow=True)
    'income'

    Subscriptions are checked first: a gym is a subscription even though
    "MEMBERSHIP" reads like a bill, and Spectrum is a bill even though it
    streams. Anything unmatched is `unknown`, which is what the review flow
    exists to resolve.
    """
    # Direction first: money arriving is never a subscription, and a
    # person-to-person rail is never classifiable without knowing the person.
    if is_inflow and _INCOME_HINTS.search(merchant.upper()):
        return INCOME
    if is_person_to_person(merchant):
        return UNKNOWN

    if _matches(merchant, _SUBSCRIPTIONS):
        return SUBSCRIPTION
    if _matches(merchant, _BILLS):
        return BILL
    # No list of publishers can ever be complete, but "MAGAZINE" or
    # "SUBSCRIPTION" in the descriptor is the merchant telling us outright.
    if _PERIODICAL_HINTS.search(merchant.upper()):
        return SUBSCRIPTION
    return UNKNOWN


def catalog_size() -> tuple[int, int]:
    """(subscriptions, bills) known to the catalog. Used by the review summary."""
    return len(_SUBSCRIPTIONS), len(_BILLS)


def resolve(
    merchant: str, verdicts: dict[str, str] | None = None, *, is_inflow: bool = False
) -> Kind:
    """The kind of a merchant, preferring the user's own answer to the catalog.

    A stored verdict always wins. The catalog is a starting guess, and being
    overruled by the person looking at their own statement is the point rather
    than a failure.
    """
    if verdicts:
        stored = verdicts.get(merchant.upper())
        if stored in ANSWERABLE:
            return stored  # type: ignore[return-value]
    return classify(merchant, is_inflow=is_inflow)


# Below this length a merchant name is too generic to match on: "AAA" would
# collide with any description containing those letters in sequence.
_MATCHABLE_LENGTH = 5


def _flatten(name: str) -> str:
    """A name reduced to letters, digits and single spaces.

    Punctuation cannot be trusted to agree across sources: a user tracks
    "T-Mobile" while a note says "T mobile payment". Comparing the flattened
    forms is what lets those meet.
    """
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", name.upper()).split())


def _already_detected(merchant: str, detected: list[RecurringSeries]) -> bool:
    """Whether detection already found this merchant under some other name.

    A user tracks "DashPass" while the bank calls it "DD DOORDASHDASHPASS";
    counted separately that is one subscription billed twice. Matched by
    containment in either direction, since neither name is reliably the longer
    one, and only for names long enough that a coincidence is implausible.
    """
    needle = _flatten(merchant)
    if len(needle) < _MATCHABLE_LENGTH:
        return False
    for series in detected:
        found = _flatten(series.merchant)
        if needle in found or found in needle:
            return True
    return False


def as_series(
    tracked: list[dict[str, object]], detected: list[RecurringSeries] | None = None
) -> list[RecurringSeries]:
    """Turn manually tracked subscriptions into RecurringSeries.

    Shaped like detected ones so every view, total and sort treats them
    identically — the distinction matters for provenance, not for what the
    thing costs. `occurrences` is zero and `transaction_ids` empty precisely
    because nothing was observed: that is what marks an entry as told-to-us
    rather than found, and `is_manual` reads it back.

    Lives here rather than in the UI so the CLI and the GUI cannot disagree
    about what the user is paying for.
    """

    step = {"weekly": 7, "biweekly": 14, "monthly": 30, "quarterly": 91, "yearly": 365}
    today = date.today()
    out: list[RecurringSeries] = []
    for item in tracked:
        # Skip anything detection already covers, or the same subscription is
        # counted twice in every total.
        if detected and _already_detected(str(item["merchant"]), detected):
            continue
        amount = item["amount"]
        started = item.get("started_on")
        out.append(
            RecurringSeries(
                merchant=str(item["merchant"]),
                account_id="",
                cadence=str(item["cadence"]),
                typical_amount=amount,  # type: ignore[arg-type]
                occurrences=0,
                first_seen=started or today,
                last_seen=started or today,
                # Projected from the start date the user gave, rolled forward
                # so an entry begun months ago still shows a future charge.
                # Without a date there is no anchor and none can be offered.
                next_expected=_project(started, str(item["cadence"]), step, today),
                confidence=1.0,  # the user's own word, not an inference
                amount_varies=False,
                transaction_ids=[],
            )
        )
    return out


def is_manual(series: RecurringSeries) -> bool:
    """True for a series the user entered rather than one detection found."""
    return series.occurrences == 0 and not series.transaction_ids


def manual_kinds(tracked: list[dict[str, object]]) -> dict[str, str]:
    """What the user said each tracked entry is, keyed for the verdict lookup.

    Tracked entries carry their own kind, so they should not fall through to
    the catalog and come back unknown — the user already answered.
    """
    return {
        str(item["merchant"]).upper(): str(item.get("kind") or SUBSCRIPTION) for item in tracked
    }


def apply_overrides(
    series: list[RecurringSeries], overrides: dict[str, dict[str, object]]
) -> list[RecurringSeries]:
    """Return `series` with the user's corrections applied.

    Detection infers an amount, a cadence and a next date from history, and
    history is sometimes a poor guide — a price rose last week, or the billing
    day moved. A correction to one field leaves the others inferred, so the
    rest keeps improving as more charges arrive.

    Confidence is forced to 1.0 on any corrected series: the number describes
    how sure the *detector* is, and once a person has said what the figure is,
    reporting 57% would be describing the wrong thing.
    """
    if not overrides:
        return series

    from dataclasses import replace
    from datetime import date as _date

    from ..core.money import Money

    out: list[RecurringSeries] = []
    for item in series:
        correction = overrides.get(item.merchant.upper())
        if not correction:
            out.append(item)
            continue

        changes: dict[str, object] = {}
        if correction.get("display_name"):
            changes["merchant"] = str(correction["display_name"])
        if correction.get("amount_minor") is not None:
            currency = str(correction.get("currency") or item.typical_amount.currency)
            # Kept as an outflow when the original was, so a corrected
            # subscription does not become income by being edited.
            magnitude = abs(int(correction["amount_minor"]))
            sign = -1 if item.typical_amount.minor < 0 else 1
            changes["typical_amount"] = Money(sign * magnitude, currency)
        if correction.get("cadence"):
            changes["cadence"] = str(correction["cadence"])
        if correction.get("next_expected"):
            # A malformed stored date should not cost the other corrections.
            with contextlib.suppress(ValueError):
                changes["next_expected"] = _date.fromisoformat(str(correction["next_expected"]))

        if changes:
            changes["confidence"] = 1.0
        out.append(replace(item, **changes) if changes else item)
    return out


def subscription_names() -> tuple[str, ...]:
    """Every service the catalogue recognises, for reuse as category rules."""
    return _SUBSCRIPTIONS


def bill_names() -> tuple[str, ...]:
    """Every biller the catalogue recognises."""
    return _BILLS


def _project(started: date | None, cadence: str, step: dict[str, int], today: date) -> date | None:
    """The next charge on or after today, or None without a start date."""
    if started is None:
        return None
    from datetime import timedelta

    days = step.get(cadence, 30)
    when = started
    while when < today:
        when += timedelta(days=days)
    return when
