"""
scanner.py – Scans the Windows Start Menu and Microsoft Store (UWP) apps.

Win32 pipeline  (unchanged):
    .lnk → resolve → .exe → PrivateExtractIconsW (256/128/48) → QPixmap

UWP pipeline (new):
    PowerShell Get-StartApps → [{Name, AppID}]
    AppID = "<PackageFamilyName>!<EntryPoint>" (e.g. "SlackTech..!Slack")
    SHGetFileInfoW("shell:AppsFolder\\<AppID>", SHGFI_ICON) → HICON → QPixmap
    Launch: ShellExecuteW("open", "explorer.exe",
                          "shell:AppsFolder\\<AppID>")

Both types share the same AppInfo dataclass and appear in the same grid.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import QFileIconProvider

# ---------------------------------------------------------------------------
# Start Menu directories (Win32 .lnk scan)
# ---------------------------------------------------------------------------

START_MENU_DIRS = [
    r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
    str(Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
        / "Start Menu" / "Programs"),
]

_DISPLAY_SIZE = 48
_EXTRACT_SIZES = [256, 128, 48]


# ---------------------------------------------------------------------------
# AppInfo dataclass — supports both Win32 (.exe) and UWP (app_id) apps
# ---------------------------------------------------------------------------

@dataclass
class AppInfo:
    name: str
    exe_path: str          # Win32: absolute path; UWP: "" (unused)
    lnk_path: str          # Win32: .lnk path;   UWP: "" (unused)
    is_uwp: bool = False
    app_id: str = ""       # UWP only: "PackageFamilyName!EntryPoint"
    pixmap: Optional[QPixmap] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# In-memory icon cache
# ---------------------------------------------------------------------------

_icon_cache: dict[str, Optional[QPixmap]] = {}


# ---------------------------------------------------------------------------
# Win32 ctypes structures
# ---------------------------------------------------------------------------

class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon",    wt.BOOL),
        ("xHotspot", wt.DWORD),
        ("yHotspot", wt.DWORD),
        # Use c_size_t (unsigned, pointer-size) instead of HBITMAP (c_void_p)
        # so that large 64-bit GDI handles are NEVER read as None.
        ("hbmMask",  ctypes.c_size_t),
        ("hbmColor", ctypes.c_size_t),
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
        ("hIcon",         wt.HANDLE),
        ("iIcon",         ctypes.c_int),
        ("dwAttributes",  wt.DWORD),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName",    ctypes.c_wchar * 80),
    ]

SHGFI_ICON        = 0x000000100
SHGFI_LARGEICON   = 0x000000000
SHGFI_USEFILEATTRIBUTES = 0x000000010

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

# Explicit argtypes for GDI handle functions.
# On 64-bit Windows, GDI handles (HGDIOBJ, HBITMAP) are 64-bit pointers.
# Without argtypes, ctypes defaults to signed 32-bit and raises OverflowError
# for handles > 2^31. Using c_size_t (unsigned pointer-size) is always safe.
_HGDI = ctypes.c_size_t   # stands in for any GDI handle (HGDIOBJ/HBITMAP/HDC)

_GetObjectW.argtypes  = [_HGDI, ctypes.c_int, ctypes.c_void_p]
_GetObjectW.restype   = ctypes.c_int

_GetDIBits.argtypes   = [
    _HGDI,          # hdc
    _HGDI,          # hbm (HBITMAP)
    wt.UINT, wt.UINT,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wt.UINT,
]
_GetDIBits.restype    = ctypes.c_int

_DeleteObject.argtypes = [_HGDI]
_DeleteObject.restype  = wt.BOOL

_GetIconInfo.argtypes  = [_HGDI, ctypes.c_void_p]  # HICON, PICONINFO
_GetIconInfo.restype   = wt.BOOL

_DestroyIcon.argtypes  = [_HGDI]
_DestroyIcon.restype   = wt.BOOL

_PrivateExtractIconsW = _user32.PrivateExtractIconsW
_PrivateExtractIconsW.argtypes = [
    wt.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.UINT),
    wt.UINT, wt.UINT,
]
_PrivateExtractIconsW.restype = wt.UINT


# ---------------------------------------------------------------------------
# HICON → QPixmap  (shared by Win32 and UWP)
# ---------------------------------------------------------------------------

def _hicon_to_pixmap(hicon: int) -> Optional[QPixmap]:
    """Convert a Windows HICON to a QPixmap with real alpha channel."""
    try:
        ii = ICONINFO()
        if not _GetIconInfo(hicon, ctypes.byref(ii)):
            return None

        # c_size_t fields return Python ints, so explicit 0-check is correct
        hbm_color = int(ii.hbmColor)
        hbm_mask  = int(ii.hbmMask)

        if hbm_color == 0:
            # Monochrome icon (mask only) — no colour bitmap, skip
            if hbm_mask:
                _DeleteObject(hbm_mask)
            return None

        bm = BITMAP()
        _GetObjectW(hbm_color, ctypes.sizeof(BITMAP), ctypes.byref(bm))
        w, h = bm.bmWidth, bm.bmHeight

        if w <= 0 or h <= 0:
            _DeleteObject(hbm_color)
            if hbm_mask:
                _DeleteObject(hbm_mask)
            return None

        bmi = BITMAPINFOHEADER()
        bmi.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth       = w
        bmi.biHeight      = -h
        bmi.biPlanes      = 1
        bmi.biBitCount    = 32
        bmi.biCompression = 0

        buf_size = w * h * 4
        buf = (ctypes.c_ubyte * buf_size)()

        hdc_screen = _GetDC(0)
        hdc_mem = _CreateCompatibleDC(hdc_screen)
        _GetDIBits(hdc_mem, hbm_color, 0, h, buf, ctypes.byref(bmi), 0)
        _DeleteDC(hdc_mem)
        _ReleaseDC(0, hdc_screen)
        _DeleteObject(hbm_color)
        if hbm_mask:
            _DeleteObject(hbm_mask)

        raw = bytes(buf)
        has_alpha = any(raw[i] != 0 for i in range(3, len(raw), 4))
        fmt = (QImage.Format.Format_ARGB32_Premultiplied
               if has_alpha else QImage.Format.Format_RGB32)

        img = QImage(raw, w, h, w * 4, fmt).copy()
        return QPixmap.fromImage(img)

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Win32 icon extraction (PrivateExtractIconsW → SHGetFileInfo → Qt)
# ---------------------------------------------------------------------------

def _extract_private(path: str) -> Optional[QPixmap]:
    """Request high-res icon via PrivateExtractIconsW (256→128→48)."""
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
    """SHGetFileInfoW: works for both file paths AND virtual shell paths."""
    try:
        info = SHFILEINFOW()
        result = _SHGetFileInfoW(path, 0, ctypes.byref(info),
                                 ctypes.sizeof(info),
                                 SHGFI_ICON | SHGFI_LARGEICON)
        if not result or not info.hIcon:
            return None
        px = _hicon_to_pixmap(info.hIcon)
        _DestroyIcon(info.hIcon)
        return px
    except Exception:
        return None


def _extract_qt(path: str) -> Optional[QPixmap]:
    """Qt QFileIconProvider fallback."""
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


def _get_icon(exe_path: str, lnk_path: str = "") -> Optional[QPixmap]:
    """Win32 icon: PrivateExtractIconsW → SHGetFileInfo → Qt."""
    key = exe_path.lower()
    if key in _icon_cache:
        return _icon_cache[key]

    px: Optional[QPixmap] = None

    if lnk_path and os.path.isfile(lnk_path):
        px = _extract_private(lnk_path)
    if px is None and os.path.isfile(exe_path):
        px = _extract_private(exe_path)
    if px is None and lnk_path and os.path.isfile(lnk_path):
        px = _extract_shgetfileinfo(lnk_path)
    if px is None and os.path.isfile(exe_path):
        px = _extract_shgetfileinfo(exe_path)
    if px is None:
        px = _extract_qt(exe_path)

    _icon_cache[key] = px
    return px


# ---------------------------------------------------------------------------
# UWP icon extraction via shell:AppsFolder\<AppID>
# ---------------------------------------------------------------------------

def _get_uwp_icon(app_id: str) -> Optional[QPixmap]:
    """
    Extract the icon for a UWP / Microsoft Store app at high resolution.

    Pipeline (all-ctypes, no extra dependencies):
      1. CoInitializeEx              — activates the shell COM namespace
      2. SHGetFileInfoW(PIDL,
             SHGFI_SYSICONINDEX)    — get this app's index into the system image list
      3. SHGetImageList(SHIL_JUMBO) — IImageList COM object for 256×256 icons
      4. IImageList::GetIcon(index) — HICON at full resolution
      5. _hicon_to_pixmap(HICON)    — BGRA → QPixmap

    Why not plain SHGetFileInfoW(string)?
      SHGetFileInfoW only accepts real filesystem paths as strings.
      Virtual shell paths ("shell:AppsFolder\\...") are namespace objects
      that require a PIDL to address — hence the two-step approach.

    Why SHGetImageList instead of SHGetFileInfoW(SHGFI_ICON|SHGFI_PIDL)?
      SHGFI_PIDL returns only a 32×32 LARGE icon from the system image list.
      SHGetImageList gives access to EXTRALARGE (48×48) and JUMBO (256×256)
      tiers, which contain the real UWP app logos at full fidelity.
    """
    cache_key = f"uwp:{app_id}"
    if cache_key in _icon_cache:
        return _icon_cache[cache_key]

    px: Optional[QPixmap] = None
    shell_path = f"shell:AppsFolder\\{app_id}"

    try:
        # 1. Ensure COM is active for this thread (idempotent call)
        COINIT_APARTMENTTHREADED = 0x2
        ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

        SHGFI_SYSICONINDEX = 0x4000
        SHGFI_LARGEICON    = 0x0
        SHGFI_PIDL_F       = 0x8

        # 2. PIDL for the virtual shell path
        pidl = ctypes.c_void_p(0)
        sfgao = ctypes.c_ulong(0)
        parse_display_name = ctypes.windll.shell32.SHParseDisplayName
        hr = parse_display_name(
            shell_path, None, ctypes.byref(pidl), 0, ctypes.byref(sfgao)
        )
        if hr != 0 or not pidl.value:
            _icon_cache[cache_key] = None
            return None

        try:
            # 3. Get the icon's index in the system image list
            info = SHFILEINFOW()
            r = ctypes.windll.shell32.SHGetFileInfoW(
                pidl, 0, ctypes.byref(info), ctypes.sizeof(info),
                SHGFI_SYSICONINDEX | SHGFI_LARGEICON | SHGFI_PIDL_F,
            )
            icon_index = info.iIcon if r else -1
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pidl)

        if icon_index < 0:
            _icon_cache[cache_key] = None
            return None

        # 4. Try image list tiers (best → acceptable)
        SHIL_JUMBO      = 4   # 256×256
        SHIL_EXTRALARGE = 2   # 48×48
        ILD_TRANSPARENT = 1

        # IImageList COM vtable slots (standard Windows IImageList)
        VTBL_RELEASE   = 2
        VTBL_GETICON   = 10

        for shil_size in (SHIL_JUMBO, SHIL_EXTRALARGE):
            image_list = ctypes.c_void_p(0)
            iid_guid = (ctypes.c_byte * 16)(
                0x26, 0x59, 0xEB, 0x46, 0x2E, 0x58, 0x17, 0x40,
                0x9F, 0xDF, 0xE8, 0x99, 0x8D, 0xAA, 0x09, 0x50,
            )
            hr2 = ctypes.windll.shell32.SHGetImageList(
                shil_size, ctypes.byref(iid_guid), ctypes.byref(image_list)
            )
            if hr2 != 0 or not image_list.value:
                continue

            # Navigate IImageList vtable
            vtbl_ptr = ctypes.cast(image_list, ctypes.POINTER(ctypes.c_void_p))[0]
            vtbl_funcs = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p))

            get_icon = ctypes.CFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,  # this
                ctypes.c_int,     # i (icon index)
                ctypes.c_uint,    # flags
                ctypes.POINTER(wt.HICON),
            )(vtbl_funcs[VTBL_GETICON])

            hicon = wt.HICON(0)
            hr3 = get_icon(image_list, icon_index, ILD_TRANSPARENT, ctypes.byref(hicon))

            release = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                vtbl_funcs[VTBL_RELEASE]
            )
            release(image_list)

            if hr3 == 0 and hicon.value:
                px = _hicon_to_pixmap(hicon.value)
                _DestroyIcon(hicon.value)
                if px and not px.isNull():
                    break   # got a good icon; stop trying smaller sizes

    except Exception:
        pass

    _icon_cache[cache_key] = px
    return px


# ---------------------------------------------------------------------------
# LNK resolution (Win32)
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
# UWP scan via PowerShell Get-StartApps
# ---------------------------------------------------------------------------

def _scan_uwp() -> list[dict]:
    """
    Run Get-StartApps via PowerShell and return a list of dicts:
        [{"Name": "Slack", "AppID": "SlackTechnologies...!Slack"}, ...]

    Get-StartApps enumerates the modern apps registered in the Start Menu,
    including all Microsoft Store / UWP / AppX packages.
    It's the official, stable API for this purpose.
    """
    try:
        ps_cmd = (
            "Get-StartApps | "
            "Where-Object { $_.AppID -match '!' } | "      # UWP apps have !EntryPoint
            "Select-Object Name, AppID | "
            "ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        data = json.loads(result.stdout.strip())
        # PowerShell returns an object (not array) when there's only one item
        if isinstance(data, dict):
            data = [data]
        return [d for d in data if d.get("AppID") and d.get("Name")]

    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_start_menu() -> list[AppInfo]:
    """
    Scan Win32 Start Menu (.lnk) AND Microsoft Store (UWP) apps.
    Returns a unified, deduplicated, sorted list of AppInfo.
    """
    apps: dict[str, AppInfo] = {}

    # ── Win32 scan (unchanged) ──────────────────────────────────────────
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

    # ── UWP scan ────────────────────────────────────────────────────────
    uwp_entries = _scan_uwp()
    for entry in uwp_entries:
        app_id = entry["AppID"]
        name   = entry["Name"]
        key    = f"uwp:{app_id.lower()}"
        if key in apps:
            continue

        pixmap = _get_uwp_icon(app_id)
        apps[key] = AppInfo(
            name=name,
            exe_path="",       # UWP: no traditional exe
            lnk_path="",
            is_uwp=True,
            app_id=app_id,
            pixmap=pixmap,
        )

    return sorted(apps.values(), key=lambda a: a.name.lower())
