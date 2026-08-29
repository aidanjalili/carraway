"""Tests for rules-based categorisation."""

import uuid
from datetime import date

from carraway.analysis.categorize import (
    BUILTIN_RULES,
    CATEGORIES,
    PRIORITY_GENERIC,
    PRIORITY_USER,
    Rule,
    categorize,
    categorize_all,
    matching_rule,
    suggest_rules,
)
from carraway.analysis.recurring import normalise_merchant
from carraway.core.models import Transaction
from carraway.core.money import Money


def make_tx(description: str, amount: str = "-12.00", day: date | None = None) -> Transaction:
    return Transaction(
        id=uuid.uuid4().hex,
        account_id="acct1",
        date=day or date(2026, 5, 14),
        amount=Money.parse(amount),
        description=description,
    )


def test_built_in_rules_cover_the_common_categories():
    expected = {
        "TRADER JOES #123 SAN JOSE CA": "Groceries",
        "CHIPOTLE 2481 05/14": "Dining",
        "LYFT *RIDE THU 3PM": "Transport",
        "AIRBNB * HMKQ2R PAYMENTS": "Travel",
        "PG&E WEB ONLINE 1234567890": "Utilities",
        "NETFLIX.COM 866-579-7172 CA": "Subscriptions",
        "CVS/PHARMACY #04512 PORTLAND OR": "Health",
        "AMZN Mktp US*2K4LM9DR3 AMZN.COM/BILL": "Shopping",
        "TICKETMASTER 800-653-8000": "Entertainment",
        "GEICO *AUTO 800-841-3000 DC": "Insurance",
        "CHEWY.COM 800-672-4399 FL": "Pets",
        "GOFUNDME* CAMPAIGN": "Gifts/Charity",
        "MONTHLY MAINTENANCE FEE": "Fees",
    }
    for description, category in expected.items():
        assert categorize(make_tx(description)) == category, description


def test_generic_keywords_catch_unknown_local_merchants():
    # The point of the generic tier: a rules engine cannot know every local
    # business, but "CAFE" in the name is still real evidence.
    assert categorize(make_tx("RIVERSIDE CAFE AND BAKERY")) == "Dining"
    assert categorize(make_tx("MIDTOWN ANIMAL HOSPITAL")) == "Pets"
    assert categorize(make_tx("VALLEY WATER DIST AUTOPAY")) == "Utilities"


def test_a_named_merchant_beats_a_generic_keyword():
    # "COSTCO WHOLESALE" contains no grocery keyword, but "WHOLE FOODS MARKET"
    # matches both the named rule and the generic \bMARKET\b one - the named
    # rule has to win, or the generic tier could quietly reassign merchants we
    # actually recognise.
    rule = matching_rule(make_tx("WHOLE FOODS MARKET #10233 AUSTIN TX"))
    assert rule is not None
    assert rule.category == "Groceries"
    assert rule.priority > PRIORITY_GENERIC


def test_user_rules_override_the_built_ins():
    tx = make_tx("STARBUCKS STORE 04122 SEATTLE WA")
    assert categorize(tx) == "Dining"

    # Someone whose employer reimburses coffee wants it filed differently.
    mine = [Rule("STARBUCKS", "Education", priority=PRIORITY_USER)]
    assert categorize(tx, mine) == "Education"

    # ...and a user rule at built-in priority must not win by accident.
    weak = [Rule("STARBUCKS", "Education", priority=1)]
    assert categorize(tx, weak) == "Dining"


def test_the_more_specific_pattern_wins_within_a_tier():
    # Both "UBER" and "UBER EATS" match a food delivery charge. The longer
    # pattern is the more specific claim and must decide it, without anyone
    # hand-tuning priorities for the pair.
    assert categorize(make_tx("UBER EATS 8005928996 CA")) == "Dining"
    assert categorize(make_tx("UBER *TRIP HELP.UBER.COM")) == "Transport"
    # Same shape, different pair.
    assert categorize(make_tx("Amazon Prime*RT4G9 AMZN.COM")) == "Subscriptions"
    assert categorize(make_tx("AMZN Mktp US*9XQ2P1TTY")) == "Shopping"


