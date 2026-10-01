from pathlib import Path

import pytest

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.voice_synthesis import VoiceSynthesisService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig(tts_backend="xtts"))


def test_voice_synthesis_refuses_unconfigured_backend(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    config = AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())
    with pytest.raises(RuntimeError, match="tts_backend"):
        VoiceSynthesisService(config, database).synthesize("babe_001", "Hallo", Path(tmp_path / "line.wav"))


def test_xtts_synthesis_uses_the_finetuned_checkpoint_when_one_is_active(tmp_path, monkeypatch):
    database = Database(tmp_path / "series.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('babe_001','demo','Babe')")
        connection.execute("INSERT INTO voice_profiles(voice_profile_id,character_id,voice_identity,backend_name,reference_dir) VALUES('vp1','babe_001','babe-voice','xtts','Series/voices/babe_001')")
        connection.execute("INSERT INTO model_registry(model_id,model_type,backend_name,local_path,active) VALUES('voice_finetune:babe_001','voice_finetune','xtts','Series/models/xtts_finetuned/babe_001',1)")
    service = VoiceSynthesisService(_config(tmp_path), database)
    calls = {}
    def fake_synthesize(text, reference_dir, output, checkpoint_dir=None):
        calls["checkpoint_dir"] = checkpoint_dir
        return str(output)
    service._xtts = type("FakeBackend", (), {"synthesize": staticmethod(fake_synthesize)})()
    result = service.synthesize("babe_001", "Hallo", tmp_path / "line.wav")
    assert result["finetuned"] is True
    assert calls["checkpoint_dir"] == tmp_path / "Series" / "models" / "xtts_finetuned" / "babe_001"


def test_xtts_synthesis_falls_back_to_zero_shot_without_a_finetune(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('babe_001','demo','Babe')")
        connection.execute("INSERT INTO voice_profiles(voice_profile_id,character_id,voice_identity,backend_name,reference_dir) VALUES('vp1','babe_001','babe-voice','xtts','Series/voices/babe_001')")
    service = VoiceSynthesisService(_config(tmp_path), database)
    calls = {}
    def fake_synthesize(text, reference_dir, output, checkpoint_dir=None):
        calls["checkpoint_dir"] = checkpoint_dir
        return str(output)
    service._xtts = type("FakeBackend", (), {"synthesize": staticmethod(fake_synthesize)})()
    result = service.synthesize("babe_001", "Hallo", tmp_path / "line.wav")
    assert result["finetuned"] is False
    assert calls["checkpoint_dir"] is None
