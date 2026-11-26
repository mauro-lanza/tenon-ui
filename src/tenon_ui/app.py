"""The tray icon: shows tenon's state and starts check/apply runs."""

import ctypes
import ctypes.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPointF, QProcess, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QIcon,
                           QPainter, QPainterPath, QPen, QPixmap, QTextCursor)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSystemTrayIcon, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from tenon_ui.state import (BUSY_MESSAGE, CLI, LOG_DIR, STATE_DIR, UPDATE_MESSAGE, Capability, Health, View, latest_log,
                            load_view, next_log_chunk, parse_capabilities, parse_report, password_outcome,
                            schema_too_new, selection_commands, stderr_tail)

AGENT_LABEL = "com.tenon.ui"
LOG_MAX_LINES = 20_000
DEBOUNCE_MS = 500
FALLBACK_MS = 60_000
# macOS QFileSystemWatcher misses appends, so a run is followed by polling the files; status (~80 ms spawn) only every few ticks.
POLL_MS = 1_000
STATUS_EVERY_TICKS = 3
SPIN_DEGREES = 30
PACKAGE_DIR = Path(__file__).parent
STATUS_TIMEOUT_MS = 10_000
UPGRADE_WARNING = ("Upgrade packages with Homebrew. Casks may quit and replace running apps, "
                   "and some steps need your admin password.")
ICON_SIZE = 22
FAILED_COLOR = "#e5484d"
RUN_HEALTH = (Health.RUNNING, Health.STALLED)
GLYPHS = {Health.UNKNOWN: "?", Health.FAILED: "!", Health.DRIFT: "~", Health.STALE: "z", Health.STALLED: "\u2026"}


@dataclass(frozen=True)
class IconSpec:
    color: str
    mask: bool  # macOS template image: the menu bar tints it for light/dark


def icon_spec(health: Health, mac: bool, dark: bool) -> IconSpec:
    """Failed stays red and non-template on both platforms: a tinted-gray failure is too easy to miss."""
    if health is Health.FAILED:
        return IconSpec(FAILED_COLOR, False)
    if mac:
        return IconSpec("#000000", True)
    return IconSpec("#fcfcfc" if dark else "#232629", False)


