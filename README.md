# tenon-ui

Tray/menu-bar app for [tenon](../tenon). It shows run state (running with %, ok, failed, drift, stale), notifies when a run finishes, and starts `tenon check` / `tenon apply`. It only uses the `tenon` CLI and the state files in `~/.local/state/tenon`.

It also shows the next scheduled run, streams the run log, opens a capabilities window (`tenon capabilities`), and runs "Upgrade packages" (macOS) and, on Linux, "Apply now" without a terminal: when `sudo -n` fails it asks for the admin password and pipes it to `tenon … --password-stdin`. The password is never stored or logged; a wrong one (exit 65) offers a retry.

When `tenon status` reports orphans, "Clean up… (N)" opens a window listing what tenon no longer manages (`tenon orphans`). Safe files are pre-ticked; unsafe files and packages never are. Remove moves files to the tenon trash and uninstalls packages (after a confirmation; `--force` only for ticked unsafe files, the password flow only for casks); Forget only drops the entry.

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

- `src/tenon_ui/state.py`: reads the CLI and state files into a `View`, plus pure helpers for capabilities, schedule and password outcomes (no Qt).
- `src/tenon_ui/app.py`: tray icon, file watcher, menu, notifications, log and capabilities windows, password flow.

## Limits

- The Dock icon is hidden at runtime through the Objective-C runtime; a later `.app` bundle should set `LSUIElement` instead.
- macOS notifications come from the Python interpreter's identity until the app is bundled.
