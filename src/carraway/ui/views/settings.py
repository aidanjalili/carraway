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
    QLineEdit,
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

        layout.addWidget(self._categorise_card())
        layout.addWidget(self._rules_card())
        layout.addWidget(self._category_list_card())
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

    def _categorise_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        heading = QLabel("Guess categories for what the rules miss")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        self.auto_box = QCheckBox("Try to categorise the rest automatically")
        self.auto_box.setChecked(bool(self.ledger.setting("auto_categorize")))
        self.auto_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_box.toggled.connect(self._toggle_auto)
        layout.addWidget(self.auto_box)

        blurb = QLabel(
            "Built-in rules recognise named merchants and clear keywords, which "
            "leaves the local businesses no list can cover. With this on, "
            "Carraway guesses at those from the words in the description and "
            "from categories you have set yourself.\n\n"
            "Every guess is marked with a ? and shown in amber, and hovering it "
            "says why. Guesses never train later guesses, so one wrong answer "
            "cannot spread."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self.auto_summary = QLabel("")
        self.auto_summary.setObjectName("Muted")
        layout.addWidget(self.auto_summary)
        self._update_auto_summary()
        return card

    def _toggle_auto(self, enabled: bool) -> None:
        self.ledger.save_setting("auto_categorize", enabled)
        self.ledger.load()
        self._update_auto_summary()
        window = self.window()
        if hasattr(window, "refresh_all"):
            window.refresh_all()

    def _update_auto_summary(self) -> None:
        if not self.ledger.setting("auto_categorize"):
            uncategorised = sum(
                1 for name in self.ledger.categories.values() if name == "Uncategorized"
            )
            self.auto_summary.setText(f"{uncategorised:,} transactions are uncategorised.")
            return
        self.auto_summary.setText(f"{len(self.ledger.guesses):,} categories are currently guesses.")

    def _rules_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        heading = QLabel("Your own rules")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "If a description contains some text, file it under a category. "
            "Matched against the description exactly as it appears in the "
            "Transactions list, and your rules beat every built-in one."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        entry = QHBoxLayout()
        entry.setSpacing(8)
        entry.addWidget(QLabel("If it contains"))
        self.rule_pattern = QLineEdit()
        self.rule_pattern.setPlaceholderText("MILLER & SONS")
        self.rule_pattern.textChanged.connect(self._preview_rule)
        entry.addWidget(self.rule_pattern, stretch=1)

        entry.addWidget(QLabel("file as"))
        self.rule_category = QComboBox()
        self.rule_category.addItems(list(self.ledger.categories_available))
        entry.addWidget(self.rule_category)

        add = QPushButton("Add")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._add_rule)
        entry.addWidget(add)
        layout.addLayout(entry)

        self.rule_preview = QLabel("")
        self.rule_preview.setObjectName("Muted")
        layout.addWidget(self.rule_preview)

        self.rules_box = QVBoxLayout()
        self.rules_box.setSpacing(4)
        layout.addLayout(self.rules_box)
        self._rebuild_rules()
        return card

    def _preview_rule(self, text: str) -> None:
        """Say how many rows a rule would catch before it is saved."""
        count = self.ledger.rule_preview(text)
        self.rule_preview.setText(
            "" if not text.strip() else f"Would match {count:,} transaction(s)."
        )

    def _add_rule(self) -> None:
        pattern = self.rule_pattern.text().strip()
        if not pattern:
            return
        self.ledger.add_rule(pattern, self.rule_category.currentText())
        self.rule_pattern.clear()
        self._rebuild_rules()
        self._refresh_window()

    def _rebuild_rules(self) -> None:
        while self.rules_box.count():
            item = self.rules_box.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
            elif item.layout():
                while item.layout().count():
                    inner = item.layout().takeAt(0)
                    if inner.widget():
                        inner.widget().setParent(None)

        if not self.ledger.user_rules:
            empty = QLabel("No rules yet.")
            empty.setObjectName("Muted")
            self.rules_box.addWidget(empty)
            return

        for rule in self.ledger.user_rules:
            row = QHBoxLayout()
            label = QLabel(f'"{rule["pattern"]}"  →  {rule["category"]}')
            row.addWidget(label)
            row.addStretch(1)
            count = QLabel(f"{self.ledger.rule_preview(rule['pattern']):,} matches")
            count.setObjectName("Muted")
            row.addWidget(count)
            drop = QPushButton("Remove")
            drop.setCursor(Qt.CursorShape.PointingHandCursor)
            drop.clicked.connect(lambda _=False, r=rule["id"]: self._remove_rule(r))
            row.addWidget(drop)
            self.rules_box.addLayout(row)

    def _remove_rule(self, rule_id: str) -> None:
        self.ledger.remove_rule(rule_id)
        self._rebuild_rules()
        self._refresh_window()

    def _category_list_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        heading = QLabel("Categories")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "Add your own, or untick one you never use. Unticking hides a "
            "category from the lists; anything already filed under it keeps "
            "its category rather than being moved somewhere else."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        entry = QHBoxLayout()
        self.new_category = QLineEdit()
        self.new_category.setPlaceholderText("Hobbies")
        self.new_category.returnPressed.connect(self._add_category)
        entry.addWidget(self.new_category, stretch=1)
        add = QPushButton("Add category")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._add_category)
        entry.addWidget(add)
        layout.addLayout(entry)

        self.category_box = QVBoxLayout()
        self.category_box.setSpacing(2)
        layout.addLayout(self.category_box)
        self._rebuild_categories()
        return card

    def _add_category(self) -> None:
        name = self.new_category.text().strip()
        if not name:
            return
        self.ledger.add_category(name)
        self.new_category.clear()
        self._rebuild_categories()
        self._refresh_window()

    def _rebuild_categories(self) -> None:
        from ...analysis.categorize import CATEGORIES

        while self.category_box.count():
            item = self.category_box.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        available = set(self.ledger.categories_available)
        counts: dict[str, int] = {}
        for name in self.ledger.categories.values():
            counts[name] = counts.get(name, 0) + 1

        for name in sorted(set(CATEGORIES) | available | set(counts)):
            box = QCheckBox(f"{name}   ({counts.get(name, 0):,})")
            box.setChecked(name in available)
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.toggled.connect(lambda shown, n=name: self._toggle_category(n, shown))
            self.category_box.addWidget(box)

    def _toggle_category(self, name: str, shown: bool) -> None:
        self.ledger.set_category_hidden(name, not shown)
        self._refresh_window()

    def _refresh_window(self) -> None:
        window = self.window()
        if hasattr(window, "refresh_all"):
            window.refresh_all()

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
        """Re-sync the checkboxes after another screen changed them."""
        if hasattr(self, "auto_box"):
            self.auto_box.blockSignals(True)
            self.auto_box.setChecked(bool(self.ledger.setting("auto_categorize")))
            self.auto_box.blockSignals(False)
            self._update_auto_summary()
        excluded = self.ledger.excluded_accounts
        for account_id, box in getattr(self, "boxes", {}).items():
            box.blockSignals(True)
            box.setChecked(account_id not in excluded)
            box.blockSignals(False)
        self._update_excluded_total()
