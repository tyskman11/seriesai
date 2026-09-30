import json
import threading
import time
import uuid

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services import episode_pipeline as pipeline_module
from seriesai.services.episode_pipeline import EpisodePipelineService
from tests.test_episode_pipeline import FakeContinuityChecker, FakeDialogueMux, FakeEditor, FakePlanner, FakeShotQueue, _insert_shots


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
    return database


def test_enqueue_never_blocks_and_reports_queue_position(tmp_path):
    database = _seeded(tmp_path)
    service = EpisodePipelineService(_config(tmp_path), database)
    first = service.enqueue("demo", "Idee eins", 2, "S01E01")
    second = service.enqueue("demo", "Idee zwei", 2, "S01E02")
    assert first["position"] == 1
    assert second["position"] == 2
    with database.connect() as connection:
        statuses = {row["job_id"]: row["status"] for row in connection.execute("SELECT job_id,status FROM generation_jobs WHERE job_type='episode_pipeline'")}
    assert statuses[first["job_id"]] == "pending" and statuses[second["job_id"]] == "pending"


def test_queue_snapshot_orders_running_then_pending_then_recent(tmp_path):
    database = _seeded(tmp_path)
    service = EpisodePipelineService(_config(tmp_path), database)
    service.enqueue("demo", "Idee eins", 2, "S01E01")
    service.enqueue("demo", "Idee zwei", 2, "S01E02")
    snapshot = service.queue_snapshot()
    assert [item["episode_code"] for item in snapshot] == ["S01E01", "S01E02"]
    assert all(item["status"] == "pending" for item in snapshot)


def test_run_forever_processes_the_queue_one_at_a_time_and_stops_when_empty(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    shot_jobs = {}
    def make_shot_queue(config, database):
        job_id = "shotjob-" + str(uuid.uuid4())[:8]
        with database.connect() as connection:
            connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json) VALUES(?,?,?,?,?)", (job_id, "demo", "draft_shot_queue", "running", "{}"))
            connection.execute("INSERT INTO generation_job_items(item_id,job_id,item_type,sequence_number,status) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), job_id, "shot", 1, "completed"))
        shot_jobs[job_id] = True
        class Q:
            def create(self, outline_path): return {"job_id": job_id, "shot_count": 1}
        return Q()
    monkeypatch.setattr(pipeline_module, "EpisodePlannerService", FakePlanner)
    monkeypatch.setattr(pipeline_module, "ContinuityChecker", FakeContinuityChecker)
    monkeypatch.setattr(pipeline_module, "ShotQueueService", make_shot_queue)
    monkeypatch.setattr(pipeline_module, "DialogueMuxService", FakeDialogueMux)
    monkeypatch.setattr(pipeline_module, "EpisodeEditorService", FakeEditor)

    service = EpisodePipelineService(_config(tmp_path), database, poll_seconds=0.01)
    service.enqueue("demo", "Idee eins", 2, "S01E01")
    service.enqueue("demo", "Idee zwei", 2, "S01E02")

    stages_seen = []
    def worker():
        try: service.run_forever(progress=lambda stage, detail: stages_seen.append(stage), poll_seconds=0.02)
        except Exception: pass
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    for _ in range(100):
        with database.connect() as connection:
            done = connection.execute("SELECT COUNT(*) FROM generation_jobs WHERE job_type='episode_pipeline' AND status='completed'").fetchone()[0]
        if done == 2: break
        time.sleep(0.05)
    with database.connect() as connection:
        statuses = [row["status"] for row in connection.execute("SELECT status FROM generation_jobs WHERE job_type='episode_pipeline' ORDER BY payload_json")]
    assert statuses.count("completed") == 2
    assert len(shot_jobs) == 2  # each queued episode got its own shot queue, processed one after another
