"""
scanner.py – Scans the Windows Start Menu for installed applications,
             resolves .lnk shortcuts to their target .exe, and extracts
             the application icon as a high-resolution QPixmap with alpha.

High-res icon pipeline:
    1. Resolve .lnk → target .exe via win32com.client
    2. Use PrivateExtractIconsW (user32, ctypes) to request icons at
       256×256, then 128×128, then 48×48 — first success wins.
    3. Fallback: SHGetFileInfoW  (32×32 system icon)
    4. Fallback: Qt QFileIconProvider
    5. Convert HICON → BGRA pixels via GetIconInfo + GetDIBits
    6. Wrap into QImage(Format_ARGB32_Premultiplied) → QPixmap
    7. Results cached in-memory by exe_path.
"""

import ctypes
import ctypes.wintypes as wt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PyQt6.QtGui import QPixmap, QImage, QIcon
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import QFileIconProvider

# ---------------------------------------------------------------------------
# Start Menu directories to scan
# ---------------------------------------------------------------------------

START_MENU_DIRS = [
    r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
    str(Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"),
]

_DISPLAY_SIZE = 48       # final display size in the grid
_EXTRACT_SIZES = [256, 128, 48]   # sizes to try, best first


@dataclass
class AppInfo:
    name: str
    exe_path: str
    lnk_path: str
    pixmap: Optional[QPixmap] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# In-memory icon cache  (keyed by normalised exe path)
# ---------------------------------------------------------------------------

_icon_cache: dict[str, Optional[QPixmap]] = {}


# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------

class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon",    wt.BOOL),
        ("xHotspot", wt.DWORD),
        ("yHotspot", wt.DWORD),
        ("hbmMask",  wt.HBITMAP),
        ("hbmColor", wt.HBITMAP),
    ]

class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType",       wt.LONG),
        ("bmWidth",      wt.LONG),
        ("bmHeight",     wt.LONG),
        ("bmWidthBytes", wt.LONG),
        ("bmPlanes",     wt.WORD),
        ("bmBitsPixel",  wt.WORD),
        ("bmBits",       ctypes.c_void_p),
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          wt.DWORD),
        ("biWidth",         wt.LONG),
        ("biHeight",        wt.LONG),
        ("biPlanes",        wt.WORD),
        ("biBitCount",      wt.WORD),
        ("biCompression",   wt.DWORD),
        ("biSizeImage",     wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed",       wt.DWORD),
        ("biClrImportant",  wt.DWORD),
    ]

class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon",        wt.HANDLE),
        ("iIcon",        ctypes.c_int),
        ("dwAttributes", wt.DWORD),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName",    ctypes.c_wchar * 80),
    ]

# Constants
SHGFI_ICON      = 0x000000100
SHGFI_LARGEICON = 0x000000000

# ---------------------------------------------------------------------------
# DLL handles
# ---------------------------------------------------------------------------

_shell32 = ctypes.windll.shell32
_user32  = ctypes.windll.user32
_gdi32   = ctypes.windll.gdi32

_SHGetFileInfoW      = _shell32.SHGetFileInfoW
_DestroyIcon         = _user32.DestroyIcon
_GetIconInfo         = _user32.GetIconInfo
_GetDC               = _user32.GetDC
_ReleaseDC           = _user32.ReleaseDC
_DeleteObject        = _gdi32.DeleteObject
_GetDIBits           = _gdi32.GetDIBits
_CreateCompatibleDC  = _gdi32.CreateCompatibleDC
_DeleteDC            = _gdi32.DeleteDC
_GetObjectW          = _gdi32.GetObjectW

# PrivateExtractIconsW lets us request a specific pixel size.
# Unlike ExtractIconEx (always 32×32 large), this one can pull 256×256.
_PrivateExtractIconsW = _user32.PrivateExtractIconsW
_PrivateExtractIconsW.argtypes = [
    wt.LPCWSTR,          # lpszFile
    ctypes.c_int,        # nIconIndex
    ctypes.c_int,        # cxIcon
    ctypes.c_int,        # cyIcon
    ctypes.POINTER(wt.HANDLE),  # phicon
    ctypes.POINTER(wt.UINT),    # piconid
    wt.UINT,             # nIcons
    wt.UINT,             # flags
]
_PrivateExtractIconsW.restype = wt.UINT


# ---------------------------------------------------------------------------
# HICON → QPixmap  (BGRA with real alpha)
# ---------------------------------------------------------------------------

def _hicon_to_pixmap(hicon: int) -> Optional[QPixmap]:
    """Convert a Windows HICON handle to a QPixmap with real alpha channel."""
    try:
        ii = ICONINFO()
        if not _GetIconInfo(hicon, ctypes.byref(ii)):
            return None

        hbmColor = ii.hbmColor
        hbmMask  = ii.hbmMask
        if not hbmColor:
            if hbmMask:
                _DeleteObject(hbmMask)
            return None

        bm = BITMAP()
        _GetObjectW(hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bm))
        w, h = bm.bmWidth, bm.bmHeight

        if w <= 0 or h <= 0:
            _DeleteObject(hbmColor)
            if hbmMask:
                _DeleteObject(hbmMask)
            return None

        # BITMAPINFOHEADER — top-down (negative height) so rows come in
        # natural order (top row first).
        bmi = BITMAPINFOHEADER()
        bmi.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth       = w
        bmi.biHeight      = -h          # negative → top-down
        bmi.biPlanes      = 1
        bmi.biBitCount    = 32
        bmi.biCompression = 0           # BI_RGB

        buf_size = w * h * 4
        buf = (ctypes.c_ubyte * buf_size)()

        hdc_screen = _GetDC(0)
        hdc_mem = _CreateCompatibleDC(hdc_screen)
        _GetDIBits(hdc_mem, hbmColor, 0, h, buf, ctypes.byref(bmi), 0)
        _DeleteDC(hdc_mem)
        _ReleaseDC(0, hdc_screen)
        _DeleteObject(hbmColor)
        if hbmMask:
            _DeleteObject(hbmMask)

        raw = bytes(buf)

        # Detect if any pixel actually has non-zero alpha.
        has_alpha = any(raw[i] != 0 for i in range(3, len(raw), 4))

        fmt = (QImage.Format.Format_ARGB32_Premultiplied
               if has_alpha
               else QImage.Format.Format_RGB32)

        img = QImage(raw, w, h, w * 4, fmt)
        # QImage doesn't own the buffer — copy it so it survives
        img = img.copy()

        return QPixmap.fromImage(img)

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Extraction strategies (ordered by quality)
# ---------------------------------------------------------------------------

