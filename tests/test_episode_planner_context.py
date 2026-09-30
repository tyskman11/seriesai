import pytest

from seriesai.config import AppConfig, HardwareConfig, ModelsConfig, PathsConfig
from seriesai.db import Database
from seriesai.services.episode_planner import EpisodePlannerService


def _config(tmp_path):
    return AppConfig(paths=PathsConfig(project_root=tmp_path, series_root=tmp_path / "Series", comfyui=tmp_path / "ComfyUI"), hardware=HardwareConfig(gpu_vram_gb=6, system_ram_gb=32), models=ModelsConfig())


def _two_series_database(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('home','Heimatserie')")
        connection.execute("INSERT INTO series(series_id,title) VALUES('guest','Gastserie')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('babe_001','home','Babe')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('kenzie_001','home','Kenzie')")
        connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES('guest_001','guest','Gaststar')")
    return database


def test_context_restricts_to_the_requested_character_subset(tmp_path):
    service = EpisodePlannerService(_config(tmp_path), _two_series_database(tmp_path))
    context = service.context("home", character_ids=["babe_001"])
    assert [character["character_id"] for character in context["characters"]] == ["babe_001"]


def test_context_rejects_unknown_requested_character(tmp_path):
    service = EpisodePlannerService(_config(tmp_path), _two_series_database(tmp_path))
    with pytest.raises(ValueError, match="Unknown or unapproved character_ids"):
        service.context("home", character_ids=["nope_001"])


def test_context_merges_crossover_series_characters(tmp_path):
    service = EpisodePlannerService(_config(tmp_path), _two_series_database(tmp_path))
    context = service.context("home", crossover_series_id="guest", crossover_character_ids=["guest_001"])
    ids = {character["character_id"] for character in context["characters"]}
    assert ids == {"babe_001", "kenzie_001", "guest_001"}
    assert context["crossover_series"]["series_id"] == "guest"
    assert [character["character_id"] for character in context["crossover_series"]["characters"]] == ["guest_001"]


def test_context_rejects_unknown_crossover_series(tmp_path):
    service = EpisodePlannerService(_config(tmp_path), _two_series_database(tmp_path))
    with pytest.raises(ValueError, match="Unknown crossover series"):
        service.context("home", crossover_series_id="nope")


def test_context_rejects_empty_crossover_character_selection(tmp_path):
    database = _two_series_database(tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE characters SET active=0 WHERE character_id='guest_001'")
    service = EpisodePlannerService(_config(tmp_path), database)
    with pytest.raises(ValueError, match="No crossover characters selected"):
        service.context("home", crossover_series_id="guest")
