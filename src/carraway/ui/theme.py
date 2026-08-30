"""Colours and the application stylesheet.

Qt's default widget styling looks like 2006, which is precisely the complaint
this project has about every other open source finance app. The palette below
is applied as one stylesheet at startup rather than per widget, so a screen can
be written as plain layout code and still come out looking deliberate.

Both a light and a dark palette are defined and chosen from the system theme,
because a finance app people keep open all day has to sit comfortably in
whichever one they already run.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    bg: str
    surface: str
    surface_alt: str
    # Distinct from both surface and surface_alt on purpose: a hover that
    # reuses the alternating-row colour is invisible on every other row.
    hover: str
    border: str
    text: str
    muted: str
    accent: str  # the green light: money, and Gatsby's dock
    danger: str
    warning: str


DARK = Palette(
    bg="#14161a",
    surface="#1c1f26",
    surface_alt="#232730",
    hover="#2e3542",
    border="#2c313c",
    text="#e6e8ec",
    muted="#8b93a3",
    accent="#4ade80",
    danger="#f87171",
    warning="#fbbf24",
)

LIGHT = Palette(
    bg="#f6f7f9",
    surface="#ffffff",
    surface_alt="#f0f2f5",
    hover="#e4e8ee",
    border="#e2e5ea",
    text="#171a1f",
    muted="#6b7280",
    accent="#16a34a",
    danger="#dc2626",
    warning="#d97706",
)


def stylesheet(p: Palette) -> str:
    return f"""
    QWidget {{
        background: {p.bg};
        color: {p.text};
        font-size: 14px;
    }}
    /* Labels inherit the window colour by default, which paints a dark
       rectangle over whatever card they sit on. */
    QLabel {{ background: transparent; }}
    QLabel#Title      {{ font-size: 26px; font-weight: 600; }}
    QLabel#Subtitle   {{ color: {p.muted}; font-size: 14px; }}
    QLabel#StatValue  {{ font-size: 28px; font-weight: 600; }}
    QLabel#StatLabel  {{ color: {p.muted}; font-size: 12px;
                         text-transform: uppercase; letter-spacing: 1px; }}
    QLabel#SectionHeading {{ font-size: 16px; font-weight: 600; }}
    QLabel#Accent     {{ color: {p.accent}; }}
    QLabel#Danger     {{ color: {p.danger}; }}
    QLabel#Muted      {{ color: {p.muted}; }}

    QFrame#Card {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: 12px;
    }}
    QFrame#Sidebar {{
        background: {p.surface};
        border: none;
        border-right: 1px solid {p.border};
    }}

    QPushButton#NavButton {{
        background: transparent;
        border: none;
        border-radius: 8px;
        padding: 11px 16px;
        text-align: left;
        color: {p.muted};
        font-size: 14px;
    }}
    QPushButton#NavButton:hover  {{ background: {p.surface_alt}; color: {p.text}; }}
    QPushButton#NavButton:checked {{
        background: {p.surface_alt};
        color: {p.text};
        font-weight: 600;
    }}

    QPushButton#FilterChip {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: 14px;
        padding: 5px 13px;
        color: {p.muted};
        font-size: 13px;
    }}
    QPushButton#FilterChip:hover {{ color: {p.text}; border-color: {p.muted}; }}
    QPushButton#FilterChip:checked {{
        background: {p.surface_alt};
        border-color: {p.accent};
        color: {p.text};
        font-weight: 600;
    }}

    QMenu {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 14px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {p.hover}; }}
    QMenu::separator {{
        height: 1px;
        background: {p.border};
        margin: 4px 8px;
    }}
    /* The filter menu is built from real QCheckBox widgets so it stays open
       while several are ticked, which means they need their own hit area and
       hover rather than inheriting a plain label's. */
    QMenu QCheckBox {{
        padding: 5px 10px;
        border-radius: 5px;
        background: transparent;
        color: {p.text};
    }}
    QMenu QCheckBox:hover {{ background: {p.hover}; }}
    QCheckBox::indicator {{
        width: 15px;
        height: 15px;
        border: 1px solid {p.muted};
        border-radius: 4px;
        background: {p.surface_alt};
    }}
    QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
    QCheckBox::indicator:checked {{
        background: {p.accent};
        border-color: {p.accent};
        /* A tick drawn as a small inset block: reliable at this size, and it
           needs no icon file. */
        image: none;
    }}

    QLineEdit {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 8px 12px;
        selection-background-color: {p.accent};
    }}
    QLineEdit:focus {{ border: 1px solid {p.accent}; }}

    QTableView, QTableWidget {{
        background: {p.surface};
        alternate-background-color: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: 12px;
        gridline-color: transparent;
        selection-background-color: {p.surface_alt};
        selection-color: {p.text};
    }}
    QHeaderView::section {{
        background: {p.surface};
        color: {p.muted};
        border: none;
        border-bottom: 1px solid {p.border};
        padding: 10px 8px;
        font-size: 12px;
        font-weight: 600;
    }}
    QTableView::item, QTableWidget::item {{ padding: 8px; border: none; }}
    /* Qt highlights the cell under the cursor, not the row, which is useless
       on a wide table — the eye loses the line between the merchant and the
       amount. Row-wide hover is enabled per view with setSelectionBehavior
       and this rule. */
    QTableView::item:hover, QTableWidget::item:hover {{
        background: {p.hover};
        color: {p.text};
    }}
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {p.border}; border-radius: 5px; min-height: 30px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{
        background: {p.border}; border-radius: 5px; min-width: 30px;
    }}
    """


# The palette the app is currently running in. Widgets that need a colour in
# code rather than in the stylesheet (a table cell's text, say) read this
# instead of hardcoding one that only works in one theme.
ACTIVE: Palette = DARK


def palette_for(is_dark: bool) -> Palette:
    return DARK if is_dark else LIGHT


def activate(is_dark: bool) -> Palette:
    """Choose the palette for this run and publish it as ACTIVE."""
    global ACTIVE
    ACTIVE = palette_for(is_dark)
    return ACTIVE
