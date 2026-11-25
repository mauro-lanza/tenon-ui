import json
from pathlib import Path

from tenon_ui.state import Health, build_view, parse_report, read_jsonl


def run(mode="apply", result="ok", changed=(), failed=(), **extra):
    return {"last_run": {"mode": mode, "result": result, "finished": "2026-11-25T13:28:19+0100",
                         "duration_seconds": 30.5, "changed": list(changed),
                         "failed": list(failed)}, **extra}


def test_ok():
    view = build_view(run(), [], None, [], None)
    assert view.health is Health.OK
    assert "30.5s" in view.last_run


def test_apply_changes_are_not_drift():
    assert build_view(run(changed=[{"item": "x"}]), [], None, [], None).health is Health.OK


def test_check_changes_are_drift():
    view = build_view(run("check", changed=[{"dest": "/a"}, {"item": "b"}]), [], None, [], None)
    assert view.health is Health.DRIFT
    assert view.changed == ("/a", "b")


def test_failed_beats_stale():
    view = build_view(run(result="failed", failed=[{"task": "t"}], stale=True), [], None, [], None)
    assert view.health is Health.FAILED
    assert view.failed == ("t",)


def test_stale():
    view = build_view(run(stale=True, days_since_success=5), [], None, [], None)
    assert view.health is Health.STALE
    assert "5 days" in view.headline


def test_parse_report():
    assert parse_report('{"stale": false}') == {"stale": False}
    for bad in ("", "not json", "[1]", "null"):
        assert parse_report(bad) is None


def test_no_report():
    assert build_view(None, [], None, [], None).health is Health.UNKNOWN


def test_running_percent():
    progress = [{"event": "start", "mode": "apply", "total_estimate": 200},
                {"event": "task", "n": 50, "task": "Install"}]
    view = build_view(run(), progress, 5.0, [], None)
    assert (view.health, view.percent, view.detail) == (Health.RUNNING, 25, "Install")


def test_launching_has_no_percent():
    view = build_view(None, [{"event": "launching", "mode": "check"}], 1.0, [], None)
    assert view.health is Health.RUNNING and view.percent is None


def test_finished_or_stalled_progress_is_not_running():
    start = {"event": "start", "mode": "apply", "total_estimate": 10}
    assert build_view(run(), [start, {"event": "end"}], 1.0, [], None).health is Health.OK
    assert build_view(run(), [start], 9999.0, [], None).health is Health.OK


def test_manifest_and_log(tmp_path: Path):
    log = tmp_path / "run.log"
    log.write_text("")
    history = [{"log": str(log)}, {"log": str(tmp_path / "pruned.log")}]
    manifest = {"files": [1, 2], "orphans": [{"path": "/x"}]}
    view = build_view(run(), [], None, history, manifest)
    assert view.log == log
    assert view.managed == "Manages 2 files, 1 no longer managed"


def test_read_jsonl_skips_partial_line(tmp_path: Path):
    path = tmp_path / "p.jsonl"
    path.write_text(json.dumps({"a": 1}) + "\n{\"b\":")
    assert read_jsonl(path) == [{"a": 1}]
    assert read_jsonl(tmp_path / "missing") == []
