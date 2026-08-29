# Carraway

**A local-first, open source money manager and subscription tracker for Linux.**

Carraway is a free alternative to Rocket Money and Monarch. It shows you where
your money actually went, and finds the recurring charges you forgot you were
paying for — without uploading your financial life to anybody's server.

> Named for Nick Carraway, the narrator who watches the money and tells you
> what really happened.

⚠️ **Status: pre-alpha.** The engine, CLI and desktop GUI all work today, and
have been run against real multi-year bank exports. See the
[roadmap](docs/ROADMAP.md).

---

## Why another finance app?

There are good open source finance tools already — [Actual Budget][actual],
[Firefly III][firefly], GnuCash, KMyMoney. Carraway is not trying to replace
them, because it is aimed at a different job:

| | Existing tools | Carraway |
|---|---|---|
| **Core question** | "Did I stick to my budget?" | "Where did my money go, and what am I still paying for?" |
| **Model** | Envelope / zero-based budgeting, or double-entry accounting | Track, categorise and surface insights |
| **Shape** | Web app, self-hosted server, or a dated desktop UI | Native desktop app, no server |
| **Setup cost** | Learn a budgeting methodology first | Import a CSV, get answers immediately |

The specific gap: **nothing in open source does subscription detection well.**
That is Rocket Money's headline feature, and it turns out to be a tractable
statistics problem rather than a proprietary secret.

## What works today

```bash
carraway accounts --add "Chase Checking" --type checking
carraway import statement.csv --account <id>   # or statement.ofx / .qfx
carraway transfers --apply                     # stop double-counting card payments
carraway categorize                            # spending broken down by category
carraway recurring                             # the subscriptions view
```

```
Found 5 recurring series in 412 transactions:

MERCHANT              CADENCE    AMOUNT   NEXT        SEEN  CONF
------------------------------------------------------------------
Netflix.Com           monthly    $15.49   2026-09-14  8     94%
Spotify Usa           monthly    $11.99   2026-09-20  8     94%
The Gym Membership    weekly     $11.00   2026-09-05  10    91%
City Power And Light  monthly*   $95.44   2026-09-18  6     78%
Domain Renewal Llc    yearly     $109.00  2027-03-03  4     71%

Total annualised: $2,296.64/year
* amount varies between charges
```

## The desktop app

```bash
pip install -e '.[gui]'
carraway-gui
```

Three screens: **Subscriptions** (what recurs, what it costs a year, what looks
cancelled), **Overview** (spending by category), and **Transactions** (sortable,
searchable). The window follows your system light/dark theme.

## Install

Requires Python 3.11+. Nothing else — the core has **zero runtime dependencies**.

```bash
git clone https://github.com/aidanjalili/carraway.git
cd carraway
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Design principles

1. **Local-first.** Your data is a single SQLite file on your machine. There is
   no account, no server, and no telemetry. Nothing leaves the device unless
   you explicitly configure a sync provider.
2. **Money is never a float.** Every amount is an integer count of cents. See
   [`core/money.py`](src/carraway/core/money.py) for why this is non-negotiable.
3. **The core knows nothing about the UI.** All logic lives in a dependency-free
   Python package driven by a CLI, so the interface can change without a rewrite.
4. **Import must never lose data.** Re-importing an overlapping statement is a
   no-op, and one malformed row never costs you the whole file.

## Bank connections — an honest note

Carraway cannot offer free automatic bank sync, and neither can any other open
source project. Aggregation providers (Plaid, MX, Finicity) charge per connected
account per month, and a project with no revenue cannot absorb that.

So the plan is:

- **File import** (CSV today, OFX/QFX next) — always free, always works.
- **Bring-your-own provider** — optional adapters for [SimpleFIN][simplefin],
  GoCardless and similar, where you supply the key and pay the provider directly
  if you want automatic sync.

This is the same trade-off Actual Budget makes, for the same reason.

## Licence

[GPL-3.0-only](LICENSE). Carraway exists because financial apps monetise your
data; the copyleft ensures a derivative cannot quietly close that door.

[actual]: https://actualbudget.org
[firefly]: https://www.firefly-iii.org
[simplefin]: https://beta-bridge.simplefin.org
