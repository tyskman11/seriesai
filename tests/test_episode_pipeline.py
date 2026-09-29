import json
import uuid
from dataclasses import dataclass

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services import episode_pipeline as pipeline_module
from seriesai.services.episode_pipeline import EpisodePipelineService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
    return database


def _insert_shots(database, shot_job_id, statuses):
    with database.connect() as connection:
        connection.execute("INSERT OR IGNORE INTO generation_jobs(job_id,series_id,job_type,status,payload_json) VALUES(?,?,?,?,?)", (shot_job_id, "demo", "draft_shot_queue", "running", "{}"))
        for index, status in enumerate(statuses, start=1):
            connection.execute("INSERT INTO generation_job_items(item_id,job_id,item_type,sequence_number,status) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), shot_job_id, "shot", index, status))


@dataclass
class FakeMuxResult:
    sequence_number: int
    status: str


class FakePlanner:
    def __init__(self, config, database): self.database = database
    def plan(self, series_id, idea, duration_minutes, notes):
        outline_path = self.database.path.parent / "outline.json"
        outline_path.write_text(json.dumps({"outline": {"title": "Der Testtitel"}}), encoding="utf-8")
        return {"outline_path": str(outline_path)}


class FakeContinuityChecker:
    def check_document(self, outline_path): return {"issues": []}


class FakeShotQueue:
    def __init__(self, config, database): self.shot_job_id = "shotjob-" + str(uuid.uuid4())[:8]
    def create(self, outline_path):
        return {"job_id": self.shot_job_id, "shot_count": 2}


class FakeDialogueMux:
    def __init__(self, config, database): pass
    def process_job(self, shot_job_id): return [FakeMuxResult(1, "skipped_no_dialogue"), FakeMuxResult(2, "skipped_no_dialogue")]


class FakeEditor:
    def __init__(self, config, database): pass
    def assemble(self, shot_job_id, episode_code):
        return {"status": "assembled", "episode_path": f"/fake/{episode_code}/episode.mp4", "shot_count": 2}


def _patch_happy_path(monkeypatch, database, shot_statuses=("completed", "completed")):
    queue = FakeShotQueue(None, database)
    _insert_shots(database, queue.shot_job_id, shot_statuses)
    monkeypatch.setattr(pipeline_module, "EpisodePlannerService", FakePlanner)
    monkeypatch.setattr(pipeline_module, "ContinuityChecker", FakeContinuityChecker)
    monkeypatch.setattr(pipeline_module, "ShotQueueService", lambda config, database: queue)
    monkeypatch.setattr(pipeline_module, "DialogueMuxService", FakeDialogueMux)
    monkeypatch.setattr(pipeline_module, "EpisodeEditorService", FakeEditor)
    return queue.shot_job_id


def test_run_completes_and_persists_a_completed_checkpoint(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    shot_job_id = _patch_happy_path(monkeypatch, database)
    service = EpisodePipelineService(_config(tmp_path), database, poll_seconds=0.01)
    stages = []
    result = service.run("demo", "eine Idee", 2, "S01E01", progress=lambda stage, detail: stages.append(stage))
    assert result["job_id"] == shot_job_id
    assert result["episode_path"] == "/fake/S01E01/episode.mp4"
    assert "planning" in stages and "rendering" in stages and "dubbing" in stages and "assembling" in stages
    with database.connect() as connection:
        row = connection.execute("SELECT status,checkpoint_json FROM generation_jobs WHERE job_type='episode_pipeline'").fetchone()
    assert row["status"] == "completed"
    assert json.loads(row["checkpoint_json"])["result"]["episode_path"] == "/fake/S01E01/episode.mp4"


def test_run_blocks_on_continuity_errors_and_marks_job_failed(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    monkeypatch.setattr(pipeline_module, "EpisodePlannerService", FakePlanner)
    class FailingChecker:
        def check_document(self, outline_path): return {"issues": [{"severity": "error", "description": "Unbekannte Figur"}]}
    monkeypatch.setattr(pipeline_module, "ContinuityChecker", FailingChecker)
    service = EpisodePipelineService(_config(tmp_path), database, poll_seconds=0.01)
    try:
        service.run("demo", "eine Idee", 2, "S01E01")
        assert False, "expected RuntimeError"
    except RuntimeError as error:
        assert "Kontinuitaetsfehler" in str(error)
    with database.connect() as connection:
        row = connection.execute("SELECT status,error_message FROM generation_jobs WHERE job_type='episode_pipeline'").fetchone()
    assert row["status"] == "failed"
    assert "Kontinuitaetsfehler" in row["error_message"]


def test_run_fails_when_shots_end_in_failure_without_assembling(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    shot_job_id = _patch_happy_path(monkeypatch, database, shot_statuses=("completed", "failed"))
    called = {"assembled": False}
    class NoCallEditor(FakeEditor):
        def assemble(self, *a, **k): called["assembled"] = True; return super().assemble(*a, **k)
    monkeypatch.setattr(pipeline_module, "EpisodeEditorService", NoCallEditor)
    service = EpisodePipelineService(_config(tmp_path), database, poll_seconds=0.01)
    try:
        service.run("demo", "eine Idee", 2, "S01E01")
        assert False, "expected RuntimeError"
    except RuntimeError as error:
        assert "fehlgeschlagen" in str(error)
    assert called["assembled"] is False


def test_run_resumes_from_a_saved_checkpoint_without_replanning(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    shot_job_id = "resumed-shot-job"
    _insert_shots(database, shot_job_id, ("completed", "completed"))

    def planner_should_not_be_called(*args, **kwargs):
        raise AssertionError("planner must not run again on resume")
    monkeypatch.setattr(pipeline_module, "EpisodePlannerService", planner_should_not_be_called)
    monkeypatch.setattr(pipeline_module, "ContinuityChecker", FakeContinuityChecker)
    monkeypatch.setattr(pipeline_module, "DialogueMuxService", FakeDialogueMux)
    monkeypatch.setattr(pipeline_module, "EpisodeEditorService", FakeEditor)

    service = EpisodePipelineService(_config(tmp_path), database, poll_seconds=0.01)
    job_id = str(uuid.uuid4())
    result = service.run("demo", "eine Idee", 2, "S01E01", job_id=job_id, resume_checkpoint={"stage": "rendering", "shot_job_id": shot_job_id, "title": "Alter Titel"})
    assert result["job_id"] == shot_job_id
    assert result["episode_path"] == "/fake/S01E01/episode.mp4"


def test_resume_pending_continues_the_interrupted_job_and_fails_ones_without_a_shot_queue(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    shot_job_id = "resumed-shot-job-2"
    _insert_shots(database, shot_job_id, ("completed",))
    monkeypatch.setattr(pipeline_module, "ContinuityChecker", FakeContinuityChecker)
    monkeypatch.setattr(pipeline_module, "DialogueMuxService", FakeDialogueMux)
    monkeypatch.setattr(pipeline_module, "EpisodeEditorService", FakeEditor)

    lost_job_id, resumable_job_id = str(uuid.uuid4()), str(uuid.uuid4())
    with database.connect() as connection:
        connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json,checkpoint_json) VALUES(?,?,?,?,?,?)",
                            (lost_job_id, "demo", "episode_pipeline", "running", json.dumps({"idea": "x", "duration_minutes": 2, "episode_code": "LOST"}), json.dumps({"stage": "planning"})))
        connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json,checkpoint_json) VALUES(?,?,?,?,?,?)",
                            (resumable_job_id, "demo", "episode_pipeline", "running", json.dumps({"idea": "x", "duration_minutes": 2, "episode_code": "RESUMED"}), json.dumps({"stage": "rendering", "shot_job_id": shot_job_id})))

    service = EpisodePipelineService(_config(tmp_path), database, poll_seconds=0.01)
    result = service.resume_pending()
    assert result["job_id"] == shot_job_id
    assert result["episode_path"] == "/fake/RESUMED/episode.mp4"
    with database.connect() as connection:
        lost = connection.execute("SELECT status,error_message FROM generation_jobs WHERE job_id=?", (lost_job_id,)).fetchone()
        resumed = connection.execute("SELECT status FROM generation_jobs WHERE job_id=?", (resumable_job_id,)).fetchone()
    assert lost["status"] == "failed" and "Shot-Queue" in lost["error_message"]
    assert resumed["status"] == "completed"
