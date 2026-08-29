# Architecture

## The one rule

**The core never imports the UI, and never depends on anything outside the
standard library.**

```
src/carraway/
├── core/        # money, models, storage  <- no dependencies, no I/O beyond SQLite
├── importers/   # CSV today; OFX/QFX, SimpleFIN next
├── analysis/    # recurring detection; categorisation next
├── cli.py       # thin layer over the above
└── ui/          # (not built yet) PySide6/Qt - will import core, never the reverse
```

Everything the GUI will do, the CLI already does through the same functions.
That is what keeps the interface swappable, and it is why the whole engine is
testable without launching a window.

## Money

Amounts are stored as an **integer count of minor units** (cents), never a
float. Binary floating point cannot represent most decimal fractions:

```python
>>> 0.1 + 0.2 == 0.3
False
```

Across thousands of transactions those errors accumulate and balances stop
reconciling. `Money.parse()` refuses float input outright, because by the time
a float reaches us the precision is already gone.

Rounding is **half-even** (banker's rounding), which is unbiased over many
operations, unlike round-half-up which drifts upward.

## Sign convention

**Negative means money leaving you.** A $12.99 charge is `-1299` on a checking
account *and* on a credit card. Card issuers usually export the opposite sign,
so normalising is the importer's job (`--flip-sign`), not the core's.

## Storage

One SQLite file, located per the XDG spec at
`~/.local/share/carraway/carraway.db`.

Schema changes are ordered entries in `MIGRATIONS` in `core/db.py`, tracked
using SQLite's built-in `user_version`. Boring on purpose: an old database
always knows how to catch up.

**Import idempotency** is enforced at the schema level. Each transaction carries
a `signature` — a hash of account, date, amount and normalised description —
under a unique index. Re-importing an overlapping statement inserts nothing. The
signature deliberately excludes category and notes, so a user editing a
transaction cannot cause it to duplicate on the next import.

## Recurring detection

The headline feature, in `analysis/recurring.py`. Statistical rather than a
hardcoded merchant list, because a list can never cover a local gym or a
regional utility.

1. **Normalise** the merchant — strip processor prefixes (`SQ *`, `POS DEBIT`),
   store numbers, dates, phone numbers and trailing state codes, so one merchant
   does not fragment into many groups.
2. **Group** by (merchant, account) and require at least 3 occurrences. Two
   charges is a coincidence; three is a pattern.
3. **Measure the gaps** between consecutive charges and match the median against
   known cadences, with tolerance scaled to the period — a yearly charge 10 days
   late is still yearly; a weekly charge 10 days late is not.
4. **Score confidence** as `0.6 × timing regularity + 0.25 × amount stability +
   0.15 × evidence`. Timing dominates deliberately: Netflix bills an identical
   figure monthly, while an electricity bill swings with usage and is still
   obviously recurring. Variable amounts are *flagged*, not disqualified.

## Testing

`pytest` runs against hand-built objects with no I/O, plus temp-file SQLite for
storage tests. Doctests in `money.py` and `recurring.py` run as part of the
suite, so the examples in the docs cannot silently rot.
