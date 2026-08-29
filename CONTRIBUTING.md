# Contributing to Carraway

Early days — issues, ideas and PRs all welcome.

## Setup

```bash
git clone https://github.com/aidanjalili/carraway.git
cd carraway
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Ground rules

1. **Never use a float for money.** Use `Money` from `core.money`. Tests will
   fail if you try, and that is deliberate.
2. **The core must not import the UI**, and `core/` must stay dependency-free.
   Analysis code has to be testable without launching a window.
3. **New behaviour comes with a test.** This app handles people's financial
   data; a silent wrong number is worse than a crash.
4. **Never commit real financial data.** `.gitignore` blocks `*.csv` and `*.db`,
   but check your diff anyway. Test fixtures must be synthetic.

## Style

`ruff` handles formatting and linting:

```bash
ruff check . && ruff format .
```

Comments should explain *why*, not *what*. The tricky decisions here are the
ones worth writing down — sign conventions, rounding modes, dedupe strategy.

## Good first issues

- Add an OFX/QFX importer (see `importers/csv_importer.py` for the shape)
- Add real-world bank CSV header variants to the column guesser
- Improve `normalise_merchant()` against descriptors it currently mangles
