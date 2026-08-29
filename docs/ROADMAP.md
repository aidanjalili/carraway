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
      descriptor a bank invents. The user needs the last word.
- [ ] Split transactions
- [ ] Separate *subscriptions* from merely *periodic spending*. Real data
      flagged a weekly corner-shop habit and a monthly takeaway order as
      recurring. Both are true and neither is a subscription.

## v0.3 — The GUI

- [x] PySide6/Qt shell, theme-aware, sidebar navigation
- [x] **Subscriptions view** — the flagship screen. Everything recurring, what
      it costs annually, and what looks cancelled
- [x] Overview: spending by category with proportional bars
- [x] Transaction list: model-backed, sortable, searchable across columns
- [ ] Inline editing and bulk recategorise
- [ ] Month-over-month trends and net worth
- [ ] Price-increase detection ("Netflix went from $15.49 to $17.99")
- [ ] Import wizard with a column-mapping UI for banks the guesser misses
- [ ] Load in a worker thread — 2,261 transactions is instant, but a decade of
      history on a slow disk should not freeze the window

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
