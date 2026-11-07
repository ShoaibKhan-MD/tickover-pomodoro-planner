"""Desktop launcher for the packaged Tickover application."""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent

if sys.platform == "darwin":
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Tickover" / "data"
elif sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Tickover" / "data"
else:
    DATA_DIR = Path.home() / ".tickover" / "data"
# Renamed from "Focus Block": move an existing data folder across so nothing is lost.
_legacy = DATA_DIR.parent.with_name("Focus Block" if sys.platform in ("darwin", "win32") else ".focus-block")
if _legacy.is_dir() and not DATA_DIR.parent.exists():
    try:
        _legacy.rename(DATA_DIR.parent)
    except OSError:
        DATA_DIR = _legacy / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# A windowed build has no console, so stdout and stderr are None. Send them to a log file;
# otherwise uvicorn's logging setup fails when the app starts.
if sys.stdout is None or sys.stderr is None:
    _log = open(DATA_DIR.parent / "tickover.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

if not os.environ.get("DATA_DIR"):
    os.environ["DATA_DIR"] = str(DATA_DIR)
# Desktop builds must stay loopback-only even if the parent shell exports HOST.
os.environ["HOST"] = "127.0.0.1"
if not os.environ.get("PORT"):
    os.environ["PORT"] = "8765"
os.environ["TICKOVER_DESKTOP"] = "1"
# Do not force UTC: app.py loads the saved browser timezone on startup.

sys.path.insert(0, str(BASE_DIR))
import uvicorn  # noqa: E402
from app import app, db, get_settings  # noqa: E402

HOST = os.environ["HOST"]
PORT = int(os.environ["PORT"])
TRAY_POLL_SECONDS = 2


def _open_browser() -> None:
    time.sleep(1.5)
    webbrowser.open(f"http://{HOST}:{PORT}")


def _tray_wanted() -> bool:
    try:
        with db() as conn:
            return bool(get_settings(conn).get("tray_icon", True))
    except Exception:
        return True


def _tray_image(size: int):
    from PIL import Image

    return Image.open(BASE_DIR / "static" / "icons" / "tray.png").convert("RGBA").resize((size, size), Image.LANCZOS)


def _run_tray(server, server_thread, pystray_module=None) -> bool:
    """Menu-bar (macOS) or system-tray (Windows) icon.

    It shows that Tickover is running and lets you reopen or quit it. Settings can hide it.
    Returns False when no tray icon can be shown; the app then simply runs without one.
    """
    try:
        pystray = pystray_module or __import__("pystray")
        image = _tray_image(44 if sys.platform == "darwin" else 64)
    except Exception as exc:
        print(f"Tray icon unavailable: {exc}", file=sys.stderr)
        return False
    url = f"http://{HOST}:{PORT}"

    def open_app(icon=None, item=None):
        webbrowser.open(url)

    def quit_app(icon, item=None):
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(f"Tickover is running ({HOST}:{PORT})", None, enabled=False),
        pystray.MenuItem("Open Tickover", open_app, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Tickover", quit_app),
    )
    icon = pystray.Icon("Tickover", image, "Tickover", menu)

    def watch(icon):  # runs once the icon is ready: follows the Settings toggle until the server stops
        shown = None
        while server_thread.is_alive():
            want = _tray_wanted()
            if want != shown:
                icon.visible = want
                shown = want
            time.sleep(TRAY_POLL_SECONDS)
        icon.stop()

    try:
        icon.run(setup=watch)
    except Exception as exc:
        print(f"Tray icon stopped: {exc}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", access_log=False))
    app.state.uvicorn_server = server
    threading.Thread(target=_open_browser, daemon=True).start()
    if sys.platform in ("darwin", "win32") and not os.environ.get("TICKOVER_NO_TRAY"):
        server_thread = threading.Thread(target=server.run, name="tickover-server")
        server_thread.start()
        _run_tray(server, server_thread)  # blocks until the icon stops; the menu bar needs the main thread
        server.should_exit = True
        server_thread.join()  # keeps the app alive if no tray icon could be shown
    else:
        server.run()
