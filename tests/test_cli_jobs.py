import time

from seriesai import cli_jobs


def test_describe_commands_covers_every_cli_subcommand():
    names = {item["name"] for item in cli_jobs.describe_commands()}
    assert {"doctor", "plan-episode", "generate-episode", "assign-voice", "import"} <= names
    doctor = next(item for item in cli_jobs.describe_commands() if item["name"] == "doctor")
    assert doctor["args"] == []


def test_describe_commands_marks_flags_positionals_and_booleans():
    commands = {item["name"]: item for item in cli_jobs.describe_commands()}
    series_id = next(arg for arg in commands["cluster-voices"]["args"] if arg["dest"] == "series_id")
    assert series_id == {"dest": "series_id", "flag": "--series-id", "kind": "store_value", "required": True, "help": "", "default": None, "type": "text"}
    force = next(arg for arg in commands["cluster-voices"]["args"] if arg["dest"] == "force")
    assert force["kind"] == "store_true" and force["required"] is False
    source = next(arg for arg in commands["import"]["args"] if arg["dest"] == "source")
    assert source["kind"] == "positional" and source["type"] == "path"


def test_start_rejects_unknown_command():
    try:
        cli_jobs.start("not-a-real-command", {})
        assert False, "expected ValueError"
    except ValueError as error:
        assert "Unknown command" in str(error)


def test_start_rejects_missing_required_argument():
    try:
        cli_jobs.start("cluster-voices", {})
        assert False, "expected ValueError"
    except ValueError as error:
        assert "series_id" in str(error) or "--series-id" in str(error)


def test_start_runs_a_real_quick_command_and_tracks_it_until_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_jobs, "JOBS_DIR", tmp_path / "cli_jobs")
    result = cli_jobs.start("doctor", {})
    assert "job_id" in result and result["argv"] == ["doctor"]
    for _ in range(100):
        status = cli_jobs.status(result["job_id"])
        if status["status"] != "running":
            break
        time.sleep(0.1)
    assert status["status"] == "completed"
    assert status["returncode"] == 0
    assert '"platform"' in status["log_tail"]
    history = cli_jobs.recent()
    assert any(job["job_id"] == result["job_id"] for job in history)


def test_start_builds_flags_and_positionals_in_declared_order(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_jobs, "JOBS_DIR", tmp_path / "cli_jobs")
    result = cli_jobs.start("cluster-voices", {"series_id": "demo", "force": True})
    assert result["argv"] == ["cluster-voices", "--series-id", "demo", "--force"]
    for _ in range(50):
        if cli_jobs.status(result["job_id"])["status"] != "running": break
        time.sleep(0.1)
