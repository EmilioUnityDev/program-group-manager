"""
launcher.py – Launch and terminate groups of applications.

Supports both Win32 (.exe) and UWP (Microsoft Store / AppX) apps.

Group persistence format (groups.json)
---------------------------------------
Each entry in a group is either:
  - A Win32 exe path:  "C:\\Program Files\\App\\app.exe"
  - A UWP identifier: "uwp:<AppID>"  (e.g. "uwp:SlackTech..!Slack")

Launch strategies
-----------------
  Win32:  subprocess.Popen (detached) → on PermissionError/740, ShellExecuteW("runas")
  UWP:    ShellExecuteW("open", "explorer.exe", "shell:AppsFolder\\<AppID>")
          UWP apps don't run as exe, ShellExecute is the only clean launcher.

Single-instance detection
--------------------------
  Win32:  psutil.process_iter, compare exe path (normpath, case-insensitive)
  UWP:    psutil.process_iter, look for any proc whose exe path contains
          "WindowsApps\\<PackageFamilyName>" (the pfn is everything before '!'
          in the AppID).  This works because UWP apps always run from
          C:\\Program Files\\WindowsApps\\<PFN>_ver\\App.exe.

Close group
-----------
  Win32:  terminate via psutil (unchanged).
  UWP:    psutil look-up by WindowsApps\\<PackageFamilyName>, then terminate.
          Note: some UWP processes run under ApplicationFrameHost.exe and
          cannot always be killed cleanly — we do a best-effort termination.
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

import psutil

# ---------------------------------------------------------------------------
# Win32 ShellExecuteW
# ---------------------------------------------------------------------------

_shell32 = ctypes.windll.shell32

_ShellExecuteW = _shell32.ShellExecuteW
_ShellExecuteW.argtypes = [
    wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int,
]
_ShellExecuteW.restype = wt.HINSTANCE

SW_SHOWNORMAL = 1
_ERROR_ELEVATION_REQUIRED = 740

# UWP identifier prefix stored in groups.json
UWP_PREFIX = "uwp:"


# ---------------------------------------------------------------------------
# Launch result (for per-app status feedback)
# ---------------------------------------------------------------------------

@dataclass
class LaunchResult:
    identifier: str   # exe_path or "uwp:<AppID>"
    status: str       # "launched" | "already_running" | "elevated" | "error"
    detail: str = ""


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------

def is_uwp_id(identifier: str) -> bool:
    return identifier.startswith(UWP_PREFIX)


def app_id_from_identifier(identifier: str) -> str:
    """Strip the 'uwp:' prefix and return the raw AppID."""
    return identifier[len(UWP_PREFIX):]


def package_family_name(app_id: str) -> str:
    """
    Extract PackageFamilyName from AppID.
    AppID format: "<PackageFamilyName>!<EntryPoint>"
    """
    return app_id.split("!")[0]


def make_uwp_identifier(app_id: str) -> str:
    return f"{UWP_PREFIX}{app_id}"


# ---------------------------------------------------------------------------
# Single-instance detection
# ---------------------------------------------------------------------------

def is_running_win32(exe_path: str) -> bool:
    """True if a process with this exe path is running (case-insensitive)."""
    key = os.path.normpath(exe_path).lower()
    for proc in psutil.process_iter(["exe"]):
        try:
            proc_exe = proc.info.get("exe")
            if proc_exe and os.path.normpath(proc_exe).lower() == key:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


def is_running_uwp(app_id: str) -> bool:
    """
    True if a UWP process is running for this AppID.

    Detection: any process whose exe path contains
    'WindowsApps\\<PackageFamilyName>' (case-insensitive substring).
    UWP apps always live under C:\\Program Files\\WindowsApps\\<PFN>_ver\\.
    """
    pfn = package_family_name(app_id).lower()
    needle = f"windowsapps\\{pfn}"
    for proc in psutil.process_iter(["exe"]):
        try:
            proc_exe = (proc.info.get("exe") or "").lower()
            if needle in proc_exe:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


def is_running(identifier: str) -> bool:
    """Dispatcher: check if a Win32 or UWP app is already running."""
    if is_uwp_id(identifier):
        return is_running_uwp(app_id_from_identifier(identifier))
    return is_running_win32(identifier)


# ---------------------------------------------------------------------------
# Single app launch
# ---------------------------------------------------------------------------

def _launch_win32(exe_path: str) -> LaunchResult:
    """Launch a Win32 exe with normal → runas fallback."""
    if is_running_win32(exe_path):
        return LaunchResult(exe_path, "already_running", "Already open")

    cwd = os.path.dirname(exe_path) or None

    # Normal launch
    try:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            [exe_path], cwd=cwd,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
        return LaunchResult(exe_path, "launched")
    except PermissionError:
        pass
    except OSError as exc:
        if exc.winerror == _ERROR_ELEVATION_REQUIRED:
            pass
        else:
            return LaunchResult(exe_path, "error", str(exc))
    except Exception as exc:
        return LaunchResult(exe_path, "error", str(exc))

    # Elevated launch (UAC)
    try:
        ret = _ShellExecuteW(None, "runas", exe_path, None, cwd, SW_SHOWNORMAL)
        if int(ret) > 32:
            return LaunchResult(exe_path, "elevated", "Launched with admin rights")
        return LaunchResult(exe_path, "error", f"ShellExecuteW returned {int(ret)}")
    except Exception as exc:
        return LaunchResult(exe_path, "error", str(exc))


def _launch_uwp(identifier: str) -> LaunchResult:
    """
    Launch a UWP app via shell:AppsFolder.

    Uses ShellExecuteW to tell Windows to open the app's virtual shell folder
    entry — the same mechanism the Start Menu uses. Works for all Store apps
    regardless of whether they expose a traditional .exe.
    """
    app_id = app_id_from_identifier(identifier)

    if is_running_uwp(app_id):
        return LaunchResult(identifier, "already_running", "Already open")

    shell_target = f"shell:AppsFolder\\{app_id}"
    try:
        ret = _ShellExecuteW(
            None, "open", "explorer.exe", shell_target, None, SW_SHOWNORMAL,
        )
        if int(ret) > 32:
            return LaunchResult(identifier, "launched")
        return LaunchResult(identifier, "error", f"ShellExecuteW returned {int(ret)}")
    except Exception as exc:
        return LaunchResult(identifier, "error", str(exc))


# ---------------------------------------------------------------------------
# Group launch  (public API — unchanged signature)
# ---------------------------------------------------------------------------

_launch_results: list[LaunchResult] = []
_launch_lock = threading.Lock()


def launch_group(identifiers: list[str]) -> None:
    """
    Launch all apps in the group concurrently.

    Each identifier is either a Win32 exe path or "uwp:<AppID>".
    Already-running apps are silently skipped.
    """
    with _launch_lock:
        _launch_results.clear()

    def _run(ident: str) -> None:
        result = (_launch_uwp(ident) if is_uwp_id(ident)
                  else _launch_win32(ident))
        with _launch_lock:
            _launch_results.append(result)

    for ident in identifiers:
        t = threading.Thread(target=_run, args=(ident,), daemon=True)
        t.start()


def get_last_launch_results() -> list[LaunchResult]:
    with _launch_lock:
        return list(_launch_results)


# ---------------------------------------------------------------------------
# Group close  (public API — unchanged signature)
# ---------------------------------------------------------------------------

def close_group(identifiers: list[str]) -> list[str]:
    """
    Terminate all running processes for Win32 or UWP apps in the group.
    Returns list of identifiers that were actually terminated.
    """
    terminated: list[str] = []

    # Separate Win32 vs UWP
    win32_ids = [i for i in identifiers if not is_uwp_id(i)]
    uwp_ids   = [i for i in identifiers if is_uwp_id(i)]

    # ── Win32 close ─────────────────────────────────────────────────────
    normalised = {os.path.normpath(p).lower() for p in win32_ids}
    to_kill: list[psutil.Process] = []

    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            proc_exe = proc.info.get("exe") or ""
            if os.path.normpath(proc_exe).lower() in normalised:
                proc.terminate()
                terminated.append(proc_exe)
                to_kill.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if to_kill:
        _gone, alive = psutil.wait_procs(to_kill, timeout=3)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    # ── UWP close ───────────────────────────────────────────────────────
    for ident in uwp_ids:
        app_id = app_id_from_identifier(ident)
        pfn = package_family_name(app_id).lower()
        needle = f"windowsapps\\{pfn}"
        uwp_procs: list[psutil.Process] = []

        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                proc_exe = (proc.info.get("exe") or "").lower()
                if needle in proc_exe:
                    proc.terminate()
                    uwp_procs.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        if uwp_procs:
            terminated.append(ident)
            _gone, alive = psutil.wait_procs(uwp_procs, timeout=3)
            for proc in alive:
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    return terminated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pids_for(identifiers: list[str]) -> list[int]:
    """Return PIDs for all running processes matching the given identifiers."""
    win32_ids = {os.path.normpath(i).lower()
                 for i in identifiers if not is_uwp_id(i)}
    uwp_needles = [
        f"windowsapps\\{package_family_name(app_id_from_identifier(i)).lower()}"
        for i in identifiers if is_uwp_id(i)
    ]
    pids = []
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            proc_exe = proc.info.get("exe") or ""
            norm = os.path.normpath(proc_exe).lower()
            if norm in win32_ids:
                pids.append(proc.info["pid"])
            elif any(n in norm for n in uwp_needles):
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids


def running_exes(identifiers: list[str]) -> set[str]:
    """Return the subset of identifiers that have a running process."""
    active: set[str] = set()
    win32_map = {os.path.normpath(i).lower(): i
                 for i in identifiers if not is_uwp_id(i)}
    uwp_map = {
        f"windowsapps\\{package_family_name(app_id_from_identifier(i)).lower()}": i
        for i in identifiers if is_uwp_id(i)
    }
    for proc in psutil.process_iter(["exe"]):
        try:
            proc_exe = (proc.info.get("exe") or "")
            norm = os.path.normpath(proc_exe).lower()
            if norm in win32_map:
                active.add(win32_map[norm])
            else:
                for needle, ident in uwp_map.items():
                    if needle in norm:
                        active.add(ident)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return active
