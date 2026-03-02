"""
groups.py – Load and save application groups to/from groups.json.

Schema:
{
    "GroupName": ["C:\\path\\to\\app1.exe", "C:\\path\\to\\app2.exe"],
    ...
}
"""

import json
import os
import sys
from pathlib import Path


def _app_dir() -> Path:
    """
    Return the directory where persistent data (groups.json) should live.

    - Frozen exe (PyInstaller --onefile): next to the .exe
    - Normal Python run: project root (parent of core/)
    """
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(sys.executable))
    return Path(__file__).resolve().parent.parent


GROUPS_FILE = _app_dir() / "groups.json"


def _load_raw() -> dict[str, list[str]]:
    if GROUPS_FILE.exists():
        try:
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_raw(data: dict[str, list[str]]) -> None:
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_groups() -> list[str]:
    """Return sorted list of group names."""
    return sorted(_load_raw().keys())


def get_group(name: str) -> list[str]:
    """Return exe paths for a group (empty list if not found)."""
    return _load_raw().get(name, [])


def create_group(name: str) -> bool:
    """Create a new empty group. Returns False if name already exists."""
    name = name.strip()
    if not name:
        return False
    data = _load_raw()
    if name in data:
        return False
    data[name] = []
    _save_raw(data)
    return True


def rename_group(old_name: str, new_name: str) -> bool:
    """Rename a group. Returns False if old doesn't exist or new already does."""
    new_name = new_name.strip()
    data = _load_raw()
    if old_name not in data or new_name in data or not new_name:
        return False
    data[new_name] = data.pop(old_name)
    _save_raw(data)
    return True


def delete_group(name: str) -> bool:
    """Delete a group. Returns False if not found."""
    data = _load_raw()
    if name not in data:
        return False
    del data[name]
    _save_raw(data)
    return True


def set_group_apps(name: str, exe_paths: list[str]) -> None:
    """Overwrite the exe list for a group (creates it if absent)."""
    data = _load_raw()
    data[name] = list(exe_paths)
    _save_raw(data)
