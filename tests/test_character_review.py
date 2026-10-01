import pytest

from seriesai.db import Database
from seriesai.services.character_review import CharacterReviewService


def test_approval_creates_stable_character_and_aliases(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO character_candidates(candidate_id,series_id,canonical_name,aliases_json,confidence,evidence_json,source_signature) VALUES('c1','demo','Babe','[\"Babette\"]',0.9,'[]','test')")
    result = CharacterReviewService(database).approve("demo", "c1", "babe_001")
    assert result["character_id"] == "babe_001"
    with database.connect() as connection:
        character = connection.execute("SELECT canonical_name FROM characters WHERE character_id='babe_001'").fetchone()
        aliases = {row[0] for row in connection.execute("SELECT alias FROM character_aliases WHERE character_id='babe_001'")}
    assert character["canonical_name"] == "Babe"
    assert aliases == {"Babe", "Babette"}


def test_approving_a_second_candidate_with_an_already_used_name_is_rejected(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO character_candidates(candidate_id,series_id,canonical_name,aliases_json,confidence,evidence_json,source_signature) VALUES('c1','demo','Babe','[]',0.9,'[]','test')")
        connection.execute("INSERT INTO character_candidates(candidate_id,series_id,canonical_name,aliases_json,confidence,evidence_json,source_signature) VALUES('c2','demo','Someone Else','[]',0.9,'[]','test')")
    service = CharacterReviewService(database)
    service.approve("demo", "c1", "babe_001")
    with pytest.raises(ValueError):
        service.approve("demo", "c2", "babe_002", canonical_name="Babe")


def test_renaming_a_character_to_an_already_used_name_is_rejected(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO series(series_id,title) VALUES('demo','Demo')")
        connection.execute("INSERT INTO character_candidates(candidate_id,series_id,canonical_name,aliases_json,confidence,evidence_json,source_signature) VALUES('c1','demo','Babe','[]',0.9,'[]','test')")
        connection.execute("INSERT INTO character_candidates(candidate_id,series_id,canonical_name,aliases_json,confidence,evidence_json,source_signature) VALUES('c2','demo','Kenzie','[]',0.9,'[]','test')")
    service = CharacterReviewService(database)
    service.approve("demo", "c1", "babe_001")
    service.approve("demo", "c2", "kenzie_001")
    with pytest.raises(ValueError):
        service.rename("kenzie_001", "Babe")
