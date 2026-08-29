"""The application shell: a sidebar of screens and the screen itself."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import db
from .data import Ledger
from .views.dashboard import DashboardView
from .views.subscriptions import SubscriptionsView
from .views.transactions import TransactionsView

# Subscriptions comes first on purpose: it is the screen the app exists for,
# and it is what someone opening Carraway for the first time should meet.
_SCREENS = [
    ("Subscriptions", SubscriptionsView),
    ("Overview", DashboardView),
    ("Transactions", TransactionsView),
]


class MainWindow(QMainWindow):
    def __init__(self, database: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Carraway")
        self.resize(1180, 760)

        self.ledger = Ledger(path=database or db.default_db_path())
        self.ledger.load()

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        for _, factory in _SCREENS:
            self.stack.addWidget(factory(self.ledger))
        root_layout.addWidget(self.stack, stretch=1)

        self.setCentralWidget(root)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 22, 14, 18)
        layout.setSpacing(4)

        wordmark = QLabel("Carraway")
        wordmark.setStyleSheet("font-size: 19px; font-weight: 700; padding: 0 8px 4px 8px;")
        tagline = QLabel("your money, locally")
        tagline.setObjectName("Muted")
        tagline.setStyleSheet("padding: 0 8px 18px 8px; font-size: 12px;")
        layout.addWidget(wordmark)
        layout.addWidget(tagline)

        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, (name, _) in enumerate(_SCREENS):
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, i=index: self.stack.setCurrentIndex(i))
            group.addButton(button, index)
            layout.addWidget(button)

        layout.addStretch(1)

        accounts = QLabel(f"{len(self.ledger.accounts)} accounts")
        accounts.setObjectName("Muted")
        accounts.setStyleSheet("padding: 0 8px; font-size: 12px;")
        privacy = QLabel("Nothing leaves this device.")
        privacy.setObjectName("Muted")
        privacy.setStyleSheet("padding: 4px 8px 0 8px; font-size: 11px;")
        layout.addWidget(accounts)
        layout.addWidget(privacy)
        return sidebar
