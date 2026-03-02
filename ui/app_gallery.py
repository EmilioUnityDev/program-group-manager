"""
app_gallery.py – Scrollable grid of AppCard widgets.

Responsibilities
----------------
* Display all scanned applications grouped into a responsive QGridLayout.
* Synchronise selected state with a given group's exe path list.
* Expose helpers so MainWindow can read / write the current selection.
"""

import math
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QScrollArea, QGridLayout, QVBoxLayout,
    QLabel, QSizePolicy,
)

from core.scanner import AppInfo
from ui.app_card import AppCard

COLS = 7          # default columns; adapts on resize
CARD_SPACING = 8


class AppGallery(QWidget):
    """
    A scrollable grid of AppCard tiles.

    Signals
    -------
    selection_changed()  – emitted whenever any card is toggled.
    """

    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards: list[AppCard] = []
        self._cols = COLS

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Scroll area ──────────────────────────────────────────────────
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical { background: #1A1A2E; width: 8px; border-radius: 4px; }"
            "QScrollBar::handle:vertical { background: #44446A; border-radius: 4px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )

        # ── Container inside scroll area ─────────────────────────────────
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(CARD_SPACING)
        self._grid.setContentsMargins(12, 12, 12, 12)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

        # Empty state placeholder
        self._placeholder = QLabel("Scanning applications…")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet("color: #555577; font-size: 15px;")
        self._placeholder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        outer.addWidget(self._placeholder)

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def populate(self, apps: list[AppInfo], selected_paths: Optional[list[str]] = None) -> None:
        """Fill the gallery with AppCard widgets for each AppInfo."""
        # Clear existing cards
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()

        selected_set = {p.lower() for p in (selected_paths or [])}

        for i, app in enumerate(apps):
            is_selected = app.exe_path.lower() in selected_set
            card = AppCard(
                name=app.name,
                exe_path=app.exe_path,
                pixmap=app.pixmap,
                selected=is_selected,
            )
            card.toggled.connect(self._on_card_toggled)
            self._cards.append(card)
            row, col = divmod(i, self._cols)
            self._grid.addWidget(card, row, col)

        has_apps = bool(apps)
        self._placeholder.setVisible(not has_apps)
        self._scroll.setVisible(has_apps)

    def update_placeholder(self, text: str) -> None:
        self._placeholder.setText(text)

    # ------------------------------------------------------------------
    # Selection API
    # ------------------------------------------------------------------

    def get_selected_paths(self) -> list[str]:
        """Return exe paths of all currently selected cards."""
        return [c.exe_path for c in self._cards if c.selected]

    def set_selected_paths(self, paths: list[str]) -> None:
        """Update card selection to match the given path list (no signals)."""
        path_set = {p.lower() for p in paths}
        for card in self._cards:
            card.set_selected(card.exe_path.lower() in path_set, emit=False)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_card_toggled(self, exe_path: str, selected: bool) -> None:
        self.selection_changed.emit()

    def resizeEvent(self, event) -> None:
        """Reflow the grid when the widget is resized."""
        super().resizeEvent(event)
        from ui.app_card import CARD_W
        avail = self._scroll.viewport().width() - 24  # subtract margins
        cols = max(1, avail // (CARD_W + CARD_SPACING))
        if cols != self._cols:
            self._cols = cols
            self._reflow()

    def _reflow(self) -> None:
        """Re-insert cards into the grid with the updated column count."""
        cards = self._cards[:]
        while self._grid.count():
            item = self._grid.takeAt(0)
            # Don't delete – just remove from layout
        for i, card in enumerate(cards):
            row, col = divmod(i, self._cols)
            self._grid.addWidget(card, row, col)
