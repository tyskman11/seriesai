from pathlib import Path

import pytest

from seriesai.config import load_config


def test_config_loads_repository_defaults():
    root = Path(__file__).parents[1]
    config = load_config(root / "config.yaml")
    assert config.hardware.prefer_cpu_offload is True
    assert config.models.llm_backend == "llamacpp"
    assert config.models.transcription_backend == config.transcription.backend == "faster_whisper"


def test_config_rejects_conflicting_transcription_backend(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
paths: {project_root: ., series_root: Series, comfyui: ComfyUI}
hardware: {gpu_vram_gb: 6, system_ram_gb: 32}
models: {transcription_backend: whisperx}
transcription: {backend: faster_whisper}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="must name the same backend"):
        load_config(config_path)


def test_config_fills_legacy_transcription_selector(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
paths: {project_root: ., series_root: Series, comfyui: ComfyUI}
hardware: {gpu_vram_gb: 6, system_ram_gb: 32}
models: {}
transcription: {backend: faster_whisper}
""", encoding="utf-8")
    assert load_config(config_path).models.transcription_backend == "faster_whisper"
