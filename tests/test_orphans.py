import json
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from tenon_ui import app as tray_app
from tenon_ui.state import (forget_args, needs_force, needs_password, orphan_count, parse_orphans, parse_results,
                            remove_args)


def row(id_, kind="file", manager=None, safe=True, **extra):
    return {"id": id_, "kind": kind, "manager": manager, "path": f"/p/{id_}" if kind == "file" else None,
            "name": id_ if kind == "package" else None, "layer": "l", "capability": "c", "since": "t",
            "safe": safe, "reason": "why", **extra}


FAKE = json.dumps({"schema_version": 1, "orphans": [row("a"), row("b", safe=False), row("c", "package", "cask")]})


def test_preselection_and_package_reason():
    a, b, c = parse_orphans(FAKE)
    assert (a.preselected, b.preselected, c.preselected) == (True, False, False)
    assert c.shown_reason == "may have been installed before tenon"
    assert c.label == "c (cask)" and a.label == "/p/a"


def test_command_building():
    a, b, c = parse_orphans(FAKE)
    assert remove_args([a], False) == ["orphans", "remove", "--json", "a"]
    assert remove_args([a, b], False) == ["orphans", "remove", "--json", "--force", "a", "b"]
    assert "--force" not in remove_args([a, c], False)
    assert needs_force([b]) and not needs_force([a, c])
    assert needs_password([c]) and not needs_password([a])
    assert "--password-stdin" in remove_args([c], True)
    assert forget_args([a, b]) == ["orphans", "forget", "--json", "a", "b"]


def test_parsing():
    assert parse_orphans("nope") is None
    assert parse_orphans(json.dumps({"schema_version": 2, "orphans": []})) is None
    assert parse_results(json.dumps({"results": [{"id": "a", "ok": False, "message": "m"}]})) == [("a", False, "m")]
    assert parse_results("") is None
    assert orphan_count({"orphans": 3}) == 3 and orphan_count({}) == 0 and orphan_count(None) == 0


def test_window_model_and_remove_flow(monkeypatch):
    QApplication.instance() or QApplication([])
    window = tray_app.OrphansWindow()
    window.on_loaded(0, FAKE, "")
    states = [window.table.item(r, 0).checkState() for r in range(3)]
    assert states == [Qt.CheckState.Checked, Qt.CheckState.Unchecked, Qt.CheckState.Unchecked]
    assert window.table.item(2, 4).text() == "may have been installed before tenon"

    window.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    window.table.item(2, 0).setCheckState(Qt.CheckState.Checked)
    runs = []
    monkeypatch.setattr(window, "run", lambda args, done, password=None: runs.append((args, password)))
    monkeypatch.setattr(tray_app, "sudo_is_passwordless", lambda: False)
    monkeypatch.setattr(tray_app, "ask_password", lambda: "pw")
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    window.remove()
    assert runs == [(["orphans", "remove", "--json", "--force", "--password-stdin", "a", "b", "c"], "pw")]

    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(window, "load", lambda: shown.append("reload"))
    out = json.dumps({"results": [{"id": "a", "ok": True, "message": ""}, {"id": "b", "ok": False, "message": "busy"}]})
    window.on_done(1, out, "")
    assert shown == ["OK: /p/a\nFailed: /p/b: busy", "reload"]

    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(a[2]))
    window.on_done(75, "", "")
    assert shown[-1] == tray_app.BUSY_MESSAGE
    window.on_loaded(0, json.dumps({"schema_version": 2, "orphans": []}), "")
    assert window.status.text() == tray_app.UPDATE_MESSAGE


def test_menu_shows_cleanup_only_with_orphans(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    monkeypatch.setattr(tray, "refresh", lambda: None)
    tray.view = tray_app.load_view(None)
    tray.report = {"orphans": 2}
    tray.on_menu_about_to_show()
    assert "Clean up\u2026 (2)" in [a.text() for a in tray.menu.actions()]
    tray.report = {"orphans": 0}
    tray.on_menu_about_to_show()
    assert not [a for a in tray.menu.actions() if a.text().startswith("Clean up")]
