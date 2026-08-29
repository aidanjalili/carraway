# Roadmap

Ordered by dependency, not ambition. Each milestone should be independently
useful — no six-month stretch with nothing to show.

## v0.1 — Core engine ✅

- [x] Exact integer-cents money arithmetic
- [x] SQLite storage with versioned migrations
- [x] CSV import with column auto-detection and idempotent re-import
- [x] Recurring-charge detection with confidence scoring
- [x] CLI: `accounts`, `import`, `recurring`, `summary`
- [x] Test suite

## v0.2 — Make the data trustworthy

- [x] OFX/QFX import, both 1.x SGML and 2.x XML
- [x] Automatic categorisation — rules engine with a seam for a learned model
- [x] Transfer matching, so moving money between your own accounts stops being
      counted as both income and spending
- [x] Merchant normalisation hardened against real statements
- [ ] User-editable merchant alias table — normalisation can fold "NETFLIX.COM"
      and "NETFLIX, INC." together, but no heuristic will ever unify every
      descriptor a bank invents. The user needs the last word. Real data makes
      the case: "KWIK TRIP", "KWIK TRIP VERONA" and "KWIK TRIP NORTHFIELD" are
      three merchants to the normaliser because it cannot know which trailing
      word is a town. Folding a merchant into a shorter one that is a prefix of
      it would catch most of these automatically.
- [ ] Learned categorisation. The built-in rules reach ~70% of a real ledger;
      the rest is local businesses no shipped ruleset can ever name. The seam
      is already there: `categorize_all(..., fallback=)`.
- [ ] Split transactions
- [x] One merchant billing several things — rent plus fees under one
      descriptor, or every Apple subscription under one name
- [x] Separate *subscriptions* from *bills* from *habits*, with a catalog of
      ~160 known services and ~50 known billers
- [x] Ask the user about anything unrecognised, and remember the answer
      forever (`carraway review`)

## v0.3 — The GUI

- [x] PySide6/Qt shell, theme-aware, sidebar navigation
- [x] **Subscriptions view** — the flagship screen. Everything recurring, what
      it costs annually, and what looks cancelled
- [x] Overview: spending by category with proportional bars
- [x] Transaction list: model-backed, sortable, searchable across columns
- [x] Classify a merchant from the GUI — double-click, right-click, or the
      button that walks the unclassified queue
- [ ] Inline editing and bulk recategorise
- [x] Net worth over time, reconstructed from balances and transactions
- [x] Goal-driven budgeting: state a savings target, get per-category allowances
- [x] Export to LibreOffice Calc (.ods) and CSV
- [x] Recurring income and person-to-person payments surfaced for review
- [x] A cancelled subscription stays visible but stops counting as money paid
- [x] Price-increase detection ("Netflix went from $15.49 to $17.99")
- [ ] Import wizard with a column-mapping UI for banks the guesser misses
- [ ] Load in a worker thread — 2,261 transactions is instant, but a decade of
      history on a slow disk should not freeze the window

## v0.4 — Bank sync (opt-in, bring your own key)

- [x] Provider adapter interface
- [x] SimpleFIN Bridge adapter (US, ~$15/yr, no business agreement needed)
- [x] Venmo, via the unofficial mobile API and via its CSV export
- [x] Credential storage in the system keyring, with a 0600 file fallback
- [ ] GoCardless adapter (UK/EU open banking, free tier)
- [ ] Background sync with conflict handling against manual edits
- [ ] Sync from the GUI rather than only the CLI

## v0.5 — Ship it

- [ ] Flatpak package + Flathub submission
- [ ] Price-increase alerts ("Netflix went from $15.49 to $17.99")
- [ ] Budgets and goals — deliberately *last*, so budgeting is optional rather
      than the price of entry
- [x] Spreadsheet export, because your data should never be hostage

## Explicitly not planned

- **A hosted service or accounts.** The pitch is that nothing leaves your
  machine; running a server would undermine it.
- **Bill negotiation or "cancel it for you".** Rocket Money's version of this
  requires acting on your behalf with your credentials. Carraway will tell you
  what to cancel and let you do it.
- **Selling anonymised data.** Ever. It is why this project exists.
