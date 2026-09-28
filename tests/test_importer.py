import json
from pathlib import Path

from seriesai.config import AppConfig
from seriesai.db import Database
from seriesai.services import EpisodeImporter, ImportResult


def test_episode_name_and_existing_import_resume(tmp_path, monkeypatch):
    source = tmp_path / "Demo - S01E01.mp4"
    source.write_bytes(b"not a real video")
    config = AppConfig.model_validate({"paths": {"project_root": str(tmp_path), "series_root": str(tmp_path / "Series"), "comfyui": str(tmp_path / "ComfyUI")}, "hardware": {"gpu_vram_gb": 6, "system_ram_gb": 32}, "models": {}})
    database = Database(config.paths.series_root / "series.db")
    database.initialize()
    importer = EpisodeImporter(config, database)
    monkeypatch.setattr(importer, "_ffprobe", lambda _: {"format": {"duration": "1"}})
    monkeypatch.setattr(importer, "_extract_audio", lambda _, output: output.write_bytes(b"audio"))
    first = importer.import_episode(source, "demo", "Demo")
    second = importer.import_episode(source, "demo", "Demo")
    assert first.status == "imported"
    assert second.status == "already_imported"
    assert first.episode_id == "demo_s01e01"


def test_folder_import_uses_episode_order(tmp_path, monkeypatch):
    config = AppConfig.model_validate({"paths": {"project_root": str(tmp_path), "series_root": str(tmp_path / "Series"), "comfyui": str(tmp_path / "ComfyUI")}, "hardware": {"gpu_vram_gb": 6, "system_ram_gb": 32}, "models": {}})
    database = Database(config.paths.series_root / "series.db")
    database.initialize()
    importer = EpisodeImporter(config, database)
    folder = tmp_path / "input"
    folder.mkdir()
    for name in ("Demo S01E02.mp4", "Demo S01E01.mp4"):
        (folder / name).write_bytes(name.encode())
    monkeypatch.setattr(importer, "_ffprobe", lambda _: {})
    monkeypatch.setattr(importer, "_extract_audio", lambda _, output: output.write_bytes(b"audio"))
    assert [item.episode_id for item in importer.import_folder(folder, "demo", "Demo")] == ["demo_s01e01", "demo_s01e02"]


def test_folder_import_continues_after_single_failure(tmp_path, monkeypatch):
    config = AppConfig.model_validate({"paths": {"project_root": str(tmp_path), "series_root": str(tmp_path / "Series"), "comfyui": str(tmp_path / "ComfyUI")}, "hardware": {"gpu_vram_gb": 6, "system_ram_gb": 32}, "models": {}})
    database = Database(config.paths.series_root / "series.db")
    database.initialize()
    importer = EpisodeImporter(config, database)
    folder = tmp_path / "input"
    folder.mkdir()
    (folder / "Demo S01E01.mp4").write_bytes(b"bad")
    (folder / "Demo S01E02.mp4").write_bytes(b"good")
    monkeypatch.setattr(importer, "import_episode", lambda path, *args: (_ for _ in ()).throw(ValueError("bad file")) if "E01" in str(path) else ImportResult("demo_s01e02", "hash", "imported", Path(path)))
    assert [result.status for result in importer.import_folder(folder, "demo", "Demo")] == ["failed", "imported"]
    assert "bad file" in (tmp_path / "logs" / "import.log").read_text(encoding="utf-8")


def test_existing_episode_resumes_missing_audio(tmp_path, monkeypatch):
    config = AppConfig.model_validate({"paths": {"project_root": str(tmp_path), "series_root": str(tmp_path / "Series"), "comfyui": str(tmp_path / "ComfyUI")}, "hardware": {"gpu_vram_gb": 6, "system_ram_gb": 32}, "models": {}})
    database = Database(config.paths.series_root / "series.db")
    database.initialize()
    importer = EpisodeImporter(config, database)
    source = tmp_path / "Demo S01E01.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(importer, "_ffprobe", lambda _: {})
    monkeypatch.setattr(importer, "_extract_audio", lambda _, output: output.write_bytes(b"audio"))
    importer.import_episode(source, "demo", "Demo")
    with database.connect() as connection:
        connection.execute("DELETE FROM import_artifacts WHERE artifact_type = 'audio_16khz_mono'")
    assert importer.import_episode(source, "demo", "Demo").status == "resumed"
