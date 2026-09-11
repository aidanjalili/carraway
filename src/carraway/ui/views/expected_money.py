"""Say that money is on its way before the bank has seen it.

A cheque in the post is the case this exists for. The money is yours, the
figure is known, and until it clears net worth is wrong by exactly that much
— so you say so, and delete the entry when it lands.

The one rule the whole screen is built around: **the real figure and the
projected one are never the same number.** Net worth keeps saying what the
bank says. What is coming is added on a line of its own, named, and it can be
removed in one click. An unconfirmed amount quietly folded into a balance is
a figure nobody can audit six weeks later, which is the opposite of the point
of writing it down.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ...core.models import Account
from ...core.money import Money

_UNKNOWN_ACCOUNT = "Not sure yet"

# What the amount means. Kept as words rather than a minus sign the user has
# to remember to type: "a cheque for 400" and "a bill for 400" are the two
# things anyone actually has in mind, and one of them is negative.
_ARRIVING = "coming in"
_LEAVING = "going out"


class ExpectedMoneyDialog(QDialog):
    """Ask for one thing that has not landed yet."""

    def __init__(
        self,
        accounts: list[Account] | None = None,
        current_net: Money | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Money on its way")
        self.setMinimumWidth(460)
        self._current_net = current_net

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel("Money on its way")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "A cheque in the post, a reimbursement owed to you, a bill you know "
            "is coming. Carraway will show what your net worth becomes once it "
            "lands, without touching the figure your bank reports. Delete the "
            "entry when the money actually arrives."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        form = QFormLayout()
        form.setSpacing(9)

        self.description = QLineEdit()
        self.description.setPlaceholderText("Tax refund cheque")
        form.addRow("What it is", self.description)

        amount_row = QHBoxLayout()
        amount_row.setSpacing(8)
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("412.50")
        amount_row.addWidget(self.amount, 1)
        self.direction = QComboBox()
        self.direction.addItems([_ARRIVING, _LEAVING])
        amount_row.addWidget(self.direction)
        form.addRow("Amount", amount_row)

        # Optional on purpose. "Sometime this month" is often all anyone
        # knows, and a required date would mean inventing one that then looks
        # like a fact. The picker stays visible while it is off -- greyed but
        # readable -- rather than appearing and disappearing under the cursor.
        self.dated = QCheckBox("I know roughly when")
        self.dated.setChecked(True)
        self.expected_on = QDateEdit()
        self.expected_on.setCalendarPopup(True)
        self.expected_on.setDisplayFormat("yyyy-MM-dd")
        self.expected_on.setDate(QDate.currentDate().addDays(7))
        self.expected_on.calendarWidget().setGridVisible(True)
        when = QVBoxLayout()
        when.setContentsMargins(0, 0, 0, 0)
        when.setSpacing(5)
        when.addWidget(self.dated)
        when.addWidget(self.expected_on)
        form.addRow("Expected", when)
        self.dated.toggled.connect(self.expected_on.setEnabled)

        self.account = QComboBox()
        self.account.addItem(_UNKNOWN_ACCOUNT, "")
        for account in accounts or []:
            label = (
                f"{account.name} · {account.institution}" if account.institution else account.name
            )
            self.account.addItem(label, account.id)
        self.account.setToolTip(
            "Only matters if that account is one you have left out of net "
            "worth — then this is left out of it too."
        )
        form.addRow("Landing in", self.account)

        self.note = QLineEdit()
        self.note.setPlaceholderText("posted Tuesday, should clear within the week")
        form.addRow("Note", self.note)
        layout.addLayout(form)

        self.preview = QLabel("")
        self.preview.setObjectName("Muted")
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.amount.textChanged.connect(self._update_preview)
        self.direction.currentTextChanged.connect(lambda _: self._update_preview())

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    # -- the figure being previewed ---------------------------------------

    def _magnitude(self) -> Money | None:
        raw = self.amount.text().strip().replace("$", "").replace(",", "")
        if not raw:
            return None
        try:
            return abs(Money.parse(raw))
        except (ValueError, TypeError, ArithmeticError):
            return None

    def _signed(self) -> Money | None:
        """The amount with the sign the direction asks for."""
        magnitude = self._magnitude()
        if magnitude is None:
            return None
        if self.direction.currentText() == _LEAVING:
            return Money(-magnitude.minor, magnitude.currency)
        return magnitude

    def _update_preview(self) -> None:
        """Say what net worth becomes, as they type.

        The whole reason for the feature is a number the user is trying to
        arrive at, so showing it before they commit is most of the value.
        """
        amount = self._signed()
        if amount is None or self._current_net is None:
            self.preview.setText("")
            return
        after = Money(self._current_net.minor + amount.minor, self._current_net.currency)
        self.preview.setText(
            f"Net worth reads {self._current_net.format()} today. "
            f"With this it would be {after.format()}."
        )

    def _accept(self) -> None:
        if not self.description.text().strip():
            self.preview.setText("Say what it is first.")
            return
        amount = self._signed()
        if amount is None:
            self.preview.setText(f"'{self.amount.text()}' is not an amount I can read.")
            return
        if amount.minor == 0:
            self.preview.setText("Nothing arriving is not worth writing down.")
            return
        self.accept()

    @property
    def values(self) -> dict:
        return {
            "description": self.description.text().strip(),
            "amount": self._signed() or Money.zero(),
            "expected_on": (
                self.expected_on.date().toPython() if self.dated.isChecked() else None
            ),
            "account_id": self.account.currentData() or "",
            "note": self.note.text().strip(),
        }


def prompt(
    accounts: list[Account] | None = None,
    current_net: Money | None = None,
    parent=None,
) -> dict | None:
    """Run the dialog. Returns the values, or None if cancelled."""
    dialog = ExpectedMoneyDialog(accounts, current_net, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.values


def describe(entry) -> str:
    """One line for a list: when it lands, or that nobody knows."""
    if entry.expected_on is None:
        return "date unknown"
    days = (entry.expected_on - date.today()).days
    if days < 0:
        # Worth saying rather than showing a date that has quietly gone by:
        # a cheque that was due last week is the one to chase.
        return f"{entry.expected_on} · {abs(days)} days overdue"
    if days == 0:
        return f"{entry.expected_on} · today"
    return f"{entry.expected_on} · in {days} days"
