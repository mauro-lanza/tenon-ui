import json
from pathlib import Path

from tenon_ui.state import (Health, build_view, listed_capabilities, next_log_chunk, parse_capabilities, parse_report, password_outcome,
                            read_jsonl, schedule_line, selection_commands)


def test_schedule_line_password_outcome_and_selection_diff():
    assert schedule_line({"schedule": {"upgrade": "11:00", "check": "12:00"}}) == "Next: upgrade 11:00 \u00b7 check 12:00"
    assert schedule_line({"schedule": None}) == schedule_line(None) == ""
    assert [password_outcome(c) for c in (0, 1, 64, 65)] == ["done", "done", "error", "retry"]
    assert selection_commands({"a", "b"}, {"a", "b"}, {"a", "b"}) == []
    assert selection_commands({"a"}, {"c", "b"}, {"a", "b", "c"}) == [["capabilities", "select", "b", "c"],
                                                                      ["capabilities", "deselect", "a"]]
    assert parse_capabilities("nope") is None
    assert parse_capabilities('{"capabilities":[{"id":"x"}]}') is None


def test_offerable_parsing_listing_and_selection_guard():
    rows = ('{"capabilities":[{"id":"old","selected":true,"active":false},'
            '{"id":"mac","selected":true,"active":false,"offerable":false},'
            '{"id":"gone","selected":false,"active":false,"offerable":false}]}')
    caps = parse_capabilities(rows)
    assert {c.id: c.offerable for c in caps} == {"old": True, "mac": False, "gone": False}
    shown, hidden = listed_capabilities(caps)
    assert sorted(c.id for c in shown) == ["mac", "old"] and hidden == 1
    assert selection_commands({"mac"}, {"mac", "gone"}, {"old"}) == []
    assert selection_commands({"mac"}, set(), {"old"}) == [["capabilities", "deselect", "mac"]]


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
    view = build_view(run(), [], None, history, {"files": [1, 2]})
    assert view.log == log
    assert view.managed == "Manages 2 files"


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


def test_partial_is_reported():
    report = run()
    report["last_run"]["partial"] = True
    assert build_view(report, [], None, [], {"files": []}).partial


def test_changed_list_is_capped_with_remainder():
    view = build_view(run("check", changed=[{"item": str(i)} for i in range(13)]), [], None, [], None)
    assert len(view.changed) == 10 and view.changed_more == 3


def test_read_jsonl_skips_partial_line(tmp_path: Path):
    path = tmp_path / "p.jsonl"
    path.write_text(json.dumps({"a": 1}) + "\n{\"b\":")
    assert read_jsonl(path) == [{"a": 1}]
    assert read_jsonl(tmp_path / "missing") == []


def current(**fields):
    return {"schema_version": 1, "current_run": {"mode": "apply", "phase": "running", "total": 10, "n": 4,
                                                 "task": "t", "log": "/l.log", **fields}}


def test_current_run_drives_running_and_percent():
    view = build_view(current(), [], None, [], None)
    assert (view.health, view.percent, view.indeterminate, view.current_log) == (Health.RUNNING, 40, False, Path("/l.log"))
    idle = build_view({"schema_version": 1, "current_run": None}, [{"event": "start"}], 1.0, [], None)
    assert idle.health is not Health.RUNNING


def test_indeterminate_for_launching_flag_or_unknown_total_and_stalled_is_distinct():
    for report in (current(phase="launching"), current(indeterminate=True), current(total=0)):
        view = build_view(report, [], None, [], None)
        assert view.indeterminate and view.percent is None
    stalled = build_view(current(phase="stalled"), [], None, [], None)
    assert stalled.health is Health.STALLED and "stalled" in stalled.headline


def test_current_run_prefers_fresher_progress_and_absent_fields_fall_back():
    progress = [{"event": "start", "total_estimate": 10}, {"event": "task", "n": 7, "task": "x"}]
    assert build_view(current(n=1), progress, 1.0, [], None).percent == 70
    fallback = build_view(None, [{"event": "launching", "mode": "check", "log": "/f.log"}], 1.0, [], None)
    assert fallback.health is Health.RUNNING and fallback.indeterminate and fallback.current_log == Path("/f.log")
    assert build_view({"last_run": {"result": "ok"}}, [{"event": "start"}], 1.0, [], None).health is Health.RUNNING


def test_lock_just_taken_reports_all_nulls_as_running_indeterminate(tmp_path):
    report = {"schema_version": 1, "current_run": {"run_id": None, "mode": None, "started": None, "log": None,
                                                   "task": None, "n": 0, "total": None}}
    view = build_view(report, [], None, [], None)
    assert (view.health, view.percent, view.indeterminate, view.current_log) == (Health.RUNNING, None, True, None)
    newest = tmp_path / "20260101-000000-apply.log"
    newest.write_text("x\n")
    assert next_log_chunk(tmp_path, None, 0, view.current_log) == (newest, 2, "x\n", True)


def test_unknown_schema_asks_for_an_update():
    view = build_view({"schema_version": 2}, [], None, [], None)
    assert view.health is Health.UNKNOWN and "Update tenon-ui" in view.headline
    assert parse_capabilities('{"schema_version": 2, "capabilities": []}') is None
    assert password_outcome(75) == "busy"


def test_log_chunk_prefers_current_log(tmp_path):
    (tmp_path / "2-new.log").write_text("n\n")
    mine = tmp_path / "1-mine.log"
    mine.write_text("m\n")
    assert next_log_chunk(tmp_path, None, 0, mine)[:3] == (mine, 2, "m\n")
