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


def normalise_merchant(description: str) -> str:
    """Reduce a raw bank description to a stable merchant key.

    >>> normalise_merchant("SQ *BLUE BOTTLE COFFEE #402 05/14 SAN FRANCISCO CA")
    'BLUE BOTTLE COFFEE SAN FRANCISCO'
    >>> normalise_merchant("NETFLIX.COM 866-579-7172 CA")
    'NETFLIX.COM'

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
    text = _PHONE.sub(" ", text)
    text = _DATE_FRAGMENT.sub(" ", text)
    text = _STORE_NUMBER.sub(" ", text)
    text = _REF_CODE.sub(" ", text)
    text = _MASKED_CARD.sub(" ", text)
    text = _LONG_DIGITS.sub(" ", text)
    text = _ORDER_CODE.sub(" ", text)

    text = re.sub(r"[^\w\s.&'-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _TRAILING_STATE.sub("", text).strip()
    return re.sub(r"\s+", " ", text).strip()


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


def _predict_next(last: date, cadence: str, day_of_month: int | None) -> date:
    """Project the next expected charge date from the last one seen."""
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
    groups: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for tx in transactions:
        if tx.is_transfer or tx.pending:
            continue
        if not include_inflows and not tx.is_outflow:
            continue
        key = tx.merchant.upper() if tx.merchant else normalise_merchant(tx.description)
        if not key:
            continue
        groups[(key, tx.account_id)].append(tx)

    results: list[RecurringSeries] = []
    for (merchant, account_id), txs in groups.items():
        if len(txs) < min_occurrences:
            continue
        txs.sort(key=lambda t: t.date)

        gaps = [
            (b.date - a.date).days
            # Ragged by design: pairing each item with its successor.
            for a, b in zip(txs, txs[1:], strict=False)
            if (b.date - a.date).days > 0  # same-day repeats are not a cadence
        ]
        if len(gaps) < min_occurrences - 1:
            continue

        match = _classify_gaps([float(g) for g in gaps])
        if match is None:
            continue
        cadence, regularity = match

        minors = [t.amount.minor for t in txs]
        stability, varies = _amount_stability(minors)

        # Regularity of timing is the stronger signal; amount consistency
        # refines it. More observations also make the pattern more credible.
        evidence = min(len(txs) / 6.0, 1.0)
        confidence = 0.6 * regularity + 0.25 * stability + 0.15 * evidence
        if confidence < min_confidence:
            continue

        # Median resists a one-off price change skewing the typical amount.
        typical = Money(int(statistics.median(sorted(minors))), txs[0].amount.currency)
        day_of_month = (
            int(statistics.median([t.date.day for t in txs])) if cadence == "monthly" else None
        )

        results.append(
            RecurringSeries(
                merchant=merchant.title(),
                account_id=account_id,
                cadence=cadence,
                typical_amount=typical,
                occurrences=len(txs),
                first_seen=txs[0].date,
                last_seen=txs[-1].date,
                next_expected=_predict_next(txs[-1].date, cadence, day_of_month),
                confidence=round(confidence, 3),
                amount_varies=varies,
                transaction_ids=[t.id for t in txs],
            )
        )

    results.sort(key=lambda s: (-s.confidence, -abs(s.annualised.minor)))
    return results


def stale(
    series: list[RecurringSeries], today: date, *, grace_days: int = 10
) -> list[RecurringSeries]:
    """Series whose next charge is overdue — likely cancelled, or worth a look."""
    return [s for s in series if s.next_expected and (today - s.next_expected).days > grace_days]
