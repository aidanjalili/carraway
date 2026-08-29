"""Match the two halves of a transfer between your own accounts.

Moving $500 from checking to savings lands in the ledger as *two* rows: a
-$500 outflow on checking and a +$500 inflow on savings. Left alone, the app
reports $500 of spending and $500 of income that never happened, and every
total downstream — monthly spend, category breakdowns, income — is wrong by
double the transfer. Paying a credit card is the same shape and is by far the
most common case: -$X on checking, +$X on the card.

The whole module is biased toward **precision over recall**. A missed transfer
costs the user one row they can group by hand; a wrong pair silently corrupts
the totals of two accounts at once and looks exactly like correct output. So a
pair must carry an explicit transfer-shaped description before we will consider
it at all — an equal-and-opposite amount landing in the same week is not, on
its own, distinguishable from a $50 purchase that happens to coincide with a
$50 transfer.

Three signals are combined into a confidence score:

* **Amount** (0.45) — equal and opposite is the only near-proof we have.
* **Dates** (0.25) — same day is typical, but ACH settles with a lag, so a
  window of a few days is allowed and scored down as it widens.
* **Wording** (0.30) — "TRANSFER", "PAYMENT THANK YOU" and friends. Also a
  hard gate: no transfer wording, no pair.

Matching is greedy best-first, so each transaction ends up in at most one pair
and the most convincing pairs claim their halves before weaker ones can.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from ..core.models import Transaction
from ..core.money import Money

MIN_CONFIDENCE = 0.6  # below this we would rather leave the rows ungrouped
MAX_DAYS = 4  # ACH between institutions clears in 1-3 business days; 4 covers a weekend

# Weights for the three signals, summing to 1.0. Amount dominates because it is
# the only signal that comes close to proof; dates and wording corroborate it.
_W_AMOUNT = 0.45
_W_DATE = 0.25
_W_KEYWORD = 0.30

# A fee taken in transit is the only reason two halves of one transfer should
# ever differ, so the allowance is deliberately mean: 1% of the amount, capped
# at $25 (about the worst outgoing domestic wire fee). On a $50 transfer that
# is 50 cents, which keeps $48 and $50 firmly unmatched — loosening this is the
# quickest way to start pairing unrelated transactions of similar size.
_FEE_RATE_DIVISOR = 100  # 1%
_FEE_CAP_MINOR = 2500

# Wording that only appears when money moves between accounts you own. Zelle,
# Venmo, PayPal and Cash App are deliberately absent: they move money to other
# *people* just as often as between your own accounts, so treating them as
# transfer wording would erase real spending.
_STRONG_KEYWORDS = re.compile(
    r"""
    \bTRANSFER\b | \bXFER\b | \bTFR\b | \bTRNSFR\b
    | \b(?:TO|FROM)\s+(?:SAVINGS|CHECKING|SHARE|MONEY\s?MARKET)\b
    | \b(?:ONLINE|INTERNET|MOBILE|TELEPHONE)\s+BANKING\b
    | \bPAYMENT\s*[-–—:]?\s*THANK\s*YOU\b
    | \b(?:CREDIT\s+CARD|CARD)\s+(?:PAYMENT|PMT)\b
    | \bCARDMEMBER\b | \bAUTO\s?-?PAY\b | \bBILL\s?PAY\b | \bE-?PAY(?:MENT)?\b
    | \b(?:ONLINE|MOBILE|ELECTRONIC)\s+(?:PAYMENT|PMT)\b
    | \bWIRE\s+(?:TRANSFER|OUT|IN)\b
    | \bOVERDRAFT\s+PROTECTION\b
    """,
    re.VERBOSE,
)

# Rails and generic verbs that are consistent with a transfer but appear on
# ordinary spending too ("ACH DEBIT COMCAST"). They can only ever add to a pair
# that already has strong wording on its other half, never create one.
_WEAK_KEYWORDS = re.compile(
    r"\bACH\b | \bDEPOSIT\b | \bWITHDRAWAL\b | \bPAYMENT\b | \bFUNDS\b | \bINTERNAL\b",
    re.VERBOSE,
)


def _keyword_tier(description: str) -> int:
    """Rate a description's transfer wording: 2 strong, 1 weak, 0 none.

    >>> _keyword_tier("ONLINE BANKING TRANSFER TO SAVINGS")
    2
    >>> _keyword_tier("ACH DEBIT")
    1
    >>> _keyword_tier("SQ *BLUE BOTTLE COFFEE")
    0
    """
    text = description.upper()
    if _STRONG_KEYWORDS.search(text):
        return 2
    if _WEAK_KEYWORDS.search(text):
        return 1
    return 0


def _keyword_score(out_tier: int, in_tier: int) -> float:
    """Score the wording of both halves together. 0.0 means "do not pair".

    At least one half must speak plainly about a transfer. Banks label the two
    legs asymmetrically all the time — "ONLINE PAYMENT" on checking against a
    bare "PAYMENT THANK YOU" on the card — so demanding it on both sides would
    lose the most common case of all.
    """
    tiers = sorted((out_tier, in_tier))
    if tiers == [2, 2]:
        return 1.0
    if tiers[1] < 2:
        return 0.0
    return 0.75 if tiers[0] == 1 else 0.6


def _fee_allowance(sent: Money) -> Money:
    """The largest shortfall we will still call a fee rather than a mismatch."""
    return Money(min(abs(sent.minor) // _FEE_RATE_DIVISOR, _FEE_CAP_MINOR), sent.currency)


def _amount_score(outflow: Transaction, inflow: Transaction) -> tuple[float, Money] | None:
    """Score how well the amounts line up, or None if they cannot be a pair.

    Returns `(score, fee)`. Only a *shortfall* is tolerated: money can go
    missing in transit to a fee, but more money arriving than left is interest,
    a refund or a coincidence — never one transfer.
    """
    sent = -outflow.amount
    received = inflow.amount
    if sent.currency != received.currency:
        return None

    fee = sent - received
    if fee.minor < 0:
        return None
    if fee.minor == 0:
        return 1.0, fee

    allowance = _fee_allowance(sent)
    if fee.minor > allowance.minor:
        return None
    # Degrade with the size of the discrepancy so an exact partner always wins
    # the greedy pass against a merely tolerable one.
    return 1.0 - 0.4 * (fee.minor / allowance.minor), fee


def _date_score(days_apart: int, max_days: int) -> float:
    """Same-day scores 1.0, falling to 0.5 at the edge of the window."""
    if max_days <= 0:
        return 1.0
    return 1.0 - 0.5 * (days_apart / max_days)


@dataclass(slots=True)
class TransferPair:
    """Two transactions believed to be the halves of one transfer."""

    outflow: Transaction
    inflow: Transaction
    confidence: float  # 0.0-1.0, how sure we are these belong together
    fee: Money  # what went missing in transit; zero for an exact match
    reason: str  # why we paired them, for a --explain view and for debugging

    @property
    def days_apart(self) -> int:
        return abs((self.inflow.date - self.outflow.date).days)

    @property
    def transaction_ids(self) -> list[str]:
        return [self.outflow.id, self.inflow.id]


def score_pair(
    outflow: Transaction, inflow: Transaction, *, max_days: int = MAX_DAYS
) -> TransferPair | None:
    """Score one candidate pair, or None if it is not a credible transfer.

    Exposed separately from `find_transfers` so the scoring rules can be tested
    directly rather than inferred from which pairs survived matching.
    """
    if not outflow.is_outflow or inflow.is_outflow or not inflow.amount:
        return None
    # A transfer moves money *between* accounts. Two rows on one account are at
    # best a correction, and pairing them would hide real activity.
    if outflow.account_id == inflow.account_id:
        return None

    days_apart = abs((inflow.date - outflow.date).days)
    if days_apart > max_days:
        return None

    amounts = _amount_score(outflow, inflow)
    if amounts is None:
        return None
    amount_score, fee = amounts

    words = _keyword_score(_keyword_tier(outflow.description), _keyword_tier(inflow.description))
    if words == 0.0:
        return None

    confidence = (
        _W_AMOUNT * amount_score + _W_DATE * _date_score(days_apart, max_days) + _W_KEYWORD * words
    )
    if confidence < MIN_CONFIDENCE:
        return None

    amount_note = "exact amount" if not fee else "amount short by " + fee.format()
    word_note = "transfer wording on both sides" if words == 1.0 else "transfer wording on one side"
    reason = f"{amount_note}, {days_apart}d apart, {word_note}"
    return TransferPair(
        outflow=outflow,
        inflow=inflow,
        confidence=round(confidence, 3),
        fee=fee,
        reason=reason,
    )


def find_transfers(
    transactions: list[Transaction], *, max_days: int = MAX_DAYS
) -> list[TransferPair]:
    """Find transfer pairs in a transaction list, most convincing first.

    Every transaction appears in at most one pair. Candidates are scored, then
    claimed greedily best-first: the strongest pair takes its two halves out of
    circulation before any weaker candidate can reach them. That ordering is
    what stops a coincidental same-amount purchase from stealing the half of a
    transfer that a genuine partner wanted.

    Transactions already carrying a `transfer_group` are skipped, so running
    this over a ledger a second time neither re-pairs nor re-splits them.
    """
    inflows_by_date: dict[date, list[Transaction]] = defaultdict(list)
    outflows: list[Transaction] = []
    for tx in transactions:
        if tx.is_transfer or not tx.amount:
            continue
        if tx.is_outflow:
            outflows.append(tx)
        else:
            inflows_by_date[tx.date].append(tx)

    # Bucketing inflows by date keeps this near-linear on a real ledger; a plain
    # nested loop is O(n^2) and a decade of history is a lot of transactions.
    candidates: list[TransferPair] = []
    for outflow in outflows:
        for offset in range(-max_days, max_days + 1):
            # The window runs both ways: the credit sometimes posts before the
            # debit clears, especially across two institutions.
            for inflow in inflows_by_date.get(outflow.date + timedelta(days=offset), ()):
                pair = score_pair(outflow, inflow, max_days=max_days)
                if pair is not None:
                    candidates.append(pair)

    # Ties are broken on the tighter date gap and then on ids, so the result is
    # deterministic rather than dependent on input order.
    candidates.sort(key=lambda p: (-p.confidence, p.days_apart, p.outflow.id, p.inflow.id))

    claimed: set[str] = set()
    pairs: list[TransferPair] = []
    for pair in candidates:
        if pair.outflow.id in claimed or pair.inflow.id in claimed:
            continue
        claimed.update(pair.transaction_ids)
        pairs.append(pair)
    return pairs


def apply_transfer_groups(transactions: list[Transaction], pairs: list[TransferPair]) -> int:
    """Stamp a shared `transfer_group` on both halves of each pair.

    Returns the number of transactions marked. Halves are looked up by id in
    `transactions` so this works on the caller's own objects even when the pairs
    were built from a different list of them.

    A pair is skipped whole if either half already belongs to a group: stamping
    only one half would orphan its existing partner, quietly putting a leg of an
    older transfer back into the spending totals.
    """
    by_id = {tx.id: tx for tx in transactions}
    marked = 0
    for pair in pairs:
        outflow = by_id.get(pair.outflow.id)
        inflow = by_id.get(pair.inflow.id)
        if outflow is None or inflow is None or outflow.is_transfer or inflow.is_transfer:
            continue
        group = uuid.uuid4().hex
        outflow.transfer_group = group
        inflow.transfer_group = group
        marked += 2
    return marked
