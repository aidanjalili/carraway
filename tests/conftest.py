"""Guards every test in the suite needs, whichever file it lives in.

Both of these exist because of one night. A test built the real main window,
which schedules a bank sync 600ms after it opens. Nothing ran an event loop
long enough to fire that timer -- until enough later tests had each spun one
for a few dozen milliseconds, at which point it fired, found a *real* SimpleFIN
access URL in the developer's keyring, and started a live sync. The thread was
still running when the window was collected, Qt aborted, and systemd wrote a
core dump of several hundred MB. Thirty-eight times, on an 8 GB laptop.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_credentials(tmp_path, monkeypatch):
    """Never read or write the machine's keyring or config from a test.

    The keyring belongs to the person running the suite, and it holds a
    credential that reaches real bank accounts. A test that finds it can spend
    their SimpleFIN quota and pull their transactions. Every test gets an empty
    config directory and no keyring, the same isolation the credential tests
    already set up for themselves -- now for everyone, so a new test cannot
    forget it.
    """
    from carraway.sync import credentials

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    yield


@pytest.fixture(autouse=True)
def _stop_background_threads():
    """Quit and wait on any thread a test started, before the next one runs.

    Qt treats a QThread destroyed while running as fatal, and the destruction
    usually happens far from the test that caused it -- whenever the garbage
    collector reaches the widget that owned it. Stopping them here pins the
    failure to the test that started the work, rather than to an abort at
    interpreter exit that names no test at all.
    """
    yield
    try:
        from carraway.ui import sync_worker
        from carraway.ui.views import pocket
    except ImportError:  # Qt not installed; nothing can have started a thread
        return
    sync_worker.stop_all()
    pocket.stop_all_runners()
