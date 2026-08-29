"""Preferences, and the ledger-level choices that change what the numbers mean.

Everything here is stored in the database rather than a config file, because
these are decisions about *this* ledger — which accounts count towards net
worth, say — and would be meaningless beside a different one.

The account toggles also appear pinned to the net worth screen. That is
deliberate duplication: the question "what is this excluding retirement?" is
asked while looking at the number, and a screen away is a screen too far.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.money import Money, total
from ..data import Ledger
from ..widgets import Card


class SettingsView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(16)

        title = QLabel("Settings")
        title.setObjectName("Title")
        subtitle = QLabel("Kept with your data, and remembered between sessions.")
        subtitle.setObjectName("Subtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        layout.addWidget(self._accounts_card())
        layout.addWidget(self._defaults_card())
        layout.addWidget(self._data_card())
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

    # -- sections --------------------------------------------------------

    def _accounts_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        heading = QLabel("Accounts counted towards net worth")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "Leave out anything you cannot spend — a pension or a brokerage "
            "account — to see what you actually have available. The total is "
            "recalculated straight away."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        excluded = self.ledger.excluded_accounts
        self.boxes: dict[str, QCheckBox] = {}
        for account in self.ledger.accounts:
            row = QHBoxLayout()
            box = QCheckBox(account.name)
            box.setChecked(account.id not in excluded)
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.toggled.connect(
                lambda checked, account_id=account.id: self._toggle(account_id, checked)
            )
            self.boxes[account.id] = box
            row.addWidget(box)
            row.addStretch(1)

            balance = self.ledger.balances.get(account.id)
            detail = QLabel(balance.format() if balance else "no balance recorded")
            detail.setObjectName("Muted")
            row.addWidget(detail)
            layout.addLayout(row)

        self.excluded_total = QLabel("")
        self.excluded_total.setObjectName("Muted")
        layout.addWidget(self.excluded_total)
        self._update_excluded_total()
        return card

    def _defaults_card(self) -> Card:
        card = Card()
        form = QFormLayout(card)
        form.setContentsMargins(20, 16, 20, 16)
        form.setSpacing(10)

        heading = QLabel("What each screen opens on")
        heading.setObjectName("SectionHeading")
        form.addRow(heading)

        self.networth_zoom = QComboBox()
        self.networth_zoom.addItems(["monthly", "weekly", "daily"])
        self.networth_zoom.setCurrentText(str(self.ledger.setting("networth_granularity")))
        self.networth_zoom.currentTextChanged.connect(
            lambda value: self.ledger.save_setting("networth_granularity", value)
        )
        form.addRow("Net worth", self.networth_zoom)

        self.spending_zoom = QComboBox()
        self.spending_zoom.addItems(["monthly", "weekly", "daily", "yearly"])
        self.spending_zoom.setCurrentText(str(self.ledger.setting("spending_granularity")))
        self.spending_zoom.currentTextChanged.connect(
            lambda value: self.ledger.save_setting("spending_granularity", value)
        )
        form.addRow("Spending", self.spending_zoom)

        self.spending_chart = QComboBox()
        self.spending_chart.addItems(["Pie", "Bars", "Table", "Trend"])
        self.spending_chart.setCurrentText(str(self.ledger.setting("spending_chart")))
        self.spending_chart.currentTextChanged.connect(
            lambda value: self.ledger.save_setting("spending_chart", value)
        )
        form.addRow("Spending chart", self.spending_chart)
        return card

    def _data_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        heading = QLabel("Your data")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        where = QLabel(f"Everything lives in {self.ledger.path}")
        where.setObjectName("Muted")
        where.setWordWrap(True)
        layout.addWidget(where)

        counts = QLabel(
            f"{len(self.ledger.transactions):,} transactions across "
            f"{len(self.ledger.accounts)} accounts. Nothing leaves this device."
        )
        counts.setObjectName("Muted")
        layout.addWidget(counts)

        row = QHBoxLayout()
        backup = QPushButton("Back up now")
        backup.setCursor(Qt.CursorShape.PointingHandCursor)
        backup.clicked.connect(self._backup)
        row.addWidget(backup)
        row.addStretch(1)
        layout.addLayout(row)
        return card

    # -- actions ---------------------------------------------------------

    def _toggle(self, account_id: str, included: bool) -> None:
        excluded = self.ledger.excluded_accounts
        if included:
            excluded.discard(account_id)
        else:
            excluded.add(account_id)
        self.ledger.save_setting("networth_excluded_accounts", sorted(excluded))
        self._update_excluded_total()

    def _update_excluded_total(self) -> None:
        excluded = self.ledger.excluded_accounts
        if not excluded:
            self.excluded_total.setText("Everything is counted.")
            return
        amounts = [self.ledger.balances[i] for i in excluded if i in self.ledger.balances]
        left_out = total([abs(a) for a in amounts]) if amounts else Money.zero()
        self.excluded_total.setText(
            f"{len(excluded)} account(s) left out, holding {left_out.format()}."
        )

    def _backup(self) -> None:
        from ...core import backup

        saved = backup.snapshot(self.ledger.path, tag="manual")
        QMessageBox.information(
            self,
            "Backed up",
            f"Saved to {saved}" if saved else "Nothing to back up yet.",
        )

    def refresh(self) -> None:
        """Re-sync the checkboxes after the net worth screen changed them."""
        excluded = self.ledger.excluded_accounts
        for account_id, box in getattr(self, "boxes", {}).items():
            box.blockSignals(True)
            box.setChecked(account_id not in excluded)
            box.blockSignals(False)
        self._update_excluded_total()