def test_payroll_is_income_only_when_the_money_comes_in():
    # Sign convention: negative is money leaving you. The same word means
    # salary on the way in and a payroll run on the way out.
    assert categorize(make_tx("DIRECT DEP ACME CORP PAYROLL", "2400.00")) == "Income"
    assert categorize(make_tx("ACME CORP PAYROLL", "-2400.00")) == "Uncategorized"

    # A tax refund is income; a tax payment is not.
    assert categorize(make_tx("IRS TREAS 310 TAX REF", "1204.00")) == "Income"
    assert categorize(make_tx("IRS USATAXPYMT 12345678", "-1204.00")) == "Taxes"


def test_normalisation_defeats_processor_noise():
    # Store numbers, processor prefixes and trailing state codes must not stop
    # a merchant matching, which is why rules run against the normalised name.
    for description in (
        "SQ *BLUE BOTTLE #402 SF CA",
        "POS DEBIT SQ *BLUE BOTTLE COFFEE 05/14",
        "BLUE BOTTLE COFFEE 8005551234 OAKLAND CA",
    ):
        assert categorize(make_tx(description)) == "Dining", description


def test_rules_can_match_the_raw_description_instead():
    # Toast is restaurant point-of-sale, so its prefix is itself evidence - but
    # normalisation strips exactly that kind of prefix, so the rule has to opt
    # into the raw text.
    tx = make_tx("TST* THE LITTLE SPOT SAN JOSE CA")
    assert normalise_merchant(tx.description) == "THE LITTLE SPOT SAN JOSE"
    assert categorize(tx) == "Dining"


def test_a_preset_merchant_field_is_used_when_present():
    tx = make_tx("UNINTELLIGIBLE BANK GIBBERISH 0042")
    assert categorize(tx) == "Uncategorized"
    # An importer or the user cleaned the name up; that beats re-deriving one.
    tx.merchant = "Planet Fitness"
    assert categorize(tx) == "Health"


def test_unmatched_input_falls_back_rather_than_raising():
    assert categorize(make_tx("QWTZ HOLDINGS 44 LLC")) == "Uncategorized"
    assert categorize(make_tx("")) == "Uncategorized"
    assert categorize(make_tx("   ", "0.00")) == "Uncategorized"
    # Regex metacharacters in a description must not blow up substring matching.
    assert categorize(make_tx("*** (UNKNOWN) [?] +++")) == "Uncategorized"


def test_transfers_are_never_spending():
    # Both halves of a transfer are money the user still has, so neither may
    # land in a spending category and be double-counted.
    tx = make_tx("WIRE OUT TO BROKERAGE", "-5000.00")
    tx.transfer_group = "grp1"
    assert categorize(tx) == "Transfer"
    assert categorize_all([tx]) == ["Transfer"]
    # Recognised by name too, before any transfer matching has run.
    assert categorize(make_tx("ONLINE TRANSFER TO SAVINGS 1234", "-500.00")) == "Transfer"


def test_categorize_all_returns_one_category_per_transaction_in_order():
    txs = [
        make_tx("SAFEWAY #1234 SAN JOSE CA"),
        make_tx("QWTZ HOLDINGS 44 LLC"),
        make_tx("SPOTIFY USA 866-679-9129"),
    ]
    assert categorize_all(txs) == ["Groceries", "Uncategorized", "Subscriptions"]


def test_fallback_only_sees_what_the_rules_missed():
    # This is the seam the learned model will occupy: rules first, because a
    # rule can be shown to the user and argued with.
    txs = [make_tx("SAFEWAY #1234"), make_tx("QWTZ HOLDINGS 44 LLC")]
    seen = []

    def guess(tx):
        seen.append(tx.description)
        return "Shopping"

    assert categorize_all(txs, fallback=guess) == ["Groceries", "Shopping"]
    assert seen == ["QWTZ HOLDINGS 44 LLC"]

    # Returning None is how a model declines to guess.
    assert categorize_all(txs, fallback=lambda tx: None)[1] == "Uncategorized"


