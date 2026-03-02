"""
app_card.py – A single application tile: icon + name with opacity toggle.

States
------
* selected   (in group) → full opacity, highlighted border
* unselected (not in group) → 30 % opacity, muted
* hovered    → slight brightness lift regardless of selected state

SCROLL FIX:
    Previous implementation used QGraphicsOpacityEffect which creates
    offscreen render surfaces.  Inside a QScrollArea these surfaces
    desync during scrolling, making selected widgets appear "stuck".

    This version replaces the effect with **direct pixmap alpha
    manipulation**: we paint a semi-transparent copy of the icon pixmap
    and change the label/background colours via RGBA stylesheets.
    No QGraphicsEffect is used — widgets scroll normally.

ANIMATION:
    QVariantAnimation drives a float property (0.0 → 1.0) that
    blends between unselected and selected opacity on icon + labels.
"""

from typing import Optional

from PyQt6.QtCore import (
    Qt, pyqtSignal, QSize, QVariantAnimation, QEasingCurve,
)
from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont, QEnterEvent, QImage
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QSizePolicy,
)

CARD_W = 100
CARD_H = 114
ICON_SIZE = 48
SELECTED_OPACITY = 1.0
UNSELECTED_OPACITY = 0.30
HOVER_LIFT = 0.15

_CARD_RADIUS = "10px"
_ANIM_DURATION_MS = 180


def _apply_opacity_to_pixmap(src: QPixmap, opacity: float) -> QPixmap:
    """Return a copy of *src* painted at the given opacity onto a transparent canvas."""
    if src.isNull():
        return src
    result = QPixmap(src.size())
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setOpacity(opacity)
    painter.drawPixmap(0, 0, src)
    painter.end()
    return result


class AppCard(QWidget):
    """
    Clickable widget representing a single application.

    Signals
    -------
    toggled(exe_path: str, selected: bool)
    """

    toggled = pyqtSignal(str, bool)

    def __init__(
        self,
        name: str,
        exe_path: str,
        pixmap: Optional[QPixmap] = None,
        selected: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._exe_path = exe_path
        self._name = name
        self._selected = selected
        self._hovered = False

        # Store the full-resolution source pixmap (never mutated)
        self._src_pixmap: Optional[QPixmap] = None
        if pixmap and not pixmap.isNull():
            self._src_pixmap = pixmap.scaled(
                ICON_SIZE, ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{name}\n{exe_path}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 10, 6, 6)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        # ── Icon ────────────────────────────────────────────────────────
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(ICON_SIZE, ICON_SIZE)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        if not self._src_pixmap:
            self._icon_label.setText("📦")
            self._icon_label.setStyleSheet(
                "font-size: 28px; background: transparent;"
            )
        layout.addWidget(self._icon_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        # ── Name label ──────────────────────────────────────────────────
        self._name_label = QLabel(name, self)
        self._name_label.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self._name_label.setWordWrap(True)
        self._name_label.setMaximumWidth(CARD_W - 10)
        self._name_label.setFont(QFont("Segoe UI", 7))
        layout.addWidget(self._name_label)

        # ── Animation ───────────────────────────────────────────────────
        self._current_opacity = self._target_opacity()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(_ANIM_DURATION_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.valueChanged.connect(self._on_anim_tick)

        self._refresh_visuals()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def exe_path(self) -> str:
        return self._exe_path

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, value: bool, emit: bool = False) -> None:
        self._selected = value
        self._animate_to_target()
        if emit:
            self.toggled.emit(self._exe_path, value)

    # ------------------------------------------------------------------
    # Hover events
    # ------------------------------------------------------------------

    def enterEvent(self, event: QEnterEvent) -> None:  # type: ignore[override]
        self._hovered = True
        self._animate_to_target()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._animate_to_target()
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    # Internal — opacity computation
    # ------------------------------------------------------------------

    def _target_opacity(self) -> float:
        base = SELECTED_OPACITY if self._selected else UNSELECTED_OPACITY
        if self._hovered:
            base = min(1.0, base + HOVER_LIFT)
        return base

    def _animate_to_target(self) -> None:
        target = self._target_opacity()
        if abs(self._current_opacity - target) < 0.01:
            return
        self._anim.stop()
        self._anim.setStartValue(self._current_opacity)
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_anim_tick(self, value) -> None:
        self._current_opacity = float(value)
        self._refresh_visuals()

    # ------------------------------------------------------------------
    # Internal — visual refresh (no QGraphicsEffect)
    # ------------------------------------------------------------------

    def _refresh_visuals(self) -> None:
        op = self._current_opacity

        # 1) Icon pixmap with baked-in alpha
        if self._src_pixmap:
            self._icon_label.setPixmap(_apply_opacity_to_pixmap(self._src_pixmap, op))
            self._icon_label.setStyleSheet("background: transparent;")

        # 2) Name label colour
        alpha_int = max(0, min(255, int(op * 255)))
        self._name_label.setStyleSheet(
            f"color: rgba(192, 192, 216, {alpha_int});"
            f"background: transparent;"
        )

        # 3) Card background / border
        if self._selected:
            border = "2px solid #7B61FF"
            bg = f"rgba(42, 39, 64, {alpha_int})"
        else:
            border = "1px solid rgba(255,255,255,0.04)"
            bg = f"rgba(28, 28, 44, {alpha_int})"

        self.setStyleSheet(
            f"AppCard {{"
            f"  border: {border};"
            f"  border-radius: {_CARD_RADIUS};"
            f"  background: {bg};"
            f"}}"
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_selected(not self._selected, emit=True)
        super().mousePressEvent(event)
