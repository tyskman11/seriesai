from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class PathsConfig(BaseModel):
    project_root: Path
    series_root: Path
    comfyui: Path
    ffmpeg: str = "ffmpeg"


class HardwareConfig(BaseModel):
    gpu_vram_gb: int = Field(ge=1)
    system_ram_gb: int = Field(ge=1)
    prefer_cpu_offload: bool = True


class ModelsConfig(BaseModel):
    transcription_backend: str | None = None
    vision_backend: str | None = None
    llm_backend: str | None = None
    tts_backend: str | None = None
    video_backend: str | None = None


class TranscriptionConfig(BaseModel):
    backend: str = "faster_whisper"
    model: str = "medium"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = Field(default=5, ge=1, le=10)


class OllamaConfig(BaseModel):
    base_url: str = "http://127.0.0.1:11434"
    model: str | None = None
    timeout_seconds: int = Field(default=180, ge=1)


class ComfyUIConfig(BaseModel):
    """Local ComfyUI API settings; no remote endpoint is accepted by policy."""
    api_url: str = "http://127.0.0.1:8188"
    workflow_template: Path | None = None
    poll_seconds: float = Field(default=2.0, ge=.2, le=30)
    timeout_seconds: int = Field(default=7200, ge=30)


class PiperConfig(BaseModel):
    executable: str = "piper"
    models_root: Path | None = None


class ProcessingConfig(BaseModel):
    checkpoint_enabled: bool = True
    max_job_retries: int = Field(default=3, ge=0)


class LocalizationConfig(BaseModel):
    analysis_language: str = "de"
    output_language: str = "de"


class AppConfig(BaseModel):
    paths: PathsConfig
    hardware: HardwareConfig
    models: ModelsConfig
    transcription: TranscriptionConfig = TranscriptionConfig()
    ollama: OllamaConfig = OllamaConfig()
    comfyui_api: ComfyUIConfig = ComfyUIConfig()
    piper: PiperConfig = PiperConfig()
    processing: ProcessingConfig = ProcessingConfig()
    localization: LocalizationConfig = LocalizationConfig()


def load_config(path: Path | str) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}
    config = AppConfig.model_validate(raw)
    base = config_path.resolve().parent
    config.paths.project_root = _resolve_path(base, config.paths.project_root)
    config.paths.series_root = _resolve_path(config.paths.project_root, config.paths.series_root)
    config.paths.comfyui = _resolve_path(config.paths.project_root, config.paths.comfyui)
    if config.comfyui_api.workflow_template:
        config.comfyui_api.workflow_template = _resolve_path(config.paths.project_root, config.comfyui_api.workflow_template)
    if config.piper.models_root:
        config.piper.models_root = _resolve_path(config.paths.project_root, config.piper.models_root)
    return config


def _resolve_path(base: Path, value: Path) -> Path:
    return value if value.is_absolute() else (base / value).resolve()
