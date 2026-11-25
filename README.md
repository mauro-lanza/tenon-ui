# tenon-ui

Tray/menu-bar app for [tenon](../tenon). It shows run state (running with %, ok, failed, drift, stale), notifies when a run finishes, and starts `tenon check` / `tenon apply`. It only uses the `tenon` CLI and the state files in `~/.local/state/tenon`.

Targets macOS 13+ (Apple Silicon) and KDE Plasma 6 on Wayland. Python 3.13+.

## Install and run

```bash
uv tool install --force .
tenon-ui
```

## Develop

```bash
uv run --with pytest pytest
uv run tenon-ui
```

Tests need no display (`QT_QPA_PLATFORM=offscreen` is set by the smoke test).

## Layout

- `src/tenon_ui/state.py`: reads the CLI and state files into a `View` (no Qt).
- `src/tenon_ui/app.py`: tray icon, file watcher, menu, notifications.

## Limits

- The Dock icon is hidden at runtime through the Objective-C runtime; a later `.app` bundle should set `LSUIElement` instead.
- macOS notifications come from the Python interpreter's identity until the app is bundled.
