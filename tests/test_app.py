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


def test_start_apply_asks_for_a_password_only_on_linux_without_passwordless_sudo(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    detached, asked = [], []
    monkeypatch.setattr(tray, "start_run", lambda args: detached.append(args))
    monkeypatch.setattr(tray, "run_with_password", lambda args: asked.append(args))
    monkeypatch.setattr(tray_app, "sudo_is_passwordless", lambda: False)
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    tray.start_apply()
    assert len(detached) == 1 and not asked
    monkeypatch.setattr(tray_app.sys, "platform", "linux")
    tray.start_apply()
    assert asked == [["apply"]]
    monkeypatch.setattr(tray_app, "sudo_is_passwordless", lambda: True)
    tray.start_apply()
    assert len(detached) == 2


def test_upgrade_needs_confirmation_then_password(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    asked = []
    monkeypatch.setattr(tray, "run_with_password", lambda args: asked.append(args))
    monkeypatch.setattr(tray_app, "sudo_is_passwordless", lambda: False)
    yes, no = tray_app.QMessageBox.StandardButton.Yes, tray_app.QMessageBox.StandardButton.No
    monkeypatch.setattr(tray_app.QMessageBox, "question", staticmethod(lambda *a: no))
    tray.start_upgrade()
    assert not asked
    monkeypatch.setattr(tray_app.QMessageBox, "question", staticmethod(lambda *a: yes))
    tray.start_upgrade()
    assert asked == [["upgrade", "--yes"]]


class Process:
    def __init__(self, err=""):
        self.err = err.encode()

    def readAllStandardError(self):
        return self.err


def test_wrong_password_offers_retry_once_per_user_choice(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    asked = []
    monkeypatch.setattr(tray, "run_with_password", lambda args: asked.append(args))
    retry, cancel = tray_app.QMessageBox.StandardButton.Retry, tray_app.QMessageBox.StandardButton.Cancel
    monkeypatch.setattr(tray_app.QMessageBox, "exec", lambda self: retry)
    tray.on_privileged_finished(["upgrade", "--yes"], 0, Process())
    assert not asked
    tray.on_privileged_finished(["upgrade", "--yes"], 65, Process())
    assert asked == [["upgrade", "--yes"]]
    monkeypatch.setattr(tray_app.QMessageBox, "exec", lambda self: cancel)
    tray.on_privileged_finished(["upgrade", "--yes"], 65, Process())
    assert len(asked) == 1


def test_privileged_failure_shows_stderr_tail(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    shown = []
    monkeypatch.setattr(tray_app.QMessageBox, "critical", staticmethod(lambda *a: shown.append(a[2])))
    tray.on_privileged_finished(["apply"], 1, Process("a\nboom\n"))
    tray.on_privileged_finished(["apply"], 2, Process(""))
    assert "boom" in shown[0] and shown[1] == "tenon exited with 2"


CAPS = ('{"capabilities":[{"id":"b","desc":"B tool","layer":"public","selected":false,"active":false},'
        '{"id":"a","desc":"A tool","layer":"public","selected":true,"active":true}]}')


def test_settings_window_lists_filters_and_diffs_the_selection():
    qt = QApplication.instance() or QApplication([])
    window = tray_app.SettingsWindow(lambda: None)
    window.show_capabilities(tray_app.parse_capabilities(CAPS))
    assert [window.tree.topLevelItem(i).text(0) for i in range(2)] == ["a", "b"]
    assert window.tree.topLevelItem(0).text(3) == "active"
    window.tree.topLevelItem(1).setCheckState(0, tray_app.Qt.CheckState.Checked)
    window.tree.topLevelItem(0).setCheckState(0, tray_app.Qt.CheckState.Unchecked)
    assert tray_app.selection_commands(window.selected, window.wanted()) == [
        ["capabilities", "select", "b"], ["capabilities", "deselect", "a"]]
    window.filter.setText("A tool")
    assert not window.tree.topLevelItem(0).isHidden() and window.tree.topLevelItem(1).isHidden()
    qt.processEvents()


def test_menu_shows_schedule_and_capabilities(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    monkeypatch.setattr(tray, "refresh", lambda: None)
    tray.view = tray_app.load_view({"schedule": {"upgrade": "11:00", "check": "12:00"}})
    tray.on_menu_about_to_show()
    texts = [a.text() for a in tray.menu.actions()]
    assert "Next: upgrade 11:00 \u00b7 check 12:00" in texts and "Capabilities\u2026" in texts
    assert "Apply now" in texts


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


def test_restart_quits_on_macos_so_launchd_relaunches(monkeypatch):
    quits = []
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    monkeypatch.setattr(tray_app.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    monkeypatch.setattr(tray_app.os, "execv", lambda *a: 1 / 0)
    tray_app.restart()
    assert quits == [1]


def test_restart_re_execs_the_interpreter_with_the_same_argv(monkeypatch):
    monkeypatch.setattr(tray_app.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(tray_app.os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(tray_app.sys, "argv", ["tenon-ui"])
    tray_app.restart()
    assert calls == [(tray_app.sys.executable, [tray_app.sys.executable, "tenon-ui"])]


def test_icons_render_for_every_health():
    QApplication.instance() or QApplication([])
    for health in Health:
        spec = tray_app.icon_spec(health, True, False)
        assert not tray_app.make_icon(health, 42, spec, 2.0).isNull()


def test_icon_spec_is_template_on_macos_and_follows_the_scheme_on_linux():
    for health in Health:
        mac = tray_app.icon_spec(health, True, False)
        assert mac.mask == (health is not Health.FAILED)
        assert not tray_app.icon_spec(health, False, True).mask
    assert tray_app.icon_spec(Health.FAILED, True, True).color == tray_app.FAILED_COLOR
    assert tray_app.icon_spec(Health.OK, False, True).color == "#fcfcfc"
    assert tray_app.icon_spec(Health.OK, False, False).color == "#232629"


def test_log_window_streams_appends_and_switches_to_a_new_run(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    monkeypatch.setattr(tray_app, "LOG_DIR", tmp_path)
    (tmp_path / "20260101-000000-apply.log").write_text("one\n")
    window = tray_app.LogWindow()
    window.refresh()
    assert window.toPlainText() == "one\n"
    with (tmp_path / "20260101-000000-apply.log").open("a") as f:
        f.write("two\n")
    window.refresh()
    assert window.toPlainText() == "one\ntwo\n"
    (tmp_path / "20260102-000000-check.log").write_text("new\n")
    window.refresh()
    assert window.toPlainText() == "new\n"
    qt.processEvents()


def running_report(**fields):
    return {"schema_version": 1, "current_run": {"mode": "apply", "phase": "running", "total": 10, "n": 2, **fields}}


def test_poll_timer_runs_only_while_a_run_is_in_progress(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    monkeypatch.setattr(tray, "refresh", lambda: None)
    tray.apply_report(running_report())
    assert tray.poll.isActive() and tray.health is Health.RUNNING
    tray.apply_report(running_report(phase="stalled"))
    assert tray.poll.isActive() and tray.health is Health.STALLED
    tray.apply_report({"schema_version": 1, "current_run": None})
    assert not tray.poll.isActive()


def test_run_finished_notifies_from_running_or_stalled(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    monkeypatch.setattr(tray, "refresh", lambda: None)
    messages = []
    monkeypatch.setattr(tray.tray, "showMessage", lambda *a: messages.append(a))
    for phase in ("running", "stalled"):
        tray.apply_report(running_report(phase=phase))
        tray.apply_report({"schema_version": 1, "current_run": None})
    assert len(messages) == 2


def test_poll_tick_spins_indeterminate_and_asks_status_every_few_ticks(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    calls = []
    monkeypatch.setattr(tray, "refresh", lambda: calls.append(1))
    tray.apply_report(running_report(phase="launching"))
    for _ in range(tray_app.STATUS_EVERY_TICKS):
        tray.on_poll()
    assert tray.spin == tray_app.SPIN_DEGREES * tray_app.STATUS_EVERY_TICKS and calls == [1]
    tray.apply_report({"current_run": None})


def test_exit_75_says_a_run_is_in_progress_and_other_codes_show_stderr(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    monkeypatch.setattr(tray, "refresh", lambda: None)
    shown = []
    monkeypatch.setattr(tray_app.QMessageBox, "information", staticmethod(lambda *a: shown.append(a[2])))
    monkeypatch.setattr(tray_app.QMessageBox, "critical", staticmethod(lambda *a: shown.append(a[2])))

    class Process:
        def readAllStandardError(self):
            return b"a\nb\nboom"

    tray.on_run_finished(0, Process())
    tray.on_run_finished(75, Process())
    tray.on_run_finished(1, Process())
    assert shown == ["A run is already in progress", "a\nb\nboom"]


def test_quit_asks_only_while_our_run_is_active(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    quits, answers = [], []
    monkeypatch.setattr(tray_app, "quit_app", lambda: quits.append(1))
    monkeypatch.setattr(tray_app.QMessageBox, "question", staticmethod(lambda *a: answers.pop(0)))

    class Runner:
        def state(self):
            return tray_app.QProcess.ProcessState.Running

    tray.request_quit()
    assert quits == [1]
    tray.runner = Runner()
    answers[:] = [tray_app.QMessageBox.StandardButton.No, tray_app.QMessageBox.StandardButton.Yes]
    tray.request_quit()
    assert quits == [1]
    tray.request_quit()
    assert quits == [1, 1]


def test_log_window_follows_the_current_run_log(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(tray_app, "LOG_DIR", tmp_path)
    (tmp_path / "20260102-000000-check.log").write_text("newest\n")
    running = tmp_path / "20260101-000000-apply.log"
    running.write_text("mine\n")
    window = tray_app.LogWindow()
    window.current = running
    window.refresh()
    assert window.toPlainText() == "mine\n"

