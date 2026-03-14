"""
main.py – FlowLauncher entry point.

Usage
-----
    py -3.12 main.py
"""

import logging
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QPalette, QColor
from PyQt6.QtWidgets import QApplication

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

from ui.main_window import MainWindow


# ---------------------------------------------------------------------------
# Dark stylesheet — comprehensive coverage to prevent any white leakage
# ---------------------------------------------------------------------------

DARK_STYLE = """
/* ── Base ─────────────────────────────────────────────────────────── */
QMainWindow, QMainWindow > QWidget {
    margin: 0;
    padding: 0;
}

QMainWindow {
    background-color: #12121E;
    border: none;
}

QWidget {
    background-color: #12121E;
    color: #E0E0F0;
    font-family: "Segoe UI", "Inter", sans-serif;
}

/* ── Menu bar (kill any white strip) ────────────────────────────── */
QMenuBar {
    background-color: #12121E;
    color: #E0E0F0;
    border: none;
    padding: 0;
    margin: 0;
}

/* ── Tooltip ──────────────────────────────────────────────────────── */
QToolTip {
    background: #1E1E30;
    color: #C0C0D0;
    border: 1px solid #444466;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
}

/* ── MessageBox ───────────────────────────────────────────────────── */
QMessageBox {
    background: #1A1A2E;
}
QMessageBox QLabel {
    color: #E0E0F0;
}
QMessageBox QPushButton {
    background: #7B61FF;
    color: #fff;
    border-radius: 6px;
    padding: 6px 18px;
    min-width: 70px;
}
QMessageBox QPushButton:hover {
    background: #9E8AFF;
}

/* ── Dialog ───────────────────────────────────────────────────────── */
QDialog {
    background: #1A1A2E;
}

/* ── ScrollBar ────────────────────────────────────────────────────── */
QScrollBar:vertical {
    background: #1A1A2E;
    width: 8px;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #44446A;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: none;
}

/* ── StatusBar ────────────────────────────────────────────────────── */
QStatusBar {
    background: #0E0E18;
    color: #555577;
    font-size: 11px;
    border-top: 1px solid #1E1E30;
    padding: 2px 8px;
}
QStatusBar::item {
    border: none;
}
"""


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("FlowLauncher")
    app.setApplicationDisplayName("FlowLauncher")
    app.setStyleSheet(DARK_STYLE)

    # Force dark palette at the Qt level to prevent any white leakage
    palette = QPalette()
    dark = QColor("#12121E")
    palette.setColor(QPalette.ColorRole.Window, dark)
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#E0E0F0"))
    palette.setColor(QPalette.ColorRole.Base, dark)
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#1A1A2E"))
    palette.setColor(QPalette.ColorRole.Button, dark)
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#E0E0F0"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#E0E0F0"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#1E1E30"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#C0C0D0"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#7B61FF"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(palette)

    # Default font
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
