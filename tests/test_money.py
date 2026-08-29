"""Money must be exact. These tests exist to make regressions here loud."""

from decimal import Decimal

import pytest

from carraway.core.money import CurrencyMismatch, Money, total


def test_parse_common_formats():
    assert Money.parse("12.50").minor == 1250
    assert Money.parse("$1,234.56").minor == 123456
    assert Money.parse("-45.00").minor == -4500
    assert Money.parse("(45.00)").minor == -4500  # accounting-style negative
    assert Money.parse("0.01").minor == 1
    assert Money.parse("100").minor == 10000


def test_float_input_is_rejected():
    # Precision is already lost by the time a float reaches us, so refuse it
    # loudly rather than silently storing a wrong number.
    with pytest.raises(TypeError):
        Money.parse(12.50)
    with pytest.raises(TypeError):
        Money(1250) * 1.5


def test_the_classic_float_bug_does_not_happen():
    tenth = Money.parse("0.10")
    assert total([tenth] * 10) == Money.parse("1.00")

    # The equivalent float computation drifts, which is the whole point.
    # Note: accumulate in a loop rather than with sum(). Python 3.14 gave
    # sum() compensated (Neumaier) summation, so sum([0.1] * 10) is now
    # exactly 1.0 -- but every naive running total in the wild still drifts.
    running = 0.0
    for _ in range(10):
        running += 0.1
    assert running != 1.0


def test_arithmetic_and_currency_safety():
    assert Money(1000) + Money(250) == Money(1250)
    assert Money(1000) - Money(250) == Money(750)
    assert -Money(500) == Money(-500)
    assert abs(Money(-500)) == Money(500)
    assert Money(1000) * 3 == Money(3000)
    with pytest.raises(CurrencyMismatch):
        Money(100, "USD") + Money(100, "EUR")


def test_rounding_is_half_even():
    # Banker's rounding avoids the upward bias of always rounding .5 away.
    assert Money.parse(Decimal("0.005")).minor == 0
    assert Money.parse(Decimal("0.015")).minor == 2


def test_zero_decimal_currency():
    yen = Money.parse("1000", "JPY")
    assert yen.minor == 1000  # not 100000; JPY has no minor unit
    assert "1,000" in yen.format()


def test_formatting():
    assert Money(123456).format() == "$1,234.56"
    assert Money(-500).format() == "-$5.00"
    assert Money(125000, "CAD").format() == "1,250.00 CAD"


def test_empty_total_is_zero():
    assert total([]) == Money.zero()
