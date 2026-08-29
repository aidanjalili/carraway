"""Ask what a recurring merchant is, from inside the app.

The CLI has `carraway review` for this, but a desktop app that tells its user
to go and open a terminal has not really shipped the feature. Same questions,
same storage, same answers — reachable from the table where the user is already
looking at the row that puzzles them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
)

from ...analysis import subscriptions
from ...core.models import RecurringSeries

# Wording matters more than the enum names here: the user is answering "what is
# this?", not picking a taxonomy, so each option says what it means for them.
_CHOICES: list[tuple[str, str, str]] = [
    (
        subscriptions.SUBSCRIPTION,
        "Subscription",
        "A service billing on a schedule. You could cancel it.",
    ),
    (
        subscriptions.BILL,
        "Bill",
        "Rent, utilities, insurance, a loan. Not optional.",
    ),
    (
        subscriptions.INCOME,
        "Income",
        "Money arriving on a schedule. Salary, a deposit, someone paying you.",
    ),
    (
        subscriptions.HABIT,
        "Habit",
        "Regular spending at an ordinary merchant. Not a commitment.",
    ),
    (
        subscriptions.CANCELLED,
        "Cancelled",
        "You have stopped paying. Kept visible, but not counted.",
    ),
]


class ClassifyDialog(QDialog):
    """A single question about a single merchant."""

    def __init__(self, series: RecurringSeries, current: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What is this?")
        self.setMinimumWidth(560)
        self.series = series

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        name = QLabel(series.merchant)
        name.setObjectName("SectionHeading")
        layout.addWidget(name)

        direction = "in" if series.typical_amount.minor > 0 else "out"
        facts = QLabel(
            f"{direction} {abs(series.typical_amount).format()} {series.cadence}"
            f"  ·  {series.annualised.format()}/yr"
            f"  ·  seen {series.occurrences}x since {series.first_seen}"
        )
        facts.setObjectName("Muted")
        layout.addWidget(facts)

        if subscriptions.is_person_to_person(series.merchant):
            hint = QLabel("Person-to-person payment — only you know who is at the other end.")
            hint.setObjectName("Muted")
            hint.setWordWrap(True)
            hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)
            layout.addWidget(hint)

        layout.addSpacing(6)

        self.group = QButtonGroup(self)
        for index, (kind, label, explanation) in enumerate(_CHOICES):
            button = QRadioButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            # Pre-select whatever the app currently believes, so confirming a
            # correct guess is one click and only a correction needs thought.
            button.setChecked(kind == current)
            self.group.addButton(button, index)
            layout.addWidget(button)

            # The explanation goes on its own line rather than inside the
            # radio label: QRadioButton does not wrap, so a long label is
            # silently clipped instead of resizing the dialog.
            caption = QLabel(explanation)
            caption.setObjectName("Muted")
            caption.setWordWrap(True)
            # A wrapping label reports the height of a single line unless its
            # policy says otherwise, so without this the captions overlap.
            caption.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding)
            caption.setContentsMargins(24, 0, 0, 8)
            layout.addWidget(caption)

        if self.group.checkedId() < 0:
            # No stored answer yet. Money arriving is far more often income
            # than a subscription, so the direction picks the opening guess
            # rather than always landing on the first option.
            inflow = series.typical_amount.minor > 0
            fallback = subscriptions.INCOME if inflow else subscriptions.SUBSCRIPTION
            index = next(i for i, (kind, _, _) in enumerate(_CHOICES) if kind == fallback)
            self.group.button(index).setChecked(True)

        layout.addSpacing(8)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def chosen(self) -> str:
        return _CHOICES[self.group.checkedId()][0]
