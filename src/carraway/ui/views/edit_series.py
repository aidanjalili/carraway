"""Correct what detection inferred about a recurring charge.

Detection reads an amount, a cadence and a next date out of history, and
history is sometimes a poor guide: a price rose last week, the billing day
moved, or a merchant's bank descriptor is unreadable. Rather than making the
user live with a wrong figure or delete the series entirely, any field can be
corrected.

Each correction is independent. Fixing the next charge date leaves the amount
inferred, so it keeps improving as more charges arrive — and "Reset" throws
away every correction and goes back to what was detected.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt
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
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.models import RecurringSeries
from ...core.money import Money

_CADENCES = ["weekly", "biweekly", "monthly", "quarterly", "yearly"]


class EditSeriesDialog(QDialog):
    """Edit one series. Only the fields whose boxes are ticked are saved."""

    def __init__(self, series: RecurringSeries, edited: bool, parent=None) -> None:
        super().__init__(parent)
        self.series = series
        self.setWindowTitle(f"Edit {series.merchant}")
        self.setMinimumWidth(460)
        self.reset_requested = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel(series.merchant)
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        detected = QLabel(
            f"Detected from {series.occurrences} charges"
            if series.occurrences
            else "You added this one by hand"
        )
        detected.setObjectName("Muted")
        layout.addWidget(detected)

        blurb = QLabel(
            "Tick a field to correct it. Anything left unticked stays inferred "
            "from your history and keeps improving as more charges arrive."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        form = QFormLayout()
        form.setSpacing(9)

        self.name_on = QCheckBox()
        self.name = QLineEdit(series.merchant)
        form.addRow(self._field("Name", self.name_on), self.name)

        self.amount_on = QCheckBox()
        self.amount = QLineEdit(f"{abs(series.typical_amount).decimal:.2f}")
        form.addRow(self._field("Amount", self.amount_on), self.amount)

        self.cadence_on = QCheckBox()
        self.cadence = QComboBox()
        self.cadence.addItems(_CADENCES)
        if series.cadence in _CADENCES:
            self.cadence.setCurrentText(series.cadence)
        form.addRow(self._field("Billed", self.cadence_on), self.cadence)

        self.date_on = QCheckBox()
        self.next_date = QDateEdit()
        self.next_date.setCalendarPopup(True)
        self.next_date.setDisplayFormat("yyyy-MM-dd")
        when = series.next_expected or date.today()
        self.next_date.setDate(QDate(when.year, when.month, when.day))
        form.addRow(self._field("Next charge", self.date_on), self.next_date)

        layout.addLayout(form)

        # Ticking a box is how a field is saved, so editing one should tick it
        # rather than making the user do both.
        self.name.textEdited.connect(lambda _: self.name_on.setChecked(True))
        self.amount.textEdited.connect(lambda _: self.amount_on.setChecked(True))
        self.cadence.activated.connect(lambda _: self.cadence_on.setChecked(True))
        self.next_date.dateChanged.connect(lambda _: self.date_on.setChecked(True))

        self.warning = QLabel("")
        self.warning.setObjectName("Danger")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        row = QHBoxLayout()
        if edited:
            reset = QPushButton("Reset to detected")
            reset.setCursor(Qt.CursorShape.PointingHandCursor)
            reset.setToolTip("Throw away every correction and use what was detected.")
            reset.clicked.connect(self._reset)
            row.addWidget(reset)
        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

    def _field(self, label: str, box: QCheckBox) -> QWidget:
        """A checkbox and its label as one widget, since addRow needs a widget."""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        box.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(box)
        row.addWidget(QLabel(label))
        row.addStretch(1)
        return holder

    def _reset(self) -> None:
        self.reset_requested = True
        self.accept()

    def _accept(self) -> None:
        if self.amount_on.isChecked():
            try:
                Money.parse(self.amount.text().strip().replace("$", "").replace(",", ""))
            except (ValueError, TypeError):
                self.warning.setText(f"'{self.amount.text()}' is not an amount I can read.")
                return
        if self.name_on.isChecked() and not self.name.text().strip():
            self.warning.setText("A name cannot be empty.")
            return
        self.accept()

    @property
    def corrections(self) -> dict:
        """Only the ticked fields. An unticked one is cleared, not left stale."""
        raw = self.amount.text().strip().replace("$", "").replace(",", "")
        amount = Money.parse(raw) if self.amount_on.isChecked() and raw else None
        chosen = self.next_date.date()
        return {
            "display_name": self.name.text().strip() if self.name_on.isChecked() else None,
            "amount_minor": abs(amount.minor) if amount else None,
            "currency": amount.currency if amount else None,
            "cadence": self.cadence.currentText() if self.cadence_on.isChecked() else None,
            "next_expected": (
                date(chosen.year(), chosen.month(), chosen.day()).isoformat()
                if self.date_on.isChecked()
                else None
            ),
        }


def prompt(series: RecurringSeries, edited: bool, parent=None) -> dict | None:
    """Run the dialog. Returns corrections, {} to reset, or None if cancelled."""
    dialog = EditSeriesDialog(series, edited, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return {} if dialog.reset_requested else dialog.corrections
