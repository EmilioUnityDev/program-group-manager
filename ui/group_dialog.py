"""
group_dialog.py – Modal dialog for creating / renaming a group.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton,
)


class GroupDialog(QDialog):
    """Simple input dialog that returns a group name string."""

    def __init__(self, parent=None, title: str = "New Group", initial: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(340)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(16)
        self._layout.setContentsMargins(24, 24, 24, 20)

        # Title label
        lbl = QLabel("Group name:")
        lbl.setStyleSheet("color: #C0C0D0; font-size: 13px;")
        self._layout.addWidget(lbl)

        # Input field
        self._edit = QLineEdit(initial)
        self._edit.setPlaceholderText("e.g. Work, Gaming, Editing…")
        self._edit.setStyleSheet(
            "QLineEdit {"
            "  background: #1A1A2E; color: #E0E0FF;"
            "  border: 1px solid #444466; border-radius: 6px;"
            "  padding: 8px 10px; font-size: 13px;"
            "}"
            "QLineEdit:focus { border-color: #7B61FF; }"
        )
        self._edit.returnPressed.connect(self._accept)
        self._layout.addWidget(self._edit)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setStyleSheet(self._btn_style(accent=False))
        self._btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_cancel)

        self._btn_ok = QPushButton("OK")
        self._btn_ok.setDefault(True)
        self._btn_ok.setStyleSheet(self._btn_style(accent=True))
        self._btn_ok.clicked.connect(self._accept)
        btn_row.addWidget(self._btn_ok)

        self._layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    def group_name(self) -> str:
        return self._edit.text().strip()

    # ------------------------------------------------------------------
    def _accept(self):
        if self.group_name():
            self.accept()

    @staticmethod
    def _btn_style(accent: bool) -> str:
        bg = "#7B61FF" if accent else "#2A2A3E"
        hover = "#9E8AFF" if accent else "#3A3A50"
        return (
            f"QPushButton {{"
            f"  background: {bg}; color: #FFFFFF;"
            f"  border: none; border-radius: 6px;"
            f"  padding: 8px 20px; font-size: 13px;"
            f"}}"
            f"QPushButton:hover {{ background: {hover}; }}"
            f"QPushButton:pressed {{ background: {'#6A50EE' if accent else '#222232'}; }}"
        )
