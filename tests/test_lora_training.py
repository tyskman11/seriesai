import subprocess
import uuid
from pathlib import Path

import pytest

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.lora_training import LoraTrainingService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded(tmp_path, *, source_deleted=False):
    database = Database(tmp_path / "series.db")
    database.initialize()
    video = tmp_path / "e1.mp4"; video.write_bytes(b"fake-mp4")
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('hudson_001','demo','Hudson')")
        connection.execute("INSERT INTO episodes(episode_id,series_id,season_number,episode_number,title,source_path,source_deleted) VALUES('demo_s01e01','demo',1,1,'E1',?,?)", (str(video), int(source_deleted)))
        connection.execute("INSERT INTO scenes(scene_id,episode_id,sequence_number,start_seconds,end_seconds) VALUES('sc1','demo_s01e01',1,10.0,20.0)")
        connection.execute("INSERT INTO character_events(event_id,character_id,episode_id,scene_id,occurred_at,event_type,description) VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), "hudson_001", "demo_s01e01", "sc1", "2020-01-01", "appearance", "Hudson appears"))
    return database


def test_prepare_video_clips_cuts_one_clip_per_scene(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    service = LoraTrainingService(_config(tmp_path), database)
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = service.prepare_video_clips("hudson_001")
    assert result["clip_count"] == 1
    assert len(calls) == 1
    with database.connect() as connection:
        row = connection.execute("SELECT status FROM training_datasets WHERE dataset_id='character_lora_video:hudson_001'").fetchone()
    assert row["status"] == "ready"


def test_prepare_video_clips_skips_episodes_with_deleted_source(tmp_path, monkeypatch):
    database = _seeded(tmp_path, source_deleted=True)
    service = LoraTrainingService(_config(tmp_path), database)
    with pytest.raises(ValueError, match="deleted or missing"):
        service.prepare_video_clips("hudson_001")


def test_prepare_video_clips_requires_scene_level_appearances(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('hudson_001','demo','Hudson')")
    service = LoraTrainingService(_config(tmp_path), database)
    with pytest.raises(ValueError, match="character_events"):
        service.prepare_video_clips("hudson_001")
