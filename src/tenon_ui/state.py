"""Reads tenon's CLI and state files and turns them into a display-ready `View`.

No Qt here, so it runs and tests without a display.
"""

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

STATE_DIR = Path.home() / ".local/state/tenon"
CLI = Path.home() / ".local/bin/tenon"
# Past this with no new progress line, a run without an end marker was killed.
STALLED_SECONDS = 300
LISTED_ITEMS = 6


class Health(Enum):
    UNKNOWN = "unknown"
    RUNNING = "running"
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


def _running(progress: list[dict], progress_age: float | None) -> dict | None:
    if not progress or progress[-1].get("event") == "end":
        return None
    if progress_age is None or progress_age >= STALLED_SECONDS:
        return None
    head, last = progress[0], progress[-1]
    return {"mode": head.get("mode", "?"), "launching": head.get("event") == "launching",
            "n": last.get("n", 0), "total": head.get("total_estimate") or 0,
            "task": last.get("task", "")}


def build_view(report: dict | None, progress: list[dict], progress_age: float | None,
               history: list[dict], manifest: dict | None) -> View:
    status = (report or {}).get("last_run")
    run = _running(progress, progress_age)
    failed = tuple(_label(e) for e in (status or {}).get("failed") or [])
    changed = tuple(_label(e) for e in (status or {}).get("changed") or [])
    # `changed` under check means drift; under apply it means drift repaired.
    drift = len(changed) if (status or {}).get("mode") == "check" else 0
    stale = bool((report or {}).get("stale"))
    days = (report or {}).get("days_since_success")

    percent = None
    if run:
        health = Health.RUNNING
        if run["launching"]:
            headline, detail = f"Starting {run['mode']}: syncing layers", ""
        else:
            of = f" of {run['total']}" if run["total"] else ""
            headline = f"{run['mode']}: task {run['n']}{of}"
            detail = run["task"]
            if run["total"]:
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
        orphans = len(manifest.get("orphans") or [])
        if orphans:
            managed += f", {orphans} no longer managed"

    return View(health, percent, headline, detail, last_run, failed[:LISTED_ITEMS],
                changed[:LISTED_ITEMS], sync, managed, _existing_log(history))


def load_view(report: dict | None, state_dir: Path = STATE_DIR) -> View:
    progress_file = state_dir / "progress.jsonl"
    try:
        progress_age = time.time() - progress_file.stat().st_mtime
    except OSError:
        progress_age = None
    return build_view(report, read_jsonl(progress_file), progress_age,
                      read_jsonl(state_dir / "history.jsonl")[-10:],
                      read_json(state_dir / "managed-paths.json"))
