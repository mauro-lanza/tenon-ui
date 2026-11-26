"""Reads tenon's CLI and state files and turns them into a display-ready `View`.

No Qt here, so it runs and tests without a display.
"""

import json
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

STATE_DIR = Path.home() / ".local/state/tenon"
CLI = Path.home() / ".local/bin/tenon"
LOG_DIR = Path.home() / ("Library/Logs/tenon" if sys.platform == "darwin" else ".local/state/tenon/logs")
# Past this with no new progress line, a run without an end marker was killed.
STALLED_SECONDS = 300
LISTED_FAILED = 6
LISTED_CHANGED = 10
SCHEMA_VERSION = 1
UPDATE_MESSAGE = "Update tenon-ui: tenon reports a newer data format than this app reads"


class Health(Enum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    STALLED = "stalled"
    OK = "ok"
    FAILED = "failed"
    DRIFT = "drift"
    STALE = "stale"


@dataclass(frozen=True)
class View:
    health: Health
    percent: int | None
    headline: str
    detail: str
    last_run: str
    failed: tuple[str, ...]
    changed: tuple[str, ...]
    sync: str
    managed: str
    log: Path | None
    partial: bool
    changed_more: int
    runs: tuple["RunRow", ...]
    schedule: str
    indeterminate: bool
    current_log: Path | None


@dataclass(frozen=True)
class Capability:
    id: str
    desc: str
    layer: str
    selected: bool
    active: bool
    offerable: bool = True


EXIT_USAGE = 64
EXIT_BAD_PASSWORD = 65
EXIT_BUSY = 75
BUSY_MESSAGE = "A run is already in progress"


def schedule_line(report: dict | None) -> str:
    schedule = (report or {}).get("schedule")
    if not schedule:
        return ""
    return "Next: " + " \u00b7 ".join(f"{name} {schedule[name]}" for name in ("upgrade", "check") if schedule.get(name))


def password_outcome(code: int) -> str:
    """What a privileged run's exit code asks of the UI: 'retry' (wrong password), 'error' (bad usage), 'busy' or 'done'."""
    return {EXIT_BAD_PASSWORD: "retry", EXIT_USAGE: "error", EXIT_BUSY: "busy"}.get(code, "done")


def schema_too_new(data: object) -> bool:
    version = data.get("schema_version") if isinstance(data, dict) else None
    return isinstance(version, int) and version > SCHEMA_VERSION


def stderr_tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def parse_capabilities(text: str) -> list[Capability] | None:
    """Parses `tenon capabilities --json`, sorted by layer then id; None when it is not that."""
    try:
        data = json.loads(text)
        if schema_too_new(data):
            return None
        rows = data["capabilities"]
        caps = [Capability(r["id"], r.get("desc") or "", r.get("layer") or "", bool(r["selected"]), bool(r["active"]),
                        bool(r.get("offerable", True)))
                for r in rows]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return sorted(caps, key=lambda c: (c.layer, c.id))


def listed_capabilities(caps: list[Capability]) -> tuple[list[Capability], int]:
    """Capabilities to show (offerable or already selected) and how many others are hidden."""
    shown = [c for c in caps if c.offerable or c.selected]
    return shown, len(caps) - len(shown)


def selection_commands(selected: set[str], wanted: set[str], offerable: set[str]) -> list[list[str]]:
    """`tenon capabilities` argument lists that turn the `selected` set into `wanted`; never selects a non-offerable id."""
    commands = []
    to_select = (wanted - selected) & offerable
    if to_select:
        commands.append(["capabilities", "select", *sorted(to_select)])
    if selected - wanted:
        commands.append(["capabilities", "deselect", *sorted(selected - wanted)])
    return commands


@dataclass(frozen=True)
class Orphan:
    id: str
    kind: str
    manager: str | None
    path: str | None
    name: str | None
    layer: str
    capability: str
    safe: bool
    reason: str

    @property
    def is_package(self) -> bool:
        return self.kind == "package"

    @property
    def label(self) -> str:
        if self.is_package:
            return f"{self.name} ({self.manager})"
        return self.path or self.id

    @property
    def shown_reason(self) -> str:
        return PACKAGE_REASON if self.is_package else self.reason

    @property
    def preselected(self) -> bool:
        return not self.is_package and self.safe


PACKAGE_REASON = "may have been installed before tenon"


def orphan_count(report: dict | None) -> int:
    count = (report or {}).get("orphans")
    return count if isinstance(count, int) else 0


def parse_orphans(text: str) -> list[Orphan] | None:
    """Parses `tenon orphans --json`; None when it is not that or the schema is too new."""
    try:
        data = json.loads(text)
        if schema_too_new(data):
            return None
        return [Orphan(r["id"], r["kind"], r.get("manager"), r.get("path"), r.get("name"), r.get("layer") or "",
                       r.get("capability") or "", bool(r.get("safe")), r.get("reason") or "")
                for r in data["orphans"]]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def needs_force(chosen: list[Orphan]) -> bool:
    return any(not o.is_package and not o.safe for o in chosen)


def needs_password(chosen: list[Orphan]) -> bool:
    return any(o.manager == "cask" for o in chosen)


def remove_args(chosen: list[Orphan], password: bool) -> list[str]:
    """`tenon orphans remove` arguments: --force only for an unsafe file, --password-stdin only when a password is piped."""
    return ["orphans", "remove", "--json", *(["--force"] if needs_force(chosen) else []),
            *(["--password-stdin"] if password else []), *(o.id for o in chosen)]


def forget_args(chosen: list[Orphan]) -> list[str]:
    return ["orphans", "forget", "--json", *(o.id for o in chosen)]


def parse_results(text: str) -> list[tuple[str, bool, str]] | None:
    """(id, ok, message) rows of a remove/forget `--json` reply; None when unreadable."""
    try:
        return [(r["id"], bool(r["ok"]), r.get("message") or "") for r in json.loads(text)["results"]]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class RunRow:
    text: str
    log: Path | None


def parse_report(text: str) -> dict | None:
    """Parses `tenon status --json` output (the engine owns the staleness threshold)."""
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        return None
    return report if isinstance(report, dict) else None


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line being appended mid-read
    return entries


def _label(entry: dict) -> str:
    return str(entry.get("dest") or entry.get("item") or entry.get("task") or "?")


def _existing_log(history: list[dict]) -> Path | None:
    for entry in reversed(history):
        log = entry.get("log")
        if log and Path(log).exists():
            return Path(log)
    return None


def _run_row(entry: dict) -> RunRow:
    """One history line; the log is linked only while it still exists (old successful ones are pruned)."""
    when = (entry.get("finished") or "")[:16].replace("T", " ")
    text = (f"{when}{'*' if entry.get('partial') else ''}  {entry.get('result', '?')}  "
            f"{entry.get('duration_seconds', '?')}s  ({entry.get('changed', '?')} changed)")
    log = entry.get("log")
    return RunRow(text, Path(log) if log and Path(log).exists() else None)


def _running(report: dict | None, progress: list[dict], progress_age: float | None) -> dict | None:
    """The run in progress: `current_run` (run.lock is the truth) when the engine reports it, else progress.jsonl."""
    live = progress if progress and progress[-1].get("event") != "end" else []
    if report and "current_run" in report:
        current = report["current_run"]
        if not current:
            return None
        last = live[-1] if live else {}
        total = current.get("total") or (live[0].get("total_estimate") or 0 if live else 0)
        launching = current.get("phase") == "launching"
        return {"mode": current.get("mode") or "?", "launching": launching, "stalled": current.get("phase") == "stalled",
                "indeterminate": bool(current.get("indeterminate")) or launching or not total,
                "n": last.get("n") or current.get("n") or 0, "total": total,
                "task": last.get("task") or current.get("task") or "", "log": current.get("log")}
    if not live or progress_age is None or progress_age >= STALLED_SECONDS:
        return None
    head, last = live[0], live[-1]
    launching = head.get("event") == "launching"
    total = head.get("total_estimate") or 0
    return {"mode": head.get("mode", "?"), "launching": launching, "stalled": False,
            "indeterminate": launching or not total, "n": last.get("n", 0), "total": total,
            "task": last.get("task", ""), "log": head.get("log")}


def build_view(report: dict | None, progress: list[dict], progress_age: float | None,
               history: list[dict], manifest: dict | None) -> View:
    if schema_too_new(report):
        return View(Health.UNKNOWN, None, UPDATE_MESSAGE, "", "", (), (), "", "", None, False, 0, (), "", False, None)
    status = (report or {}).get("last_run")
    run = _running(report, progress, progress_age)
    failed = tuple(_label(e) for e in (status or {}).get("failed") or [])
    changed = tuple(_label(e) for e in (status or {}).get("changed") or [])
    # `changed` under check means drift; under apply it means drift repaired.
    drift = len(changed) if (status or {}).get("mode") == "check" else 0
    stale = bool((report or {}).get("stale"))
    days = (report or {}).get("days_since_success")

    percent = None
    if run:
        health = Health.STALLED if run["stalled"] else Health.RUNNING
        if run["stalled"]:
            headline, detail = f"{run['mode']}: stalled at task {run['n']}", run["task"]
        elif run["launching"]:
            headline, detail = f"Starting {run['mode']}: syncing layers", ""
        else:
            of = f" of {run['total']}" if run["total"] else ""
            headline = f"{run['mode']}: task {run['n']}{of}"
            detail = run["task"]
            if not run["indeterminate"]:
                percent = min(100, 100 * run["n"] // run["total"])
    elif status is None:
        health, headline, detail = Health.UNKNOWN, "No run recorded yet", ""
    elif failed or status.get("result") == "failed":
        health, headline, detail = Health.FAILED, f"{len(failed)} task(s) failed", ""
    elif stale:
        health, headline, detail = Health.STALE, f"Not syncing: last clean run {days} days ago", ""
    elif drift:
        health, headline, detail = Health.DRIFT, f"{drift} item(s) would change", ""
    else:
        health, headline, detail = Health.OK, "In sync, no drift", ""

    last_run = ""
    if status:
        when = (status.get("finished") or "")[:16].replace("T", " ")
        last_run = (f"Last {status.get('mode', '?')}: {status.get('result', '?')}, "
                    f"{when}, {status.get('duration_seconds', '?')}s")

    sync = ""
    if days is not None and not stale:
        sync = "Last clean run today" if days == 0 else f"Last clean run {days} day(s) ago"

    managed = ""
    if manifest:
        managed = f"Manages {len(manifest.get('files') or [])} files"

    runs = tuple(_run_row(entry) for entry in reversed(history))
    return View(health, percent, headline, detail, last_run, failed[:LISTED_FAILED],
                changed[:LISTED_CHANGED], sync, managed, _existing_log(history),
                bool((status or {}).get("partial")), max(0, len(changed) - LISTED_CHANGED),
                runs, schedule_line(report),
                bool(run and (run["indeterminate"] or run["stalled"])),
                Path(run["log"]) if run and run["log"] else None)


def latest_log(log_dir: Path) -> Path | None:
    """The newest run log: names start with a timestamp, so the last name is the current or most recent run."""
    return max(log_dir.glob("*.log"), default=None, key=lambda p: p.name)


def next_log_chunk(log_dir: Path, shown: Path | None, offset: int,
                   current: Path | None = None) -> tuple[Path | None, int, str, bool]:
    """Returns (log, new offset, new text, replaced) for the window showing `shown` up to `offset`.

    `current` is the running run's log; it wins over the newest file.

    `replaced` means a newer run's log took over (or the file shrank): the caller clears first.
    Only whole lines are returned; a partial last line waits for its newline.
    """
    log = current if current and current.exists() else latest_log(log_dir)
    if log is None:
        return None, 0, "", shown is not None
    try:
        size = log.stat().st_size
        replaced = log != shown or size < offset
        start = 0 if replaced else offset
        with log.open("rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return shown, offset, "", False
    complete = data[: data.rfind(b"\n") + 1]
    return log, start + len(complete), complete.decode(errors="replace"), replaced


def load_view(report: dict | None, state_dir: Path = STATE_DIR) -> View:
    progress_file = state_dir / "progress.jsonl"
    try:
        progress_age = time.time() - progress_file.stat().st_mtime
    except OSError:
        progress_age = None
    return build_view(report, read_jsonl(progress_file), progress_age,
                      read_jsonl(state_dir / "history.jsonl")[-10:],
                      read_json(state_dir / "managed-paths.json"))
