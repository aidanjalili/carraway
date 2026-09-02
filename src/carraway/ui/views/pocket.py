"""Carraway Pocket: the phone half, seen from the desktop.

The desktop is the source of truth and the server is a post box. That shapes
this screen more than anything else: there is no "sign in", no account, and
nothing to configure beyond which inbox this computer talks to. What there
*is* is the ability to admit a phone, see what is waiting, bring it in, and
cut a device off — in that order, because that is the order they happen in.

Pairing is a link, shown as a QR code, because the alternative is reading
sixteen random characters off a laptop screen and typing them into a phone
keyboard. Every network call runs off the painting thread; they are single
round trips, but a phone tethered on a train is still a phone on a train.
"""

from __future__ import annotations

import atexit

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..data import Ledger
from ..widgets import Card, QRCode

# Threads in flight, held here rather than only on the widget that started
# them. A card can be closed while a round trip is outstanding, and Qt aborts
# the process if it deletes a QThread that is still running -- so the thread
# has to outlive the widget, and this is what keeps it referenced until it
# has actually finished.
_IN_FLIGHT: set = set()


def _wait_for_in_flight() -> None:
    """Let any outstanding request finish before the process goes away.

    Qt aborts if a QThread is destroyed while still running, so an app that
    quits during a publish takes a SIGABRT on the way out -- create a budget,
    close the window straight away, and the last thing Carraway does is crash.
    Holding the threads in `_IN_FLIGHT` kept them alive; nothing waited for
    them. Two seconds each is generous for a request that has already been
    given twenty, and it is a bounded wait rather than a hang.
    """
    for thread in list(_IN_FLIGHT):
        thread.quit()
        thread.wait(2000)
    _IN_FLIGHT.clear()


_ATEXIT_ARMED = False


def _arm_shutdown() -> None:
    """Make sure the wait happens however the process ends.

    Two hooks, because they cover different exits. `aboutToQuit` fires when a
    running event loop is asked to stop -- the normal case, closing the
    window. `atexit` catches the rest: a script that never ran an event loop,
    or an interpreter shutting down for any other reason. Either way the
    QThread must not be collected while it is still running, or Qt aborts.
    """
    global _ATEXIT_ARMED
    if not _ATEXIT_ARMED:
        atexit.register(_wait_for_in_flight)
        _ATEXIT_ARMED = True

    app = QCoreApplication.instance()
    if app is None or getattr(app, "_pocket_shutdown_armed", False):
        return
    app.aboutToQuit.connect(_wait_for_in_flight)
    app._pocket_shutdown_armed = True


class _Task(QObject):
    """Runs one callable off the painting thread and reports back."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, work) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        from ...sync.pocket import PocketError

        try:
            self.done.emit(self._work())
        except PocketError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            # A failed round trip must never take the window down with it.
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _Runner(QObject):
    """Owns the thread, so callers do not have to.

    The callbacks are bound methods of this object rather than lambdas
    connected straight to the worker's signals, and that is load-bearing. A
    bare lambda has no thread affinity, so Qt runs it on the thread that
    emitted -- meaning teardown would call `wait()` on the worker thread from
    inside the worker thread, and Qt aborts the process. Bound methods of a
    QObject living on the GUI thread give a queued connection, which is what
    puts the result back where the widgets are.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._task: _Task | None = None
        self._on_done = None
        self._on_failed = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, work, on_done, on_failed) -> bool:
        """Run `work` off the GUI thread. False if one is already in flight."""
        if self.busy:
            return False
        self._on_done, self._on_failed = on_done, on_failed
        # Unparented on purpose: see _IN_FLIGHT.
        self._thread = QThread()
        self._task = _Task(work)
        self._task.moveToThread(self._thread)
        self._thread.started.connect(self._task.run)
        self._task.done.connect(self._done)
        self._task.failed.connect(self._failed)
        _arm_shutdown()
        _IN_FLIGHT.add(self._thread)
        self._thread.finished.connect(lambda t=self._thread: _IN_FLIGHT.discard(t))
        self._thread.start()
        return True

    def stop(self) -> None:
        """Wait for anything in flight. Called before the widget goes away."""
        self._teardown()

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
            self._task = None

    def _done(self, value) -> None:
        callback, self._on_done = self._on_done, None
        self._teardown()
        if callback is not None:
            callback(value)

    def _failed(self, message: str) -> None:
        callback, self._on_failed = self._on_failed, None
        self._teardown()
        if callback is not None:
            callback(message)


