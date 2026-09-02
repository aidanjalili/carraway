"""Telling a subscription from a bill from a habit."""

from datetime import date

from carraway.analysis import subscriptions as subs
from carraway.analysis.subscriptions import (
    BILL,
    HABIT,
    SUBSCRIPTION,
    UNKNOWN,
    classify,
    resolve,
)
from carraway.core.money import Money


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


def test_dismissed_is_answerable_but_never_counted():
    from carraway.analysis.subscriptions import ANSWERABLE, COUNTED, DISMISSED, resolve

    # A user can say "detection got this wrong", and that answer sticks — but
    # a dismissed series must never reach a total.
    assert DISMISSED in ANSWERABLE
    assert DISMISSED not in COUNTED
    assert resolve("Netflix", {"NETFLIX": DISMISSED}) == DISMISSED


def test_corrections_replace_only_the_fields_given():
    from datetime import date

    from carraway.analysis.subscriptions import apply_overrides
    from carraway.core.money import Money

    original = _detected("Netflix", "8.43")
    corrected = apply_overrides(
        [original],
        {"NETFLIX": {"amount_minor": 2499, "currency": "USD", "next_expected": "2026-10-01"}},
    )[0]

    assert corrected.typical_amount == Money.parse("-24.99")
    assert corrected.next_expected == date(2026, 10, 1)
    # Untouched fields stay inferred, so they keep improving with more charges.
    assert corrected.cadence == original.cadence
    assert corrected.merchant == original.merchant


def test_a_corrected_amount_keeps_its_direction():
    from carraway.analysis.subscriptions import apply_overrides

    # Editing a subscription must not turn it into income.
    corrected = apply_overrides(
        [_detected("Netflix", "8.43")], {"NETFLIX": {"amount_minor": 2499}}
    )[0]
    assert corrected.typical_amount.minor < 0


def test_correcting_a_series_makes_it_certain():
    from carraway.analysis.subscriptions import apply_overrides

    # Confidence describes how sure the detector is. Once a person has stated
    # the figure, reporting 57% would be describing the wrong thing.
    corrected = apply_overrides(
        [_detected("Netflix", "8.43")], {"NETFLIX": {"amount_minor": 2499}}
    )[0]
    assert corrected.confidence == 1.0


def test_a_series_with_no_correction_is_returned_untouched():
    from carraway.analysis.subscriptions import apply_overrides

    original = _detected("Netflix", "8.43")
    assert apply_overrides([original], {"SPOTIFY": {"amount_minor": 100}})[0] is original
    assert apply_overrides([original], {})[0] is original


def test_a_start_date_projects_the_next_charge():
    from datetime import date, timedelta

    from carraway.analysis.subscriptions import as_series

    # Without a start date a tracked entry has a cadence but no anchor, so no
    # charge can be projected and it cannot appear in Upcoming at all.
    started = date.today() - timedelta(days=45)
    entry = _tracked("Gym", "29.54", "monthly")
    entry["started_on"] = started

    series = as_series([entry])[0]
    assert series.next_expected is not None
    # Rolled forward, so a start date months old still projects a future date.
    assert series.next_expected >= date.today()
    assert series.first_seen == started


def test_without_a_start_date_no_charge_is_projected():
    from carraway.analysis.subscriptions import as_series

    entry = _tracked("Gym", "29.54", "monthly")
    entry["started_on"] = None
    assert as_series([entry])[0].next_expected is None


def test_a_tracked_entry_carries_the_account_that_pays_for_it():
    # A detected series gets account_id from the transactions it was found in.
    # A tracked one has none, so the user's answer fills the same field —
    # otherwise every screen that groups or labels by account skips them.
    tracked = [
        {
            "merchant": "Gym",
            "amount": Money.parse("-29.54"),
            "cadence": "monthly",
            "kind": "subscription",
            "paid_via_account": "wf",
            "started_on": date(2026, 8, 30),
        }
    ]
    series = subs.as_series(tracked)
    assert series[0].account_id == "wf"


