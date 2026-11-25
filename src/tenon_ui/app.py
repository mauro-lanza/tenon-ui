"""The tray icon: shows tenon's state and starts check/apply runs."""

import ctypes
import ctypes.util
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QProcess, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from tenon_ui.state import CLI, STATE_DIR, Health, View, load_view, parse_report

AGENT_LABEL = "com.tenon.ui"
DEBOUNCE_MS = 500
FALLBACK_MS = 60_000
PACKAGE_DIR = Path(__file__).parent
STATUS_TIMEOUT_MS = 10_000
UPGRADE_COMMAND = Path.home() / ".local/share/tenon/actions/upgrade.command"
ICON_PX = 64
COLORS = {
    Health.UNKNOWN: "#8e8e93", Health.RUNNING: "#0a84ff", Health.OK: "#30d158",
    Health.FAILED: "#ff453a", Health.DRIFT: "#ff9f0a", Health.STALE: "#ff9f0a",
}
GLYPHS = {Health.UNKNOWN: "?", Health.FAILED: "!", Health.DRIFT: "~", Health.STALE: "z"}


def make_icon(health: Health, percent: int | None) -> QIcon:
    pixmap = QPixmap(ICON_PX, ICON_PX)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(COLORS[health]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, ICON_PX - 8, ICON_PX - 8)
    text = f"{percent}" if health is Health.RUNNING and percent is not None else GLYPHS.get(health, "")
    if health is Health.OK:
        text = "\u2713"
    if health is Health.RUNNING and percent is None:
        text = "\u21bb"
    painter.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(30 if len(text) < 3 else 24)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return QIcon(pixmap)


def hide_dock_icon() -> None:
    """Accessory activation policy through the Objective-C runtime; no pyobjc needed."""
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.restype = ctypes.c_void_p
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
    send_set = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(
        ("objc_msgSend", objc))
    ns_app = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
    send_set(ns_app, objc.sel_registerName(b"setActivationPolicy:"), 1)  # NSApplicationActivationPolicyAccessory


def open_file(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def apply_needs_terminal() -> bool:
    """True where a detached `tenon apply` would silently skip its root steps (Linux without passwordless sudo)."""
    if sys.platform == "darwin":
        return False
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0


def install_signature() -> tuple:
    """Name, mtime and size of every installed source file; a reinstall rewrites them."""
    return tuple((p.name, s.st_mtime_ns, s.st_size) for p in sorted(PACKAGE_DIR.glob("*.py")) for s in [p.stat()])


def restart() -> None:
    """Re-exec onto the new code; works with or without a supervisor."""
    os.execv(sys.executable, [sys.executable, *sys.argv])


def quit_app() -> None:
    """Quit; on macOS also unload the KeepAlive agent so launchd does not relaunch us."""
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}/{AGENT_LABEL}"
        loaded = subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
        if loaded:
            # Detached: bootout terminates this process.
            subprocess.Popen(["launchctl", "bootout", target], start_new_session=True)
            return
    QApplication.quit()


