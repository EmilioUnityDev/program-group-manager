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
  Win32:  psutil.process_iter, compare exe path (normpath, case-insensitive).
          Squirrel-aware: also matches processes whose exe lives under the same
          parent folder (e.g. stored=AppData/App/App.exe but running from
          AppData/App/app-3.4.27/App.exe).
  UWP:    psutil.process_iter, look for any proc whose exe path contains
          "WindowsApps\\<PackageFamilyName>" (the pfn is everything before '!'
          in the AppID).  This works because UWP apps always run from
          C:\\Program Files\\WindowsApps\\<PFN>_ver\\App.exe.

Close group
-----------
  Win32:  terminate via psutil, with Squirrel + WindowsApps fallbacks.
  UWP:    psutil look-up by WindowsApps\\<PackageFamilyName>, then terminate.
          Note: some UWP processes run under ApplicationFrameHost.exe and
          cannot always be killed cleanly — we do a best-effort termination.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

import psutil

log = logging.getLogger(__name__)

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


def _uwp_needle(app_id: str) -> str:
    """
    Build a version-independent substring needle for matching UWP process exe paths.

    WindowsApps folder format:
      <PackageName>_<Version>_<Arch>__<PublisherId>

    The PackageFamilyName (PFN) stored in the AppID is:
      <PackageName>_<PublisherId>

    The PFN is therefore NOT a direct substring of the folder name because the
    folder inserts <Version>_<Arch>__ between PackageName and PublisherId.

    We use only the PackageName part (everything before the first '_' in the PFN)
    followed by an underscore, which uniquely identifies the app directory without
    depending on version, architecture, or publisher ID.

    Example:
      AppID  = "com.tinyspeck.slackdesktop_8yrtsj140pw4g!Slack"
      PFN    = "com.tinyspeck.slackdesktop_8yrtsj140pw4g"
      needle = "windowsapps\\com.tinyspeck.slackdesktop_"   # matches the folder
    """
    pfn = package_family_name(app_id).lower()
    # PackageName is everything before the first '_' in the PFN
    package_name = pfn.split("_")[0]
    return f"windowsapps\\{package_name}_"


def make_uwp_identifier(app_id: str) -> str:
    return f"{UWP_PREFIX}{app_id}"


def _windowsapps_needle(exe_path: str) -> str | None:
    """
    If *exe_path* lives inside a WindowsApps folder (e.g. Slack, ChatGPT stored
    as Win32 paths in groups.json), return a version-independent substring needle
    built from the PackageFamilyName only.

    WindowsApps folder format:
      ...\\\\WindowsApps\\\\<PFN>_<version>_<arch>__<publisherId>\\\\...

    We strip everything from the first version-segment (starts with a digit)
    and use only the PFN, which never changes on update.

    Returns None if the path is not inside WindowsApps.
    """
    low = exe_path.lower()
    idx = low.find("\\windowsapps\\")
    if idx == -1:
        return None
    # Grab the folder name immediately after \\WindowsApps\\
    rest = low[idx + len("\\windowsapps\\"):]
    folder = rest.split("\\")[0]   # e.g. "com.tinyspeck.slackdesktop_4.48.100.0_x64__8yrtsj140pw4g"
    # PFN = segments before the first one that starts with a digit (that's the version)
    pfn_parts: list[str] = []
    for part in folder.split("_"):
        if part and part[0].isdigit():
            break
        pfn_parts.append(part)
    if not pfn_parts:
        return None
    return f"windowsapps\\{'_'.join(pfn_parts)}"


def _squirrel_parent(exe_path_norm: str) -> str | None:
    """
    Many Electron/Squirrel apps (SourceTree, Postman, Slack desktop installer,
    etc.) install a stub launcher at:
        AppData\\Local\\AppName\\AppName.exe
    but the real process runs from a versioned sub-directory:
        AppData\\Local\\AppName\\app-3.4.27\\AppName.exe

    This helper returns the *parent directory* of the stored exe (lower-case,
    normalised) so we can match any process running from that same parent,
    regardless of which 'app-X.Y.Z' sub-folder they live in.

    Returns None for system/program-files paths (we only apply this heuristic
    for %APPDATA% / %LOCALAPPDATA% style paths to avoid false positives).
    """
    lower = exe_path_norm.lower()
    appdata_markers = (
        "\\appdata\\local\\",
        "\\appdata\\roaming\\",
    )
    for marker in appdata_markers:
        if marker in lower:
            parent_dir = os.path.dirname(exe_path_norm)
            log.debug("Squirrel parent dir for %s → %s", exe_path_norm, parent_dir)
            return parent_dir
    return None


# ---------------------------------------------------------------------------
# Win32 process matching helpers
# ---------------------------------------------------------------------------

