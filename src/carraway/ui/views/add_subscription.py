"""Record a subscription the app cannot possibly detect.

Anything paid through Venmo, Zelle or PayPal reaches the bank as "VENMO
PAYMENT", never as the service behind it, so no amount of pattern detection
will ever find it. Comparing a real user's own list against detection showed
this was most of what was missing — seven of ten misses were paid through an
intermediary, and the rest were annual charges with too few occurrences to
establish a pattern.

The honest answer is to let the user say what they know.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ...core.money import Money

_CADENCES = ["monthly", "yearly", "weekly", "biweekly", "quarterly"]
_PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}


class AddSubscriptionDialog(QDialog):
    """Ask for the details of a subscription the ledger cannot show."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Track a subscription")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel("Track a subscription")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "For anything paid through Venmo, Zelle or PayPal, your bank only "
            "ever sees the transfer — never the service. Tell Carraway about it "
            "and it will count towards your totals."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        form = QFormLayout()
        form.setSpacing(9)

        self.merchant = QLineEdit()
        self.merchant.setPlaceholderText("T-Mobile")
        form.addRow("Service", self.merchant)

        self.amount = QLineEdit()
        self.amount.setPlaceholderText("35.00")
        form.addRow("Amount", self.amount)

        self.cadence = QComboBox()
        self.cadence.addItems(_CADENCES)
        form.addRow("Billed", self.cadence)

        self.kind = QComboBox()
        self.kind.addItems(["subscription", "bill"])
        form.addRow("Kind", self.kind)

        self.paid_via = QLineEdit()
        self.paid_via.setPlaceholderText("venmo to dad")
        form.addRow("Paid via", self.paid_via)

        self.notes = QLineEdit()
        form.addRow("Notes", self.notes)
        layout.addLayout(form)

        self.preview = QLabel("")
        self.preview.setObjectName("Muted")
        layout.addWidget(self.preview)
        self.amount.textChanged.connect(self._update_preview)
        self.cadence.currentTextChanged.connect(self._update_preview)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _update_preview(self) -> None:
        """Show the annual cost as they type: $15/month reads differently from $180/year."""
        amount = self._amount_or_none()
        if amount is None:
            self.preview.setText("")
            return
        per_year = _PER_YEAR.get(self.cadence.currentText(), 0)
        yearly = abs(amount) * per_year
        self.preview.setText(f"That is {yearly.format()} a year.")

    def _amount_or_none(self) -> Money | None:
        raw = self.amount.text().strip().replace("$", "").replace(",", "")
        if not raw:
            return None
        try:
            return Money.parse(raw)
        except (ValueError, TypeError):
            return None

    def _accept(self) -> None:
        if not self.merchant.text().strip():
            self.preview.setText("Give it a name first.")
            return
        if self._amount_or_none() is None:
            self.preview.setText(f"'{self.amount.text()}' is not an amount I can read.")
            return
        self.accept()

    @property
    def values(self) -> dict:
        return {
            "merchant": self.merchant.text().strip(),
            "amount": self._amount_or_none() or Money.zero(),
            "cadence": self.cadence.currentText(),
            "kind": self.kind.currentText(),
            "paid_via": self.paid_via.text().strip(),
            "notes": self.notes.text().strip(),
        }


def prompt(parent=None) -> dict | None:
    """Run the dialog. Returns the values, or None if cancelled."""
    dialog = AddSubscriptionDialog(parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.values
