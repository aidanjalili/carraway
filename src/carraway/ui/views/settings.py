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
    QInputDialog,
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
from ..widgets import Card, refresh_everything
from .pocket import PocketCard


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
        layout.addWidget(PocketCard(self.ledger))
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
            "its category rather than being moved somewhere else.\n\n"
            "Star the ones you watch. A starred category is marked wherever "
            "it appears and sorts to the top of the budget tables — it "
            "changes nothing about the money, only where your eye lands."
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

        favourites = self.ledger.favourite_categories
        # The built-ins under the names the user gave them. Listed as shipped,
        # a renamed built-in stayed on as a second, empty row under its old
        # name -- and renaming or ticking that ghost rewrote the alias, sending
        # everything already moved to the new name somewhere else again.
        renames = self.ledger.category_renames
        builtins = {renames.get(name, name) for name in CATEGORIES}
        # Starred first, then the rest alphabetically, so this list is
        # ordered the same way the tables it controls are.
        names = sorted(
            builtins | available | set(counts),
            key=lambda n: (n not in favourites, n),
        )
        for name in names:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(6)

            star = QPushButton("★" if name in favourites else "☆")
            star.setObjectName("InfoDot")
            star.setFlat(True)
            star.setCheckable(True)
            star.setChecked(name in favourites)
            star.setFixedSize(22, 22)
            star.setCursor(Qt.CursorShape.PointingHandCursor)
            star.setToolTip(f"Watch {name} across the app")
            star.clicked.connect(lambda on, n=name: self._toggle_favourite(n, on))
            line.addWidget(star)

            box = QCheckBox(f"{name}   ({counts.get(name, 0):,})")
            box.setChecked(name in available)
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.toggled.connect(lambda shown, n=name: self._toggle_category(n, shown))
            line.addWidget(box, stretch=1)

            # Right-click to rename, on the whole row -- the checkbox is where
            # the pointer is, and a menu only on the blank space beside it
            # would be one nobody finds.
            for target in (row, box):
                target.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                target.customContextMenuRequested.connect(
                    lambda pos, n=name, w=target: self._category_menu(n, w, pos)
                )
            self.category_box.addWidget(row)

    def _category_menu(self, name: str, widget, position) -> None:
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        rename = QAction("Rename…", self)
        reserved = name in self.ledger.RESERVED_CATEGORIES
        rename.setEnabled(not reserved)
        if reserved:
            # Said in the menu rather than refused after the dialog, so nobody
            # types a new name only to be told it cannot be used.
            rename.setText("Rename… (used by the app itself)")
        rename.triggered.connect(lambda: self._rename_category(name))
        menu.addAction(rename)
        menu.exec(widget.mapToGlobal(position))

    def _rename_category(self, name: str) -> None:
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        new, ok = QInputDialog.getText(
            self,
            "Rename category",
            f"Rename {name} to:\n\nEverything filed under it moves with it -- rules, "
            "budgets, tracked subscriptions and transactions you filed by hand.",
            text=name,
        )
        if not ok:
            return
        problem = self.ledger.rename_category(name, new)
        if problem:
            QMessageBox.information(self, "Could not rename", problem)
            return
        self._rebuild_categories()
        self._refresh_window()

    def _toggle_favourite(self, name: str, favourite: bool) -> None:
        self.ledger.set_favourite_category(name, favourite)
        self._rebuild_categories()
        self._refresh_window()

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

        heading = QLabel("Your accounts")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        blurb = QLabel(
            "Ticked accounts count towards net worth. Leave out anything you "
            "cannot spend — a pension or a brokerage account — to see what you "
            "actually have available. The total is recalculated straight away.\n\n"
            "Rename any of them: banks name accounts for their own filing, not "
            "for you, and the new name is used everywhere and kept through "
            "future refreshes."
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

            balance = self.ledger.current_balances.get(account.id)
            detail = QLabel(balance.format() if balance else "no balance recorded")
            detail.setObjectName("Muted")
            row.addWidget(detail)

            rename = QPushButton("Rename…")
            rename.setCursor(Qt.CursorShape.PointingHandCursor)
            rename.setToolTip(f"Change what {account.name} is called")
            rename.clicked.connect(
                lambda _=False, account_id=account.id: self._rename_account(account_id)
            )
            row.addWidget(rename)
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

        heading = QLabel("Appearance")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        panels_note = QLabel(
            "Panels and table columns you have dragged keep their size "
            "between sessions. This puts every screen back to the proportions "
            "it ships with. Column widths take effect on the next restart; "
            "panels change straight away."
        )
        panels_note.setObjectName("Muted")
        panels_note.setWordWrap(True)
        layout.addWidget(panels_note)

        panels_row = QHBoxLayout()
        reset_panels = QPushButton("Reset panel and column sizes")
        reset_panels.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_panels.clicked.connect(self._reset_panels)
        panels_row.addWidget(reset_panels)
        self.panels_result = QLabel("")
        self.panels_result.setObjectName("Muted")
        panels_row.addWidget(self.panels_result)
        panels_row.addStretch(1)
        layout.addLayout(panels_row)

        heading = QLabel("Your data")
        heading.setObjectName("SectionHeading")
        layout.addWidget(heading)

        where = QLabel(f"Everything lives in {self.ledger.path}")
        where.setObjectName("Muted")
        where.setWordWrap(True)
        layout.addWidget(where)

        # The last sentence has to stay honest. With Pocket connected a small
        # summary does leave, so say which one rather than repeating a claim
        # that has quietly stopped being true.
        leaves = (
            "Nothing leaves this device."
            if not self.ledger.pocket_configured
            else "The only thing that leaves is the Pocket summary: category "
            "names and what is left in each."
        )
        counts = QLabel(
            f"{len(self.ledger.transactions):,} transactions across "
            f"{len(self.ledger.accounts)} accounts. {leaves}"
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

    def _rename_account(self, account_id: str) -> None:
        """Let the user call an account what they call it.

        Renaming is display-only: the account keeps its id, its history and
        its link to the provider, so a refresh lands in the same place rather
        than creating a second copy under the bank's own name.
        """
        account = next((a for a in self.ledger.accounts if a.id == account_id), None)
        if account is None:
            return

        name, agreed = QInputDialog.getText(
            self,
            "Rename account",
            f"What should “{account.name}” be called?",
            text=account.name,
        )
        if not agreed:
            return
        name = name.strip()
        if not name or name == account.name:
            return

        clash = next(
            (
                a
                for a in self.ledger.accounts
                if a.id != account_id and a.name.lower() == name.lower()
            ),
            None,
        )
        if clash is not None:
            # Two accounts with one name makes every list ambiguous, and the
            # Pocket inbox matches on the name outright.
            QMessageBox.warning(
                self,
                "That name is taken",
                f"Another account is already called “{clash.name}”. "
                "Pick something that tells them apart.",
            )
            return

        self.ledger.rename_account(account_id, name)
        refresh_everything(self)

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
        current = self.ledger.current_balances
        amounts = [current[i] for i in excluded if i in current]
        left_out = total([abs(a) for a in amounts]) if amounts else Money.zero()
        self.excluded_total.setText(
            f"{len(excluded)} account(s) left out, holding {left_out.format()}."
        )

    def _reset_panels(self) -> None:
        """Forget every screen's saved layout.

        The screens already built keep their current sizes until they are
        rebuilt, so they are put back directly as well -- a reset that only
        takes effect after a restart is a reset that looks broken.
        """
        from ..widgets import PanelSplitter, refresh_everything, reset_columns

        PanelSplitter.reset_all(self.ledger)
        reset_columns(self.ledger)
        # The live screens are reset directly as well. Clearing the saved
        # state alone would leave every screen already built showing the
        # sizes it was dragged to until the app restarted, which is a reset
        # that looks broken.
        for splitter in self.window().findChildren(PanelSplitter):
            splitter.reset()
        self.panels_result.setText("Back to default.")
        refresh_everything(self)

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
