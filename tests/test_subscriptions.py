"""Telling a subscription from a bill from a habit."""

from carraway.analysis.subscriptions import BILL, HABIT, SUBSCRIPTION, UNKNOWN, classify, resolve


def test_known_subscriptions():
    for merchant in [
        "Netflix",
        "Spotify Usa",
        "Icloud Storage",
        "Digitalocean",
        "Anthropic",
        "Adobe Creative Cloud",
        "Dd Doordashdashpass",
        "Anytime Fit Abc Club Fees",
        "Xbox Game Pass",
    ]:
        assert classify(merchant) == SUBSCRIPTION, merchant


def test_known_bills():
    for merchant in [
        "Mill District Ap Rent Web",
        "Madison Gas El Billpay",
        "Xcel Energy-Mn",
        "Usaa Insurance Paymen",
        "Spectrum Ppd",
        "Northfield Mn Utl Tel",
    ]:
        assert classify(merchant) == BILL, merchant


def test_unrecognised_merchants_stay_unknown():
    # A catalog can never be complete. Guessing here would be worse than
    # admitting it and asking, because a wrong guess is never revisited.
    for merchant in ["Down Town Tobacco Northfield", "Mojoch London", "Miller & Sons Verona"]:
        assert classify(merchant) == UNKNOWN, merchant


def test_longest_catalog_match_wins():
    # "AMAZON PRIME" is a subscription; a plain Amazon purchase is not, and
    # must not be dragged in by a shorter prefix.
    assert classify("Amazon Prime Membership") == SUBSCRIPTION


def test_gyms_are_subscriptions_not_bills():
    # "MEMBERSHIP" reads bill-shaped, but a gym is the single most commonly
    # forgotten cancellable thing there is.
    assert classify("Planet Fitness Club Fees") == SUBSCRIPTION
    assert classify("Ymca Of Dane County") == SUBSCRIPTION


def test_stored_verdict_overrides_the_catalog():
    # The person reading their own statement is the authority, not the list.
    verdicts = {"NETFLIX": HABIT, "DOWN TOWN TOBACCO": SUBSCRIPTION}
    assert resolve("Netflix", verdicts) == HABIT
    assert resolve("Down Town Tobacco", verdicts) == SUBSCRIPTION
    # A merchant with no stored answer still falls through to the catalog.
    assert resolve("Spotify", verdicts) == SUBSCRIPTION
    assert resolve("Mojoch London", verdicts) == UNKNOWN


def test_resolve_without_any_verdicts():
    assert resolve("Netflix") == SUBSCRIPTION
    assert resolve("Netflix", {}) == SUBSCRIPTION


def test_magazines_and_publishers():
    # Magazines bill yearly, so they are the easiest recurring charge to
    # forget — and the publisher on the statement rarely matches the title.
    for merchant in [
        "Inst Xfer Conde Nast Web",
        "Natgeo Mag",
        "Tim Time Magazine",
        "The Atlantic Www.Theatlantdc",
        "The Free Press",
        "Hearst Magazines",
        "Consumer Reports",
    ]:
        assert classify(merchant) == SUBSCRIPTION, merchant


def test_periodical_hint_catches_unnamed_publishers():
    # No catalog of publishers can be complete, but a descriptor saying
    # "MAGAZINE" or "SUBSCRIPTION" is the merchant telling us outright.
    assert classify("Some Local Magazine Co") == SUBSCRIPTION
    assert classify("Obscure Quarterly Subscription") == SUBSCRIPTION
    # The hint must not drag in businesses that merely sound similar.
    assert classify("Crescendo Espresso Bar Madison") == UNKNOWN
    assert classify("Holiday Inn Express Suites") == UNKNOWN
