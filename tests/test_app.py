import os
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication

from tenon_ui import app as tray_app
from tenon_ui.state import Health


def test_tray_builds_and_menu_renders():
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    deadline = time.monotonic() + 15
    while tray.view is None and time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(0.02)
    assert tray.view is not None
    tray.on_menu_about_to_show()
    texts = [a.text() for a in tray.menu.actions()]
    assert "Quit" in texts


class FakeRun:
    def __init__(self, returncode):
        self.returncode = returncode


def test_quit_boots_out_the_loaded_agent_on_macos(monkeypatch):
    popen, quit_calls = [], []
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    monkeypatch.setattr(tray_app.subprocess, "run", lambda *a, **k: FakeRun(0))
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda argv, **k: popen.append((argv, k)))
    monkeypatch.setattr(tray_app.QApplication, "quit", lambda: quit_calls.append(1))
    tray_app.quit_app()
    assert popen == [(["launchctl", "bootout", f"gui/{os.getuid()}/com.tenon.ui"], {"start_new_session": True})]
    assert not quit_calls


def test_quit_is_plain_when_agent_not_loaded_or_not_macos(monkeypatch):
    popen, quit_calls = [], []
    monkeypatch.setattr(tray_app.subprocess, "Popen", lambda *a, **k: popen.append(a))
    monkeypatch.setattr(tray_app.QApplication, "quit", lambda: quit_calls.append(1))
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    monkeypatch.setattr(tray_app.subprocess, "run", lambda *a, **k: FakeRun(113))
    tray_app.quit_app()
    monkeypatch.setattr(tray_app.sys, "platform", "linux")
    tray_app.quit_app()
    assert quit_calls == [1, 1] and not popen


def test_icons_render_for_every_health():
    QApplication.instance() or QApplication([])
    for health in Health:
        assert not tray_app.make_icon(health, 42).isNull()
