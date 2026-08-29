"""Telling one charge seen twice from two charges that look alike."""

import uuid
from datetime import date

from carraway.analysis.duplicates import find_duplicates, same_charge
from carraway.core.models import Transaction
from carraway.core.money import Money


def tx(description, amount="-188.45", when=date(2026, 8, 25), account="a1"):
    return Transaction(
        id=uuid.uuid4().hex,
        account_id=account,
        date=when,
        amount=Money.parse(amount),
        description=description,
    )


def test_a_masked_account_number_is_the_same_charge():
    # Chase's CSV shows the full number; SimpleFIN masks it. One payment.
    assert same_charge(
        "CHASE CREDIT CRD AUTOPAY PPD ID: 4760039224",
        "CHASE CREDIT CRD AUTOPAY PPD ID: XXXXXX9224",
    )


def test_punctuation_and_spacing_do_not_make_two_charges():
    assert same_charge("CHECK # 121", "CHECK 121")
    assert same_charge("SQ *BLUE BOTTLE", "SQ*BLUE  BOTTLE")


def test_different_reference_numbers_are_different_charges():
    # The false positive that made this module worth writing carefully: two
    # Venmo payments on one day for one amount differ ONLY in their reference
    # numbers. Stripping digits from both would delete a real payment.
    assert not same_charge(
        "VENMO PAYMENT 1049991103000 WEB ID: 3264681992",
        "VENMO PAYMENT 1050022817724 WEB ID: 3264681992",
    )
    assert not same_charge("ORCA*00X6ZLC 2063985346 WA", "ORCA*00X68Q5 2063985346 WA")


def test_different_merchants_sharing_an_amount_are_left_alone():
    assert not same_charge("HUDSONNEWS ST866 DES PLAINES IL", "AMZ*v0ixccg35 ORD Hudson EAST")


def test_finds_the_masked_pair_and_keeps_the_better_description():
    groups = find_duplicates(
        [
            tx("CHASE CREDIT CRD AUTOPAY PPD ID: XXXXXX9224"),
            tx("CHASE CREDIT CRD AUTOPAY PPD ID: 4760039224"),
        ]
    )
    assert len(groups) == 1
    # The unmasked copy survives: it is the one a person can act on later.
    assert "XXXX" not in groups[0].keep.description
    assert len(groups[0].remove) == 1


def test_identical_descriptions_are_not_our_business():
    # Import-time dedupe handles those, and two genuinely separate identical
    # purchases are indistinguishable from a duplicate. Guessing deletes data.
    assert find_duplicates([tx("BLUE BOTTLE COFFEE"), tx("BLUE BOTTLE COFFEE")]) == []


def test_different_days_or_accounts_never_pair():
    assert find_duplicates([tx("CHECK # 121"), tx("CHECK 121", when=date(2026, 8, 26))]) == []
    assert find_duplicates([tx("CHECK # 121"), tx("CHECK 121", account="a2")]) == []


def test_different_amounts_never_pair():
    assert find_duplicates([tx("CHECK # 121"), tx("CHECK 121", amount="-188.46")]) == []


def test_wasted_reports_what_the_extra_copies_add():
    groups = find_duplicates(
        [
            tx("CHASE CREDIT CRD AUTOPAY PPD ID: XXXXXX9224"),
            tx("CHASE CREDIT CRD AUTOPAY PPD ID: 4760039224"),
        ]
    )
    assert groups[0].wasted == Money.parse("-188.45")
