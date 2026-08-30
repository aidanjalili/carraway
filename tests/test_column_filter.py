"""Per-column value filtering, tested without a running event loop."""

import pytest

pytest.importorskip("PySide6")


class _FakeCell:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _FakeTable:
    """Enough of QTableWidget for ColumnFilter's logic, minus the Qt."""

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows

    def rowCount(self) -> int:  # noqa: N802
        return len(self._rows)

    def item(self, row: int, column: int):
        return _FakeCell(self._rows[row][column])


def _filter(rows):
    from carraway.ui.widgets import ColumnFilter

    # __init__ wires up Qt signals, so the logic is exercised on a bare
    # instance rather than a constructed widget.
    instance = ColumnFilter.__new__(ColumnFilter)
    instance._table = _FakeTable(rows)
    instance.allowed = {}
    instance._boxes = []
    return instance


ROWS = [
    ["Netflix", "subscription"],
    ["Grok", "cancelled"],
    ["Rent", "bill"],
    ["Spotify", "subscription"],
]


def test_an_unfiltered_column_accepts_everything():
    column = _filter(ROWS)
    assert not column.is_filtered(1)
    assert all(column.accepts(1, value) for value in ("bill", "cancelled", "subscription"))


def test_choosing_one_value_hides_the_rest():
    column = _filter(ROWS)
    column.allowed[1] = {"cancelled"}
    assert column.accepts(1, "cancelled")
    assert not column.accepts(1, "subscription")
    assert column.is_filtered(1)


def test_unticking_one_value_means_all_but_that_one():
    # An unfiltered column starts as everything, so the first untick has to
    # subtract rather than reduce the selection to nothing.
    column = _filter(ROWS)
    column._toggle(1, "bill", False)
    assert column.accepts(1, "subscription")
    assert not column.accepts(1, "bill")


def test_unticking_everything_clears_the_filter():
    # Hiding every row would leave an empty table and no obvious way back.
    column = _filter(ROWS)
    for value in ("bill", "cancelled", "subscription"):
        column._toggle(1, value, False)
    assert not column.is_filtered(1)
    assert column.accepts(1, "bill")


def test_ticking_everything_back_is_not_a_filter():
    column = _filter(ROWS)
    column.allowed[1] = {"cancelled"}
    for value in ("bill", "subscription"):
        column._toggle(1, value, True)
    assert not column.is_filtered(1)


def test_choices_come_from_the_whole_column_not_the_visible_rows():
    # Filtering to one value must not remove the others from the menu, or
    # there is no way back to them.
    column = _filter(ROWS)
    column.allowed[1] = {"cancelled"}
    assert column._values(1) == ["bill", "cancelled", "subscription"]


def test_columns_filter_independently():
    column = _filter(ROWS)
    column.allowed[0] = {"Netflix"}
    column.allowed[1] = {"subscription"}
    assert column.accepts(0, "Netflix") and column.accepts(1, "subscription")
    assert not column.accepts(0, "Grok")
    column._reset(0)
    assert column.accepts(0, "Grok")
    assert column.is_filtered(1)
