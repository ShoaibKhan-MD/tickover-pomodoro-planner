"""Checks the desktop launcher in separate processes so its start-up side effects stay isolated."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(tmp_path, **extra):
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), APPDATA=str(tmp_path), PYTHONPATH=str(ROOT))
    for key in ("TICKOVER_DESKTOP", "DATA_DIR", "PORT"):
        env.pop(key, None)
    env.update(extra)
    return env


def test_windowed_build_without_console_streams_still_starts(tmp_path):
    marker = tmp_path / "ok.txt"
    code = (
        "import sys; sys.stdout = None; sys.stderr = None\n"
        "import launcher, uvicorn\n"
        "uvicorn.Config(launcher.app, log_level='warning')\n"
        f"open({str(marker)!r}, 'w').write('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "ok"


def test_tray_icon_follows_settings_and_quit_stops_the_app(tmp_path):
    code = r"""
import threading, time
import launcher

class Item:
    def __init__(self, text, action=None, enabled=True, default=False):
        self.text, self.action, self.enabled, self.default = text, action, enabled, default

class Menu:
    SEPARATOR = object()
    def __init__(self, *items): self.items = items

class Icon:
    def __init__(self, name, image, title, menu):
        self.menu, self.log, self._visible, self.stopped = menu, [], None, threading.Event()
        fake.icon = self
    @property
    def visible(self): return self._visible
    @visible.setter
    def visible(self, value): self._visible = value; self.log.append(value)
    def run(self, setup):
        threading.Thread(target=setup, args=(self,), daemon=True).start()
        self.stopped.wait(10)
    def stop(self): self.stopped.set()

class Fake: Menu, MenuItem, Icon = Menu, Item, Icon
fake = Fake()

class Server: should_exit = False

launcher.TRAY_POLL_SECONDS = 0.05
answers = iter([True, True, False, False, True])
launcher._tray_wanted = lambda: next(answers, True)
thread = threading.Thread(target=lambda: time.sleep(10), daemon=True); thread.start()

def press_quit():
    time.sleep(0.7)
    item = [i for i in fake.icon.menu.items if getattr(i, "text", "") == "Quit Tickover"][0]
    item.action(fake.icon, item)
threading.Thread(target=press_quit, daemon=True).start()

assert launcher._run_tray(Server, thread, pystray_module=fake) is True
assert Server.should_exit is True
assert fake.icon.log == [True, False, True], fake.icon.log
texts = [getattr(i, "text", None) for i in fake.icon.menu.items]
assert texts[0].startswith("Tickover is running") and "Open Tickover" in texts and "Quit Tickover" in texts, texts
print("tray ok")
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "tray ok" in result.stdout, result.stdout + result.stderr


def test_launcher_serves_the_app_and_quit_endpoint_stops_it(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "launcher.py"], cwd=ROOT, env=_env(tmp_path, PORT=str(port), TICKOVER_NO_TRAY="1"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        health = None
        for _ in range(60):
            try:
                health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1))
                break
            except Exception:
                time.sleep(0.5)
        assert health and health["desktop_mode"] is True
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/desktop/quit", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=5)
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
