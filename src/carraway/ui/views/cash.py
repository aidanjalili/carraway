"""Cash: the one account no feed can tell you about.

Every other account in Carraway is synced or imported, and the balance is
whatever the bank last said. Cash is different — a note spent at a market
leaves no record anywhere, so the user is the only source, and the app has to
let them say both what moved and what is actually left.

Those two answers can disagree, and the disagreement is the useful part. If
you remember spending money but not on what, the honest record is a balance
you typed plus a line saying the history is short by that much — not a
silently adjusted number that makes the ledger look complete when it is not.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ...core.money import Money


def _parse(text: str) -> Money | None:
    raw = text.strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        return Money.parse(raw)
    except (ValueError, TypeError):
        return None


class SetBalanceDialog(QDialog):
    """Ask what the account actually holds, and offer to reconcile."""

    def __init__(self, account: str, implied: Money | None, parent=None) -> None:
        super().__init__(parent)
        self.implied = implied
        # The gap is kept as a value rather than read back off the checkbox's
        # visibility: a widget that has never been shown reports itself
        # invisible, so asking the UI what the user meant gets the wrong
        # answer in exactly the cases worth testing.
        self.gap = Money.zero()
        self.setWindowTitle(f"How much is in {account}?")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel(f"How much is in {account}?")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "Type what is really there. Carraway records it as today's balance "
            "and uses it for net worth."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        form = QFormLayout()
        form.setSpacing(9)
        self.amount = QLineEdit()
        if implied is not None:
            self.amount.setText(f"{implied.decimal:.2f}")
            self.amount.selectAll()
        self.amount.setPlaceholderText("240.00")
        form.addRow("Balance", self.amount)
        layout.addLayout(form)

        self.gap_note = QLabel("")
        self.gap_note.setWordWrap(True)
        layout.addWidget(self.gap_note)

        # Ticked by default: someone who has just corrected a balance almost
        # always does want the history to match it. Untickable because the
        # alternative — a line the user did not ask for — is worse.
        self.correct = QCheckBox("Add a line for the difference")
        self.correct.setChecked(True)
        self.correct.setToolTip(
            "Records the gap as a 'Cash adjustment' transaction dated today, so "
            "your transaction history adds up to the balance you just typed."
        )
        self.correct.setVisible(False)
        layout.addWidget(self.correct)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.amount.textChanged.connect(lambda _: self._update_gap())
        self._update_gap()

    def _update_gap(self) -> None:
        """Say what the records currently claim, and by how much it differs."""
        typed = _parse(self.amount.text())
        if self.implied is None:
            self.gap = typed or Money.zero()
            self.gap_note.setObjectName("Muted")
            self.gap_note.setText("Nothing on record for this account yet.")
            self.correct.setVisible(self.gap.minor != 0)
            self._restyle()
            return
        if typed is None:
            self.gap = Money.zero()
            self.gap_note.setText(f"Your records say {self.implied.format()}.")
            self.gap_note.setObjectName("Muted")
            self.correct.setVisible(False)
            self._restyle()
            return

        gap = self.gap = Money(typed.minor - self.implied.minor, typed.currency)
        if gap.minor == 0:
            self.gap_note.setObjectName("Muted")
            self.gap_note.setText("That matches your records exactly.")
            self.correct.setVisible(False)
        else:
            short = gap.minor < 0
            self.gap_note.setObjectName("Danger" if short else "Accent")
            direction = "less than" if short else "more than"
            self.gap_note.setText(
                f"That is {abs(gap).format()} {direction} your records show "
                f"({self.implied.format()})."
            )
            self.correct.setText(
                f"Add a {abs(gap).format()} "
                f"{'spending' if short else 'income'} line for the difference"
            )
            self.correct.setVisible(True)
        self._restyle()

    def _restyle(self) -> None:
        """Qt only re-reads a stylesheet when the object name changes under it."""
        self.gap_note.style().unpolish(self.gap_note)
        self.gap_note.style().polish(self.gap_note)

    def _accept(self) -> None:
        if _parse(self.amount.text()) is None:
            self.gap_note.setObjectName("Danger")
            self.gap_note.setText(f"'{self.amount.text()}' is not an amount I can read.")
            self._restyle()
            return
        self.accept()

    @property
    def result_values(self) -> dict:
        return {
            "amount": _parse(self.amount.text()) or Money.zero(),
            # A correction only means anything when there is a gap to correct.
            "correction": self.gap.minor != 0 and self.correct.isChecked(),
        }


class AddCashTransactionDialog(QDialog):
    """Record one movement of cash by hand."""

    def __init__(self, account: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Add a {account} transaction")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel(f"Add a {account} transaction")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "Negative for money spent, positive for money received — the same "
            "convention as every other account here."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        form = QFormLayout()
        form.setSpacing(9)

        self.when = QDateEdit()
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("yyyy-MM-dd")
        self.when.setDate(QDate.currentDate())
        form.addRow("Date", self.when)

        self.description = QLineEdit()
        self.description.setPlaceholderText("Farmers market")
        form.addRow("Description", self.description)

        self.amount = QLineEdit()
        self.amount.setPlaceholderText("-24.00")
        form.addRow("Amount", self.amount)
        layout.addLayout(form)

        self.warning = QLabel("")
        self.warning.setObjectName("Danger")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        if not self.description.text().strip():
            self.warning.setText("Give it a description first.")
            return
        amount = _parse(self.amount.text())
        if amount is None:
            self.warning.setText(f"'{self.amount.text()}' is not an amount I can read.")
            return
        if amount.minor == 0:
            self.warning.setText("A transaction for nothing is not worth recording.")
            return
        self.accept()

    @property
    def result_values(self) -> dict:
        chosen = self.when.date()
        return {
            "when": date(chosen.year(), chosen.month(), chosen.day()),
            "description": self.description.text().strip(),
            "amount": _parse(self.amount.text()) or Money.zero(),
        }


def ask_balance(account: str, implied: Money | None, parent=None) -> dict | None:
    dialog = SetBalanceDialog(account, implied, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.result_values


def ask_transaction(account: str, parent=None) -> dict | None:
    dialog = AddCashTransactionDialog(account, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.result_values