def make_icon(health: Health, percent: int | None, spec: IconSpec, ratio: float, spin: int = 0) -> QIcon:
    """State is encoded by shape (ring + check / progress arc / filled '!' / glyphs), not by colour.

    Without a percent the arc is a quarter that `spin` rotates."""
    pixmap = QPixmap(round(ICON_SIZE * ratio), round(ICON_SIZE * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(spec.color)
    pen = QPen(color, 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    ring = QRectF(3, 3, ICON_SIZE - 6, ICON_SIZE - 6)
    if health is Health.FAILED:
        painter.setBrush(color)
        painter.drawEllipse(ring)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    elif health is Health.RUNNING:
        track = QColor(color)
        track.setAlphaF(0.35)
        painter.setPen(QPen(track, 2))
        painter.drawEllipse(ring)
        painter.setPen(pen)
        sweep = 360 * (percent if percent is not None else 25) // 100
        painter.drawArc(ring, (90 - spin) * 16, -sweep * 16)
    else:
        painter.drawEllipse(ring)
    if health is Health.OK:
        check = QPainterPath(QPointF(7, 11.5))
        check.lineTo(10, 14.5)
        check.lineTo(15, 8)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(check)
    elif health is not Health.RUNNING:
        font = QFont()
        font.setBold(True)
        font.setPixelSize(13)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, ICON_SIZE, ICON_SIZE), Qt.AlignmentFlag.AlignCenter, GLYPHS[health])
    painter.end()
    icon = QIcon(pixmap)
    icon.setIsMask(spec.mask)
    return icon


def _ns_app_send(selector: bytes, arg: int) -> None:
    """Calls a one-argument selector on NSApplication.sharedApplication through the Objective-C runtime; no pyobjc needed."""
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.restype = ctypes.c_void_p
    get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
    send = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(("objc_msgSend", objc))
    ns_app = get(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
    send(ns_app, objc.sel_registerName(selector), arg)


def hide_dock_icon() -> None:
    _ns_app_send(b"setActivationPolicy:", 1)  # NSApplicationActivationPolicyAccessory


def activate_app() -> None:
    """An accessory app is never frontmost on its own; without this its windows open behind others."""
    _ns_app_send(b"activateIgnoringOtherApps:", 1)


class LogWindow(QPlainTextEdit):
    """Streams the newest run log: the current run while one is in progress, the last run otherwise."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setMaximumBlockCount(LOG_MAX_LINES)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.resize(900, 600)
        self.log: Path | None = None
        self.current: Path | None = None
        self.offset = 0
        self.watcher = QFileSystemWatcher()
        self.watcher.directoryChanged.connect(self.refresh)
        self.watcher.fileChanged.connect(self.refresh)

    def present(self) -> None:
        self.refresh()
        activate_window(self)

    def refresh(self) -> None:
        # Watches drop when a file is replaced or a directory is created late, so re-add each time.
        wanted = [str(p) for p in (LOG_DIR, self.log) if p and p.exists()]
        missing = [p for p in wanted if p not in self.watcher.files() + self.watcher.directories()]
        if missing:
            self.watcher.addPaths(missing)
        self.log, self.offset, text, replaced = next_log_chunk(LOG_DIR, self.log, self.offset, self.current)
        self.setWindowTitle(f"tenon log: {self.log.name}" if self.log else "tenon log")
        if replaced:
            self.clear()
        if not text:
            return
        bar = self.verticalScrollBar()
        at_bottom = replaced or bar.value() >= bar.maximum()
        position = bar.value()
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        bar.setValue(bar.maximum() if at_bottom else position)


def open_file(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def sudo_is_passwordless() -> bool:
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


def ask_password() -> str | None:
    """None when cancelled; an empty string means skip the steps that need root."""
    text, ok = QInputDialog.getText(None, "tenon", "Admin password (leave empty to skip steps that need it):",
                                    QLineEdit.EchoMode.Password)
    return text if ok else None


def activate_window(window: QWidget) -> None:
    window.show()
    window.raise_()
    window.activateWindow()
    if sys.platform == "darwin":
        activate_app()


class SettingsWindow(QWidget):
    """Checkbox per capability; Apply writes the selection diff through the CLI, then offers a run."""

    def __init__(self, apply_now) -> None:
        super().__init__()
        self.apply_now = apply_now
        self.setWindowTitle("tenon capabilities")
        self.resize(760, 560)
        self.selected: set[str] = set()
        self.queue: list[list[str]] = []
        self.process: QProcess | None = None

        self.filter = QLineEdit(placeholderText="Filter")
        self.filter.textChanged.connect(self.apply_filter)
        self.tree = QTreeWidget(columnCount=4)
        self.tree.setHeaderLabels(["Capability", "Description", "Layer", ""])
        self.tree.setRootIsDecorated(False)
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self.save)
        note = QLabel("Deselecting a capability stops managing it; nothing is uninstalled.")
        self.status = QLabel("Loading\u2026")
        bottom = QHBoxLayout()
        bottom.addWidget(note, 1)
        bottom.addWidget(self.apply_button)
        layout = QVBoxLayout(self)
        for part in (self.filter, self.tree, self.status):
            layout.addWidget(part)
        layout.addLayout(bottom)

    def present(self) -> None:
        self.load()
        activate_window(self)

    def load(self) -> None:
        process = QProcess(self)
        process.finished.connect(lambda code, _: self.on_loaded(code, process))
        process.errorOccurred.connect(lambda error: error is QProcess.ProcessError.FailedToStart
                                      and self.status.setText("tenon is not installed"))
        process.start(str(CLI), ["capabilities", "--json"])

    def on_loaded(self, code: int, process: QProcess) -> None:
        text = bytes(process.readAllStandardOutput()).decode(errors="replace")
        try:
            too_new = schema_too_new(json.loads(text))
        except json.JSONDecodeError:
            too_new = False
        caps = parse_capabilities(text) if code == 0 else None
        if too_new:
            self.status.setText(UPDATE_MESSAGE)
            return
        if caps is None:
            self.status.setText("Could not read capabilities")
            return
        self.show_capabilities(caps)

    def show_capabilities(self, caps: list[Capability]) -> None:
        self.tree.clear()
        self.selected = {c.id for c in caps if c.selected}
        for cap in caps:
            item = QTreeWidgetItem([cap.id, cap.desc, cap.layer, "active" if cap.active else ""])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if cap.selected else Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(item)
        for column in range(4):
            self.tree.resizeColumnToContents(column)
        self.status.setText(f"{len(caps)} capabilities")
        self.apply_filter(self.filter.text())

    def apply_filter(self, text: str) -> None:
        needle = text.lower()
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            item.setHidden(bool(needle) and not any(needle in item.text(c).lower() for c in range(3)))

    def wanted(self) -> set[str]:
        items = (self.tree.topLevelItem(row) for row in range(self.tree.topLevelItemCount()))
        return {item.text(0) for item in items if item.checkState(0) is Qt.CheckState.Checked}

    def save(self) -> None:
        self.queue = selection_commands(self.selected, self.wanted())
        self.apply_button.setEnabled(False)
        self.run_next()

    def run_next(self) -> None:
        if not self.queue:
            self.apply_button.setEnabled(True)
            self.load()
            answer = QMessageBox.question(self, "tenon", "Selection saved. Apply now?")
            if answer == QMessageBox.StandardButton.Yes:
                self.apply_now()
            return
        process = self.process = QProcess(self)
        process.finished.connect(lambda code, _: self.on_command_finished(code, process))
        process.start(str(CLI), self.queue.pop(0))

    def on_command_finished(self, code: int, process: QProcess) -> None:
        if code != 0:
            error = bytes(process.readAllStandardError()).decode(errors="replace").strip()
            QMessageBox.critical(self, "tenon", error or f"tenon exited with {code}")
            self.queue.clear()
            self.apply_button.setEnabled(True)
            self.load()
            return
        self.run_next()


def install_signature() -> tuple:
    """Name, mtime and size of every installed source file; a reinstall rewrites them."""
    return tuple((p.name, s.st_mtime_ns, s.st_size) for p in sorted(PACKAGE_DIR.glob("*.py")) for s in [p.stat()])


def restart() -> None:
    """Restart onto the new code. macOS: exit and let the KeepAlive agent relaunch, since an exec'd process loses its status item."""
    if sys.platform == "darwin":
        QApplication.quit()
        return
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
        self.health, self.percent = Health.UNKNOWN, None
        self.indeterminate = False
        self.spin = 0
        self.log_window: LogWindow | None = None
        self.settings_window: SettingsWindow | None = None
        self.runner: QProcess | None = None
        self.tray = QSystemTrayIcon()
        self.update_icon()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.update_icon)
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
        self.poll = QTimer(interval=POLL_MS)
        self.poll.timeout.connect(self.on_poll)
        self.report: dict | None = None
        self.ticks = 0
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
        missing = [str(p) for p in wanted if str(p) not in self.watcher.files() + self.watcher.directories()]
        if missing:
            self.watcher.addPaths(missing)

    def on_fallback(self) -> None:
        self.watch_state()  # the state dir may not have existed at start
        self.refresh()

    def update_icon(self, *_: object) -> None:
        dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        ratio = max(2.0, QGuiApplication.primaryScreen().devicePixelRatio())
        spec = icon_spec(self.health, sys.platform == "darwin", dark)
        self.tray.setIcon(make_icon(self.health, self.percent, spec, ratio, self.spin))

    def set_polling(self, running: bool) -> None:
        """The 1 s poll exists only while a run is in progress."""
        if running and not self.poll.isActive():
            self.ticks = 0
            self.poll.start()
        elif not running and self.poll.isActive():
            self.poll.stop()
            self.spin = 0

    def on_poll(self) -> None:
        self.ticks += 1
        if self.view is not None and self.view.indeterminate and self.view.health is Health.RUNNING:
            self.spin = (self.spin + SPIN_DEGREES) % 360
        self.show_view(load_view(self.report))
        if self.log_window is not None and self.log_window.isVisible():
            self.log_window.refresh()
        if self.ticks % STATUS_EVERY_TICKS == 0:
            self.refresh()

    def show_view(self, view: View) -> None:
        self.view = view
        self.health, self.percent, self.indeterminate = view.health, view.percent, view.indeterminate
        self.update_icon()
        self.tray.setToolTip(f"tenon: {view.headline}")
        if self.log_window is not None:
            self.log_window.current = view.current_log

    def show_settings(self) -> None:
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self.start_apply)
        self.settings_window.present()

    def start_apply(self) -> None:
        """macOS applies need no password; Linux asks for one unless sudo is passwordless."""
        if sys.platform == "darwin" or sudo_is_passwordless():
            self.start_run(["apply"])
        else:
            self.run_with_password(["apply"])

    def start_upgrade(self) -> None:
        answer = QMessageBox.question(None, "Upgrade packages", UPGRADE_WARNING + "\n\nContinue?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if sudo_is_passwordless():
            self.start_run(["upgrade", "--yes"])
        else:
            self.run_with_password(["upgrade", "--yes"])

    def start_run(self, args: list[str]) -> None:
        """Runs the CLI as our child so its exit code surfaces; Quit asks first because the child dies with us."""
        process = self.runner = QProcess()
        process.setStandardOutputFile(QProcess.nullDevice())
        process.finished.connect(lambda code, _: self.on_run_finished(code, process))
        process.errorOccurred.connect(lambda error: error is QProcess.ProcessError.FailedToStart
                                      and QMessageBox.critical(None, "tenon", "tenon is not installed"))
        process.start(str(CLI), args)

    def on_run_finished(self, code: int, process: QProcess) -> None:
        self.refresh()
        if code == 0:
            return
        if password_outcome(code) == "busy":
            QMessageBox.information(None, "tenon", BUSY_MESSAGE)
            return
        error = stderr_tail(bytes(process.readAllStandardError()).decode(errors="replace"))
        QMessageBox.critical(None, "tenon", error or f"tenon exited with {code}")

    def request_quit(self) -> None:
        if self.runner is not None and self.runner.state() != QProcess.ProcessState.NotRunning:
            answer = QMessageBox.question(None, "tenon", "A run is in progress, quit anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        quit_app()

    def run_with_password(self, args: list[str]) -> None:
        """Feeds the password to the CLI on stdin only; progress shows through the state files."""
        password = ask_password()
        if password is None:
            return
        process = self.runner = QProcess()
        process.setStandardOutputFile(QProcess.nullDevice())
        process.finished.connect(lambda code, _: self.on_privileged_finished(args, code, process))
        process.start(str(CLI), [*args, "--password-stdin"])
        process.write(password.encode() + b"\n")
        process.closeWriteChannel()
        password = ""

    def on_privileged_finished(self, args: list[str], code: int, process: QProcess) -> None:
        outcome = password_outcome(code)
        if outcome == "retry":
            box = QMessageBox(QMessageBox.Icon.Warning, "tenon", "Incorrect password. Nothing was run.",
                              QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Cancel)
            if box.exec() == QMessageBox.StandardButton.Retry:
                self.run_with_password(args)
        elif outcome == "busy":
            QMessageBox.information(None, "tenon", BUSY_MESSAGE)
        elif code != 0:
            error = stderr_tail(bytes(process.readAllStandardError()).decode(errors="replace"))
            QMessageBox.critical(None, "tenon", error or f"tenon exited with {code}")

    def show_log(self) -> None:
        if self.log_window is None:
            self.log_window = LogWindow()
        self.log_window.present()

    def on_state_changed(self, _path: str) -> None:
        self.watch_state()
        if self.log_window is not None and self.log_window.isVisible():
            self.log_window.refresh()  # picks up a new run's log and re-adds lost watches
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
        previous = self.view
        self.report = report
        self.show_view(load_view(report))
        view = self.view
        finished = ((report or {}).get("last_run") or {}).get("finished")
        run_completed = report is not None and self.seen_report and finished != self.last_finished
        if report is not None:
            self.seen_report, self.last_finished = True, finished
        self.set_polling(view.health in RUN_HEALTH)
        if previous and previous.health in RUN_HEALTH and view.health not in RUN_HEALTH:
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
        label(view.schedule)
        if view.orphans:
            orphans = menu.addMenu("No longer managed, still on disk")
            for path in view.orphans:
                orphans.addAction(path).setEnabled(False)
            orphans.addSeparator()
            orphans.addAction("Nothing is deleted automatically").setEnabled(False)
        menu.addSeparator()
        idle = view.health not in RUN_HEALTH
        self.add_run(menu, "Check for drift", ["check"], idle)
        apply = QAction("Apply now", menu)
        apply.setEnabled(idle)
        apply.triggered.connect(self.start_apply)
        menu.addAction(apply)
        if sys.platform == "darwin":
            upgrade = QAction("Upgrade packages\u2026", menu)
            upgrade.setEnabled(idle)
            upgrade.triggered.connect(self.start_upgrade)
            menu.addAction(upgrade)
        settings = QAction("Capabilities\u2026", menu)
        settings.triggered.connect(self.show_settings)
        menu.addAction(settings)
        if latest_log(LOG_DIR):
            show_log = QAction("Show log", menu)
            show_log.triggered.connect(self.show_log)
            menu.addAction(show_log)
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
        quit_action.triggered.connect(self.request_quit)
        menu.addAction(quit_action)

    def add_run(self, menu: QMenu, text: str, args: list[str], enabled: bool) -> None:
        action = QAction(text, menu)
        action.setEnabled(enabled)
        action.triggered.connect(lambda: self.start_run(args))
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
