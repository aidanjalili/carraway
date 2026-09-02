"""Matching a provider's accounts to ones the user already has."""

from carraway.core.models import Account, AccountType
from carraway.sync.linking import score, suggest


def local(name, kind=AccountType.CHECKING, institution="", external=""):
    return Account(
        id=name.lower().replace(" ", "")[:12],
        name=name,
        type=kind,
        institution=institution,
        external_id=external,
    )


def test_account_numbers_are_the_strongest_signal():
    # The user's own name and the bank's rarely agree on wording, but both
    # tend to carry the last four digits somewhere.
    value, reason = score(local("CHASE COLLEGE (6822)"), local("Chase Checking 6822"))
    assert value > 0.9
    assert "6822" in reason


def test_institution_and_wording_when_there_are_no_digits():
    remote = local("WELLS FARGO ACTIVE CASH VISA CARD", institution="Wells Fargo")
    existing = local("Wells Fargo Card", institution="Wells Fargo")
    value, _ = score(remote, existing)
    assert 0.5 < value < 0.95


def test_unrelated_accounts_do_not_match():
    value, _ = score(local("CHASE SAVINGS (6571)"), local("Wells Fargo Card"))
    assert value == 0.0


def test_each_local_account_is_claimed_at_most_once():
    # Two provider accounts must not both link to the same existing one, or a
    # ledger ends up merging two genuinely different accounts.
    remotes = [
        local("CHASE COLLEGE (6822)", institution="Chase", external="r1"),
        local("CHASE SAVINGS (6822)", institution="Chase", external="r2"),
    ]
    existing = [local("Chase Checking 6822", institution="Chase")]

    matched = [s for s in suggest(remotes, existing) if s.local]
    assert len(matched) == 1


def test_already_linked_accounts_are_not_re_proposed():
    remotes = [local("CHASE COLLEGE (6822)", external="ext-1")]
    existing = [local("Chase Checking 6822", external="ext-1")]
    assert suggest(remotes, existing) == []


def test_genuinely_new_accounts_are_reported_as_new():
    remotes = [local("Amazon Prime Rewards Visa", external="r9")]
    existing = [local("Chase Checking 6822")]

    proposals = suggest(remotes, existing)
    assert len(proposals) == 1
    assert proposals[0].local is None


def test_noise_words_alone_are_not_a_match():
    # "Card" and "Account" appear in half the account names in existence.
    value, _ = score(local("Some Card Account"), local("Another Card Account"))
    assert value == 0.0


def test_a_brokerage_account_named_for_what_it_holds():
    """Alpaca calls its account "Portfolio Value", which names no account type.

    This was a real miss: the account synced fine, landed as `checking`, and
    so counted as money available to spend.
    """
    from carraway.sync.accounts import classify_account

    assert classify_account("Portfolio Value (0388)") is AccountType.INVESTMENT
    assert (
        classify_account("Individual", institution="Alpaca Markets Login") is AccountType.INVESTMENT
    )


def test_the_institution_never_overrides_a_clear_name():
    """A broker can hold a cash account, so the name is tried first."""
    from carraway.sync.accounts import classify_account

    assert (
        classify_account("Chase Freedom Unlimited (6550)", institution="Robinhood")
        is AccountType.CREDIT_CARD
    )
    assert classify_account("CHASE SAVINGS (6571)", institution="Schwab") is AccountType.SAVINGS


def test_a_plain_bank_says_nothing_about_the_type():
    """Only brokerages are a signal: a bank holds current accounts and cards."""
    from carraway.sync.accounts import classify_account

    assert classify_account("Mystery Account", institution="Chase Bank") is AccountType.CHECKING
