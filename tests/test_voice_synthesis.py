from pathlib import Path

import pytest

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.voice_synthesis import VoiceSynthesisService


def test_voice_synthesis_refuses_unconfigured_backend(tmp_path):
    database = Database(tmp_path / "series.db"); database.initialize()
    config = AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())
    with pytest.raises(RuntimeError, match="tts_backend"):
        VoiceSynthesisService(config, database).synthesize("babe_001", "Hallo", Path(tmp_path / "line.wav"))
