"""Tests for transfer matching - the thing that stops totals being double-counted."""

import uuid
from datetime import date

from carraway.analysis.recurring import detect, normalise_merchant
from carraway.analysis.transfers import apply_transfer_groups, find_transfers, score_pair
from carraway.core.models import Transaction
from carraway.core.money import Money


def make_tx(day: date, amount: str, description: str, account="checking") -> Transaction:
    return Transaction(
        id=uuid.uuid4().hex,
        account_id=account,
        date=day,
        amount=Money.parse(amount),
        description=description,
        merchant=normalise_merchant(description),
    )


def test_matches_a_clean_checking_to_savings_transfer():
    out = make_tx(date(2026, 3, 4), "-500.00", "ONLINE BANKING TRANSFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 4), "500.00", "ONLINE BANKING TRANSFER FROM CHECKING", "savings")

    pairs = find_transfers([out, inn])

    assert len(pairs) == 1
    assert pairs[0].outflow is out
    assert pairs[0].inflow is inn
    assert pairs[0].fee == Money.zero()
    assert pairs[0].days_apart == 0
    assert pairs[0].confidence > 0.95


def test_matches_a_credit_card_payment():
    # The most common real transfer of all: -$X on checking, +$X on the card.
    # The two legs are worded completely differently by the two institutions.
    out = make_tx(date(2026, 3, 12), "-450.00", "ONLINE PAYMENT TO CHASE CARD 4412")
    inn = make_tx(date(2026, 3, 14), "450.00", "PAYMENT - THANK YOU", "visa")

    pairs = find_transfers([out, inn])

    assert len(pairs) == 1
    assert pairs[0].transaction_ids == [out.id, inn.id]
    assert pairs[0].confidence > 0.85


def test_settlement_lag_inside_the_window_still_matches():
    out = make_tx(date(2026, 3, 2), "-1200.00", "XFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 5), "1200.00", "TRANSFER FROM CHECKING", "savings")

    pairs = find_transfers([out, inn])

    assert len(pairs) == 1
    assert pairs[0].days_apart == 3
    # Wider gaps are worth less, so a same-day partner would outrank this one.
    assert pairs[0].confidence < 1.0


def test_a_gap_beyond_max_days_is_not_matched():
    out = make_tx(date(2026, 3, 2), "-1200.00", "XFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 9), "1200.00", "TRANSFER FROM CHECKING", "savings")

    assert find_transfers([out, inn]) == []
    # ...unless the caller widens the window deliberately.
    assert len(find_transfers([out, inn], max_days=10)) == 1


def test_credit_posting_before_the_debit_still_matches():
    # Across two institutions the inflow sometimes lands first.
    out = make_tx(date(2026, 3, 6), "-300.00", "TRANSFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 4), "300.00", "TRANSFER FROM CHECKING", "savings")

    assert len(find_transfers([out, inn])) == 1


def test_two_rows_on_one_account_are_never_a_transfer():
    out = make_tx(date(2026, 3, 4), "-500.00", "ONLINE BANKING TRANSFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 4), "500.00", "ONLINE BANKING TRANSFER FROM CHECKING")

    assert out.account_id == inn.account_id
    assert find_transfers([out, inn]) == []
    assert score_pair(out, inn) is None


def test_a_coincidental_purchase_is_not_mistaken_for_the_transfer():
    # Alice buys something for $50 the same day she moves $50 to savings. The
    # purchase must not steal the savings deposit: that would erase real
    # spending AND leave the true outflow counted as spending.
    coffee = make_tx(date(2026, 3, 4), "-50.00", "SQ *BLUE BOTTLE COFFEE")
    out = make_tx(date(2026, 3, 4), "-50.00", "ONLINE TRANSFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 4), "50.00", "ONLINE TRANSFER FROM CHECKING", "savings")

    pairs = find_transfers([coffee, out, inn])

    assert len(pairs) == 1
    assert pairs[0].outflow is out
    assert coffee.id not in pairs[0].transaction_ids


def test_an_equal_and_opposite_pair_with_no_transfer_wording_is_left_alone():
    # Precision over recall: a missed transfer is one row the user groups by
    # hand, a wrong pair silently corrupts the totals of two accounts.
    out = make_tx(date(2026, 3, 4), "-50.00", "SQ *BLUE BOTTLE COFFEE")
    inn = make_tx(date(2026, 3, 4), "50.00", "COUNTER DEPOSIT", "savings")

    assert find_transfers([out, inn]) == []


def test_each_transaction_lands_in_at_most_one_pair():
    # One outflow with two plausible partners. Greedy best-first must take the
    # stronger one and leave the other unpaired rather than reusing the half.
    out = make_tx(date(2026, 3, 4), "-500.00", "ONLINE BANKING TRANSFER TO SAVINGS")
    exact = make_tx(date(2026, 3, 4), "500.00", "TRANSFER FROM CHECKING", "savings")
    weaker = make_tx(date(2026, 3, 7), "500.00", "ACH DEPOSIT", "savings")

    pairs = find_transfers([out, exact, weaker])

    assert len(pairs) == 1
    assert pairs[0].inflow is exact
    # Scored on its own the weaker candidate is a legitimate pair; it loses
    # only because its half was already claimed.
    assert score_pair(out, weaker) is not None


def test_identical_transfers_on_one_day_pair_off_disjointly():
    outs = [make_tx(date(2026, 3, 4), "-200.00", "TRANSFER TO SAVINGS") for _ in range(3)]
    ins = [
        make_tx(date(2026, 3, 4), "200.00", "TRANSFER FROM CHECKING", "savings") for _ in range(3)
    ]

    pairs = find_transfers(outs + ins)

    assert len(pairs) == 3
    used = [tx_id for pair in pairs for tx_id in pair.transaction_ids]
    assert len(set(used)) == 6


def test_a_wire_fee_is_tolerated_but_a_real_mismatch_is_not():
    out = make_tx(date(2026, 3, 4), "-5000.00", "WIRE TRANSFER OUT")
    fee_taken = make_tx(date(2026, 3, 4), "4975.00", "WIRE TRANSFER IN", "savings")
    too_far = make_tx(date(2026, 3, 4), "4970.00", "WIRE TRANSFER IN", "savings")

    matched = score_pair(out, fee_taken)
    assert matched is not None
    assert matched.fee == Money.parse("25.00")
    # $30 is past the $25 cap, and a cap is what keeps "close enough" from
    # becoming "any two amounts in the same neighbourhood".
    assert score_pair(out, too_far) is None


def test_fee_tolerance_does_not_scale_down_to_small_amounts():
    # 1% of $50 is 50 cents, so $48 and $50 stay firmly unmatched.
    out = make_tx(date(2026, 3, 4), "-50.00", "TRANSFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 4), "48.00", "TRANSFER FROM CHECKING", "savings")

    assert score_pair(out, inn) is None


def test_more_money_arriving_than_left_is_not_a_transfer():
    # A fee can only ever take money out in transit; a surplus is interest or a
    # coincidence, so we refuse rather than guess.
    out = make_tx(date(2026, 3, 4), "-500.00", "TRANSFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 4), "505.00", "TRANSFER FROM CHECKING", "savings")

    assert score_pair(out, inn) is None


def test_apply_transfer_groups_stamps_both_halves():
    out = make_tx(date(2026, 3, 4), "-500.00", "TRANSFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 4), "500.00", "TRANSFER FROM CHECKING", "savings")
    coffee = make_tx(date(2026, 3, 4), "-4.75", "SQ *BLUE BOTTLE COFFEE")
    txs = [out, inn, coffee]

    marked = apply_transfer_groups(txs, find_transfers(txs))

    assert marked == 2
    assert out.transfer_group == inn.transfer_group != ""
    assert out.is_transfer and inn.is_transfer
    assert coffee.transfer_group == ""


def test_each_pair_gets_its_own_group_id():
    txs = [make_tx(date(2026, 3, 4), "-200.00", "TRANSFER TO SAVINGS") for _ in range(2)]
    txs += [
        make_tx(date(2026, 3, 4), "200.00", "TRANSFER FROM CHECKING", "savings") for _ in range(2)
    ]

    assert apply_transfer_groups(txs, find_transfers(txs)) == 4
    assert len({tx.transfer_group for tx in txs}) == 2


def test_matching_is_idempotent_over_an_already_grouped_ledger():
    out = make_tx(date(2026, 3, 4), "-500.00", "TRANSFER TO SAVINGS")
    inn = make_tx(date(2026, 3, 4), "500.00", "TRANSFER FROM CHECKING", "savings")
    txs = [out, inn]
    apply_transfer_groups(txs, find_transfers(txs))
    group = out.transfer_group

    # A second pass must neither re-pair nor re-stamp the same rows.
    assert find_transfers(txs) == []
    assert apply_transfer_groups(txs, find_transfers(txs)) == 0
    assert out.transfer_group == group


def test_grouped_transfers_disappear_from_recurring_detection():
    # A monthly savings sweep looks exactly like a subscription until it is
    # grouped, which is the whole point of wiring these two together.
    txs = []
    for month in range(1, 7):
        day = date(2026, month, 5)
        txs.append(make_tx(day, "-500.00", "ONLINE BANKING TRANSFER TO SAVINGS"))
        txs.append(make_tx(day, "500.00", "ONLINE BANKING TRANSFER FROM CHECKING", "savings"))

    assert len(detect(txs)) == 1  # before grouping it reads as a monthly charge

    pairs = find_transfers(txs)
    assert len(pairs) == 6
    assert apply_transfer_groups(txs, pairs) == 12
    assert detect(txs) == []
