"""
launcher.py – Launch and terminate groups of applications.

Features
--------
* Single-instance guard:  skips launching if the exe is already running.
* UAC elevation fallback: tries normal launch first; if Windows returns
  ERROR_ELEVATION_REQUIRED (740), retries with ShellExecuteW("runas").
* close_group:  uses psutil to find matching processes by executable path
                and terminates them gracefully (SIGTERM → SIGKILL fallback).
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
# Win32: ShellExecuteW for UAC elevation
# ---------------------------------------------------------------------------

_shell32 = ctypes.windll.shell32

_ShellExecuteW = _shell32.ShellExecuteW
_ShellExecuteW.argtypes = [
    wt.HWND,        # hwnd
    wt.LPCWSTR,     # lpOperation  ("runas", "open", …)
    wt.LPCWSTR,     # lpFile
    wt.LPCWSTR,     # lpParameters
    wt.LPCWSTR,     # lpDirectory
    ctypes.c_int,   # nShowCmd
]
_ShellExecuteW.restype = wt.HINSTANCE

SW_SHOWNORMAL = 1
_ERROR_ELEVATION_REQUIRED = 740   # WinError code for "needs admin"


# ---------------------------------------------------------------------------
# Launch result (for per-app feedback)
# ---------------------------------------------------------------------------

@dataclass
class LaunchResult:
    exe_path: str
    status: str       # "launched" | "already_running" | "elevated" | "error"
    detail: str = ""


# ---------------------------------------------------------------------------
# Single-instance check
# ---------------------------------------------------------------------------

def is_running(exe_path: str) -> bool:
    """
    Return True if there is already a running process whose executable
    matches *exe_path* (case-insensitive path comparison).
    """
    key = os.path.normpath(exe_path).lower()
    for proc in psutil.process_iter(["exe"]):
        try:
            proc_exe = proc.info.get("exe")
            if proc_exe and os.path.normpath(proc_exe).lower() == key:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


# ---------------------------------------------------------------------------
# Launch a single app (normal → runas fallback)
# ---------------------------------------------------------------------------

def _launch_single(exe_path: str) -> LaunchResult:
    """
    Launch one executable.

    Strategy:
        1. Check if already running → skip.
        2. Try subprocess.Popen (silent, no UAC prompt).
        3. On PermissionError / OSError 740 → retry with ShellExecuteW("runas").
    """
    # ── 1. Single-instance guard ──────────────────────────────────────
    if is_running(exe_path):
        return LaunchResult(exe_path, "already_running",
                            "Application is already open")

    cwd = os.path.dirname(exe_path) or None

    # ── 2. Normal launch ──────────────────────────────────────────────
    try:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            [exe_path],
            cwd=cwd,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
        return LaunchResult(exe_path, "launched")

    except PermissionError:
        pass  # fall through to runas
    except OSError as exc:
        if exc.winerror == _ERROR_ELEVATION_REQUIRED:
            pass  # fall through to runas
        else:
            return LaunchResult(exe_path, "error", str(exc))
    except Exception as exc:
        return LaunchResult(exe_path, "error", str(exc))

    # ── 3. Elevated launch (UAC prompt) ───────────────────────────────
    try:
        ret = _ShellExecuteW(
            None,           # hwnd
            "runas",        # verb  → triggers UAC
            exe_path,       # file
            None,           # params
            cwd,            # working dir
            SW_SHOWNORMAL,  # nShowCmd
        )
        # ShellExecuteW returns HINSTANCE > 32 on success
        if int(ret) > 32:
            return LaunchResult(exe_path, "elevated",
                                "Launched with administrator privileges")
        else:
            return LaunchResult(exe_path, "error",
                                f"ShellExecuteW returned {int(ret)}")
    except Exception as exc:
        return LaunchResult(exe_path, "error", str(exc))


# ---------------------------------------------------------------------------
# Launch group  (public API — unchanged signature)
# ---------------------------------------------------------------------------

_launch_results: list[LaunchResult] = []
_launch_lock = threading.Lock()


def launch_group(exe_paths: list[str]) -> None:
    """
    Launch all executables in the group concurrently.

    Each exe is spawned in its own thread so the UI never blocks.
    Already-running apps are silently skipped.
    """
    with _launch_lock:
        _launch_results.clear()

    def _run(path: str) -> None:
        result = _launch_single(path)
        with _launch_lock:
            _launch_results.append(result)

    for path in exe_paths:
        t = threading.Thread(target=_run, args=(path,), daemon=True)
        t.start()


def get_last_launch_results() -> list[LaunchResult]:
    """Return results from the most recent launch_group call."""
    with _launch_lock:
        return list(_launch_results)


# ---------------------------------------------------------------------------
# Close group  (unchanged logic)
# ---------------------------------------------------------------------------

def close_group(exe_paths: list[str]) -> list[str]:
    """
    Terminate all running processes whose executable matches one of the paths.

    Returns a list of exe paths that were actually terminated.
    Normalises paths to lowercase for case-insensitive comparison on Windows.
    """
    normalised = {os.path.normpath(p).lower() for p in exe_paths}
    terminated: list[str] = []

    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            proc_exe = proc.info.get("exe") or ""
            if os.path.normpath(proc_exe).lower() in normalised:
                proc.terminate()
                terminated.append(proc_exe)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # Give processes a moment then force-kill any survivors
    pids = _pids_for(exe_paths)
    procs = []
    for pid in pids:
        try:
            if psutil.pid_exists(pid):
                procs.append(psutil.Process(pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if procs:
        _gone, alive = psutil.wait_procs(procs, timeout=3)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    return terminated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pids_for(exe_paths: list[str]) -> list[int]:
    """Return PIDs of all running processes matching the given exe paths."""
    normalised = {os.path.normpath(p).lower() for p in exe_paths}
    pids = []
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            proc_exe = proc.info.get("exe") or ""
            if os.path.normpath(proc_exe).lower() in normalised:
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids


def running_exes(exe_paths: list[str]) -> set[str]:
    """
    Return the subset of exe_paths that currently have a running process.
    Useful for colour-coding cards or buttons.
    """
    normalised = {os.path.normpath(p).lower(): p for p in exe_paths}
    active: set[str] = set()
    for proc in psutil.process_iter(["exe"]):
        try:
            key = os.path.normpath(proc.info.get("exe") or "").lower()
            if key in normalised:
                active.add(normalised[key])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return active
