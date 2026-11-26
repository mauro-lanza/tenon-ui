import json
from pathlib import Path

from tenon_ui.state import (Health, build_view, next_log_chunk, parse_capabilities, parse_report, password_outcome,
                            read_jsonl, schedule_line, selection_commands)


def test_schedule_line_password_outcome_and_selection_diff():
    assert schedule_line({"schedule": {"upgrade": "11:00", "check": "12:00"}}) == "Next: upgrade 11:00 \u00b7 check 12:00"
    assert schedule_line({"schedule": None}) == schedule_line(None) == ""
    assert [password_outcome(c) for c in (0, 1, 64, 65)] == ["done", "done", "error", "retry"]
    assert selection_commands({"a", "b"}, {"a", "b"}) == []
    assert selection_commands({"a"}, {"c", "b"}) == [["capabilities", "select", "b", "c"],
                                                     ["capabilities", "deselect", "a"]]
    assert parse_capabilities("nope") is None
    assert parse_capabilities('{"capabilities":[{"id":"x"}]}') is None


def test_log_chunk_growth_partial_line_replacement_and_empty(tmp_path):
    assert next_log_chunk(tmp_path, None, 0) == (None, 0, "", False)
    first = tmp_path / "20260101-000000-apply.log"
    first.write_text("a\nb")
    log, offset, text, replaced = next_log_chunk(tmp_path, None, 0)
    assert (log, offset, text, replaced) == (first, 2, "a\n", True)
    first.write_text("a\nb\nc\n")
    assert next_log_chunk(tmp_path, first, offset) == (first, 6, "b\nc\n", False)
    assert next_log_chunk(tmp_path, first, 6) == (first, 6, "", False)
    second = tmp_path / "20260102-000000-check.log"
    second.write_text("x\n")
    assert next_log_chunk(tmp_path, first, 6) == (second, 2, "x\n", True)
    second.write_text("")
    assert next_log_chunk(tmp_path, second, 2) == (second, 0, "", True)


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


def test_recent_runs_newest_first_with_log_only_when_it_exists(tmp_path: Path):
    log = tmp_path / "run.log"
    log.write_text("")
    history = [{"finished": "2026-11-24T09:00:01", "result": "ok", "duration_seconds": 5,
                "changed": 0, "log": str(tmp_path / "pruned.log")},
               {"finished": "2026-11-25T09:00:01", "result": "failed", "duration_seconds": 7,
                "changed": 2, "partial": True, "log": str(log)}]
    rows = build_view(run(), [], None, history, None).runs
    assert rows[0].text == "2026-11-25 09:00*  failed  7s  (2 changed)"
    assert rows[0].log == log
    assert rows[1].text == "2026-11-24 09:00  ok  5s  (0 changed)"
    assert rows[1].log is None


def test_orphans_are_capped_and_partial_is_reported():
    manifest = {"files": [], "orphans": [{"path": f"/o{i}"} for i in range(12)]}
    report = run()
    report["last_run"]["partial"] = True
    view = build_view(report, [], None, [], manifest)
    assert view.partial
    assert view.orphans == tuple(f"/o{i}" for i in range(10))


def test_changed_list_is_capped_with_remainder():
    view = build_view(run("check", changed=[{"item": str(i)} for i in range(13)]), [], None, [], None)
    assert len(view.changed) == 10 and view.changed_more == 3


def test_read_jsonl_skips_partial_line(tmp_path: Path):
    path = tmp_path / "p.jsonl"
    path.write_text(json.dumps({"a": 1}) + "\n{\"b\":")
    assert read_jsonl(path) == [{"a": 1}]
    assert read_jsonl(tmp_path / "missing") == []
