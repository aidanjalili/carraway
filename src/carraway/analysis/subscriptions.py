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

import re
from typing import Literal

Kind = Literal["subscription", "bill", "habit", "unknown"]

SUBSCRIPTION: Kind = "subscription"
BILL: Kind = "bill"
HABIT: Kind = "habit"
UNKNOWN: Kind = "unknown"

# Matched as substrings against the normalised merchant. Names are held in the
# form normalisation leaves them in: uppercase, no ".COM", no corporate suffix.
_SUBSCRIPTIONS: tuple[str, ...] = (
    # streaming video
    "NETFLIX",
    "HULU",
    "DISNEY",
    "MAX",
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
    "BOX",
    "MEGA",
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
    "NPM",
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
    "MINT MOBILE",
    "GOOGLE FI",
    "VISIBLE",
    "CRICKET WIRELESS",
    "BOOST MOBILE",
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


def _matches(merchant: str, names: tuple[str, ...]) -> str | None:
    upper = merchant.upper()
    # Longest first, so "AMAZON PRIME" wins over a hypothetical "AMAZON".
    for name in sorted(names, key=len, reverse=True):
        if name in upper:
            return name
    return None


def classify(merchant: str) -> Kind:
    """What kind of recurring thing this merchant is, from the catalog alone.

    >>> classify("Netflix")
    'subscription'
    >>> classify("Madison Gas El Billpay Ppd")
    'bill'
    >>> classify("Down Town Tobacco Northfield")
    'unknown'

    Subscriptions are checked first: a gym is a subscription even though
    "MEMBERSHIP" reads like a bill, and Spectrum is a bill even though it
    streams. Anything unmatched is `unknown`, which is what the review flow
    exists to resolve.
    """
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


def resolve(merchant: str, verdicts: dict[str, str] | None = None) -> Kind:
    """The kind of a merchant, preferring the user's own answer to the catalog.

    A stored verdict always wins. The catalog is a starting guess, and being
    overruled by the person looking at their own statement is the point rather
    than a failure.
    """
    if verdicts:
        stored = verdicts.get(merchant.upper())
        if stored in (SUBSCRIPTION, BILL, HABIT):
            return stored  # type: ignore[return-value]
    return classify(merchant)