def test_built_ins_can_be_switched_off_entirely():
    txs = [make_tx("SAFEWAY #1234")]
    only_mine = [Rule("SAFEWAY", "Shopping")]
    assert categorize_all(txs, only_mine, include_builtins=False) == ["Shopping"]
    assert categorize_all(txs, include_builtins=False) == ["Uncategorized"]


def test_regex_rules_match_on_word_boundaries():
    # A plain substring rule for "VET" would file every veterans' charity and
    # every Corvette part under Pets.
    assert categorize(make_tx("OAK PARK VET CLINIC")) == "Pets"
    assert categorize(make_tx("CORVETTE PARTS DIRECT")) == "Uncategorized"


def test_suggest_rules_ranks_by_count_then_value():
    txs = (
        [make_tx("BODEGA ON FIFTH", "-6.00") for _ in range(5)]
        + [make_tx("HANSEN PLUMBING CO", "-400.00") for _ in range(3)]
        + [make_tx("SAFEWAY #1234", "-88.00") for _ in range(9)]
    )
    suggestions = suggest_rules(txs)

    # "CO" is stripped as a trailing state code (Colorado); the suggestion is
    # keyed on the same normalised name a rule would be matched against.
    assert [s.merchant for s in suggestions] == ["BODEGA ON FIFTH", "HANSEN PLUMBING"]
    top = suggestions[0]
    assert top.count == 5
    assert top.total == Money.parse("-30.00")
    assert top.example == "BODEGA ON FIFTH"
    assert len(top.transaction_ids) == 5


def test_suggest_rules_groups_past_processor_noise():
    # The whole value of suggesting is that one merchant arrives as one
    # suggestion, not as four near-identical ones.
    txs = [
        make_tx("SQ *HANSEN PLUMBING #12 05/14", "-120.00"),
        make_tx("POS DEBIT SQ *HANSEN PLUMBING", "-80.00"),
        make_tx("SQ *HANSEN PLUMBING #98 CA", "-200.00"),
    ]
    suggestions = suggest_rules(txs)

    assert len(suggestions) == 1
    assert suggestions[0].merchant == "HANSEN PLUMBING"
    assert suggestions[0].count == 3
    assert suggestions[0].total == Money.parse("-400.00")


def test_suggest_rules_ignores_one_offs_and_respects_limits():
    # Names must differ by a real word, not a trailing digit: normalisation
    # strips digits precisely so one merchant does not fragment per store.
    names = ["ALPHA STORE", "BETA MARKET", "GAMMA GOODS", "DELTA SHOP", "EPS MART", "ZETA OUTLET"]

    once = [make_tx(name, "-9.00") for name in names]
    assert suggest_rules(once) == []

    repeated = [make_tx(name, "-9.00") for name in names for _ in range(2)]
    assert len(suggest_rules(repeated, limit=3)) == 3


def test_an_accepted_suggestion_becomes_a_winning_rule():
    txs = [make_tx("HANSEN PLUMBING CO", "-400.00") for _ in range(3)]
    assert categorize_all(txs) == ["Uncategorized"] * 3

    accepted = suggest_rules(txs)[0].as_rule("Shopping")
    assert accepted.priority == PRIORITY_USER
    assert categorize_all(txs, [accepted]) == ["Shopping"] * 3


def test_every_built_in_rule_targets_a_known_category():
    # A typo in the ruleset would otherwise show up as a phantom category in
    # the spending breakdown rather than as a failure here.
    assert {rule.category for rule in BUILTIN_RULES} <= set(CATEGORIES)
    assert all(rule.pattern.strip() for rule in BUILTIN_RULES)


