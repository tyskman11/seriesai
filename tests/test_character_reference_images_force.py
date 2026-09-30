import uuid

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.character_reference_images import CharacterReferenceImageService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute(
            "INSERT INTO character_candidates(candidate_id,series_id,canonical_name,confidence,evidence_json,source_signature) VALUES('c1','demo','Migo',0.8,'[\"demo_s01e01\"]','test')"
        )
        connection.execute("INSERT INTO episodes(episode_id,series_id,season_number,episode_number,title,source_path) VALUES('demo_s01e01','demo',1,1,'E1','x.mp4')")
        connection.execute("INSERT INTO import_artifacts(artifact_id,episode_id,artifact_type,file_path,status) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), "demo_s01e01", "audio_16khz_mono", "Series/demo/cache/imports/e1/audio.flac", "completed"))
    return database


def test_create_for_candidate_skips_when_reference_exists_without_force(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    service = CharacterReferenceImageService(_config(tmp_path), database)
    calls = []
    monkeypatch.setattr(service, "_best_face", lambda frame_dir: calls.append(1) or (5000.0, tmp_path / "cache" / "imports" / "e1" / "scene_frames" / "frame.jpg", (0, 0, 50, 50)))
    monkeypatch.setattr(service, "_save_crop", staticmethod(lambda *a: None))
    assert service.create_for_candidate("c1", ["demo_s01e01"]) is True
    assert len(calls) == 1
    assert service.create_for_candidate("c1", ["demo_s01e01"]) is False  # already has one; not re-detected
    assert len(calls) == 1


def test_create_for_candidate_with_force_redetects_and_updates(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    service = CharacterReferenceImageService(_config(tmp_path), database)
    monkeypatch.setattr(service, "_best_face", lambda frame_dir: (5000.0, tmp_path / "cache" / "imports" / "e1" / "scene_frames" / "frame.jpg", (0, 0, 50, 50)))
    monkeypatch.setattr(service, "_save_crop", staticmethod(lambda *a: None))
    assert service.create_for_candidate("c1", ["demo_s01e01"]) is True
    with database.connect() as connection:
        first_id = connection.execute("SELECT reference_id FROM visual_references WHERE entity_id='c1'").fetchone()["reference_id"]
    assert service.create_for_candidate("c1", ["demo_s01e01"], force=True) is True
    with database.connect() as connection:
        rows = connection.execute("SELECT reference_id FROM visual_references WHERE entity_id='c1' AND reference_kind='review_face'").fetchall()
    assert len(rows) == 1 and rows[0]["reference_id"] == first_id  # updated in place, not duplicated


def test_create_for_candidate_returns_false_when_no_face_found(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    service = CharacterReferenceImageService(_config(tmp_path), database)
    monkeypatch.setattr(service, "_best_face", lambda frame_dir: None)
    assert service.create_for_candidate("c1", ["demo_s01e01"]) is False
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM visual_references WHERE entity_id='c1'").fetchone()[0] == 0