def _build_win32_match_sets(
    win32_ids: list[str],
) -> tuple[set[str], set[str], dict[str, str]]:
    """
    Build three match structures for the given Win32 identifiers:

    normalised:          exact normalised lower-case exe paths
    windowsapps_pfns:    version-independent PFN needles (WindowsApps only)
    squirrel_parents:    lower-case parent dirs (AppData paths only)
                         mapped back to the original stored path

    Returns (normalised, windowsapps_pfns, squirrel_parents).
    """
    normalised: set[str] = set()
    windowsapps_pfns: set[str] = set()
    squirrel_parents: dict[str, str] = {}  # parent_dir → stored_path

    for p in win32_ids:
        norm = os.path.normpath(p).lower()
        normalised.add(norm)

        wa_needle = _windowsapps_needle(norm)
        if wa_needle:
            windowsapps_pfns.add(wa_needle)

        sq_parent = _squirrel_parent(norm)
        if sq_parent:
            squirrel_parents[sq_parent] = p

    return normalised, windowsapps_pfns, squirrel_parents


def _match_win32_proc(
    proc_exe: str,
    normalised: set[str],
    windowsapps_pfns: set[str],
    squirrel_parents: dict[str, str],
) -> str | None:
    """
    Return the match reason string if proc_exe matches any of the stored
    Win32 identifiers, or None if it doesn't match.
    """
    norm = os.path.normpath(proc_exe).lower()

    if norm in normalised:
        return "exact"

    for needle in windowsapps_pfns:
        if needle in norm:
            return f"windowsapps-pfn({needle})"

    proc_parent = os.path.dirname(norm)
    for sq_parent, stored in squirrel_parents.items():
        if proc_parent.startswith(sq_parent):
            return f"squirrel-parent({sq_parent})"

    return None


# ---------------------------------------------------------------------------
# Single-instance detection
# ---------------------------------------------------------------------------

def is_running_win32(exe_path: str) -> bool:
    """True if a process matching this Win32 exe path is running.

    Uses exact-path, WindowsApps-PFN, and Squirrel-parent matching so that
    Squirrel-installed Electron apps (SourceTree, Postman…) and Store-hosted
    Win32 apps are correctly detected even after an auto-update.
    """
    norm = os.path.normpath(exe_path).lower()
    normalised = {norm}
    windowsapps_pfns: set[str] = set()
    squirrel_parents: dict[str, str] = {}

    wa = _windowsapps_needle(norm)
    if wa:
        windowsapps_pfns.add(wa)

    sq = _squirrel_parent(norm)
    if sq:
        squirrel_parents[sq] = exe_path

    for proc in psutil.process_iter(["exe"]):
        try:
            proc_exe = proc.info.get("exe") or ""
            if not proc_exe:
                continue
            reason = _match_win32_proc(proc_exe, normalised, windowsapps_pfns, squirrel_parents)
            if reason:
                log.debug("is_running_win32 HIT  [%s]  stored=%s  running=%s", reason, exe_path, proc_exe)
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


def is_running_uwp(app_id: str) -> bool:
    """
    True if a UWP process is running for this AppID.

    Detection: any process whose exe path contains the package-name needle
    (see _uwp_needle). UWP apps always live under
    C:\\Program Files\\WindowsApps\\<PackageName>_<Version>_<Arch>__<PublisherId>\\.
    """
    needle = _uwp_needle(app_id)
    log.debug("is_running_uwp: app_id=%s  needle=%s", app_id, needle)
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
    log.info("Launch Win32: %s", exe_path)
    if is_running_win32(exe_path):
        log.info("  → already running, skipping")
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
        log.info("  → launched OK")
        return LaunchResult(exe_path, "launched")
    except PermissionError:
        log.warning("  → PermissionError, trying elevated launch")
    except OSError as exc:
        if exc.winerror == _ERROR_ELEVATION_REQUIRED:
            log.warning("  → elevation required (740), trying runas")
        else:
            log.error("  → OSError: %s", exc)
            return LaunchResult(exe_path, "error", str(exc))
    except Exception as exc:
        log.error("  → unexpected error: %s", exc)
        return LaunchResult(exe_path, "error", str(exc))

    # Elevated launch (UAC)
    try:
        ret = _ShellExecuteW(None, "runas", exe_path, None, cwd, SW_SHOWNORMAL)
        if int(ret) > 32:
            log.info("  → launched elevated (runas)")
            return LaunchResult(exe_path, "elevated", "Launched with admin rights")
        log.error("  → ShellExecuteW runas returned %d", int(ret))
        return LaunchResult(exe_path, "error", f"ShellExecuteW returned {int(ret)}")
    except Exception as exc:
        log.error("  → runas exception: %s", exc)
        return LaunchResult(exe_path, "error", str(exc))


