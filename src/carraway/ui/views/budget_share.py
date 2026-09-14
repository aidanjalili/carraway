"""Count only part of a transaction toward your budgets.

A utility bill split with a flatmate is neither excluded nor counted in full:
two thirds of it was never yours. Before this the honest options were to
overstate the month by $70 or to hide a real $104 charge completely, and
people quite reasonably picked whichever lie was smaller.

The dialog asks for the part that *is* yours, not the part that is not.
"Count $34.91 of this" is the thought people actually have; "exclude $69.83"
is the same fact arrived at backwards.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ...core.money import Money

#: Splitting a bill evenly is most of what this is for, so the arithmetic is
#: a button rather than something to do in your head.
WAYS = (2, 3, 4)


class BudgetShareDialog(QDialog):
    """Ask how much of one transaction counts."""

    def __init__(self, description: str, amount: Money, counted: Money, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Count part of this")
        self.setMinimumWidth(430)
        self._whole = abs(amount)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel("Count part of this")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            f"{description}\n{abs(amount).format()} left your account. Say how "
            "much of it was actually yours to spend — the rest stops counting "
            "toward your budgets, but the transaction itself is untouched and "
            "still shows in Spending and in every total."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Count"))
        self.amount = QLineEdit(f"{abs(counted).decimal:.2f}")
        row.addWidget(self.amount, 1)
        row.addWidget(QLabel(f"of {abs(amount).format()}"))
        layout.addLayout(row)

        splits = QHBoxLayout()
        splits.setSpacing(8)
        splits.addWidget(QLabel("Split it"))
        for ways in WAYS:
            button = QPushButton(f"{ways} ways")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, n=ways: self._split(n))
            splits.addWidget(button)
        whole = QPushButton("All of it")
        whole.setCursor(Qt.CursorShape.PointingHandCursor)
        whole.clicked.connect(lambda: self.amount.setText(f"{self._whole.decimal:.2f}"))
        splits.addWidget(whole)
        splits.addStretch(1)
        layout.addLayout(splits)

        self.preview = QLabel("")
        self.preview.setObjectName("Muted")
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.amount.textChanged.connect(self._update_preview)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update_preview()

    def _split(self, ways: int) -> None:
        """Your share of an even split, rounded to the cent.

        Rounded down, so splitting three ways never claims more of the bill
        than there is. The odd cent stays out of the budget, which is the
        forgiving direction to be wrong in.
        """
        self.amount.setText(f"{(self._whole.minor // ways) / 100:.2f}")

    def _counted_or_none(self) -> Money | None:
        raw = self.amount.text().strip().replace("$", "").replace(",", "")
        if not raw:
            return None
        try:
            return abs(Money.parse(raw))
        except (ValueError, TypeError, ArithmeticError):
            return None

    def _update_preview(self) -> None:
        counted = self._counted_or_none()
        if counted is None:
            self.preview.setText("")
            return
        if counted.minor > self._whole.minor:
            self.preview.setText(
                f"That is more than the {self._whole.format()} that left the "
                "account. It will be capped at the whole amount."
            )
            return
        held = Money(self._whole.minor - counted.minor, counted.currency)
        if not held.minor:
            self.preview.setText("All of it counts, which is the normal case.")
            return
        share = counted.minor / self._whole.minor * 100 if self._whole.minor else 0
        self.preview.setText(
            f"{counted.format()} counts ({share:.0f}%), {held.format()} does not."
        )

    def _accept(self) -> None:
        if self._counted_or_none() is None:
            self.preview.setText(f"'{self.amount.text()}' is not an amount I can read.")
            return
        self.accept()

    @property
    def excluded(self) -> Money:
        """The part that does *not* count, which is what gets stored."""
        counted = self._counted_or_none() or Money.zero()
        counted = Money(min(counted.minor, self._whole.minor), self._whole.currency)
        return Money(self._whole.minor - counted.minor, self._whole.currency)


def prompt(description: str, amount: Money, counted: Money, parent=None) -> Money | None:
    """Ask how much counts. Returns the amount to hold back, or None if cancelled."""
    dialog = BudgetShareDialog(description, amount, counted, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.excluded
