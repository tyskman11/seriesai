import uuid

import pytest

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.raw_video_cleanup import RawVideoCleanupService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded(tmp_path, *, audio_done=True, scenes_done=True):
    database = Database(tmp_path / "series.db")
    database.initialize()
    video = tmp_path / "e1.mp4"
    video.write_bytes(b"fake-mp4")
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO episodes(episode_id,series_id,season_number,episode_number,title,source_path) VALUES('demo_s01e01','demo',1,1,'E1',?)", (str(video),))
        if audio_done:
            connection.execute("INSERT INTO import_artifacts(artifact_id,episode_id,artifact_type,file_path,status) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), "demo_s01e01", "audio_16khz_mono", "audio.flac", "completed"))
        if scenes_done:
            connection.execute("INSERT INTO analysis_runs(analysis_run_id,episode_id,stage_name,status) VALUES(?,?,?,?)", (str(uuid.uuid4()), "demo_s01e01", "scene_detection", "completed"))
    return database, video


def test_cleanup_blocked_when_audio_extraction_not_done(tmp_path):
    database, video = _seeded(tmp_path, audio_done=False)
    service = RawVideoCleanupService(_config(tmp_path), database)
    with pytest.raises(ValueError, match="Audio-Extraktion"):
        service.cleanup("demo_s01e01", confirm=True)
    assert video.is_file()


def test_cleanup_blocked_when_scene_detection_not_done(tmp_path):
    database, video = _seeded(tmp_path, scenes_done=False)
    service = RawVideoCleanupService(_config(tmp_path), database)
    with pytest.raises(ValueError, match="Szenenerkennung"):
        service.cleanup("demo_s01e01", confirm=True)
    assert video.is_file()


def test_cleanup_without_confirm_is_a_dry_run(tmp_path):
    database, video = _seeded(tmp_path)
    service = RawVideoCleanupService(_config(tmp_path), database)
    result = service.cleanup("demo_s01e01", confirm=False)
    assert result["action"] == "dry_run"
    assert video.is_file()


def test_cleanup_deletes_and_marks_source_deleted_once_ready(tmp_path):
    database, video = _seeded(tmp_path)
    service = RawVideoCleanupService(_config(tmp_path), database)
    result = service.cleanup("demo_s01e01", confirm=True)
    assert result["action"] == "deleted"
    assert not video.is_file()
    with database.connect() as connection:
        assert connection.execute("SELECT source_deleted FROM episodes WHERE episode_id='demo_s01e01'").fetchone()[0] == 1
    # Calling again is a no-op, not an error.
    second = service.cleanup("demo_s01e01", confirm=True)
    assert second["action"] == "none"