def _launch_uwp(identifier: str) -> LaunchResult:
    """
    Launch a UWP app via shell:AppsFolder.

    Uses ShellExecuteW to tell Windows to open the app's virtual shell folder
    entry — the same mechanism the Start Menu uses. Works for all Store apps
    regardless of whether they expose a traditional .exe.
    """
    app_id = app_id_from_identifier(identifier)
    log.info("Launch UWP: %s  (AppID=%s)", identifier, app_id)

    if is_running_uwp(app_id):
        log.info("  → already running, skipping")
        return LaunchResult(identifier, "already_running", "Already open")

    shell_target = f"shell:AppsFolder\\{app_id}"
    try:
        ret = _ShellExecuteW(
            None, "open", "explorer.exe", shell_target, None, SW_SHOWNORMAL,
        )
        if int(ret) > 32:
            log.info("  → UWP launched OK")
            return LaunchResult(identifier, "launched")
        log.error("  → ShellExecuteW returned %d for %s", int(ret), shell_target)
        return LaunchResult(identifier, "error", f"ShellExecuteW returned {int(ret)}")
    except Exception as exc:
        log.error("  → UWP launch exception: %s", exc)
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
    log.info("=== launch_group: %d identifiers ===", len(identifiers))
    for ident in identifiers:
        log.info("  • %s", ident)

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
# Termination helper
# ---------------------------------------------------------------------------


def _terminate_all(procs: list["psutil.Process"], timeout: int = 3) -> None:
    """
    Gracefully terminate a list of processes, then force-kill survivors.

    Errors (AccessDenied, NoSuchProcess) are handled per-process so that
    one protected process cannot block termination of the others.

    Strategy
    --------
    1. Send SIGTERM / WM_CLOSE to every process.
    2. Wait up to *timeout* seconds for them to exit.
    3. Force-kill any that are still alive.
    """
    if not procs:
        log.debug("_terminate_all: empty list, nothing to do")
        return

    log.info("_terminate_all: terminating %d process(es)", len(procs))
    for proc in procs:
        try:
            log.debug("  terminate PID %d (%s)", proc.pid, getattr(proc, 'name', lambda: '?')())
            proc.terminate()
        except psutil.NoSuchProcess:
            log.debug("  PID %d already gone (NoSuchProcess)", proc.pid)
        except psutil.AccessDenied:
            log.warning("  PID %d: AccessDenied on terminate() — process may be elevated", proc.pid)
        except psutil.ZombieProcess:
            log.debug("  PID %d is a zombie", proc.pid)
        except Exception as exc:
            log.error("  PID %d: unexpected error on terminate(): %s", proc.pid, exc)

    _gone, alive = psutil.wait_procs(procs, timeout=timeout)
    log.info("  → %d exited cleanly, %d still alive after %ds", len(_gone), len(alive), timeout)

    for proc in alive:
        try:
            log.warning("  force-killing PID %d (%s)", proc.pid, getattr(proc, 'name', lambda: '?')())
            proc.kill()
        except psutil.NoSuchProcess:
            log.debug("  PID %d already gone before kill()", proc.pid)
        except psutil.AccessDenied:
            log.error("  PID %d: AccessDenied on kill() — elevated process cannot be killed from user context", proc.pid)
        except Exception as exc:
            log.error("  PID %d: unexpected error on kill(): %s", proc.pid, exc)


# ---------------------------------------------------------------------------
# Group close  (public API — unchanged signature)
# ---------------------------------------------------------------------------

