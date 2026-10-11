from pathlib import Path

from seriesai.config import load_config


def test_config_loads_repository_defaults():
    root = Path(__file__).parents[1]
    config = load_config(root / "config.yaml")
    assert config.hardware.prefer_cpu_offload is True
    assert config.models.llm_backend == "llamacpp"
