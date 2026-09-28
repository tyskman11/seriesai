"""Read-only local render/TTS readiness diagnostics."""
from __future__ import annotations

from urllib.error import URLError
from urllib.request import urlopen

from ..config import AppConfig


def render_readiness(config: AppConfig) -> dict:
    models = config.paths.comfyui / "models"
    wan = list((models / "unet").glob("Wan*.gguf")) if models.is_dir() else []
    encoder = list((models / "text_encoders").glob("umt5*.gguf")) if models.is_dir() else []
    vae = list((models / "vae").glob("wan*.safetensors")) if models.is_dir() else []
    api_online = False
    try:
        with urlopen(config.comfyui_api.api_url.rstrip("/") + "/system_stats", timeout=2) as response:
            api_online = response.status == 200
    except (URLError, OSError):
        pass
    template = config.comfyui_api.workflow_template
    configured = config.models.video_backend == "comfyui" and bool(template and template.is_file())
    ready = bool(wan and encoder and vae and api_online and configured)
    if ready: state = "ready"
    elif wan and encoder and vae: state = "models_ready"
    else: state = "models_missing"
    return {"state": state, "ready": ready, "api_online": api_online, "workflow_configured": configured,
            "wan_models": [path.name for path in wan], "text_encoders": [path.name for path in encoder], "vaes": [path.name for path in vae],
            "required_next_step": None if ready else ("export_and_configure_local_workflow" if api_online else "start_local_comfyui")}
