### Venmo

**Use the CSV export.** It is the only route that reliably works:

```bash
venmo.com -> Statements -> Download CSV     # 90 days at a time
carraway import venmo-statement.csv         # makes its own Venmo account
```

`carraway import` recognises the format — preamble, trailer rows and
`- $25.00` amounts included — and overlapping exports deduplicate against each
other, which matters because of that 90-day cap.

Three other routes were investigated and none of them work:

| Route | Why not |
|---|---|
| Venmo developer account | New API access closed in 2016 and never reopened. Only businesses approved before then still have it, and that API was for *accepting* payments as a merchant, never for reading your own history. |
| SimpleFIN | Venmo is not a supported institution. |
| Venmo's mobile API | Implemented in `sync/venmo_api.py` and left in place, but Venmo's risk checks refuse the sign-in: `OAuth2 Exception: Unable to complete your request`. It also breaks Venmo's terms, and its token is not read-only. |

⚠️ If you enable the mobile API anyway, know that the token Venmo issues can
move money and never expires. Carraway keeps it in the system keyring, only
ever issues `GET` requests with it, and `carraway venmo logout` revokes it at
Venmo rather than merely forgetting it. Your password is used once and never
written anywhere.

# Carraway

**A local-first, open source money manager and subscription tracker for Linux.**

Carraway is a free alternative to Rocket Money and Monarch. It shows you where
your money actually went, and finds the recurring charges you forgot you were
paying for — without uploading your financial life to anybody's server.

> Named for Nick Carraway, the narrator who watches the money and tells you
> what really happened.

### 🤖 This project was written by AI

Essentially all of the code, tests and documentation in this repository were
written by Claude (Anthropic's Claude Code), working from a human's direction,
review and real bank data. That is stated up front because you are about to
point this software at your financial life and you deserve to know how it was
made.

What that means in practice:

- **Read the code before you trust it.** That is good advice for any finance
  tool and better advice here. The engine is deliberately small and heavily
  commented for exactly this reason.
- **It has been exercised against real statements**, not only synthetic
  fixtures: several bugs in this history were found by running it over two
  years of actual bank exports, and each one has a regression test.
- **The design decisions are documented**, in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and in the commit history, so
  you can audit the reasoning rather than only the result.
- **Nothing is uploaded anywhere.** Your data stays in a SQLite file on your
  machine, which is the one property that matters most and the easiest to
  verify: there is no network code in the core at all.

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
carraway recurring                             # everything that repeats
carraway subscriptions                         # split into subscriptions, bills, habits
carraway review                                # answer what it could not place
carraway known                                 # recognised, but too little history to detect
carraway prices                                # what quietly went up in price
carraway networth                              # net worth over time
carraway budget 5000 --months 6                # what you can spend to save $5,000
carraway export ~/carraway.ods                 # open it in LibreOffice Calc
carraway dedupe                                # one charge imported from two sources
```

Recurring is not the same as cancellable. Rent, utilities and insurance repeat
just as reliably as Netflix, and so does a weekly corner-shop habit. Carraway
sorts them apart using a catalog of known services and billers, asks you about
anything it does not recognise, and never asks twice.

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
source project. Aggregation providers charge per connected account per month,
and a project with no revenue cannot absorb that. So you bring your own
provider: you hold the account, you pay them, and Carraway never sees a bank
password.

### SimpleFIN Bridge (recommended)

Read-only access to thousands of US institutions for about **$15/year**, paid to
[SimpleFIN][simplefin] directly.

```bash
pip install -e '.[sync]'
carraway simplefin check      # verify a token pastes correctly, without using it
carraway simplefin setup      # claim it and connect
carraway sync simplefin
```

On the first sync Carraway offers to link the provider's accounts to any you
already imported from files — "CHASE COLLEGE (6822)" and "Chase Checking 6822"
are the same account, and linking them is what stops every overlapping
transaction being stored twice.

Note that SimpleFIN caps a request at **90 days**, so file import remains the
way to load older history.

The access URL is stored in your system keyring where one is available, and in
a `0600` file under `~/.config/carraway` where one is not — the app tells you
which before it saves anything.

⚠️ **A setup token is single-use.** Every attempt that reaches SimpleFIN spends
it, including one that appears to fail, so a second try with the same token
returns "already claimed". Use `carraway simplefin check` first: it verifies the
token decodes without claiming it.

### Venmo — read this before enabling it

⚠️ **Venmo retired its public API.** Carraway can sign in the way the Venmo
mobile app does, and that carries real risks you are opting into:

- **It breaks Venmo's terms of service**, and Venmo may suspend or close an
  account for automated access.
- **The token is not read-only.** Venmo issues one token for everything and it
  never expires, so anyone holding it can move money. Carraway stores it in the
  keyring, only ever issues `GET` requests with it, and `carraway venmo logout`
  revokes it at Venmo rather than merely forgetting it locally.
- **It will break** when an undocumented endpoint changes.

Your password is used once during sign-in and never written anywhere.

```bash
carraway venmo login
carraway sync venmo
carraway venmo logout          # revoke the token when you are done
```

**The safer path, and the default:** Venmo's own CSV export
(venmo.com → Statements → Download CSV, 90 days at a time) imports with no API
access at all, and `carraway import` recognises the format automatically.

```bash
carraway import venmo-statement.csv --account <id>
```

## Licence

[GPL-3.0-only](LICENSE). Carraway exists because financial apps monetise your
data; the copyleft ensures a derivative cannot quietly close that door.

[actual]: https://actualbudget.org
[firefly]: https://www.firefly-iii.org
[simplefin]: https://beta-bridge.simplefin.org
