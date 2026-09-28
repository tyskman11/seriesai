import json

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.episode_commit import EpisodeCommitService


def test_commit_applies_reviewed_payload_after_backup(tmp_path):
    root = tmp_path; series_root = root / "Series"; output = series_root / "Generated" / "S01E02"; output.mkdir(parents=True)
    (output / "episode.mp4").write_bytes(b"test")
    (output / "metadata.json").write_text("{}", encoding="utf-8")
    (output / "commit_payload.json").write_text(json.dumps({
        "review_note": "reviewed", "world_events": [{"event_type": "test", "description": "A fact", "occurred_at": "2026-01-01"}],
        "character_state_changes": [{"character_id": "a_001", "field_name": "job", "new_value": "builder"}],
        "memories": [{"character_id": "a_001", "memory_type": "core", "description": "A fact", "importance": .9, "emotional_weight": .8}],
        "relationship_updates": [], "open_story_arcs": [{"title": "Next", "description": "Continue", "priority": .6}],
    }), encoding="utf-8")
    database = Database(series_root / "series.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('a_001','demo','A')")
    config = AppConfig(paths=PathsConfig(project_root=root, series_root=series_root, comfyui=root / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())
    result = EpisodeCommitService(config, database).commit("demo", "demo_s01e02", "S01E02", "2026-01-01")
    assert result["applied"] == {"world_events": 1, "character_state_changes": 1, "memories": 1, "relationship_updates": 0, "open_story_arcs": 1}
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM character_memories").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM open_story_arcs").fetchone()[0] == 1
