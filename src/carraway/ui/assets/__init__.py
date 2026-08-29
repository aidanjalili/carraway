"""Bundled resources, and how to find them.

Separate from ui.app so a window can ask for the icon without importing the
module that constructs windows, which is a cycle.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon


def app_icon() -> QIcon:
    """The application icon, preferring the copy installed in the icon theme.

    Also shipped inside the package, so a `pip install` with no desktop entry
    still has an icon rather than falling back to a placeholder.
    """
    themed = QIcon.fromTheme("carraway")
    if not themed.isNull():
        return themed

    bundled = Path(__file__).with_name("carraway.svg")
    return QIcon(str(bundled)) if bundled.exists() else QIcon()
