"""When a tracked subscription last billed.

A detected series knows its own dates: it was found in real charges, and the
next one is counted forward from the last that actually happened. A tracked
entry has none of that, so the next charge can only be projected from a date
the user supplies -- and without one, the Next charge column is simply blank,
which is what an entry imported from a spreadsheet looks like.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .add_subscription import next_charge


class BillingDateDialog(QDialog):
    def __init__(
        self,
        merchant: str,
        cadence: str,
        current: date | None,
        parent: QWidget | None = None,
        suggested: date | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"When did {merchant} last bill?")
        self.cadence = cadence
        self.chosen: date | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        blurb = QLabel(
            "The next charge is counted forward from this date, so it is worth "
            "getting roughly right. An approximate date gives an approximate "
            "answer; no date gives none at all."
        )
        blurb.setWordWrap(True)
        blurb.setObjectName("Muted")
        layout.addWidget(blurb)

        form = QFormLayout()
        self.field = QDateEdit()
        self.field.setCalendarPopup(True)
        self.field.setDisplayFormat("yyyy-MM-dd")
        # Prefer what is already set, then a charge in the statements that
        # looks like this entry, and only then today -- which is a placeholder
        # rather than an answer.
        opening = current or suggested
        self.field.setDate(QDate(opening) if opening else QDate.currentDate())
        # A billing date is in the past by definition -- it already happened.
        self.field.setMaximumDate(QDate.currentDate())
        self.field.dateChanged.connect(lambda _: self._preview())
        form.addRow("Last billed", self.field)
        layout.addLayout(form)

        if suggested is not None and current is None:
            found = QLabel(
                f"Found a charge on {suggested.isoformat()} that looks like this "
                "one, and filled it in. Change it if that is not the right one."
            )
            found.setWordWrap(True)
            found.setObjectName("Accent")
            layout.addWidget(found)

        self.preview = QLabel("")
        self.preview.setObjectName("Muted")
        layout.addWidget(self.preview)

        self.forget = QCheckBox("Don't know — leave the next charge blank")
        self.forget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forget.toggled.connect(self._toggle)
        layout.addWidget(self.forget)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._preview()

    def _toggle(self, clearing: bool) -> None:
        self.field.setEnabled(not clearing)
        self._preview()

    def _preview(self) -> None:
        if self.forget.isChecked():
            self.preview.setText("No next charge will be shown.")
            return
        started = self.field.date().toPython()
        self.preview.setText(f"Next charge would be {next_charge(started, self.cadence)}.")

    def _accept(self) -> None:
        # None is a real answer here -- "I do not know" -- and is why the
        # caller distinguishes it from the dialog being cancelled.
        self.chosen = None if self.forget.isChecked() else self.field.date().toPython()
        self.accept()


def prompt(
    merchant: str,
    cadence: str,
    current: date | None,
    parent: QWidget | None = None,
    suggested: date | None = None,
) -> date | None | object:
    """Ask for a billing date. Returns the date, None to clear, or CANCELLED."""
    dialog = BillingDateDialog(merchant, cadence, current, parent, suggested)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return CANCELLED
    return dialog.chosen


# A sentinel, because None already means "clear it" and the caller has to be
# able to tell that apart from the user pressing Cancel.
CANCELLED = object()
