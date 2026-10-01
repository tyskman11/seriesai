import uuid

import cv2

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.character_face_dataset import CharacterFaceDatasetService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('hudson_001','demo','Hudson')")
        connection.execute("INSERT INTO character_candidates(candidate_id,series_id,canonical_name,confidence,evidence_json,source_signature,status) VALUES('c1','demo','Hudson',0.8,'[]','test','approved')")
        connection.execute(
            "INSERT INTO visual_references(reference_id,entity_type,entity_id,reference_kind,file_path,quality_score) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), "character_candidate", "c1", "review_face", "Series/demo/cache/imports/e1/anchor.jpg", 0.9),
        )
        connection.execute("INSERT INTO episodes(episode_id,series_id,season_number,episode_number,title,source_path) VALUES('demo_s01e01','demo',1,1,'E1','x.mp4')")
        connection.execute("INSERT INTO import_artifacts(artifact_id,episode_id,artifact_type,file_path,status) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), "demo_s01e01", "audio_16khz_mono", "Series/demo/cache/imports/e1/audio.flac", "completed"))
    frame_dir = tmp_path / "Series" / "demo" / "cache" / "imports" / "e1" / "scene_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    # The fake embedding below is just the first byte of the file's content, so a "matching"
    # frame must start with the same byte as the anchor image to be treated as the same person.
    (frame_dir / "match.jpg").write_bytes(b"another-frame-of-the-same-person")
    (frame_dir / "stranger.jpg").write_bytes(b"someone-else-entirely")
    (tmp_path / "Series" / "demo" / "cache" / "imports" / "e1" / "anchor.jpg").write_bytes(b"anchor-face-crop")
    return database


# Each fake "image" is just the file's bytes, read through a monkeypatched cv2.imread so the
# test never needs a real detector/recognizer model file. _embed_best_face below decodes that
# marker back into a numeric "embedding" (its first byte) so the real scoring/thresholding
# logic in collect_training_images still runs unmodified.
def _fake_imread(path):
    return bytes(open(path, "rb").read())


def test_collect_training_images_keeps_only_frames_matching_the_anchor_identity(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    service = CharacterFaceDatasetService(_config(tmp_path), database)
    monkeypatch.setattr(cv2, "imread", _fake_imread)
    monkeypatch.setattr(service, "_load_detector", lambda: object())
    monkeypatch.setattr(service, "_load_recognizer", lambda: type("FakeRecognizer", (), {"match": staticmethod(lambda a, b, const: 1.0 if a == b else 0.0)})())
    monkeypatch.setattr(service, "_embed_best_face", lambda image, detector, recognizer: (image[:1], (0, 0, 10, 10)))
    monkeypatch.setattr(service, "_save_crop", staticmethod(lambda *a: None))

    result = service.collect_training_images("hudson_001", target_count=10)

    assert result["collected"] == 1
    with database.connect() as connection:
        rows = connection.execute("SELECT file_path,source_episode_id FROM visual_references WHERE entity_type='character' AND entity_id='hudson_001' AND reference_kind='training_crop'").fetchall()
    assert len(rows) == 1
    assert rows[0]["source_episode_id"] == "demo_s01e01"


def test_collect_training_images_raises_without_a_reviewed_anchor(tmp_path, monkeypatch):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('hudson_001','demo','Hudson')")
    service = CharacterFaceDatasetService(_config(tmp_path), database)
    try:
        service.collect_training_images("hudson_001")
        assert False, "expected ValueError"
    except ValueError:
        pass
