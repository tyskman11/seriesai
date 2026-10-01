import uuid

import numpy as np
import pytest

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.voice_training import VoiceTrainingService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _seeded(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('babe_001','demo','Babe')")
        connection.execute("INSERT INTO voice_clusters(cluster_id,series_id,character_id,segment_count,total_seconds,centroid,status) VALUES('vc1','demo','babe_001',2,10.0,?,'approved')", (np.zeros(4, dtype=np.float32).tobytes(),))
        connection.execute("INSERT INTO episodes(episode_id,series_id,season_number,episode_number,title,source_path) VALUES('demo_s01e01','demo',1,1,'E1','x.mp4')")
        connection.execute("INSERT INTO import_artifacts(artifact_id,episode_id,artifact_type,file_path,status) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), "demo_s01e01", "audio_16khz_mono", "audio.flac", "completed"))
        connection.execute("INSERT INTO transcripts(transcript_id,episode_id,backend_name,model_name,language,text_path,json_path,status) VALUES('t1','demo_s01e01','whisper','medium','de','t.txt','t.json','completed')")
        for index, (start, end, text) in enumerate([(0.0, 3.0, "Hallo Welt"), (5.0, 8.0, "Wie geht es dir")], start=1):
            connection.execute("INSERT INTO transcript_segments(segment_id,transcript_id,sequence_number,start_seconds,end_seconds,text) VALUES(?,?,?,?,?,?)", (f"seg{index}", "t1", index, start, end, text))
            connection.execute("INSERT INTO voice_embeddings(segment_id,episode_id,start_seconds,end_seconds,embedding,cluster_id) VALUES(?,?,?,?,?,?)", (f"seg{index}", "demo_s01e01", start, end, np.zeros(4, dtype=np.float32).tobytes(), "vc1"))
    return database


def test_prepare_dataset_cuts_every_assigned_segment_and_writes_metadata(tmp_path, monkeypatch):
    database = _seeded(tmp_path)
    service = VoiceTrainingService(_config(tmp_path), database)
    extracted = []
    monkeypatch.setattr(service, "_extract", lambda source, target, start, duration: (target.parent.mkdir(parents=True, exist_ok=True), target.write_bytes(b"wav"), extracted.append((start, duration)))[0])
    result = service.prepare_dataset("babe_001")
    assert result["clip_count"] == 2
    metadata = (tmp_path / "Series" / "datasets" / "babe_001" / "voice_finetune" / "metadata.csv").read_text(encoding="utf-8")
    assert "Hallo Welt" in metadata and "Wie geht es dir" in metadata
    assert len(extracted) == 2
    with database.connect() as connection:
        row = connection.execute("SELECT status FROM training_datasets WHERE dataset_id='voice_finetune:babe_001'").fetchone()
    assert row["status"] == "ready"


def test_prepare_dataset_requires_an_approved_cluster(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('babe_001','demo','Babe')")
    service = VoiceTrainingService(_config(tmp_path), database)
    with pytest.raises(ValueError, match="approved voice cluster"):
        service.prepare_dataset("babe_001")
