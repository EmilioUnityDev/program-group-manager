"""
main_window.py – FlowLauncher main application window.

Layout
------
┌─────────────────────────────────────────────────────┐
│  [FlowLauncher]   [Group ▾]  [+New] [Rename] [✕Del] │  ← top bar
│─────────────────────────────────────────────────────│
│                                                     │
│   AppGallery (scrollable grid of icons)             │  ← centre
│                                                     │
│─────────────────────────────────────────────────────│
│  [Search …]          [▶ Launch Group] [■ Close Group]│  ← bottom bar
└─────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import threading
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont, QColor, QPalette
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QPushButton, QLineEdit,
    QMessageBox, QStatusBar, QSizePolicy, QFrame,
)

from core import groups as grp_store
from core import launcher
from core.scanner import AppInfo, scan_start_menu
from ui.app_gallery import AppGallery
from ui.group_dialog import GroupDialog


# ---------------------------------------------------------------------------
# Worker: scan Start Menu in a background thread
# ---------------------------------------------------------------------------

class _ScanWorker(QObject):
    finished = pyqtSignal(list)  # list[AppInfo]

    def run(self) -> None:
        apps = scan_start_menu()
        self.finished.emit(apps)


class _ScanThread(QThread):
    def __init__(self, worker: _ScanWorker):
        super().__init__()
        self._worker = worker
        self._worker.moveToThread(self)
        self.started.connect(self._worker.run)


# ---------------------------------------------------------------------------
# Worker: close group in a background thread
# ---------------------------------------------------------------------------

class _CloseWorker(QObject):
    finished = pyqtSignal(list)  # list[str] terminated

    def __init__(self, exe_paths: list[str]):
        super().__init__()
        self._paths = exe_paths

    def run(self) -> None:
        terminated = launcher.close_group(self._paths)
        self.finished.emit(terminated)


class _CloseThread(QThread):
    def __init__(self, worker: _CloseWorker):
        super().__init__()
        self._worker = worker
        self._worker.moveToThread(self)
        self.started.connect(self._worker.run)


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("FlowLauncher")
        self.setMinimumSize(900, 620)

        # ── Eliminate any white margin / frame background ──────────────
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QMainWindow { background: #12121E; border: none; }")
        self.setMenuBar(None)   # No native menu bar → no white strip

        self._all_apps: list[AppInfo] = []
        self._current_group: Optional[str] = None
        self._scan_thread: Optional[_ScanThread] = None
        self._close_thread: Optional[_CloseThread] = None

        self._build_ui()
        self._refresh_groups()
        self._start_scan()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_top_bar())
        layout.addWidget(self._build_search_bar())
        layout.addWidget(self._build_separator())

        self._gallery = AppGallery()
        layout.addWidget(self._gallery, stretch=1)

        layout.addWidget(self._build_separator())
        layout.addWidget(self._build_bottom_bar())

        # Status bar
        sb = QStatusBar()
        sb.setStyleSheet(
            "QStatusBar { color: #555577; font-size: 11px;"
            "  background: #0E0E18; border-top: 1px solid #1E1E30;"
            "  padding: 2px 8px; }"
            "QStatusBar::item { border: none; }"
        )
        sb.setContentsMargins(0, 0, 0, 0)
        self.setStatusBar(sb)

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(56)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setStyleSheet("background: #0E0E18;")

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)

        # App name
        title = QLabel("⚡ FlowLauncher")
        title.setStyleSheet("color: #9B8AFF; font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        layout.addStretch()

        # Group selector
        lbl = QLabel("Group:")
        lbl.setStyleSheet("color: #888899; font-size: 13px;")
        layout.addWidget(lbl)

        self._grp_combo = QComboBox()
        self._grp_combo.setMinimumWidth(180)
        self._grp_combo.setStyleSheet(self._combo_style())
        self._grp_combo.currentTextChanged.connect(self._on_group_changed)
        layout.addWidget(self._grp_combo)

        # New / Rename / Delete
        self._btn_new = self._make_btn("＋ New", "#7B61FF", "#9E8AFF")
        self._btn_new.clicked.connect(self._new_group)
        layout.addWidget(self._btn_new)

        self._btn_rename = self._make_btn("✏ Rename", "#2A2A3E", "#3A3A50")
        self._btn_rename.clicked.connect(self._rename_group)
        layout.addWidget(self._btn_rename)

        self._btn_delete = self._make_btn("✕ Delete", "#3E1A1A", "#5E2A2A")
        self._btn_delete.clicked.connect(self._delete_group)
        layout.addWidget(self._btn_delete)

        return bar

    def _build_search_bar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(44)
        bar.setStyleSheet("background: #16162A;")

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 6, 16, 6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Filter applications…")
        self._search.setStyleSheet(
            "QLineEdit {"
            "  background: #1E1E30; color: #C0C0D0;"
            "  border: 1px solid #333355; border-radius: 6px;"
            "  padding: 4px 10px; font-size: 13px;"
            "}"
            "QLineEdit:focus { border-color: #7B61FF; }"
        )
        self._search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search)

        return bar

    def _build_bottom_bar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(60)
        bar.setStyleSheet("background: #12121E;")

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        # Save selection hint
        hint = QLabel("Click apps to add/remove them from the selected group.")
        hint.setStyleSheet("color: #444466; font-size: 11px;")
        layout.addWidget(hint, stretch=1)

        self._btn_save = self._make_btn("💾  Save Group", "#2A3E2A", "#3A5A3A", min_w=140)
        self._btn_save.clicked.connect(self._save_group)
        layout.addWidget(self._btn_save)

        self._btn_launch = self._make_btn("▶  Launch Group", "#1B4A1B", "#2E7A2E", min_w=150)
        self._btn_launch.setObjectName("launch_btn")
        self._btn_launch.clicked.connect(self._launch_group)
        layout.addWidget(self._btn_launch)

        self._btn_close_grp = self._make_btn("■  Close Group", "#4A1B1B", "#7A2E2E", min_w=150)
        self._btn_close_grp.setObjectName("close_btn")
        self._btn_close_grp.clicked.connect(self._close_group)
        layout.addWidget(self._btn_close_grp)

        return bar

    @staticmethod
    def _build_separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet("background: #252535;")
        return line

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _start_scan(self) -> None:
        self._gallery.update_placeholder("⏳  Scanning Start Menu…")
        self.statusBar().showMessage("Scanning installed applications…")

        worker = _ScanWorker()
        worker.finished.connect(self._on_scan_done)
        self._scan_thread = _ScanThread(worker)
        self._scan_thread.start()

    def _on_scan_done(self, apps: list[AppInfo]) -> None:
        self._all_apps = apps
        self._apply_filter()
        count = len(apps)
        self.statusBar().showMessage(f"Found {count} application{'s' if count != 1 else ''}.")
        if self._scan_thread:
            self._scan_thread.quit()
            self._scan_thread.wait()

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def _refresh_groups(self) -> None:
        names = grp_store.list_groups()
        prev = self._grp_combo.currentText()
        self._grp_combo.blockSignals(True)
        self._grp_combo.clear()
        self._grp_combo.addItem("— No group —")
        self._grp_combo.addItems(names)
        # Restore previous selection if still present
        idx = self._grp_combo.findText(prev)
        self._grp_combo.setCurrentIndex(max(0, idx))
        self._grp_combo.blockSignals(False)
        has_group = self._grp_combo.currentIndex() > 0
        self._set_group_btns_enabled(has_group)

    def _on_group_changed(self, name: str) -> None:
        is_real = name and name != "— No group —"
        self._current_group = name if is_real else None
        self._set_group_btns_enabled(is_real)
        selected = grp_store.get_group(name) if is_real else []
        self._gallery.set_selected_paths(selected)

    def _set_group_btns_enabled(self, enabled: bool) -> None:
        for btn in (self._btn_rename, self._btn_delete,
                    self._btn_save, self._btn_launch, self._btn_close_grp):
            btn.setEnabled(enabled)

    def _new_group(self) -> None:
        dlg = GroupDialog(self, title="New Group")
        if dlg.exec():
            name = dlg.group_name()
            if grp_store.create_group(name):
                self._refresh_groups()
                idx = self._grp_combo.findText(name)
                if idx >= 0:
                    self._grp_combo.setCurrentIndex(idx)
            else:
                QMessageBox.warning(self, "FlowLauncher", f"Group '{name}' already exists.")

    def _rename_group(self) -> None:
        if not self._current_group:
            return
        dlg = GroupDialog(self, title="Rename Group", initial=self._current_group)
        if dlg.exec():
            new_name = dlg.group_name()
            if grp_store.rename_group(self._current_group, new_name):
                self._refresh_groups()
                idx = self._grp_combo.findText(new_name)
                if idx >= 0:
                    self._grp_combo.setCurrentIndex(idx)
            else:
                QMessageBox.warning(self, "FlowLauncher",
                                    f"Could not rename to '{new_name}'. Name may already exist.")

    def _delete_group(self) -> None:
        if not self._current_group:
            return
        reply = QMessageBox.question(
            self, "Delete Group",
            f"Delete group '{self._current_group}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            grp_store.delete_group(self._current_group)
            self._current_group = None
            self._refresh_groups()
            self._gallery.set_selected_paths([])

    def _save_group(self) -> None:
        if not self._current_group:
            return
        paths = self._gallery.get_selected_paths()
        grp_store.set_group_apps(self._current_group, paths)
        self.statusBar().showMessage(
            f"Group '{self._current_group}' saved ({len(paths)} app{'s' if len(paths) != 1 else ''}).",
            4000,
        )

    # ------------------------------------------------------------------
    # Launch / Close
    # ------------------------------------------------------------------

    def _launch_group(self) -> None:
        if not self._current_group:
            return
        paths = grp_store.get_group(self._current_group)
        if not paths:
            QMessageBox.information(self, "FlowLauncher",
                                    "This group has no applications. Save your selection first.")
            return
        launcher.launch_group(paths)
        self.statusBar().showMessage(
            f"Launching group '{self._current_group}'…", 5000,
        )
        # Brief green flash on the button
        self._btn_launch.setStyleSheet(self._make_btn_style("#2E7A2E", "#45AA45"))
        QTimer.singleShot(1500, lambda: self._btn_launch.setStyleSheet(
            self._make_btn_style("#1B4A1B", "#2E7A2E")))

        # After a delay, collect and show per-app results
        QTimer.singleShot(2500, self._show_launch_results)

    def _show_launch_results(self) -> None:
        """Show a summary of the last launch attempt in the status bar."""
        results = launcher.get_last_launch_results()
        if not results:
            return
        launched  = sum(1 for r in results if r.status == "launched")
        skipped   = sum(1 for r in results if r.status == "already_running")
        elevated  = sum(1 for r in results if r.status == "elevated")
        errors    = sum(1 for r in results if r.status == "error")

        parts = []
        if launched:
            parts.append(f"{launched} launched")
        if elevated:
            parts.append(f"{elevated} elevated")
        if skipped:
            parts.append(f"{skipped} already open")
        if errors:
            parts.append(f"{errors} failed")

        msg = "Group: " + ", ".join(parts) + "."
        self.statusBar().showMessage(msg, 6000)

    def _close_group(self) -> None:
        if not self._current_group:
            return
        paths = grp_store.get_group(self._current_group)
        if not paths:
            return

        # Amber "working" state
        self._btn_close_grp.setEnabled(False)
        self._btn_close_grp.setText("⏳  Closing…")
        self._btn_close_grp.setStyleSheet(self._make_btn_style("#4A3B00", "#7A6000"))
        self.statusBar().showMessage("Closing group processes…")

        worker = _CloseWorker(paths)
        worker.finished.connect(self._on_close_done)
        self._close_thread = _CloseThread(worker)
        self._close_thread.start()

    def _on_close_done(self, terminated: list[str]) -> None:
        self._btn_close_grp.setEnabled(True)
        self._btn_close_grp.setText("■  Close Group")
        self._btn_close_grp.setStyleSheet(self._make_btn_style("#4A1B1B", "#7A2E2E"))
        n = len(terminated)
        self.statusBar().showMessage(
            f"Closed {n} process{'es' if n != 1 else ''} from "
            f"group '{self._current_group}'.", 5000,
        )
        if self._close_thread:
            self._close_thread.quit()
            self._close_thread.wait()

    # ------------------------------------------------------------------
    # Search / filter
    # ------------------------------------------------------------------

    def _on_search_changed(self, text: str) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        q = self._search.text().strip().lower()
        visible = [a for a in self._all_apps if q in a.name.lower()] if q else self._all_apps
        selected = grp_store.get_group(self._current_group) if self._current_group else []
        self._gallery.populate(visible, selected)

    # ------------------------------------------------------------------
    # Style helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_btn(text: str, bg: str, hover: str, min_w: int = 100) -> QPushButton:
        btn = QPushButton(text)
        btn.setMinimumWidth(min_w)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(MainWindow._make_btn_style(bg, hover))
        return btn

    @staticmethod
    def _make_btn_style(bg: str, hover: str) -> str:
        return (
            f"QPushButton {{"
            f"  background: {bg}; color: #E0E0FF;"
            f"  border: none; border-radius: 6px;"
            f"  padding: 8px 14px; font-size: 12px; font-weight: bold;"
            f"}}"
            f"QPushButton:hover {{ background: {hover}; }}"
            f"QPushButton:disabled {{ background: #1A1A2E; color: #444466; }}"
        )

    @staticmethod
    def _combo_style() -> str:
        return (
            "QComboBox {"
            "  background: #1E1E30; color: #C0C0D0;"
            "  border: 1px solid #333355; border-radius: 6px;"
            "  padding: 5px 10px; font-size: 13px;"
            "}"
            "QComboBox:hover { border-color: #7B61FF; }"
            "QComboBox::drop-down { border: none; width: 24px; }"
            "QComboBox QAbstractItemView {"
            "  background: #1A1A2E; color: #C0C0D0;"
            "  selection-background-color: #2A2A4E;"
            "  border: 1px solid #333355;"
            "}"
        )
