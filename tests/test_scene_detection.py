from pathlib import Path

from seriesai.services.scene_detection import SceneDetectionService


def test_scene_detection_returns_completed_result_for_precompleted_stage(tmp_path, monkeypatch):
    from seriesai.config import AppConfig
    from seriesai.db import Database

    config = AppConfig.model_validate({"paths": {"project_root": str(tmp_path), "series_root": str(tmp_path / "Series"), "comfyui": str(tmp_path / "ComfyUI")}, "hardware": {"gpu_vram_gb": 6, "system_ram_gb": 32}, "models": {}})
    database = Database(config.paths.series_root / "series.db")
    database.initialize()
    cache = tmp_path / "cache"
    cache.mkdir()
    metadata = cache / "ffprobe.json"
    metadata.write_text("{}")
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id, title) VALUES ('demo', 'Demo')")
        connection.execute("INSERT INTO episodes(episode_id, series_id, source_path, source_metadata_json) VALUES ('demo_s01e01', 'demo', 'video.mp4', ?)", (str(metadata),))
        connection.execute("INSERT INTO scenes(scene_id, episode_id, sequence_number) VALUES ('scene_1', 'demo_s01e01', 1)")
        connection.execute("INSERT INTO analysis_runs(analysis_run_id, episode_id, stage_name, status) VALUES ('run', 'demo_s01e01', 'scene_detection', 'completed')")
    result = SceneDetectionService(config, database).analyze_episode("demo_s01e01")
    assert result.status == "already_analyzed"
    assert result.scene_count == 1
