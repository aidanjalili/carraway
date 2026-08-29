"""Match a provider's accounts to the ones already in the ledger.

Someone who imported CSV statements before connecting a provider has both, and
the same account under two names: "Chase Checking 6822" from the file and
"CHASE COLLEGE (6822)" from SimpleFIN. Left unlinked they are two accounts, and
because deduplication is scoped per account, every overlapping transaction is
imported a second time. On real data that was 269 duplicates out of 352 rows.

Linking writes the provider's id onto the existing account, so later syncs land
in the account the user already has.

Matching only ever *proposes*. Merging the wrong two accounts is unpleasant to
undo, so a person confirms every link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.models import Account

# The last four digits of an account number, which both a bank's own export and
# a provider tend to include somewhere in the name.
_DIGIT_RUN = re.compile(r"\d{3,}")
_WORDS = re.compile(r"[a-z]+")

# Words that appear in half the account names in existence and so say nothing
# about which account this is.
_NOISE = frozenset(
    {
        "account",
        "accounts",
        "card",
        "cards",
        "bank",
        "the",
        "and",
        "checking",
        "savings",
        "credit",
        "debit",
        "visa",
        "mastercard",
        "signature",
    }
)


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A proposed link between a provider account and a local one."""

    remote: Account
    local: Account | None
    score: float
    reason: str


def _digits(name: str) -> set[str]:
    """Trailing digit groups, normalised to their last four."""
    return {run[-4:] for run in _DIGIT_RUN.findall(name)}


def _words(name: str) -> set[str]:
    return {w for w in _WORDS.findall(name.lower()) if w not in _NOISE and len(w) > 2}


def score(remote: Account, local: Account) -> tuple[float, str]:
    """How confident we are that these are the same account, and why."""
    shared_digits = _digits(remote.name) & _digits(local.name)
    if shared_digits:
        # An account number is close to proof: two unrelated accounts sharing
        # their last four digits at the same institution is rare enough to be
        # worth a confirmation prompt rather than a rejection.
        digits = sorted(shared_digits)[0]
        return 0.95, f"both names contain {digits}"

    shared_words = _words(remote.name) & _words(local.name)
    same_bank = bool(
        remote.institution
        and local.institution
        and remote.institution.lower().split()[0] == local.institution.lower().split()[0]
    )
    if shared_words and same_bank:
        return 0.7, f"same institution, and both mention {sorted(shared_words)[0]}"
    if same_bank and remote.type == local.type:
        return 0.55, f"same institution and both are {remote.type}"
    if shared_words:
        return 0.4, f"both names mention {sorted(shared_words)[0]}"
    return 0.0, "no obvious match"


def suggest(remote_accounts: list[Account], local_accounts: list[Account]) -> list[Suggestion]:
    """Propose a local account for each unlinked provider account.

    Each local account is offered at most once, best match first, so two
    provider accounts cannot both claim the same existing one.
    """
    # Already-linked accounts are settled and must not be re-proposed.
    linked = {a.external_id for a in local_accounts if a.external_id}
    candidates = [a for a in local_accounts if not a.external_id]

    scored: list[tuple[float, str, Account, Account]] = []
    for remote in remote_accounts:
        if remote.external_id in linked:
            continue
        for local in candidates:
            value, reason = score(remote, local)
            if value > 0:
                scored.append((value, reason, remote, local))
    scored.sort(key=lambda row: -row[0])

    taken_local: set[str] = set()
    taken_remote: set[str] = set()
    suggestions: list[Suggestion] = []
    for value, reason, remote, local in scored:
        if remote.external_id in taken_remote or local.id in taken_local:
            continue
        taken_remote.add(remote.external_id)
        taken_local.add(local.id)
        suggestions.append(Suggestion(remote=remote, local=local, score=value, reason=reason))

    for remote in remote_accounts:
        if remote.external_id not in taken_remote and remote.external_id not in linked:
            suggestions.append(
                Suggestion(remote=remote, local=None, score=0.0, reason="no existing match")
            )
    return suggestions