def _extract_private(path: str) -> Optional[QPixmap]:
    """
    Use PrivateExtractIconsW to pull a high-res icon.

    Tries 256 → 128 → 48 and returns the first success.
    """
    for size in _EXTRACT_SIZES:
        try:
            hicon = wt.HANDLE()
            iconid = wt.UINT()
            count = _PrivateExtractIconsW(
                path, 0, size, size,
                ctypes.byref(hicon), ctypes.byref(iconid), 1, 0,
            )
            if count and count != 0xFFFFFFFF and hicon.value:
                px = _hicon_to_pixmap(hicon.value)
                _DestroyIcon(hicon.value)
                if px and not px.isNull():
                    return px
        except Exception:
            continue
    return None


def _extract_shgetfileinfo(path: str) -> Optional[QPixmap]:
    """Fallback: SHGetFileInfoW (system-associated icon, ~32×32)."""
    try:
        info = SHFILEINFOW()
        flags = SHGFI_ICON | SHGFI_LARGEICON
        result = _SHGetFileInfoW(path, 0, ctypes.byref(info),
                                 ctypes.sizeof(info), flags)
        if not result or not info.hIcon:
            return None
        px = _hicon_to_pixmap(info.hIcon)
        _DestroyIcon(info.hIcon)
        return px
    except Exception:
        return None


def _extract_qt(path: str) -> Optional[QPixmap]:
    """Ultra-fallback: Qt QFileIconProvider."""
    try:
        from PyQt6.QtCore import QFileInfo
        provider = QFileIconProvider()
        icon = provider.icon(QFileInfo(path))
        if icon.isNull():
            return None
        px = icon.pixmap(QSize(_DISPLAY_SIZE, _DISPLAY_SIZE))
        return px if not px.isNull() else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Master icon function with cache
# ---------------------------------------------------------------------------

def _get_icon(exe_path: str, lnk_path: str = "") -> Optional[QPixmap]:
    """
    Extract the best-quality icon for an executable.

    Cascade:
        1. PrivateExtractIconsW on .lnk  (256 → 128 → 48)
        2. PrivateExtractIconsW on .exe
        3. SHGetFileInfoW on .lnk
        4. SHGetFileInfoW on .exe
        5. Qt QFileIconProvider on .exe
    """
    key = exe_path.lower()
    if key in _icon_cache:
        return _icon_cache[key]

    px: Optional[QPixmap] = None

    # 1) .lnk — high-res
    if lnk_path and os.path.isfile(lnk_path):
        px = _extract_private(lnk_path)

    # 2) .exe — high-res
    if px is None and os.path.isfile(exe_path):
        px = _extract_private(exe_path)

    # 3) .lnk — SHGetFileInfo (lower res)
    if px is None and lnk_path and os.path.isfile(lnk_path):
        px = _extract_shgetfileinfo(lnk_path)

    # 4) .exe — SHGetFileInfo
    if px is None and os.path.isfile(exe_path):
        px = _extract_shgetfileinfo(exe_path)

    # 5) Qt fallback
    if px is None:
        px = _extract_qt(exe_path)

    _icon_cache[key] = px
    return px


# ---------------------------------------------------------------------------
# LNK resolution
# ---------------------------------------------------------------------------

def _resolve_lnk(lnk_path: str) -> Optional[str]:
    """Return the target .exe path for a .lnk file, or None."""
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(lnk_path)
        target = shortcut.Targetpath
        if target and os.path.isfile(target):
            return target
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_start_menu() -> list[AppInfo]:
    """
    Scan Windows Start Menu directories and return a sorted list of AppInfo.
    Each entry has the display name, resolved exe path, lnk path, and icon.
    """
    apps: dict[str, AppInfo] = {}

    for base_dir in START_MENU_DIRS:
        if not os.path.isdir(base_dir):
            continue
        for root, _dirs, files in os.walk(base_dir):
            for fname in files:
                if not fname.lower().endswith(".lnk"):
                    continue
                lnk_full = os.path.join(root, fname)
                exe_path = _resolve_lnk(lnk_full)
                if not exe_path:
                    continue
                key = exe_path.lower()
                if key in apps:
                    continue

                name = Path(fname).stem
                pixmap = _get_icon(exe_path, lnk_full)

                apps[key] = AppInfo(
                    name=name,
                    exe_path=exe_path,
                    lnk_path=lnk_full,
                    pixmap=pixmap,
                )

    return sorted(apps.values(), key=lambda a: a.name.lower())
