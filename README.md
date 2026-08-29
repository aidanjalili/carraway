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

## Licence

[GPL-3.0-only](LICENSE). Carraway exists because financial apps monetise your
data; the copyleft ensures a derivative cannot quietly close that door.

[actual]: https://actualbudget.org
[firefly]: https://www.firefly-iii.org
[simplefin]: https://beta-bridge.simplefin.org