def test_widened_builtin_coverage():
    # Chains and patterns added after a real 2,200-transaction import dropped
    # the uncategorised share from 44% to 30%.
    expected = {
        "KWIK TRIP 401 VERONA WI": "Transport",
        "MBTA- BOSTON MA": "Transport",
        "ONSTREET PARKING MADISON": "Transport",
        "FAMILY FARE 1102 NORTHFIELD": "Groceries",
        "WINN-DIXIE #1420": "Groceries",
        "CULVERS OF NORTHFIELD": "Dining",
        "FIREHOUSE SUBS QSR MADISON": "Dining",
        "SNACK SODA VENDING CO": "Dining",
        "MENARDS DUNDAS MN": "Shopping",
        "XCEL ENERGY-MN XCELENERGY WEB": "Utilities",
        "NORTHFIELD MN UTL TEL": "Utilities",
        "WESTIN KANSAS CTY CRWN KANSAS CITY": "Travel",
    }
    for description, category in expected.items():
        assert categorize(make_tx(description, "-25.00")) == category, description


def test_inflow_only_rules_need_the_right_direction():
    # A redeemed reward is money arriving; the same words leaving would not be.
    assert categorize(make_tx("CASH BACK REDEMPTION REF", "45.00")) == "Income"
    assert categorize(make_tx("CASH BACK REDEMPTION REF", "-45.00")) != "Income"


def test_american_airlines_without_swallowing_every_american():
    assert categorize(make_tx("AMERICAN 0012345678 FORT WORTH TX", "-412.30")) == "Travel"
    # "AMERICAN" on its own must not become a travel rule.
    assert categorize(make_tx("AMERICAN FAMILY DINER", "-18.00")) != "Travel"


def test_the_subscription_catalogue_also_categorises():
    # The two lists were maintained separately, so a merchant recognised as a
    # subscription could still come back Uncategorized — DigitalOcean was known
    # to be one and had no category at all.
    assert categorize(make_tx("DIGITALOCEAN.COM", "-12.00")) == "Subscriptions"
    assert categorize(make_tx("ANYTIME FIT ABC CLUB FEES PPD", "-29.99")) == "Subscriptions"


def test_chains_the_list_had_simply_missed():
    for description, expected in [
        ("DOMINO'S 2002 414-443-6402 WI", "Dining"),
        ("SUBWAY 4141 MADISON WI", "Dining"),
        ("WAL-MART MADISON WI", "Shopping"),
        ("BURTON DINING HALL NORTHFIELD", "Dining"),
    ]:
        assert categorize(make_tx(description, "-12.00")) == expected, description


def test_a_transit_subway_is_not_a_sandwich():
    # "SUBWAY" is a sandwich shop and "SUBWAY STATION" is transit; the more
    # specific phrase has to win.
    assert categorize(make_tx("SUBWAY STATION NYC", "-2.90")) == "Transport"


def test_user_rules_outrank_everything_shipped():
    from carraway.analysis.categorize import rules_from

    stored = [{"pattern": "TRADER JOES", "category": "Dining"}]
    # Trader Joe's is a built-in grocery rule; a user saying otherwise wins,
    # because they wrote the rule while looking at the row.
    assert categorize(make_tx("TRADER JOES #182", "-42.00")) == "Groceries"
    assert categorize(make_tx("TRADER JOES #182", "-42.00"), rules_from(stored)) == "Dining"


def test_user_rules_match_the_raw_description():
    from carraway.analysis.categorize import rules_from

    # Matched against what the user can actually see in the transaction list,
    # including the reference numbers normalisation strips out.
    stored = [{"pattern": "PPD ID: 4760039224", "category": "Fees"}]
    tx = make_tx("CHASE CREDIT CRD AUTOPAY PPD ID: 4760039224", "-188.45")
    assert categorize(tx, rules_from(stored)) == "Fees"


def test_hidden_categories_are_dropped_but_added_ones_appear():
    from carraway.analysis.categorize import available_categories

    names = available_categories(["Hobbies"], {"Pets"})
    assert "Hobbies" in names
    assert "Pets" not in names
    # Uncategorized must survive whatever the user does, or rows have nowhere
    # to fall back to.
    assert "Uncategorized" in names
