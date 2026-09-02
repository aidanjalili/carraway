"""The Pocket settings card, invoked rather than merely imported.

Same rule as test_ui_views.py, for the same reason: a handler that is never
called is a handler that can call a method which does not exist and nobody
notices until a button does nothing. Every action on this card is invoked
here with its dialogs and its network stubbed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI tests need the [gui] extra")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from carraway.core import db  # noqa: E402
from carraway.core.models import Account, AccountType  # noqa: E402
from carraway.ui.data import Ledger  # noqa: E402
from carraway.ui.views import pocket as view  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    path = tmp_path / "pocket.db"
    conn = db.connect(path)
    db.upsert_account(conn, Account(id="cash", name="Cash", type=AccountType.CHECKING))
    conn.close()
    led = Ledger(path)
    led.load()
    return led


class FakeClient:
    """Stands in for a paired inbox. Records what it was asked to do."""

    def __init__(self) -> None:
        self.revoked: list[str] = []
        self.published: list[dict] = []

    def status(self) -> dict:
        return {"pending": 2, "oldest_days": 3}

    def devices(self) -> list[dict]:
        return [
            {"id": "d1", "name": "iPhone", "last_seen": "2026-09-01", "revoked": False},
            {"id": "d2", "name": "Old phone", "revoked": True},
        ]

    def create_pairing(self, label: str = "iPhone") -> dict:
        return {
            "url": "https://money.example.com/pair/abc123",
            "expires_at": "2026-09-01T12:00:00+00:00",
        }

    def revoke(self, device_id: str) -> bool:
        self.revoked.append(device_id)
        return True

    def publish(self, snapshot: dict) -> str:
        self.published.append(snapshot)
        return "2026-09-01T12:00:00+00:00"


@pytest.fixture
def paired(ledger, monkeypatch):
    """A ledger that believes it is paired, with no network behind it."""
    client = FakeClient()
    monkeypatch.setattr(Ledger, "pocket_client", lambda self: client)
    ledger.save_setting("pocket_url", "https://money.example.com")
    return ledger, client


def _settle(app, runner, limit: int = 200) -> None:
    """Let the worker thread finish and its queued signals be delivered."""
    for _ in range(limit):
        app.processEvents()
        if not runner.busy:
            break
    app.processEvents()


# -- the unconfigured state ---------------------------------------------


def test_card_offers_to_connect_when_nothing_is_set_up(app, ledger):
    card = view.PocketCard(ledger)
    assert not ledger.pocket_configured
    assert "Not connected" in card.status.text()
    labels = [
        card.buttons.itemAt(i).widget().text()
        for i in range(card.buttons.count())
        if card.buttons.itemAt(i).widget()
    ]
    assert any("Connect" in label for label in labels)


def test_connecting_stores_the_token_and_rebuilds(app, ledger, monkeypatch):
    monkeypatch.setattr(view.ConnectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        view.ConnectDialog, "__init__", lambda self, parent=None: QDialog.__init__(self, parent)
    )
    monkeypatch.setattr(
        view.ConnectDialog, "link", "https://money.example.com/pair/x", raising=False
    )
    monkeypatch.setattr(Ledger, "pair_pocket", lambda self, url: "the system keyring")
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kw: shown.append(args[2]))

    card = view.PocketCard(ledger)
    card._connect()
    _settle(app, card.runner)
    assert shown and "system keyring" in shown[0]


def test_a_cancelled_connect_dialog_does_nothing(app, ledger, monkeypatch):
    monkeypatch.setattr(view.ConnectDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    called: list[str] = []
    monkeypatch.setattr(Ledger, "pair_pocket", lambda self, url: called.append(url))
    view.PocketCard(ledger)._connect()
    assert called == []


# -- the paired state ---------------------------------------------------


def test_card_reports_what_is_waiting(app, paired):
    ledger, _ = paired
    card = view.PocketCard(ledger)
    _settle(app, card.runner)
    assert "2 waiting" in card.status.text()
    assert "3 days old" in card.status.text()


def test_revoked_devices_are_not_listed(app, paired):
    ledger, _ = paired
    card = view.PocketCard(ledger)
    _settle(app, card.runner)
    listed = []
    for i in range(card.devices.count()):
        item = card.devices.itemAt(i)
        inner = item.layout()
        if inner is not None and inner.itemAt(0).widget():
            listed.append(inner.itemAt(0).widget().text())
    assert any("iPhone" in text for text in listed)
    assert not any("Old phone" in text for text in listed)


def test_adding_a_phone_shows_a_scannable_code(app, paired, monkeypatch):
    ledger, _ = paired
    seen: list[str] = []

    class Stub(QDialog):
        def __init__(self, url, expires, parent=None):
            super().__init__(parent)
            seen.append(url)

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(view, "AddPhoneDialog", Stub)
    card = view.PocketCard(ledger)
    _settle(app, card.runner)
    card._add_phone()
    _settle(app, card.runner)
    assert seen == ["https://money.example.com/pair/abc123"]


def test_the_pairing_dialog_encodes_the_real_url(app):
    """The QR must carry the link, not a placeholder."""
    from carraway.ui.qr import encode

    url = "https://money.example.com/pair/abc123"
    dialog = view.AddPhoneDialog(url, "2026-09-01")
    codes = dialog.findChildren(view.QRCode)
    assert codes and codes[0]._grid == encode(url)


def test_collecting_reports_what_could_not_be_matched(app, paired, monkeypatch):
    ledger, _ = paired
    monkeypatch.setattr(
        Ledger,
        "collect_from_pocket",
        lambda self: {
            "configured": True,
            "added": 2,
            "skipped": 1,
            "unmatched": ["Bodega coffee"],
        },
    )
    card = view.PocketCard(ledger)
    _settle(app, card.runner)
    card._collect()
    _settle(app, card.runner)
    text = card.status.text()
    assert "Brought in 2" in text
    assert "1 were already here" in text
    # The unmatched one must be named, not silently dropped.
    assert "Bodega coffee" in text


def test_revoking_asks_first_and_then_revokes(app, paired, monkeypatch):
    ledger, client = paired
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    card = view.PocketCard(ledger)
    _settle(app, card.runner)
    card._revoke({"id": "d1", "name": "iPhone"})
    _settle(app, card.runner)
    assert client.revoked == ["d1"]


def test_declining_the_revoke_prompt_leaves_the_device_alone(app, paired, monkeypatch):
    ledger, client = paired
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    card = view.PocketCard(ledger)
    _settle(app, card.runner)
    card._revoke({"id": "d1", "name": "iPhone"})
    _settle(app, card.runner)
    assert client.revoked == []


def test_disconnecting_forgets_the_inbox(app, paired, monkeypatch):
    ledger, _ = paired
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    forgotten: list[bool] = []
    monkeypatch.setattr(Ledger, "unpair_pocket", lambda self: forgotten.append(True))
    card = view.PocketCard(ledger)
    _settle(app, card.runner)
    card._disconnect()
    assert forgotten == [True]


# -- publishing ---------------------------------------------------------


def test_publishing_is_skipped_when_pocket_is_not_set_up(app, ledger):
    from PySide6.QtWidgets import QWidget

    assert view.publish_in_background(QWidget(), ledger) is False


def test_publishing_sends_the_snapshot(app, paired):
    from PySide6.QtWidgets import QWidget

    ledger, client = paired
    owner = QWidget()
    assert view.publish_in_background(owner, ledger) is True
    _settle(app, owner._pocket_publisher)
    assert client.published == [ledger.pocket_snapshot()]


def test_a_failing_publish_stays_quiet(app, paired, monkeypatch):
    """A server that is down must not interrupt someone reading their spending."""
    from PySide6.QtWidgets import QWidget

    ledger, _ = paired
    monkeypatch.setattr(
        Ledger, "publish_to_pocket", lambda self: (_ for _ in ()).throw(OSError("down"))
    )
    owner = QWidget()
    view.publish_in_background(owner, ledger)
    _settle(app, owner._pocket_publisher)  # must not raise


def test_the_snapshot_carries_no_merchants_or_accounts(app, paired):
    """The whole security argument rests on this being true."""
    ledger, _ = paired
    snapshot = ledger.pocket_snapshot()
    flat = repr(snapshot).lower()
    for forbidden in ("cash", "account", "balance", "transaction"):
        assert forbidden not in flat.replace("'category'", ""), forbidden


# -- renaming an account ------------------------------------------------


def test_renaming_an_account_saves_the_new_name(app, ledger, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from carraway.ui.views.settings import SettingsView

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Pocket money", True))
    view = SettingsView(ledger)
    view._rename_account("cash")

    ledger.load()
    assert [a.name for a in ledger.accounts] == ["Pocket money"]


def test_cancelling_the_rename_changes_nothing(app, ledger, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from carraway.ui.views.settings import SettingsView

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Something else", False))
    SettingsView(ledger)._rename_account("cash")
    ledger.load()
    assert [a.name for a in ledger.accounts] == ["Cash"]


def test_an_empty_rename_is_ignored(app, ledger, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from carraway.ui.views.settings import SettingsView

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("   ", True))
    SettingsView(ledger)._rename_account("cash")
    ledger.load()
    assert [a.name for a in ledger.accounts] == ["Cash"]


def test_a_duplicate_name_is_refused(app, ledger, monkeypatch):
    """Two accounts with one name makes every list ambiguous."""
    from PySide6.QtWidgets import QInputDialog

    from carraway.core import db
    from carraway.core.models import Account, AccountType
    from carraway.ui.views.settings import SettingsView

    conn = db.connect(ledger.path)
    db.upsert_account(conn, Account(id="card", name="Card", type=AccountType.CREDIT_CARD))
    conn.close()
    ledger.load()

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("card", True))
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))

    SettingsView(ledger)._rename_account("cash")
    ledger.load()
    assert warned and "already called" in warned[0]
    assert sorted(a.name for a in ledger.accounts) == ["Card", "Cash"]
