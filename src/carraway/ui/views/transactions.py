"""The transaction list.

Backed by a model rather than a widget-per-cell table: a real ledger runs to
tens of thousands of rows, and QTableWidget builds an object for every cell of
every one of them. The model builds only what is on screen.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...analysis import categorize as cat
from ...core.models import Transaction
from ...core.money import Money
from .. import theme
from ..data import Ledger
from ..widgets import FilterStrip

_COLUMNS = ["Date", "Description", "Category", "Account", "Amount"]

# Qt overrides need a QModelIndex default; one shared invalid index avoids
# constructing a throwaway on every call.
_NO_PARENT = QModelIndex()


class TransactionModel(QAbstractTableModel):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger
        self.rows: list[Transaction] = list(ledger.transactions)

    def rowCount(self, parent: QModelIndex = _NO_PARENT) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = _NO_PARENT) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        tx = self.rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return [
                tx.date.isoformat(),
                tx.description,
                self._category_label(tx),
                self.ledger.account_name(tx.account_id),
                tx.amount.format(),
            ][column]

        # Sorting must compare real values, not formatted strings, or amounts
        # order lexicographically and dates only work by accident.
        if role == Qt.ItemDataRole.UserRole:
            return [
                tx.date.toordinal(),
                tx.description.lower(),
                self.ledger.categories.get(tx.id, ""),
                self.ledger.account_name(tx.account_id),
                tx.amount.minor,
            ][column]

        if role == Qt.ItemDataRole.TextAlignmentRole and column == 4:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Qt's darkGreen is unreadable on a dark background, so take the
        # accent from whichever palette is actually running.
        if role == Qt.ItemDataRole.ForegroundRole and column == 4 and not tx.is_outflow:
            return QColor(theme.ACTIVE.accent)
        # A guessed category is drawn muted, so the eye can skip the ones that
        # are only probably right.
        if role == Qt.ItemDataRole.ForegroundRole and column == 2 and self.ledger.is_guessed(tx.id):
            return QColor(theme.ACTIVE.warning)
        if role == Qt.ItemDataRole.ToolTipRole and column == 2:
            reason = self.ledger.guess_reason(tx.id)
            return f"Guessed: {reason}" if reason else None
        return None

    def _category_label(self, tx: Transaction) -> str:
        if tx.is_transfer:
            return "Transfer"
        name = self.ledger.categories.get(tx.id, "")
        # The "?" is the whole point of guessing: a guess that looks like a
        # certainty is worse than none.
        return f"{name}  ?" if self.ledger.is_guessed(tx.id) else name

    def headerData(  # noqa: N802
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        # The amount header has to sit over its right-aligned column.
        if role == Qt.ItemDataRole.TextAlignmentRole and section == 4:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class _FilterProxy(QSortFilterProxyModel):
    """Search across description, category and account at once.

    Qt's default filter looks at one column, which is never what someone means
    when they type "netflix" into a search box.

    Also narrows to a single account, so the account tabs and the search box
    compose rather than overriding each other: searching inside one account is
    the obvious thing to want and would be impossible if either reset the
    other.
    """

    def __init__(self) -> None:
        super().__init__()
        self.account_id: str | None = None
        self.kind: str = "All"

    def set_account(self, account_id: str | None) -> None:
        self.account_id = account_id
        self.invalidateFilter()

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if self.account_id is not None:
            transaction = model.rows[row]
            if transaction.account_id != self.account_id:
                return False

        if self.kind != "All":
            transaction = model.rows[row]
            category = model.ledger.categories.get(transaction.id, "")
            if self.kind == "Spending":
                if not transaction.is_outflow or transaction.is_transfer:
                    return False
            elif self.kind == "Income":
                if transaction.is_outflow or transaction.is_transfer:
                    return False
            elif self.kind == "Transfers":
                if not (transaction.is_transfer or category == "Transfer"):
                    return False
            elif self.kind == "Pending":
                if not transaction.pending:
                    return False
            elif self.kind != category:
                return False

        needle = self.filterRegularExpression().pattern().lower()
        if not needle:
            return True
        for column in (1, 2, 3):
            value = model.data(model.index(row, column, parent), Qt.ItemDataRole.DisplayRole)
            if needle in str(value).lower():
                return True
        return False


class TransactionsView(QWidget):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__()
        self.ledger = ledger

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("Transactions")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.balance_label = QLabel("")
        self.balance_label.setObjectName("Muted")
        header.addWidget(self.balance_label)
        layout.addLayout(header)

        # One button per account, with "All accounts" first. Accounts carry
        # very different volumes, so the count sits in the label: it is the
        # fastest way to see which account a run of transactions came from.
        # A wrapping row rather than a tab bar, because ten accounts in a tab
        # bar hides most of them behind scroll arrows.
        self.tabs = FilterStrip()
        self.tabs.currentChanged.connect(self._account_changed)
        layout.addWidget(self.tabs)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search description, category or account…")
        self.search.setClearButtonEnabled(True)
        search_row.addWidget(self.search, stretch=1)

        # Direction first, then categories: "show me only income" is asked far
        # more often than "show me only Pets", and a single list keeps the
        # control simple.
        self.kind = QComboBox()
        self.kind.addItems(["All", "Spending", "Income", "Transfers", "Pending"])
        self.kind.insertSeparator(self.kind.count())
        self.kind.addItems(list(cat.CATEGORIES))
        self.kind.currentTextChanged.connect(self._kind_changed)
        search_row.addWidget(QLabel("Type"))
        search_row.addWidget(self.kind)
        layout.addLayout(search_row)

        self.model = TransactionModel(ledger)
        self.proxy = _FilterProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(self._update_count)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setMouseTracking(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, stretch=1)

        self.count = QLabel("")
        self.count.setObjectName("Muted")
        layout.addWidget(self.count)

        self._build_tabs()
        self._update_count()

    def _kind_changed(self, kind: str) -> None:
        self.proxy.set_kind(kind)
        self._update_count()

    def _build_tabs(self) -> None:
        """Rebuild the tab bar from the ledger's accounts."""
        # Signals off while rebuilding: removing tabs fires currentChanged and
        # would filter to whichever account happens to be left mid-rebuild.
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)

        counts: dict[str, int] = {}
        for transaction in self.ledger.transactions:
            counts[transaction.account_id] = counts.get(transaction.account_id, 0) + 1

        self.tabs.addTab(f"All accounts  ({len(self.ledger.transactions):,})")
        self.tabs.setTabData(0, None)
        # Busiest first: an account with three transactions is rarely the one
        # someone opened this screen to look at.
        for account in sorted(self.ledger.accounts, key=lambda a: -counts.get(a.id, 0)):
            index = self.tabs.addTab(f"{account.name[:26]}  ({counts.get(account.id, 0):,})")
            self.tabs.setTabData(index, account.id)
            self.tabs.setTabToolTip(
                index, f"{account.name} — {account.institution or account.type}"
            )

        self.tabs.blockSignals(False)

    def _account_changed(self, index: int) -> None:
        account_id = self.tabs.tabData(index)
        self.proxy.set_account(account_id)
        # The account column says the same thing as the tab once one is
        # chosen, so it only earns its place on "All accounts".
        self.table.setColumnHidden(3, account_id is not None)
        self._update_balance(account_id)
        self._update_count()

    def _update_balance(self, account_id: str | None) -> None:
        """Show the account's balance, when one has been observed."""
        balances = self.ledger.balances
        if account_id is None:
            if not balances:
                self.balance_label.setText("")
                return
            net = sum(
                (
                    -abs(balance) if self._is_liability(aid) else balance
                    for aid, balance in balances.items()
                ),
                Money.zero(),
            )
            self.balance_label.setText(f"Across all accounts: {net.format()}")
            return

        balance = balances.get(account_id)
        if balance is None:
            self.balance_label.setText("No balance recorded for this account")
        else:
            owed = self._is_liability(account_id)
            label = "owed" if owed and balance.minor else "balance"
            self.balance_label.setText(f"Current {label}: {abs(balance).format()}")

    def _is_liability(self, account_id: str) -> bool:
        for account in self.ledger.accounts:
            if account.id == account_id:
                return account.type.is_liability
        return False

    def _update_count(self) -> None:
        shown, held = self.proxy.rowCount(), self.model.rowCount()
        self.count.setText(
            f"{shown:,} of {held:,} transactions" if shown != held else f"{held:,} transactions"
        )

    def refresh(self) -> None:
        self.model.beginResetModel()
        self.model.rows = list(self.ledger.transactions)
        self.model.endResetModel()
        self._build_tabs()
        self._update_balance(self.tabs.tabData(self.tabs.currentIndex()))
        self._update_count()
