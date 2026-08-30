"""Detect recurring charges — subscriptions, bills, memberships.

This is Carraway's headline feature and the thing no other open-source finance
app does well. The goal is the RocketMoney moment: *"you are paying $47/month
for four things you forgot about."*

The approach is deliberately statistical rather than a hardcoded list of known
merchants, because a merchant list can never cover a local gym or a regional
utility. We instead look for the shape of a subscription:

1. Group transactions by a normalised merchant name.
2. Within a group, look at the gaps between consecutive charges.
3. If those gaps cluster tightly around a known cadence, it recurs.
4. Score how confident we are, and predict the next charge date.

Amount stability is scored but not required: Netflix bills the same figure
every month, while an electricity bill swings wildly and is still obviously
recurring.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date, timedelta

from ..core.models import RecurringSeries, Transaction
from ..core.money import Money

# Cadence -> (expected gap in days, tolerance in days). Tolerance widens with
# the period: a yearly charge landing 10 days late is still plainly yearly,
# but a weekly charge 10 days late is not weekly.
CADENCES: dict[str, tuple[float, float]] = {
    "weekly": (7.0, 2.0),
    "biweekly": (14.0, 3.0),
    "monthly": (30.44, 5.0),  # mean Gregorian month
    "quarterly": (91.31, 10.0),
    "yearly": (365.25, 20.0),
}

MIN_OCCURRENCES = 3  # two charges is a coincidence, three is a pattern

# ...except when they are a year apart. An annual subscription cannot reach
# three charges inside a two-year statement history, so requiring three makes
# every yearly magazine, domain and insurance renewal structurally invisible.
# Two charges 365 days apart is not a coincidence in the way two charges a week
# apart is, so long cadences are allowed to qualify on a single interval.
LONG_CADENCE_MIN_OCCURRENCES = 2
LONG_CADENCES = frozenset({"yearly", "quarterly"})
MIN_CONFIDENCE = 0.55  # below this we assume noise rather than a subscription

# Payment-processor prefixes, store numbers, dates and reference codes that
# banks staple onto a description and that would otherwise split one merchant
# into many groups. Order of application matters and is enforced in
# normalise_merchant(); see the comments there.
_PROCESSOR_PREFIXES = re.compile(
    r"^(SQ|TST|SP|PY|PP|IN|POS|ACH|PAYPAL|VISA|DEBIT|CREDIT|RECURRING|CHECKCARD"
    r"|WEB|PMNT|PURCHASE)[\s*#:\-]+",
    re.IGNORECASE,
)
_PHONE = re.compile(r"\b\d{3}[-. ]?\d{3}[-. ]?\d{4}\b")
_DATE_FRAGMENT = re.compile(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b")
# No leading \b: there is no word boundary between a space and a '#'.
_STORE_NUMBER = re.compile(r"#\s*\w+")
# Each code type must be followed by digits, otherwise "ID" would eat the
# "IDAHO" in "IDAHO POWER".
_REF_CODE = re.compile(r"\b(?:REF|ID|AUTH|TRN|INV)[#:\-]?\d+\b", re.IGNORECASE)
_MASKED_CARD = re.compile(r"\bX{2,}\d+\b", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\b\d{4,}\b")
# Alphanumeric order/reference codes such as Amazon's "2K4LM9DR3". These differ
# per purchase, so leaving them in would fragment one merchant into dozens of
# groups and hide the pattern entirely. Requires 6+ chars containing both a
# letter and a digit, which spares real names like "7-ELEVEN" (the hyphen
# splits it) and "MACYS" (no digit).
_ORDER_CODE = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,}\b")
_TRAILING_STATE = re.compile(r"\s+[A-Z]{2}\s*$")
# A phone number with its country code ("1 8445052993"), and digit runs that
# are phone-shaped but not quite ("186-65797172"). Real statements are full of
# both, and each one left behind a different orphan fragment that split one
# merchant into several.
_PHONE_WITH_COUNTRY = re.compile(r"\b1[-. ]\d{3}[-. ]?\d{3}[-. ]?\d{4}\b")
_DIGIT_RUN = re.compile(r"\b[\d][\d\-. ]{6,}\b")
# Tokens that carry no identity: bare numbers, or numbers with leftover
# punctuation such as the "186-" a mangled phone number leaves behind.
_EMPTY_TOKEN = re.compile(r"^[\d\-.#*]+$")
# Corporate suffixes and TLDs. "NETFLIX.COM", "NETFLIX INC." and "NETFLIX" are
# one company, and without this they are three merchants with three separate
# recurring series, each too weak to be confident about.
_CORP_SUFFIX = {"INC", "INC.", "LLC", "LLC.", "LTD", "LTD.", "CORP", "CORP.", "CO", "CO."}
_TLD = re.compile(r"\.(COM|NET|ORG|IO|CO|US|APP|GOV)$")


def normalise_merchant(description: str) -> str:
    """Reduce a raw bank description to a stable merchant key.

    >>> normalise_merchant("SQ *BLUE BOTTLE COFFEE #402 05/14 SAN FRANCISCO CA")
    'BLUE BOTTLE COFFEE SAN FRANCISCO'
    >>> normalise_merchant("NETFLIX.COM 866-579-7172 CA")
    'NETFLIX'
    >>> normalise_merchant("NETFLIX, INC. 186-65797172 CA")
    'NETFLIX'

    City names survive, because telling "SAN FRANCISCO" from part of a business
    name needs a gazetteer we do not have. That is fine: detection only needs
    the *same* merchant to normalise the *same* way every time, not to look
    perfect. Trailing state codes are stripped since they are unambiguous.
    """
    text = description.upper().strip()

    # Banks happily stack prefixes ("POS DEBIT SQ *..."), so peel repeatedly.
    for _ in range(3):
        stripped = _PROCESSOR_PREFIXES.sub("", text)
        if stripped == text:
            break
        text = stripped

    # Phone numbers must go before the generic long-digit rule, or that rule
    # eats the last block and leaves a "866-579-" stub behind.
    text = _PHONE_WITH_COUNTRY.sub(" ", text)
    text = _PHONE.sub(" ", text)
    text = _DATE_FRAGMENT.sub(" ", text)
    text = _STORE_NUMBER.sub(" ", text)
    text = _REF_CODE.sub(" ", text)
    text = _MASKED_CARD.sub(" ", text)
    text = _DIGIT_RUN.sub(" ", text)
    text = _LONG_DIGITS.sub(" ", text)
    text = _ORDER_CODE.sub(" ", text)

    text = re.sub(r"[^\w\s.&'-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _TRAILING_STATE.sub("", text).strip()

    # Token pass: drop what carries no identity, fold the variants of one
    # company together, and collapse the doubled names some processors emit
    # ("NETFLIX.COM NETFLIX.COM").
    tokens: list[str] = []
    for token in text.split():
        if _EMPTY_TOKEN.match(token) or token in _CORP_SUFFIX:
            continue
        token = _TLD.sub("", token)
        if not token or (tokens and token == tokens[-1]):
            continue
        tokens.append(token)
    return " ".join(tokens)


def _classify_gaps(gaps: list[float]) -> tuple[str, float] | None:
    """Match a list of day-gaps to a cadence, returning (cadence, regularity).

    `regularity` is 1.0 for perfectly even spacing and falls toward 0.0 as the
    gaps scatter relative to the cadence's tolerance.
    """
    if not gaps:
        return None
    median_gap = statistics.median(gaps)

    best: tuple[str, float] | None = None
    for name, (expected, tolerance) in CADENCES.items():
        if abs(median_gap - expected) > tolerance:
            continue
        # Penalise both drift from the expected period and scatter between gaps.
        drift = abs(median_gap - expected) / tolerance
        spread = (
            statistics.median([abs(g - median_gap) for g in gaps]) / tolerance
            if len(gaps) > 1
            else 0.0
        )
        regularity = max(0.0, 1.0 - 0.4 * drift - 0.6 * min(spread, 1.5))
        if best is None or regularity > best[1]:
            best = (name, regularity)
    return best


def _amount_stability(minors: list[int]) -> tuple[float, bool]:
    """Score how consistent the charge amounts are.

    Returns `(stability, varies)`. A fixed-price subscription scores near 1.0;
    a usage-based utility bill scores low and is flagged as varying, which is
    informative rather than disqualifying.
    """
    magnitudes = [abs(m) for m in minors]
    if len(magnitudes) < 2:
        return 1.0, False
    mean = statistics.fmean(magnitudes)
    if mean == 0:
        return 0.0, True
    # Coefficient of variation: standard deviation relative to the mean, so it
    # compares a $10 subscription and a $300 bill on the same scale.
    cv = statistics.pstdev(magnitudes) / mean
    return max(0.0, 1.0 - min(cv / 0.35, 1.0)), cv > 0.12


def advance(last: date, cadence: str, day_of_month: int | None = None) -> date:
    """The next occurrence after `last`, in calendar terms.

    Public because every projection in the app needs the same arithmetic, and
    the naive version — adding 30 days for "monthly" — drifts: a charge on the
    30th walks back to the 29th, then the 28th, and is four days wrong within
    six months.
    """
    if cadence in ("weekly", "biweekly"):
        return last + timedelta(days=int(CADENCES[cadence][0]))
    if cadence == "monthly":
        month, year = last.month + 1, last.year
        if month > 12:
            month, year = 1, year + 1
        target = day_of_month or last.day
        # Clamp for short months: a charge on the 31st becomes the 28th in Feb.
        return date(year, month, min(target, _days_in_month(year, month)))
    if cadence == "quarterly":
        month, year = last.month + 3, last.year
        if month > 12:
            month, year = month - 12, year + 1
        return date(year, month, min(last.day, _days_in_month(year, month)))
    # yearly
    try:
        return last.replace(year=last.year + 1)
    except ValueError:  # 29 February in a non-leap year
        return date(last.year + 1, 3, 1)


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def _build_series(
    txs: list[Transaction],
    merchant: str,
    account_id: str,
    min_occurrences: int,
    min_confidence: float,
) -> RecurringSeries | None:
    """Score one candidate group of charges, or None if it does not recur."""
    # Deliberately the lower of the two floors: which one actually applies
    # depends on the cadence, which is not known until the gaps are measured.
    if len(txs) < min(min_occurrences, LONG_CADENCE_MIN_OCCURRENCES):
        return None
    txs = sorted(txs, key=lambda t: t.date)

    gaps = [
        (b.date - a.date).days
        # Ragged by design: pairing each item with its successor.
        for a, b in zip(txs, txs[1:], strict=False)
        if (b.date - a.date).days > 0  # same-day repeats are not a cadence
    ]
    # A single interval is enough to identify a long cadence, but never a
    # short one — see LONG_CADENCE_MIN_OCCURRENCES.
    floor = min(min_occurrences, LONG_CADENCE_MIN_OCCURRENCES)
    if len(gaps) < floor - 1 or not gaps:
        return None

    match = _classify_gaps([float(g) for g in gaps])
    if match is None:
        return None
    cadence, regularity = match

    required = (
        min(min_occurrences, LONG_CADENCE_MIN_OCCURRENCES)
        if cadence in LONG_CADENCES
        else min_occurrences
    )
    if len(txs) < required:
        return None

    minors = [t.amount.minor for t in txs]
    stability, varies = _amount_stability(minors)

    # Regularity of timing is the stronger signal; amount consistency refines
    # it. More observations also make the pattern more credible.
    evidence = min(len(txs) / 6.0, 1.0)
    confidence = 0.6 * regularity + 0.25 * stability + 0.15 * evidence
    # A two-charge series is real but thinly evidenced, and the confidence
    # figure should say so rather than presenting one interval as proof.
    if len(txs) == 2:
        confidence *= 0.85
    if confidence < min_confidence:
        return None

    # Median resists a one-off price change skewing the typical amount.
    typical = Money(int(statistics.median(sorted(minors))), txs[0].amount.currency)
    day_of_month = (
        int(statistics.median([t.date.day for t in txs])) if cadence == "monthly" else None
    )

    return RecurringSeries(
        merchant=merchant.title(),
        account_id=account_id,
        cadence=cadence,
        typical_amount=typical,
        occurrences=len(txs),
        first_seen=txs[0].date,
        last_seen=txs[-1].date,
        next_expected=advance(txs[-1].date, cadence, day_of_month),
        confidence=round(confidence, 3),
        amount_varies=varies,
        transaction_ids=[t.id for t in txs],
    )


def _fixed_amount_clusters(txs: list[Transaction], min_occurrences: int) -> list[list[Transaction]]:
    """Split a merchant's charges into groups that bill the same amount.

    One merchant often bills for several distinct things. A letting agent takes
    rent every month and parking and fees alongside it; Apple bills every
    subscription under one descriptor. Pooled together the dates look chaotic
    and a perfectly regular charge is buried in the noise.

    Only exact amounts are grouped. Anything whose amount drifts — a utility
    bill, a subscription that changed price — is better served by scoring the
    merchant as a whole, which the caller has already tried.
    """
    by_amount: dict[int, list[Transaction]] = defaultdict(list)
    for tx in txs:
        by_amount[tx.amount.minor].append(tx)
    return [group for group in by_amount.values() if len(group) >= min_occurrences]


def detect(
    transactions: list[Transaction],
    *,
    min_occurrences: int = MIN_OCCURRENCES,
    min_confidence: float = MIN_CONFIDENCE,
    include_inflows: bool = False,
) -> list[RecurringSeries]:
    """Find recurring series in a transaction list, strongest signal first.

    By default only outflows are considered, so a fortnightly paycheck does not
    show up as a subscription. Pass `include_inflows=True` to detect income
    streams too, which is how the income view will eventually be built.
    """
    # Grouped by direction as well as merchant, because a refund is not part of
    # the series it reverses. A cancelled subscription typically produces a
    # cluster of refunds at the same merchant for the same amount; pooled with
    # the charges they cancel out and the median lands on zero, hiding a real
    # subscription behind a $0.00 series.
    groups: dict[tuple[str, str, bool], list[Transaction]] = defaultdict(list)
    for tx in transactions:
        if tx.is_transfer or tx.pending:
            continue
        if not include_inflows and not tx.is_outflow:
            continue
        key = tx.merchant.upper() if tx.merchant else normalise_merchant(tx.description)
        if not key:
            continue
        groups[(key, tx.account_id, tx.is_outflow)].append(tx)

    results: list[RecurringSeries] = []
    for (merchant, account_id, _), txs in groups.items():
        whole = _build_series(txs, merchant, account_id, min_occurrences, min_confidence)
        if whole is not None:
            results.append(whole)
            continue

        # The merchant as a whole does not recur, but one of the things it
        # bills for might. Only reached on failure, so a merchant that already
        # scores well is never split.
        for cluster in _fixed_amount_clusters(txs, min_occurrences):
            found = _build_series(cluster, merchant, account_id, min_occurrences, min_confidence)
            if found is not None:
                results.append(found)

    results.sort(key=lambda s: (-s.confidence, -abs(s.annualised.minor)))
    return results


def stale(
    series: list[RecurringSeries], today: date, *, grace_days: int = 10
) -> list[RecurringSeries]:
    """Series whose next charge is overdue — likely cancelled, or worth a look."""
    return [s for s in series if s.next_expected and (today - s.next_expected).days > grace_days]


def project_from(started: date, cadence: str, today: date | None = None) -> date:
    """The first occurrence on or after today, counting forward from `started`.

    Rolled forward one period at a time rather than multiplied out, so the
    calendar rules — short months, leap days — apply at every step instead of
    only the last.
    """
    today = today or date.today()
    when = started
    # A generous bound: enough to carry a weekly charge forward a decade, and
    # a guard against a cadence this function does not recognise looping.
    for _ in range(600):
        if when >= today:
            return when
        following = advance(when, cadence, started.day if cadence == "monthly" else None)
        if following <= when:
            return when
        when = following
    return when