class ConnectDialog(QDialog):
    """Paste the pairing link that `pocket-admin pair` printed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to your Pocket inbox")
        self.link = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        explain = QLabel(
            "On your server, run <code>pocket-admin pair --label laptop</code>. "
            "It prints a link that works once, for fifteen minutes. Paste it here."
        )
        explain.setWordWrap(True)
        explain.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(explain)

        form = QFormLayout()
        self.field = QLineEdit()
        self.field.setPlaceholderText("https://money.example.com/pair/…")
        form.addRow("Pairing link", self.field)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        self.link = self.field.text().strip()
        if self.link:
            self.accept()


class AddPhoneDialog(QDialog):
    """Shows a freshly minted pairing link, as a picture and as text."""

    def __init__(self, url: str, expires: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a phone")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Pair your phone")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        # The heading used to say "point your camera at this", directly
        # contradicting the instructions below it -- which say not to, on the
        # one platform this is actually used from. The code is the path that
        # works on an iPhone; the QR is the convenience for everything else.
        lead = QLabel(
            "On iPhone, add Pocket to your Home Screen <b>first</b>, then open it "
            "from the icon and type the code below. Scanning pairs Safari, and "
            "iOS keeps a Home Screen app's login separate from Safari — so the "
            "icon would still say “Not paired”."
        )
        lead.setWordWrap(True)
        lead.setTextFormat(Qt.TextFormat.RichText)
        lead.setObjectName("Muted")
        layout.addWidget(lead)

        code = QRCode(url)
        code.setFixedSize(260, 260)
        layout.addWidget(code, alignment=Qt.AlignmentFlag.AlignHCenter)

        steps = QLabel("Anywhere else, scanning the code above is enough.")
        steps.setWordWrap(True)
        steps.setObjectName("Muted")
        layout.addWidget(steps)

        # The code on its own, because that is what gets typed into the
        # home-screen app -- where scanning cannot help. Shown in two groups
        # of four: it is read off this screen a chunk at a time, and the
        # server throws separators away before matching.
        code = url.rsplit("/pair/", 1)[-1]
        if len(code) == 8:
            code = f"{code[:4]}-{code[4:]}"
        code_label = QLabel("Pairing code")
        code_label.setObjectName("Muted")
        layout.addWidget(code_label)

        code_field = QLineEdit(code)
        code_field.setReadOnly(True)
        font = QFont("monospace")
        font.setPointSize(font.pointSize() + 6)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        code_field.setFont(font)
        code_field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(code_field)

        # And the whole link: a camera that will not focus is not a reason to
        # be unable to pair a phone.
        field = QLineEdit(url)
        field.setReadOnly(True)
        field.setCursorPosition(0)
        layout.addWidget(field)

        expiry = QLabel(f"Good once, until {expires or 'shortly'}.")
        expiry.setObjectName("Muted")
        layout.addWidget(expiry)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class VaultKeyDialog(QDialog):
    """Shows the vault key. The one time it is ever displayed."""

    def __init__(self, key: str, where: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Your vault key")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QLabel("Type this into Pocket on your phone")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        field = QLineEdit(key)
        field.setReadOnly(True)
        font = QFont("monospace")
        font.setPointSize(font.pointSize() + 5)
        field.setFont(font)
        field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(field)

        explain = QLabel(
            "Your history is encrypted with this before it leaves this computer, "
            "so the server stores something it cannot read — and neither can "
            "anyone who takes the server.<br><br>"
            f"It is kept in {where}. You only need to type it into each phone "
            "once. If you lose it, make a new one and enter that instead; the "
            "old history simply becomes unreadable."
        )
        explain.setWordWrap(True)
        explain.setTextFormat(Qt.TextFormat.RichText)
        explain.setObjectName("Muted")
        layout.addWidget(explain)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class PocketCard(Card):
    """The whole Pocket section of Settings, in one self-contained card."""

    def __init__(self, ledger: Ledger, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ledger = ledger
        self.runner = _Runner(self)

        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(20, 16, 20, 16)
        self.layout_.setSpacing(10)

        heading = QLabel("Carraway Pocket")
        heading.setObjectName("SectionHeading")
        self.layout_.addWidget(heading)

        self.blurb = QLabel()
        self.blurb.setWordWrap(True)
        self.blurb.setObjectName("Muted")
        self.layout_.addWidget(self.blurb)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.layout_.addWidget(self.status)

        self.devices = QVBoxLayout()
        self.devices.setSpacing(4)
        self.layout_.addLayout(self.devices)

        self.buttons = QHBoxLayout()
        self.layout_.addLayout(self.buttons)

        self.rebuild()

    # -- rendering -------------------------------------------------------

    def _clear(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear(item.layout())

    def _button(self, label: str, slot, *, primary: bool = False) -> QPushButton:
        button = QPushButton(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if primary:
            button.setObjectName("Primary")
        button.clicked.connect(slot)
        self.buttons.addWidget(button)
        return button

    def rebuild(self) -> None:
        self._clear(self.buttons)
        self._clear(self.devices)

        if not self.ledger.pocket_configured:
            self.blurb.setText(
                "Log cash from your phone. Carraway runs a small inbox on your own "
                "server; the phone posts what you spent, and this computer collects "
                "it. The server never sees your transactions, balances, or bank."
            )
            self.status.setText("Not connected.")
            self._button("Connect this computer…", self._connect, primary=True)
            self.buttons.addStretch(1)
            return

        url = str(self.ledger.setting("pocket_url") or "")
        host = url.split("://", 1)[-1]
        self.blurb.setText(f"Connected to {host}.")
        self.status.setText("Checking…")

        self._button("Collect now", self._collect, primary=True)
        self._button("Add a phone…", self._add_phone)
        self._button(
            "Show vault key…" if self.ledger.vault_key() else "Encrypt my history…",
            self._vault_key,
        )
        self._button("Disconnect", self._disconnect)
        self.buttons.addStretch(1)
        self._refresh_status()

    def _refresh_status(self, prefix: str = "") -> None:
        """Ask the server what is waiting, and who is trusted.

        `prefix` keeps whatever just happened in front of the count. Without
        it, the report of a collection is replaced by a status line a moment
        later and the user never finds out what came in.
        """
        client = self.ledger.pocket_client()
        if client is None:
            return

        def work():
            return client.status(), client.devices()

        def done(value) -> None:
            state, devices = value
            waiting = int(state.get("pending", 0))
            if waiting:
                oldest = state.get("oldest_days")
                age = f", oldest {oldest} days old" if oldest else ""
                tail = f"<b>{waiting} waiting to be collected</b>{age}."
            else:
                tail = "Nothing waiting. Everything has been collected."
            self.status.setTextFormat(Qt.TextFormat.RichText)
            self.status.setText(f"{prefix} {tail}".strip())
            self._show_devices(devices)

        self.runner.start(work, done, lambda message: self.status.setText(message))

    def _show_devices(self, devices: list) -> None:
        self._clear(self.devices)
        live = [d for d in devices if not d.get("revoked")]
        if not live:
            empty = QLabel("No phone paired yet.")
            empty.setObjectName("Muted")
            self.devices.addWidget(empty)
            return
        for device in live:
            row = QHBoxLayout()
            seen = device.get("last_seen") or "never"
            label = QLabel(f"{device.get('name') or 'Unnamed device'} — last seen {seen}")
            label.setObjectName("Muted")
            row.addWidget(label)
            row.addStretch(1)
            cut = QPushButton("Revoke")
            cut.setCursor(Qt.CursorShape.PointingHandCursor)
            cut.clicked.connect(lambda _=False, d=device: self._revoke(d))
            row.addWidget(cut)
            self.devices.addLayout(row)

    # -- actions ---------------------------------------------------------

    def _connect(self) -> None:
        dialog = ConnectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        link = dialog.link

        def done(where) -> None:
            QMessageBox.information(
                self,
                "Connected",
                f"This computer is paired. The token is kept in {where}.",
            )
            self.rebuild()

        self.runner.start(
            lambda: self.ledger.pair_pocket(link),
            done,
            lambda message: QMessageBox.warning(self, "Could not pair", message),
        )

    def _add_phone(self) -> None:
        client = self.ledger.pocket_client()
        if client is None:
            return

        def done(pairing) -> None:
            AddPhoneDialog(
                pairing.get("url", ""),
                str(pairing.get("expires_at", "")).replace("T", " at ")[:19],
                self,
            ).exec()
            self._refresh_status()

        self.runner.start(
            lambda: client.create_pairing("iPhone"),
            done,
            lambda message: QMessageBox.warning(self, "Could not add a phone", message),
        )

    def _collect(self) -> None:
        self.status.setText("Collecting…")

        def done(result) -> None:
            added = result.get("added", 0)
            skipped = result.get("skipped", 0)
            unmatched = result.get("unmatched") or []
            corrections = result.get("corrections") or []
            parts = [f"Brought in {added}."] if added else ["Nothing new to bring in."]
            if skipped:
                parts.append(f"{skipped} were already here.")
            for made in corrections:
                # Named outright: a transaction nobody typed should never
                # appear in the ledger without having been mentioned.
                gap = made["correction"]
                if gap.minor:
                    parts.append(
                        f"You counted {made['counted'].format()} in {made['account']}, "
                        f"so {gap.format()} was added to square it up."
                    )
                else:
                    parts.append(
                        f"You counted {made['counted'].format()} in {made['account']}, "
                        "which is exactly what was expected."
                    )
            if unmatched:
                # Named an account this ledger does not have. Left on the
                # server rather than filed under a guess.
                parts.append(
                    f"{len(unmatched)} could not be matched to an account "
                    f"and are still waiting: {', '.join(unmatched[:3])}."
                )
            from ..widgets import refresh_everything

            refresh_everything(self)
            # What was just collected belongs in the history the phone reads,
            # and a correction from a wallet count changes the figures it is
            # showing. Sending it back straight away closes that loop.
            publish_in_background(self, self.ledger)
            self._refresh_status(prefix=" ".join(parts))

        self.runner.start(
            self.ledger.collect_from_pocket,
            done,
            lambda message: self.status.setText(message),
        )

    def _revoke(self, device: dict) -> None:
        client = self.ledger.pocket_client()
        if client is None:
            return
        name = device.get("name") or "that device"
        confirm = QMessageBox.question(
            self,
            "Revoke this device?",
            f"{name} will stop being able to post to your inbox. You can always pair it again.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.runner.start(
            lambda: client.revoke(str(device.get("id"))),
            lambda _: self._refresh_status(),
            lambda message: QMessageBox.warning(self, "Could not revoke", message),
        )

    def _vault_key(self) -> None:
        """Show the vault key, minting one the first time.

        Sending history at all is opt-in, and this is the switch: until there
        is a key, nothing but category names and figures ever leaves.
        """
        from ...sync import credentials

        key = self.ledger.vault_key()
        if key is None:
            agreed = QMessageBox.question(
                self,
                "Send your history to your phone?",
                "Carraway will encrypt the last 90 days of transactions and put "
                "them where your phone can read them.\n\n"
                "The key never leaves this computer, so the server stores "
                "something it cannot read. You type the key into your phone "
                "once.\n\nWithout this, only category names and figures are "
                "sent.",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel,
            )
            if agreed != QMessageBox.StandardButton.Yes:
                return
            key = self.ledger.new_vault_key()

        from ...sync.vault import format_key

        VaultKeyDialog(format_key(key), credentials.describe_store(), self).exec()
        publish_in_background(self, self.ledger)
        self.rebuild()

    def _disconnect(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Disconnect from Pocket?",
            "This computer will forget the inbox. Anything already collected "
            "stays; anything still waiting on the server stays there until a "
            "paired computer collects it.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.ledger.unpair_pocket()
        self.rebuild()


def publish_in_background(owner: QWidget, ledger: Ledger, *, only_if_changed: bool = False) -> bool:
    """Push the budget summary to the phone, quietly. Returns False if not set up.

    Deliberately silent about failures. This runs after a bank sync, and the
    snapshot is a convenience the phone can live without — a server that is
    down should not put an error in front of someone who was reading their
    spending and never asked about their phone. The phone shows how old its
    copy is, which is the honest place for this to surface.
    """
    if not ledger.pocket_configured:
        return False

    # On a timer, skip when nothing has moved. Sealing is 600,000 PBKDF2
    # rounds and a round trip; doing that every half hour to send a byte-for-
    # byte identical payload is work for its own sake.
    if only_if_changed:
        digest = ledger.pocket_digest()
        if digest == getattr(owner, "_pocket_digest", None):
            return False
        owner._pocket_digest = digest
    # Cached on the owner so repeated syncs do not pile up thread wrappers,
    # and so the runner outlives this function.
    runner = getattr(owner, "_pocket_publisher", None)
    if runner is None:
        runner = _Runner(owner)
        owner._pocket_publisher = runner
    return runner.start(ledger.publish_to_pocket, lambda _: None, lambda _: None)
