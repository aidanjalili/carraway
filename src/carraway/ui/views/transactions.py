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
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Transaction
from .. import theme
from ..data import Ledger

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
                "Transfer" if tx.is_transfer else self.ledger.categories.get(tx.id, ""),
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
        return None

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
    """

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        needle = self.filterRegularExpression().pattern().lower()
        if not needle:
            return True
        model = self.sourceModel()
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

        title = QLabel("Transactions")
        title.setObjectName("Title")
        layout.addWidget(title)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search description, category or account…")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

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
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, stretch=1)

        self.count = QLabel("")
        self.count.setObjectName("Muted")
        layout.addWidget(self.count)
        self._update_count()

    def _update_count(self) -> None:
        shown, held = self.proxy.rowCount(), self.model.rowCount()
        self.count.setText(
            f"{shown:,} of {held:,} transactions" if shown != held else f"{held:,} transactions"
        )

    def refresh(self) -> None:
        self.model.beginResetModel()
        self.model.rows = list(self.ledger.transactions)
        self.model.endResetModel()
        self._update_count()
