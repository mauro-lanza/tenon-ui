import gc
import os
import sys

import pytest
from PySide6.QtCore import QProcess

from tenon_ui import app as tray_app

KEPT: list = []


def keeping(init):
    def wrapper(self, *args, **kwargs):
        KEPT.append(self)
        init(self, *args, **kwargs)
    return wrapper


def pytest_configure(config):
    """PySide crashes when connected Qt objects are destroyed in arbitrary order (refcount or GC), so none are."""
    gc.disable()
    for cls in (tray_app.TrayApp, tray_app.OrphansWindow, tray_app.SettingsWindow, tray_app.LogWindow):
        cls.__init__ = keeping(cls.__init__)


def pytest_sessionfinish(session, exitstatus):
    session.config.tenon_exit = int(exitstatus)


def pytest_unconfigure(config):
    """Interpreter teardown would destroy the kept objects in arbitrary order; skip it."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(getattr(config, "tenon_exit", 0))


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    monkeypatch.setattr(QProcess, "start", lambda *args, **kwargs: None)
