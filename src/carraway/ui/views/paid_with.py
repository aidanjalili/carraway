"""Which card or account a subscription actually comes out of.

Detection knows this for free — a charge it found landed in exactly one
account. A tracked entry has no transactions to read it from, so the user has
to say, and what they say splits in two:

* an **account in this ledger**, stored as a reference. It keeps naming itself
  the way every other screen names it, and survives a rename at the bank.
* **anything else** — "venmo to dad", "my mother's card", "paid at the desk".
  Real payment routes that no linked account can represent, and the reason a
  free-text option has to exist rather than a closed list of accounts.

Offered as one control because it is one question, and answering it twice in
two boxes would invite two contradictory answers.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Account

_NOT_RECORDED = "— not recorded —"
_OTHER = "Other…"


class PaidWithPicker(QWidget):
    """A dropdown of linked accounts, with a free-text escape hatch."""

    def __init__(self, accounts: list[Account], parent: QWidget | None = None) -> None:
        """`accounts` is offered in the order given — see `Ledger.payable_accounts`."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self.combo = QComboBox()
        self.combo.addItem(_NOT_RECORDED, "")
        for account in accounts:
            label = (
                f"{account.name} · {account.institution}" if account.institution else account.name
            )
            self.combo.addItem(label, account.id)
        self.combo.addItem(_OTHER, None)
        layout.addWidget(self.combo)

        self.other = QLineEdit()
        self.other.setPlaceholderText("venmo to dad, my mother's card, paid at the desk…")
        self.other.setVisible(False)
        layout.addWidget(self.other)

        self.combo.currentIndexChanged.connect(lambda _: self._sync())

    def _is_other(self) -> bool:
        return self.combo.currentData() is None

    def _sync(self) -> None:
        """Show the text box only when there is something to type into it."""
        self.other.setVisible(self._is_other())
        if self._is_other():
            self.other.setFocus()

    def set_value(self, paid_via: str = "", paid_via_account: str = "") -> None:
        """Show an existing answer. An unknown account id falls back to text."""
        if paid_via_account:
            index = self.combo.findData(paid_via_account)
            if index >= 0:
                self.combo.setCurrentIndex(index)
                self._sync()
                return
        if paid_via:
            self.combo.setCurrentIndex(self.combo.count() - 1)  # Other…
            self.other.setText(paid_via)
        else:
            self.combo.setCurrentIndex(0)
        self._sync()

    @property
    def value(self) -> dict[str, str]:
        """The answer, as the two fields the database stores.

        Only ever one of them: an account link and a description of a
        different route cannot both be true.
        """
        if self._is_other():
            return {"paid_via": self.other.text().strip(), "paid_via_account": ""}
        return {"paid_via": "", "paid_via_account": str(self.combo.currentData() or "")}


class PaidWithDialog(QDialog):
    """Change how one already-tracked subscription is paid for."""

    def __init__(
        self,
        merchant: str,
        accounts: list[Account],
        paid_via: str = "",
        paid_via_account: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"How is {merchant} paid?")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel(f"How is {merchant} paid?")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "Pick the account it bills to, or describe the route if it is not "
            "an account Carraway can see."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self.picker = PaidWithPicker(accounts)
        self.picker.set_value(paid_via, paid_via_account)
        layout.addWidget(self.picker)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def prompt(
    merchant: str,
    accounts: list[Account],
    paid_via: str = "",
    paid_via_account: str = "",
    parent: QWidget | None = None,
) -> dict[str, str] | None:
    """Run the dialog. Returns the two fields, or None if cancelled."""
    dialog = PaidWithDialog(merchant, accounts, paid_via, paid_via_account, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.picker.value
