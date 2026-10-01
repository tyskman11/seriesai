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
    # Vendored on first use by lora_training.py (git clone + its own venv), not at every
    # start.py launch - LoRA training is a deliberate, occasional, heavy action, not part of
    # the normal daily run.
    ai_toolkit: Path | None = None


class HardwareConfig(BaseModel):
    gpu_vram_gb: int = Field(ge=1)
    system_ram_gb: int = Field(ge=1)
    prefer_cpu_offload: bool = True
    # How much of this machine's hardware the project may use while it's a worker: "eco" leaves
    # headroom for using the machine for other things at the same time, "full" renders as fast
    # as possible. Drives ComfyUI's VRAM mode, an inter-shot throttle pause, and render step count.
    work_intensity: str = Field(default="full", pattern="^(eco|balanced|full)$")
    worker_timeout_seconds: float = Field(default=90.0, ge=10.0)


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
    base_url: str = "http://127.0.0.1:11435"
    model: str | None = None
    timeout_seconds: int = Field(default=1200, ge=1)  # generous: CPU fallback under concurrent GPU render load is slow


class ComfyUIConfig(BaseModel):
    """Local ComfyUI API settings; no remote endpoint is accepted by policy."""
    api_url: str = "http://127.0.0.1:8188"
    workflow_template: Path | None = None
    # Used instead of workflow_template whenever a shot has a reference_image (a real face
    # crop of its character), so generated people actually resemble the original cast.
    img2video_workflow_template: Path | None = None
    poll_seconds: float = Field(default=2.0, ge=.2, le=30)
    timeout_seconds: int = Field(default=7200, ge=30)
    # None means "derive from hardware.work_intensity" (see load_config); set explicitly here
    # to override that derivation regardless of the chosen intensity preset.
    steps: int | None = Field(default=None, ge=1, le=150)
    cfg: float | None = Field(default=None, ge=0.5, le=20)


class PiperConfig(BaseModel):
    executable: str = "piper"
    models_root: Path | None = None


class TrainingConfig(BaseModel):
    """Character-LoRA defaults, deliberately the most aggressive low-VRAM settings: this
    project targets a 6GB card, far below the ~24GB ai-toolkit/diffusion-pipe recommend
    for comfortable LTX-2 LoRA training. Lower quality/slower training trades for a chance
    of fitting in VRAM at all; raise these only on a machine with real headroom."""
    rank: int = Field(default=8, ge=1, le=128)
    resolution: int = Field(default=512, ge=64, le=1024)
    image_steps: int = Field(default=1500, ge=50)
    video_steps: int = Field(default=1000, ge=50)
    video_clip_frames: int = Field(default=25, ge=9, le=121)   # ~1s at 24fps; LTX's own default is 121 (5s)
    learning_rate: float = Field(default=1e-4, gt=0)
    batch_size: int = Field(default=1, ge=1)


class ProcessingConfig(BaseModel):
    checkpoint_enabled: bool = True
    max_job_retries: int = Field(default=3, ge=0)


# steps/cfg per hardware.work_intensity when comfyui_api.steps/cfg aren't set explicitly:
# eco trades render quality for speed, full matches the long-standing hand-tuned default.
INTENSITY_RENDER_DEFAULTS = {"eco": (20, 4.0), "balanced": (26, 4.25), "full": (32, 4.5)}


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
    training: TrainingConfig = TrainingConfig()
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
    if config.paths.ai_toolkit:
        config.paths.ai_toolkit = _resolve_path(config.paths.project_root, config.paths.ai_toolkit)
    if config.comfyui_api.workflow_template:
        config.comfyui_api.workflow_template = _resolve_path(config.paths.project_root, config.comfyui_api.workflow_template)
    if config.comfyui_api.img2video_workflow_template:
        config.comfyui_api.img2video_workflow_template = _resolve_path(config.paths.project_root, config.comfyui_api.img2video_workflow_template)
    if config.piper.models_root:
        config.piper.models_root = _resolve_path(config.paths.project_root, config.piper.models_root)
    default_steps, default_cfg = INTENSITY_RENDER_DEFAULTS.get(config.hardware.work_intensity, INTENSITY_RENDER_DEFAULTS["full"])
    if config.comfyui_api.steps is None: config.comfyui_api.steps = default_steps
    if config.comfyui_api.cfg is None: config.comfyui_api.cfg = default_cfg
    return config


def _resolve_path(base: Path, value: Path) -> Path:
    return value if value.is_absolute() else (base / value).resolve()
