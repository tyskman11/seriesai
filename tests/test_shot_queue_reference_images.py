import json
import uuid

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.shot_queue import ShotQueueService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded_database(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('a_001','demo','A')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('b_001','demo','B')")
        connection.execute(
            "INSERT INTO visual_references(reference_id,entity_type,entity_id,reference_kind,file_path,quality_score) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), "character", "a_001", "approved_face", "Series/voices_faces/a_001.jpg", 0.9),
        )
    return database


def _outline(tmp_path, scenes):
    path = tmp_path / "episode_outline.json"
    path.write_text(json.dumps({"series_id": "demo", "status": "continuity_checked", "continuity_issues": [], "outline": {"scenes": scenes}}), encoding="utf-8")
    return path


def test_create_attaches_the_scenes_first_reviewed_character_as_reference_image(tmp_path):
    database = _seeded_database(tmp_path)
    scenes = [{"sequence_number": 1, "location": "Studio", "character_ids": ["a_001", "b_001"], "duration_seconds": 5, "purpose": "p", "conflict_or_turn": "c", "dialogue": []}]
    outline_path = _outline(tmp_path, scenes)
    result = ShotQueueService(_config(tmp_path), database).create(outline_path)
    with database.connect() as connection:
        spec = json.loads(connection.execute("SELECT checkpoint_json FROM generation_job_items WHERE job_id=?", (result["job_id"],)).fetchone()[0])
    assert spec["reference_image"] == "Series/voices_faces/a_001.jpg"


def test_create_attaches_the_scenes_trained_lora_preferring_video_over_image(tmp_path):
    database = _seeded_database(tmp_path)
    with database.connect() as connection:
        connection.execute("INSERT INTO model_registry(model_id,model_type,backend_name,local_path,active) VALUES('lora_image:a_001','lora_image','comfyui','a_001_image.safetensors',1)")
        connection.execute("INSERT INTO model_registry(model_id,model_type,backend_name,local_path,active) VALUES('lora_video:a_001','lora_video','comfyui','a_001_video.safetensors',1)")
    scenes = [{"sequence_number": 1, "location": "Studio", "character_ids": ["a_001"], "duration_seconds": 5, "purpose": "p", "conflict_or_turn": "c", "dialogue": []}]
    outline_path = _outline(tmp_path, scenes)
    result = ShotQueueService(_config(tmp_path), database).create(outline_path)
    with database.connect() as connection:
        spec = json.loads(connection.execute("SELECT checkpoint_json FROM generation_job_items WHERE job_id=?", (result["job_id"],)).fetchone()[0])
    assert spec["lora_name"] == "a_001_video.safetensors"


def test_create_skips_reference_image_when_no_scene_character_has_one(tmp_path):
    database = _seeded_database(tmp_path)
    scenes = [{"sequence_number": 1, "location": "Studio", "character_ids": ["b_001"], "duration_seconds": 5, "purpose": "p", "conflict_or_turn": "c", "dialogue": []}]
    outline_path = _outline(tmp_path, scenes)
    result = ShotQueueService(_config(tmp_path), database).create(outline_path)
    with database.connect() as connection:
        spec = json.loads(connection.execute("SELECT checkpoint_json FROM generation_job_items WHERE job_id=?", (result["job_id"],)).fetchone()[0])
    assert "reference_image" not in spec
