import pytest

from seriesai.db import Database
from seriesai.services.character_review import CharacterReviewService


def _seeded_database(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
    return database


def test_create_manual_adds_character_and_matching_approved_candidate(tmp_path):
    database = _seeded_database(tmp_path)
    result = CharacterReviewService(database).create_manual("demo", "migo_001", "Migo")
    assert result == {"character_id": "migo_001", "canonical_name": "Migo", "status": "approved"}
    with database.connect() as connection:
        character = connection.execute("SELECT canonical_name FROM characters WHERE character_id='migo_001'").fetchone()
        candidate = connection.execute("SELECT status,canonical_name FROM character_candidates WHERE candidate_id='migo_001'").fetchone()
        alias = connection.execute("SELECT alias FROM character_aliases WHERE character_id='migo_001'").fetchone()
    assert character["canonical_name"] == "Migo"
    assert candidate["status"] == "approved" and candidate["canonical_name"] == "Migo"
    assert alias["alias"] == "Migo"


def test_create_manual_rejects_duplicate_id(tmp_path):
    database = _seeded_database(tmp_path)
    service = CharacterReviewService(database)
    service.create_manual("demo", "migo_001", "Migo")
    with pytest.raises(ValueError, match="already in use"):
        service.create_manual("demo", "migo_001", "Migo Zwei")


def test_create_manual_rejects_invalid_id(tmp_path):
    database = _seeded_database(tmp_path)
    with pytest.raises(ValueError, match="lowercase"):
        CharacterReviewService(database).create_manual("demo", "Migo 001", "Migo")


def test_rename_updates_character_and_candidate_and_adds_alias(tmp_path):
    database = _seeded_database(tmp_path)
    service = CharacterReviewService(database)
    service.create_manual("demo", "migo_001", "Migo")
    result = service.rename("migo_001", "Migo der Grosse")
    assert result["canonical_name"] == "Migo der Grosse"
    with database.connect() as connection:
        character = connection.execute("SELECT canonical_name FROM characters WHERE character_id='migo_001'").fetchone()
        candidate = connection.execute("SELECT canonical_name FROM character_candidates WHERE candidate_id='migo_001'").fetchone()
        aliases = {row[0] for row in connection.execute("SELECT alias FROM character_aliases WHERE character_id='migo_001'")}
    assert character["canonical_name"] == "Migo der Grosse"
    assert candidate["canonical_name"] == "Migo der Grosse"
    assert aliases == {"Migo", "Migo der Grosse"}


def test_rename_unknown_character_raises(tmp_path):
    database = _seeded_database(tmp_path)
    with pytest.raises(ValueError, match="Unknown character_id"):
        CharacterReviewService(database).rename("nope_001", "Wer")
