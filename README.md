# FlowLauncher

A dark-mode desktop application for Windows that lets you **group installed applications** and **launch or close them all at once** with a single click.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🔍 Start Menu scan | Auto-discovers all installed apps from Windows Start Menu |
| 🖼️ Icon extraction | Shows each app's native icon (via pywin32) |
| 📂 Group management | Create, rename, delete named groups |
| ▶ Launch Group | Opens all apps in the group simultaneously (non-blocking) |
| ■ Close Group | Terminates all running processes in the group |
| 💾 Persistent | Groups saved to `groups.json` |
| 🌑 Dark mode | Fully dark themed PyQt6 interface |

---

## 🛠️ Requirements

- **Windows 10 / 11**
- **Python 3.10+**

---

## ⚙️ Installation

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/program-group-manager.git
cd program-group-manager

# 2. (Optional) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

> **Dependencies**: `PyQt6`, `psutil`, `pywin32`

---

## 🚀 Running the App

```bash
python main.py
```

---

## 📁 Project Structure

```
program-group-manager/
├── main.py              # Entry point
├── requirements.txt
├── groups.json          # Auto-created; stores your groups
├── core/
│   ├── scanner.py       # Start Menu scan + icon extraction
│   ├── groups.py        # JSON persistence helpers
│   └── launcher.py      # subprocess launch + psutil kill
└── ui/
    ├── app_card.py      # App tile widget (opacity toggle)
    ├── app_gallery.py   # Scrollable grid of tiles
    ├── group_dialog.py  # "New / Rename Group" dialog
    └── main_window.py   # Main window
```

---

## 📖 How to Use

1. **Wait** for the Start Menu scan to finish (status bar shows progress).
2. **Create a group**: click **＋ New**, enter a name (e.g. "Work").
3. **Select apps**: click tiles to toggle them in/out of the group (bright = selected).
4. **Save**: click **💾 Save Group**.
5. **Launch**: click **▶ Launch Group** to open all apps at once.
6. **Close**: click **■ Close Group** to terminate all running apps in the group.

---

## 📄 License

MIT – Do what you want with it.
