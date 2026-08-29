"""Guessing a category, and being honest that it is a guess."""

import uuid
from datetime import date

from carraway.analysis.guess import guess, guess_all
from carraway.core.models import Transaction
from carraway.core.money import Money


def tx(description, amount="-24.00", category="", auto=False):
    return Transaction(
        id=uuid.uuid4().hex,
        account_id="a1",
        date=date(2026, 6, 1),
        amount=Money.parse(amount),
        description=description,
        merchant=description.upper(),
        category=category,
        auto_categorized=auto,
    )


def test_ordinary_words_name_a_kind_of_business():
    assert guess(tx("XI'AN NOODLES SEATTLE WA")).category == "Dining"
    assert guess(tx("KISMET BOOKS VERONA WI")).category == "Shopping"
    assert guess(tx("CITY WATER AND SEWER")).category == "Utilities"


def test_what_the_user_already_filed_wins():
    learned = {"MILLER & SONS VERONA": "Groceries"}
    found = guess(tx("MILLER & SONS VERONA"), learned=learned)
    assert found.category == "Groceries"
    # And it outranks a word hint, because the user is the better authority.
    assert found.confidence > guess(tx("XI'AN NOODLES")).confidence


def test_the_amount_alone_is_never_enough():
    # An earlier version read a small round amount as a likely subscription.
    # On real data that produced 167 guesses, wrong for most of them: a
    # fantasy football pool and a restaurant both charge $20.
    assert guess(tx("MATT LAWS FANTASY FOOTBALL POOL", "-20.00")) is None
    assert guess(tx("SQ *THE TILTED TABLE", "-9.99")) is None


def test_hints_match_on_word_boundaries():
    # "INN" must not match "WINNER", and a gym's "CLUB FEES" is a membership
    # rather than a bank fee.
    assert (
        guess(tx("WINNERS CIRCLE SUPPLY")) is None
        or guess(tx("WINNERS CIRCLE SUPPLY")).category != "Travel"
    )
    found = guess(tx("ANYTIME FIT ABC CLUB FEES PPD"))
    assert found is None or found.category != "Fees"


def test_a_guess_never_trains_the_next_guess():
    # One wrong answer must not propagate through the ledger unchallenged.
    history = [tx("LOCAL SPOT", category="Travel", auto=True)]
    assert guess_all(history + [tx("LOCAL SPOT")], ["Travel", "Uncategorized"]) == {}


def test_only_uncategorised_rows_are_guessed_at():
    rows = [tx("XI'AN NOODLES"), tx("XI'AN NOODLES")]
    guesses = guess_all(rows, ["Groceries", "Uncategorized"])
    # The rule match is left alone; only the gap is filled.
    assert rows[0].id not in guesses
    assert rows[1].id in guesses
