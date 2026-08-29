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


def test_short_catalog_names_do_not_match_inside_words():
    # Plain substring matching had "AWS" match "MATT LAWS", "MAX" match
    # "OFFICEMAX" and "BOX" match "LIQUOR BOX", so a Zelle payment to a person
    # was reported as a cloud subscription. A confident wrong answer is worse
    # than a miss here: the review flow catches misses, nothing catches this.
    for merchant in [
        "Zelle Payment To Matt Laws",
        "Officemax Depot Madison",
        "Liquor Box La Jolla",
        "Maxwell House Coffee",
        "Boxing Gym Downtown",
    ]:
        assert classify(merchant) == UNKNOWN, merchant

    # The real companies still match when named properly.
    assert classify("AWS Cloud Services") == SUBSCRIPTION
    assert classify("Box.com Storage") == SUBSCRIPTION
    assert classify("HBO Max") == SUBSCRIPTION


def test_long_names_match_inside_run_together_descriptors():
    # Processors concatenate words with no separator, in both directions.
    assert classify("Dd Doordashdashpass") == SUBSCRIPTION
    assert classify("SPOTIFYUSA") == SUBSCRIPTION
    assert classify("NETFLIXCOM") == SUBSCRIPTION


def test_income_only_when_the_money_is_arriving():
    from carraway.analysis.subscriptions import INCOME

    # The same word means different things by direction: a payroll line on an
    # inflow is the user's salary; on an outflow it is a business paying staff.
    assert classify("Epic Systems Cor Payroll Ppd", is_inflow=True) == INCOME
    assert classify("Cash Back Redemption Ref", is_inflow=True) == INCOME
    assert classify("Epic Systems Cor Payroll Ppd", is_inflow=False) != INCOME


def test_person_to_person_is_never_guessed():
    from carraway.analysis.subscriptions import is_person_to_person

    # Whether a recurring Zelle is income, a housemate's rent share or a habit
    # depends on who is at the other end, and only the user knows that.
    for merchant in [
        "Zelle Payment From A Relative",
        "Wise Transfer To A Friend",
        "Cash App Transfer",
        "Paypal Instant Transfer",
    ]:
        assert is_person_to_person(merchant), merchant
        assert classify(merchant, is_inflow=True) == UNKNOWN, merchant

    assert not is_person_to_person("Netflix")


def test_cancelled_is_an_answerable_kind():
    from carraway.analysis.subscriptions import ANSWERABLE, CANCELLED, INCOME

    assert CANCELLED in ANSWERABLE
    assert INCOME in ANSWERABLE
    # A stored cancellation wins over the catalog, which would still say
    # "subscription" for a merchant it recognises.
    assert resolve("Netflix", {"NETFLIX": CANCELLED}) == CANCELLED


def _tracked(merchant, amount="9.99", cadence="monthly", kind="subscription"):
    from carraway.core.money import Money

    return {
        "id": "x",
        "merchant": merchant,
        "amount": Money.parse(f"-{amount}"),
        "cadence": cadence,
        "kind": kind,
        "paid_via": "",
        "notes": "",
        "active": True,
    }


def _detected(merchant, amount="9.99"):
    from datetime import date

    from carraway.core.models import RecurringSeries
    from carraway.core.money import Money

    return RecurringSeries(
        merchant=merchant,
        account_id="a1",
        cadence="monthly",
        typical_amount=Money.parse(f"-{amount}"),
        occurrences=6,
        first_seen=date(2026, 1, 1),
        last_seen=date(2026, 6, 1),
        next_expected=date(2026, 7, 1),
        confidence=0.9,
        amount_varies=False,
        transaction_ids=["t1"],
    )


def test_a_tracked_entry_detection_already_found_is_dropped():
    from carraway.analysis.subscriptions import as_series

    # The user tracks "DashPass"; the bank calls it "DD DOORDASHDASHPASS".
    # Counted separately that is one subscription billed twice.
    kept = as_series([_tracked("DashPass")], [_detected("Dd Doordashdashpass")])
    assert kept == []

    # Something detection genuinely missed still comes through.
    kept = as_series([_tracked("T-Mobile", "35.00")], [_detected("Netflix")])
    assert [s.merchant for s in kept] == ["T-Mobile"]


def test_short_names_are_not_matched_by_containment():
    from carraway.analysis.subscriptions import as_series

    # "AAA" appearing inside an unrelated description is a coincidence, not
    # the same subscription.
    kept = as_series([_tracked("AAA", "67.00", "yearly")], [_detected("MAAAGIC CARPETS")])
    assert [s.merchant for s in kept] == ["AAA"]


def test_tracked_entries_keep_the_kind_the_user_gave_them():
    from carraway.analysis.subscriptions import BILL, manual_kinds, resolve

    kinds = manual_kinds([_tracked("T-Mobile"), _tracked("Rent Share", kind="bill")])
    assert kinds["RENT SHARE"] == BILL
    # And it wins over the catalog, which would not recognise either name.
    assert resolve("Rent Share", kinds) == BILL


def test_a_tracked_series_is_marked_manual():
    from carraway.analysis.subscriptions import as_series, is_manual

    series = as_series([_tracked("T-Mobile", "35.00")])[0]
    assert is_manual(series)
    assert not is_manual(_detected("Netflix"))
