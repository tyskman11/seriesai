from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.services.render_readiness import render_readiness


def test_render_readiness_reports_missing_models_without_network(tmp_path):
    config = AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())
    result = render_readiness(config)
    assert result["state"] == "models_missing"
    assert result["ready"] is False
