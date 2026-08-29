"""Exact monetary arithmetic.

The single most important rule in this codebase: **money is never a float.**

    >>> 0.1 + 0.2 == 0.3
    False

Binary floating point cannot represent most decimal fractions, so errors
accumulate silently across thousands of transactions and balances stop
reconciling. Instead we store an integer count of *minor units* (cents for
USD) and only convert to a decimal string at the edges for display.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

# Minor units per major unit, for currencies that are not the usual 1/100.
# Most currencies use 2 decimal places; these are the common exceptions.
_EXPONENTS = {"JPY": 0, "KRW": 0, "ISK": 0, "BHD": 3, "KWD": 3, "OMR": 3}

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


def exponent_for(currency: str) -> int:
    """Number of decimal places used by `currency` (USD -> 2, JPY -> 0)."""
    return _EXPONENTS.get(currency.upper(), 2)


class CurrencyMismatch(ValueError):
    """Raised when arithmetic mixes two different currencies."""


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An exact amount of money, stored as whole minor units.

    `Money(1250, "USD")` is $12.50. Construct from human input with
    `Money.parse("12.50")` rather than doing the multiplication yourself.
    """

    minor: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise TypeError(
                f"Money.minor must be an int (whole cents), got {type(self.minor).__name__}. "
                f"Use Money.parse() to build one from a decimal string."
            )
        object.__setattr__(self, "currency", self.currency.upper())

    # -- construction ----------------------------------------------------

    @classmethod
    def parse(cls, value: str | int | Decimal, currency: str = "USD") -> Money:
        """Build a Money from a human/CSV amount such as "-1,234.56" or "$12.50".

        Deliberately rejects float input: by the time a float reaches us the
        precision is already gone, so there is nothing we can do to recover it.
        """
        if isinstance(value, float):
            raise TypeError(
                "Refusing to build Money from a float, which has already lost precision. "
                "Pass the original string, an int of minor units, or a Decimal."
            )
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "").replace("_", "")
            for sym in _SYMBOLS.values():
                cleaned = cleaned.replace(sym, "")
            # Accounting-style negatives: (12.50) means -12.50
            if cleaned.startswith("(") and cleaned.endswith(")"):
                cleaned = "-" + cleaned[1:-1]
            cleaned = cleaned.strip()
            if not cleaned:
                raise ValueError(f"Cannot parse an empty amount from {value!r}")
            try:
                dec = Decimal(cleaned)
            except InvalidOperation as exc:
                raise ValueError(f"Cannot parse {value!r} as a monetary amount") from exc
        else:
            dec = Decimal(value)

        scale = Decimal(10) ** exponent_for(currency)
        # Banker's rounding: unbiased over many roundings, unlike round-half-up.
        minor = int((dec * scale).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        return cls(minor, currency)

    @classmethod
    def zero(cls, currency: str = "USD") -> Money:
        return cls(0, currency)

    # -- arithmetic ------------------------------------------------------

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(
                f"Cannot combine {self.currency} and {other.currency}. "
                f"Convert to a common currency first."
            )

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.minor), self.currency)

    def __mul__(self, factor: int | Decimal) -> Money:
        if isinstance(factor, float):
            raise TypeError("Multiply Money by an int or Decimal, never a float.")
        scaled = (Decimal(self.minor) * Decimal(factor)).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
        return Money(int(scaled), self.currency)

    __rmul__ = __mul__

    def __bool__(self) -> bool:
        return self.minor != 0

    # -- presentation ----------------------------------------------------

    @property
    def decimal(self) -> Decimal:
        """The amount as a Decimal, for display and reporting only."""
        return Decimal(self.minor).scaleb(-exponent_for(self.currency))

    def format(self, *, symbol: bool = True, grouping: bool = True) -> str:
        exp = exponent_for(self.currency)
        sign = "-" if self.minor < 0 else ""
        whole, _, frac = f"{abs(self.decimal):.{exp}f}".partition(".")
        if grouping:
            whole = f"{int(whole):,}"
        body = f"{whole}.{frac}" if exp else whole
        prefix = _SYMBOLS.get(self.currency, "") if symbol else ""
        suffix = "" if (symbol and prefix) else f" {self.currency}"
        return f"{sign}{prefix}{body}{suffix}"

    def __str__(self) -> str:
        return self.format()

    def __repr__(self) -> str:
        return f"Money({self.minor!r}, {self.currency!r})  # {self.format()}"


def total(amounts: list[Money], currency: str = "USD") -> Money:
    """Sum a list of Money.

    `currency` only decides the zero value for an empty list; otherwise the
    currency comes from the amounts themselves, and mixing raises.
    """
    if not amounts:
        return Money.zero(currency)
    result = Money.zero(amounts[0].currency)
    for amount in amounts:
        result = result + amount
    return result
