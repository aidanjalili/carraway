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

- [ ] OFX/QFX import (most US banks export these; better structured than CSV)
- [ ] Automatic categorisation — rules engine first, learned from user
      corrections second
- [ ] Transfer matching, so moving money between your own accounts stops being
      counted as both income and spending
- [ ] Merchant-name cleanup with a user-editable alias table
- [ ] Split transactions

## v0.3 — The GUI

- [ ] PySide6/Qt shell with a proper KDE-native feel
- [ ] Transaction list: fast, sortable, inline editing, bulk recategorise
- [ ] Dashboard: spending by category, month over month, net worth
- [ ] **Subscriptions view** — the flagship screen. Everything recurring, what
      it costs annually, what looks cancelled, what quietly went up in price
- [ ] Import wizard with a column-mapping UI for banks the guesser misses

## v0.4 — Bank sync (opt-in, bring your own key)

- [ ] Provider adapter interface
- [ ] SimpleFIN Bridge adapter (US, cheap, no business agreement needed)
- [ ] GoCardless adapter (UK/EU open banking, free tier)
- [ ] Encrypted credential storage via the system keyring
- [ ] Background sync with conflict handling against manual edits

## v0.5 — Ship it

- [ ] Flatpak package + Flathub submission
- [ ] Price-increase alerts ("Netflix went from $15.49 to $17.99")
- [ ] Budgets and goals — deliberately *last*, so budgeting is optional rather
      than the price of entry
- [ ] CSV/JSON export, because your data should never be hostage

## Explicitly not planned

- **A hosted service or accounts.** The pitch is that nothing leaves your
  machine; running a server would undermine it.
- **Bill negotiation or "cancel it for you".** Rocket Money's version of this
  requires acting on your behalf with your credentials. Carraway will tell you
  what to cancel and let you do it.
- **Selling anonymised data.** Ever. It is why this project exists.
