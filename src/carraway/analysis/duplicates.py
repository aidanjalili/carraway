"""Find the same transaction imported twice from two different sources.

Import deduplication fingerprints a transaction's raw description, which works
perfectly within one source and not at all across two. The same Chase autopay
arrives as "CHASE CREDIT CRD AUTOPAY PPD ID: 4760039224" in a CSV export and
"...XXXXXX9224" from SimpleFIN, because the two mask the account number
differently. Same money, two rows, and every total silently doubles it.

The judgement this module exists to make: two charges at one merchant, on one
day, for one amount are *sometimes* two real charges. Two $3 transit taps in a
morning are ordinary. So a pair is only called a duplicate when the sources
disagree about the description while agreeing about everything that matters —
which is precisely the fingerprint of a masking difference, and precisely not
the fingerprint of buying two coffees.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from ..core.models import Transaction

# Runs of X or * that a bank substitutes for digits it will not show.
_MASK = re.compile(r"[X*]{2,}\d*", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]")


def loosely(description: str) -> str:
    """A description with only punctuation and spacing normalised.

    Enough to see that "CHECK # 121" and "CHECK 121" are one payment, and not
    nearly enough to confuse two different reference numbers.
    """
    return " ".join(_PUNCT.sub(" ", description.upper()).split())


def is_masked(description: str) -> bool:
    """True when a bank has hidden part of a number behind X or *."""
    return _MASK.search(description) is not None


def without_numbers(description: str) -> str:
    """The description with every digit run removed.

    >>> without_numbers("CHASE CREDIT CRD AUTOPAY PPD ID: XXXXXX9224")
    'CHASE CREDIT CRD AUTOPAY PPD ID'

    Only ever compared between a masked description and an unmasked one. Two
    unmasked descriptions must never be compared this way: two payments to
    one payee on one day for one amount differ *only* in their reference
    numbers, and stripping those makes two real payments look like one.
    """
    return " ".join(
        _PUNCT.sub(" ", re.sub(r"\d+", " ", _MASK.sub(" ", description.upper()))).split()
    )


def same_charge(left: str, right: str) -> bool:
    """Whether two descriptions are two renderings of one charge.

    The whole judgement of this module. Two sources disagreeing about how much
    of an account number to show is a duplicate; two sources agreeing on the
    format but disagreeing on the digits is two different charges.
    """
    if loosely(left) == loosely(right):
        return True
    # Exactly one side masked: the digits the other shows are the ones this
    # one is hiding, so ignoring digits compares what is left.
    if is_masked(left) != is_masked(right):
        return without_numbers(left) == without_numbers(right)
    return False


@dataclass(slots=True)
class DuplicateGroup:
    """Rows believed to be one transaction imported more than once."""

    keep: Transaction
    remove: list[Transaction]
    reason: str

    @property
    def wasted(self):
        """What the extra copies add to totals that should not be there."""
        total = self.keep.amount
        for extra in self.remove:
            total = total + extra.amount
        return total - self.keep.amount


def _informative(transaction: Transaction) -> tuple[int, int, str]:
    """Rank rows so the most useful description survives a merge.

    An unmasked number beats a masked one, and a longer description beats a
    shorter one, because the copy kept is the one a person will read later.
    """
    masked = 1 if _MASK.search(transaction.description) else 0
    return (masked, -len(transaction.description), transaction.id)


def find_duplicates(transactions: list[Transaction]) -> list[DuplicateGroup]:
    """Group rows that look like the same charge seen by two sources.

    Deliberately conservative. Rows whose descriptions are already identical
    are left alone: those are handled at import time, and two genuinely
    separate identical purchases are indistinguishable from a duplicate, so
    guessing there would delete real data.
    """
    buckets: dict[tuple[str, str, int], list[Transaction]] = defaultdict(list)
    for transaction in transactions:
        key = (transaction.account_id, transaction.date.isoformat(), transaction.amount.minor)
        buckets[key].append(transaction)

    groups: list[DuplicateGroup] = []
    for rows in buckets.values():
        if len(rows) < 2:
            continue

        # Compared pairwise rather than by a shared key: "same charge" is not
        # transitive, and a key would silently merge a chain of rows that no
        # two of which actually match.
        used: set[str] = set()
        clusters: list[list[Transaction]] = []
        for index, row in enumerate(rows):
            if row.id in used:
                continue
            cluster = [row]
            for other in rows[index + 1 :]:
                if other.id in used:
                    continue
                if same_charge(row.description, other.description):
                    cluster.append(other)
                    used.add(other.id)
            if len(cluster) > 1:
                used.add(row.id)
                clusters.append(cluster)

        for shape_rows in clusters:
            raw = {" ".join(r.description.split()).upper() for r in shape_rows}
            if len(raw) < 2:
                # Identical text is import-time dedupe's business, and two real
                # identical purchases look exactly the same. Not ours to judge.
                continue
            ordered = sorted(shape_rows, key=_informative)
            groups.append(
                DuplicateGroup(
                    keep=ordered[0],
                    remove=ordered[1:],
                    reason="same account, date and amount; descriptions differ only by masking",
                )
            )

    groups.sort(key=lambda g: (g.keep.date, g.keep.id), reverse=True)
    return groups