def close_group(identifiers: list[str]) -> list[str]:
    """
    Terminate all running processes for Win32 or UWP apps in the group.
    Returns list of identifiers that were actually terminated.

    Matching strategies used
    ------------------------
    Win32:
      1. Exact normalised path   — standard case
      2. WindowsApps PFN needle  — Windows Store Win32 apps that auto-update
                                   (stored path has stale version number)
      3. Squirrel parent-dir     — Electron apps installed with Squirrel
                                   (SourceTree, Postman, Slack desktop installer)
                                   that run from AppData\\Local\\App\\app-X.Y.Z\\App.exe
                                   but whose shortcut/scanner path is
                                   AppData\\Local\\App\\App.exe (the stub)
    UWP:
      WindowsApps PFN needle     — always correct for Store apps
    """
    log.info("=== close_group: %d identifiers ===", len(identifiers))
    for ident in identifiers:
        log.info("  • %s", ident)

    terminated: list[str] = []

    # Separate Win32 vs UWP
    win32_ids = [i for i in identifiers if not is_uwp_id(i)]
    uwp_ids   = [i for i in identifiers if is_uwp_id(i)]

    log.info("  → %d Win32, %d UWP", len(win32_ids), len(uwp_ids))

    # ── Win32 close ─────────────────────────────────────────────────────
    if win32_ids:
        normalised, windowsapps_pfns, squirrel_parents = _build_win32_match_sets(win32_ids)

        log.info("Win32 match sets:")
        log.info("  exact paths:        %s", sorted(normalised))
        log.info("  windowsapps pfns:   %s", sorted(windowsapps_pfns))
        log.info("  squirrel parents:   %s", sorted(squirrel_parents.keys()))

        to_kill: list[psutil.Process] = []

        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                proc_exe = proc.info.get("exe") or ""
                if not proc_exe:
                    continue
                reason = _match_win32_proc(proc_exe, normalised, windowsapps_pfns, squirrel_parents)
                if reason:
                    log.info(
                        "  MATCH [%s]  PID=%-6d  name=%-30s  exe=%s",
                        reason, proc.info["pid"], proc.info.get("name", "?"), proc_exe,
                    )
                    to_kill.append(proc)
                    terminated.append(proc_exe)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        if not to_kill:
            log.warning("Win32: 0 processes matched — nothing to kill")
            log.warning("  Stored paths were: %s", win32_ids)
        else:
            log.info("Win32: %d process(es) matched, terminating...", len(to_kill))
            _terminate_all(to_kill)

    # ── UWP close ───────────────────────────────────────────────────────
    for ident in uwp_ids:
        app_id = app_id_from_identifier(ident)
        needle = _uwp_needle(app_id)
        log.info("UWP close: %s  needle=%s", ident, needle)
        uwp_procs: list[psutil.Process] = []

        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                proc_exe = (proc.info.get("exe") or "").lower()
                if needle in proc_exe:
                    log.info(
                        "  MATCH [uwp-pkg]  PID=%-6d  name=%-30s  exe=%s",
                        proc.info["pid"], proc.info.get("name", "?"), proc.info.get("exe", ""),
                    )
                    uwp_procs.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        if not uwp_procs:
            log.warning("UWP: no processes found for needle=%s", needle)
        else:
            log.info("UWP: %d process(es) matched for %s", len(uwp_procs), ident)
            terminated.append(ident)
            _terminate_all(uwp_procs)

    log.info("=== close_group done: terminated=%s ===", terminated)
    return terminated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pids_for(identifiers: list[str]) -> list[int]:
    """Return PIDs for all running processes matching the given identifiers."""
    win32_ids_list = [i for i in identifiers if not is_uwp_id(i)]
    normalised, windowsapps_pfns, squirrel_parents = _build_win32_match_sets(win32_ids_list)

    uwp_needles = [
        _uwp_needle(app_id_from_identifier(i))
        for i in identifiers if is_uwp_id(i)
    ]
    pids = []
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            proc_exe = proc.info.get("exe") or ""
            if not proc_exe:
                continue
            norm = os.path.normpath(proc_exe).lower()
            reason = _match_win32_proc(proc_exe, normalised, windowsapps_pfns, squirrel_parents)
            if reason:
                pids.append(proc.info["pid"])
            elif any(n in norm for n in uwp_needles):
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids


def running_exes(identifiers: list[str]) -> set[str]:
    """Return the subset of identifiers that have a running process."""
    active: set[str] = set()

    win32_ids_list = [i for i in identifiers if not is_uwp_id(i)]
    normalised, windowsapps_pfns, squirrel_parents = _build_win32_match_sets(win32_ids_list)

    # Build UWP needle → identifier map
    uwp_map = {
        _uwp_needle(app_id_from_identifier(i)): i
        for i in identifiers if is_uwp_id(i)
    }

    # Build reverse: normalised_path → stored identifier (for exact matches)
    win32_norm_to_ident = {
        os.path.normpath(i).lower(): i
        for i in win32_ids_list
    }

    for proc in psutil.process_iter(["exe"]):
        try:
            proc_exe = proc.info.get("exe") or ""
            if not proc_exe:
                continue
            norm = os.path.normpath(proc_exe).lower()

            # Win32 matching
            reason = _match_win32_proc(proc_exe, normalised, windowsapps_pfns, squirrel_parents)
            if reason:
                # Find which stored identifier this corresponds to
                # Exact match: use reverse map
                if norm in win32_norm_to_ident:
                    active.add(win32_norm_to_ident[norm])
                else:
                    # WindowsApps / Squirrel: find the stored ident by checking
                    # which stored identifier matches this running exe
                    for stored_ident in win32_ids_list:
                        stored_norm = os.path.normpath(stored_ident).lower()
                        r = _match_win32_proc(
                            proc_exe,
                            {stored_norm},
                            {_windowsapps_needle(stored_norm)} - {None},
                            {_squirrel_parent(stored_norm): stored_ident}
                            if _squirrel_parent(stored_norm) else {},
                        )
                        if r:
                            active.add(stored_ident)
                            break

            # UWP matching
            for needle, ident in uwp_map.items():
                if needle in norm:
                    active.add(ident)

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return active