def test_a_route_with_no_account_leaves_the_field_empty():
    # "venmo to dad" is not an account id and must never be mistaken for one.
    tracked = [
        {
            "merchant": "Phone",
            "amount": Money.parse("-35.00"),
            "cadence": "monthly",
            "kind": "subscription",
            "paid_via": "venmo to dad",
            "started_on": None,
        }
    ]
    series = subs.as_series(tracked)
    assert series[0].account_id == ""


# -- a corrected date is an anchor, not a pin ---------------------------


def _series(merchant="AllTrails", cadence="yearly", next_expected=None):
    from datetime import date as _d

    from carraway.core.models import RecurringSeries

    return RecurringSeries(
        merchant=merchant,
        account_id="",
        cadence=cadence,
        typical_amount=Money.parse("-36.00"),
        occurrences=0,
        first_seen=_d(2025, 1, 1),
        last_seen=_d(2025, 1, 1),
        next_expected=next_expected,
        confidence=1.0,
        amount_varies=False,
        transaction_ids=[],
    )


def test_a_corrected_date_in_the_past_rolls_forward():
    """The bug this exists for: two yearly subscriptions stuck on dates that
    had been and gone, and would have stayed stuck for good."""
    from datetime import date

    from carraway.analysis.subscriptions import apply_overrides

    out = apply_overrides(
        [_series()],
        {"ALLTRAILS": {"next_expected": "2026-08-09"}},
        today=date(2026, 9, 1),
    )
    assert out[0].next_expected == date(2027, 8, 9)


def test_it_keeps_rolling_as_the_calendar_moves():
    """The same stored anchor gives a different answer on a later day."""
    from datetime import date

    from carraway.analysis.subscriptions import apply_overrides

    override = {"NETFLIX": {"next_expected": "2026-01-15"}}
    for when, wanted in (
        (date(2026, 1, 14), date(2026, 1, 15)),  # not yet
        (date(2026, 1, 15), date(2026, 1, 15)),  # today
        (date(2026, 1, 16), date(2026, 2, 15)),  # the day after
        (date(2026, 7, 3), date(2026, 7, 15)),
        (date(2028, 2, 20), date(2028, 3, 15)),  # years later
    ):
        out = apply_overrides([_series("Netflix", "monthly")], override, today=when)
        assert out[0].next_expected == wanted, f"on {when}"


def test_a_future_correction_is_left_exactly_as_set():
    from datetime import date

    from carraway.analysis.subscriptions import apply_overrides

    out = apply_overrides(
        [_series()],
        {"ALLTRAILS": {"next_expected": "2027-04-22"}},
        today=date(2026, 9, 1),
    )
    assert out[0].next_expected == date(2027, 4, 22)


def test_a_corrected_cadence_is_what_the_date_rolls_by():
    from datetime import date

    from carraway.analysis.subscriptions import apply_overrides

    out = apply_overrides(
        [_series("Gym", "yearly")],
        {"GYM": {"next_expected": "2026-01-10", "cadence": "monthly"}},
        today=date(2026, 9, 1),
    )
    assert out[0].cadence == "monthly"
    assert out[0].next_expected == date(2026, 9, 10)


def test_a_malformed_stored_date_still_leaves_the_other_corrections():
    from datetime import date

    from carraway.analysis.subscriptions import apply_overrides

    out = apply_overrides(
        [_series()],
        {"ALLTRAILS": {"next_expected": "not-a-date", "display_name": "AllTrails+"}},
        today=date(2026, 9, 1),
    )
    assert out[0].merchant == "AllTrails+"


def test_the_stored_anchor_is_never_rewritten():
    """Recomputing on read is what keeps this working with no writes."""
    from datetime import date

    from carraway.analysis.subscriptions import apply_overrides

    overrides = {"ALLTRAILS": {"next_expected": "2026-08-09"}}
    apply_overrides([_series()], overrides, today=date(2030, 1, 1))
    assert overrides["ALLTRAILS"]["next_expected"] == "2026-08-09"