class TrayApp:
    def __init__(self, app: QApplication) -> None:
        self.view: View | None = None
        self.tray = QSystemTrayIcon(make_icon(Health.UNKNOWN, None))
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.on_menu_about_to_show)
        self.tray.setContextMenu(self.menu)

        self.status = QProcess()
        self.status.finished.connect(self.on_status_finished)
        self.status.errorOccurred.connect(self.on_status_error)
        self.status_timeout = QTimer(singleShot=True, interval=STATUS_TIMEOUT_MS)
        self.status_timeout.timeout.connect(self.status.kill)

        self.debounce = QTimer(singleShot=True, interval=DEBOUNCE_MS)
        self.debounce.timeout.connect(self.refresh)
        self.fallback = QTimer(interval=FALLBACK_MS)
        self.fallback.timeout.connect(self.on_fallback)
        self.watcher = QFileSystemWatcher()
        self.watcher.directoryChanged.connect(self.on_state_changed)
        self.watcher.fileChanged.connect(self.on_state_changed)
        self.watch_state()

        self.installed = install_signature()
        self.seen_report = False
        self.last_finished: str | None = None

        self.refresh()
        self.fallback.start()
        self.tray.show()
        app.aboutToQuit.connect(self.tray.hide)

    def watch_state(self) -> None:
        # Files replaced atomically drop their watch, so re-add on every change.
        wanted = [STATE_DIR, *STATE_DIR.glob("*.json*")]
        self.watcher.addPaths([str(p) for p in wanted if str(p) not in self.watcher.files() + self.watcher.directories()])

    def on_fallback(self) -> None:
        self.watch_state()  # the state dir may not have existed at start
        self.refresh()

    def on_state_changed(self, _path: str) -> None:
        self.watch_state()
        self.debounce.start()

    def refresh(self) -> None:
        if self.status.state() != QProcess.ProcessState.NotRunning:
            return
        self.status_timeout.start()
        self.status.start(str(CLI), ["status", "--json"])

    def on_status_error(self, error: QProcess.ProcessError) -> None:
        if error is QProcess.ProcessError.FailedToStart:
            self.status_timeout.stop()
            self.apply_report(None)

    def on_status_finished(self, code: int, exit_status: QProcess.ExitStatus) -> None:
        self.status_timeout.stop()
        ok = code == 0 and exit_status is QProcess.ExitStatus.NormalExit
        text = bytes(self.status.readAllStandardOutput()).decode(errors="replace")
        self.apply_report(parse_report(text) if ok else None)

    def apply_report(self, report: dict | None) -> None:
        previous, self.view = self.view, load_view(report)
        view = self.view
        finished = ((report or {}).get("last_run") or {}).get("finished")
        run_completed = report is not None and self.seen_report and finished != self.last_finished
        if report is not None:
            self.seen_report, self.last_finished = True, finished
        self.tray.setIcon(make_icon(view.health, view.percent))
        self.tray.setToolTip(f"tenon: {view.headline}")
        if previous and previous.health is Health.RUNNING and view.health is not Health.RUNNING:
            failed = view.health is Health.FAILED
            self.tray.showMessage("tenon", view.last_run or view.headline,
                                  QSystemTrayIcon.MessageIcon.Critical if failed
                                  else QSystemTrayIcon.MessageIcon.Information)
        if run_completed and install_signature() != self.installed:
            self.tray.hide()
            restart()

    def on_menu_about_to_show(self) -> None:
        self.refresh()
        view = self.view
        menu = self.menu
        menu.clear()
        if view is None:
            menu.addAction("Loading\u2026").setEnabled(False)
            return

        def label(text: str) -> None:
            if text:
                menu.addAction(text).setEnabled(False)

        label(view.headline)
        label(view.detail)
        menu.addSeparator()
        label(view.last_run)
        if view.partial:
            label("Partial run: only part of the machine was evaluated")
        for item in view.failed:
            label(f"  failed: {item}")
        for item in view.changed:
            label(f"  {item}")
        if view.changed_more:
            label(f"  \u2026 and {view.changed_more} more")
        label(view.sync)
        label(view.managed)
        if view.orphans:
            orphans = menu.addMenu("No longer managed, still on disk")
            for path in view.orphans:
                orphans.addAction(path).setEnabled(False)
            orphans.addSeparator()
            orphans.addAction("Nothing is deleted automatically").setEnabled(False)
        menu.addSeparator()
        idle = view.health is not Health.RUNNING
        self.add_run(menu, "Check for drift", ["check"], idle)
        if apply_needs_terminal():
            menu.addAction("Apply now: run `tenon apply` in a terminal").setEnabled(False)
        else:
            self.add_run(menu, "Apply now", ["apply"], idle)
        if sys.platform == "darwin":
            upgrade = QAction("Upgrade packages\u2026", menu)
            upgrade.setEnabled(idle)
            upgrade.triggered.connect(lambda: QProcess.startDetached("/usr/bin/open", [str(UPGRADE_COMMAND)]))
            menu.addAction(upgrade)
        if view.log:
            log = view.log
            open_log = QAction("Open log", menu)
            open_log.triggered.connect(lambda: open_file(log))
            menu.addAction(open_log)
        if view.runs:
            recent = menu.addMenu("Recent runs")
            for row in view.runs:
                item = QAction(row.text, recent)
                item.setEnabled(row.log is not None)
                if row.log:
                    item.triggered.connect(lambda _=False, path=row.log: open_file(path))
                recent.addAction(item)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(quit_app)
        menu.addAction(quit_action)

    @staticmethod
    def add_run(menu: QMenu, text: str, args: list[str], enabled: bool) -> None:
        action = QAction(text, menu)
        action.setEnabled(enabled)
        action.triggered.connect(lambda: QProcess.startDetached(str(CLI), args))
        menu.addAction(action)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if sys.platform == "darwin":
        hide_dock_icon()
    tray = TrayApp(app)  # noqa: F841  (kept alive for the event loop)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
