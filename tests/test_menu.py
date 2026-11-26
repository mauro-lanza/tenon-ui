import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication

from tenon_ui import app as tray_app
from tenon_ui.state import build_view

UPGRADE = "Upgrade packages\u2026"


def test_menu_has_upgrade_recent_runs_orphans_and_partial_note(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    tray = tray_app.TrayApp(qt)
    monkeypatch.setattr(tray, "refresh", lambda: None)
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    report = {"last_run": {"mode": "apply", "result": "ok", "partial": True}}
    history = [{"finished": "2026-11-25T09:00:00", "result": "ok"}]
    tray.view = build_view(report, [], None, history, {"orphans": [{"path": "/old"}]})
    tray.on_menu_about_to_show()
    actions = {a.text(): a for a in tray.menu.actions()}
    assert actions[UPGRADE].isEnabled()
    assert "Partial run: only part of the machine was evaluated" in actions
    orphans = actions["No longer managed, still on disk"].menu().actions()
    assert [a.text() for a in orphans if a.text()] == ["/old", "Nothing is deleted automatically"]
    assert len(actions["Recent runs"].menu().actions()) == 1

    tray.view = build_view(report, [{"event": "launching", "mode": "apply"}], 1.0, [], None)
    tray.on_menu_about_to_show()
    assert not {a.text(): a for a in tray.menu.actions()}[UPGRADE].isEnabled()

    monkeypatch.setattr(tray_app.sys, "platform", "linux")
    tray.on_menu_about_to_show()
    assert UPGRADE not in [a.text() for a in tray.menu.actions()]
