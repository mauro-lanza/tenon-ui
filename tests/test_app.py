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


def test_apply_needs_terminal_only_on_linux_without_passwordless_sudo(monkeypatch):
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    assert not tray_app.apply_needs_terminal()
    monkeypatch.setattr(tray_app.sys, "platform", "linux")
    monkeypatch.setattr(tray_app.subprocess, "run", lambda *a, **k: FakeRun(1))
    assert tray_app.apply_needs_terminal()
    monkeypatch.setattr(tray_app.subprocess, "run", lambda *a, **k: FakeRun(0))
    assert not tray_app.apply_needs_terminal()


def test_menu_without_view_shows_loading_and_apply_is_disabled_when_terminal_needed(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    monkeypatch.setattr(tray, "refresh", lambda: None)
    tray.view = None
    tray.on_menu_about_to_show()
    assert [a.text() for a in tray.menu.actions()] == ["Loading\u2026"]
    tray.view = tray_app.load_view(None)
    monkeypatch.setattr(tray_app, "apply_needs_terminal", lambda: True)
    tray.on_menu_about_to_show()
    apply = [a for a in tray.menu.actions() if a.text().startswith("Apply now")]
    assert len(apply) == 1 and not apply[0].isEnabled() and "terminal" in apply[0].text()


def test_fallback_timer_re_adds_watches(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    calls = []
    monkeypatch.setattr(tray, "watch_state", lambda: calls.append("watch"))
    monkeypatch.setattr(tray, "refresh", lambda: calls.append("refresh"))
    tray.on_fallback()
    assert calls == ["watch", "refresh"]


def test_install_signature_changes_when_a_source_file_is_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(tray_app, "PACKAGE_DIR", tmp_path)
    (tmp_path / "app.py").write_text("a")
    before = tray_app.install_signature()
    assert tray_app.install_signature() == before
    (tmp_path / "app.py").write_text("bb")
    assert tray_app.install_signature() != before


def report_finished(finished):
    return {"last_run": {"finished": finished, "mode": "check", "result": "ok"}}


def test_each_completed_run_checks_the_install(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(tray_app, "PACKAGE_DIR", tmp_path)
    (tmp_path / "app.py").write_text("a")
    tray = tray_app.TrayApp(QApplication.instance())
    tray.seen_report, tray.last_finished = False, None
    restarts = []
    monkeypatch.setattr(tray_app, "restart", lambda: restarts.append(1))
    (tmp_path / "app.py").write_text("bb")
    tray.apply_report(report_finished("t1"))  # baseline only
    tray.apply_report(report_finished("t1"))  # same run
    tray.apply_report(None)  # failed status call
    assert not restarts
    tray.apply_report(report_finished("t2"))  # new run, install changed
    assert restarts == [1]
    restarts.clear()
    tray.installed = tray_app.install_signature()
    tray.apply_report(report_finished("t3"))  # new run, install unchanged
    assert not restarts


def test_restart_re_execs_the_interpreter_with_the_same_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(tray_app.os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(tray_app.sys, "argv", ["tenon-ui"])
    tray_app.restart()
    assert calls == [(tray_app.sys.executable, [tray_app.sys.executable, "tenon-ui"])]


def test_icons_render_for_every_health():
    QApplication.instance() or QApplication([])
    for health in Health:
        assert not tray_app.make_icon(health, 42).isNull()
