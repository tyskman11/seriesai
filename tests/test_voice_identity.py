import uuid

import pytest

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.voice_identity import VoiceIdentityService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('babe_001','demo','Babe')")
        for cluster_id in ("vc_001", "vc_002"):
            connection.execute("INSERT INTO voice_clusters(cluster_id,series_id,segment_count,total_seconds,centroid) VALUES(?,?,10,60.0,?)", (cluster_id, "demo", b"\x00"))
    return database


def test_assign_cluster_allows_a_second_cluster_for_the_same_character(tmp_path):
    database = _seeded(tmp_path)
    service = VoiceIdentityService(_config(tmp_path), database)
    service.assign_cluster("vc_001", "babe_001")
    service.assign_cluster("vc_002", "babe_001")  # must not be rejected as "already has a voice cluster"
    with database.connect() as connection:
        rows = connection.execute("SELECT cluster_id,status FROM voice_clusters WHERE character_id='babe_001'").fetchall()
    assert {row["cluster_id"] for row in rows} == {"vc_001", "vc_002"}
    assert all(row["status"] == "approved" for row in rows)


def test_assign_cluster_rejects_unknown_character(tmp_path):
    database = _seeded(tmp_path)
    with pytest.raises(ValueError, match="Unknown approved character"):
        VoiceIdentityService(_config(tmp_path), database).assign_cluster("vc_001", "nope_001")


def test_assign_cluster_rejects_unknown_cluster(tmp_path):
    database = _seeded(tmp_path)
    with pytest.raises(ValueError, match="Unknown voice cluster"):
        VoiceIdentityService(_config(tmp_path), database).assign_cluster("vc_missing", "babe_001")


def test_reject_cluster_clears_character_and_can_be_reassigned(tmp_path):
    database = _seeded(tmp_path)
    service = VoiceIdentityService(_config(tmp_path), database)
    service.assign_cluster("vc_001", "babe_001")
    service.reject_cluster("vc_001")
    with database.connect() as connection:
        row = connection.execute("SELECT character_id,status FROM voice_clusters WHERE cluster_id='vc_001'").fetchone()
    assert row["character_id"] is None and row["status"] == "rejected"
    service.assign_cluster("vc_001", "babe_001")  # a rejected cluster can still be assigned later
    with database.connect() as connection:
        row = connection.execute("SELECT status FROM voice_clusters WHERE cluster_id='vc_001'").fetchone()
    assert row["status"] == "approved"


def test_build_references_combines_multiple_clusters_of_one_character_instead_of_overwriting(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    service = VoiceIdentityService(_config(tmp_path), database)
    service.assign_cluster("vc_001", "babe_001")
    service.assign_cluster("vc_002", "babe_001")

    # Two fake segments per cluster; _cut just drops an empty placeholder file instead of calling ffmpeg.
    segments_by_cluster = {
        "vc_001": [{"episode_id": "e1", "start_seconds": 0.0, "end_seconds": 5.0, "segment_id": "s1"}, {"episode_id": "e1", "start_seconds": 5.0, "end_seconds": 10.0, "segment_id": "s2"}],
        "vc_002": [{"episode_id": "e2", "start_seconds": 0.0, "end_seconds": 5.0, "segment_id": "s3"}, {"episode_id": "e2", "start_seconds": 5.0, "end_seconds": 10.0, "segment_id": "s4"}],
    }
    monkeypatch.setattr(service, "_best_segments", lambda cluster_id, count, **kw: segments_by_cluster[cluster_id][:count])
    cuts: list[str] = []
    def fake_cut(episode_id, start, end, target, sample_rate):
        cuts.append(target.name); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b"")
    monkeypatch.setattr(service, "_cut", fake_cut)

    results = service.build_references("demo", clips=4)
    assert len(results) == 1 and results[0]["character_id"] == "babe_001"
    assert sorted(results[0]["cluster_ids"]) == ["vc_001", "vc_002"]
    assert results[0]["reference_clips"] == 4  # 2 clips from each cluster, not just the last cluster's
    reference_dir = tmp_path / "Series" / "voices" / "babe_001"
    assert sorted(p.name for p in reference_dir.glob("ref_*.wav")) == ["ref_01.wav", "ref_02.wav", "ref_03.wav", "ref_04.wav"]
    with database.connect() as connection:
        profile = connection.execute("SELECT backend_name FROM voice_profiles WHERE character_id='babe_001'").fetchone()
    assert profile["backend_name"] == "xtts"
