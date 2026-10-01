from seriesai.db import Database


def test_relationship_proposal_schema_is_review_only(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize(tmp_path / "backups")
    assert database.schema_version() == 11
    with database.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(relationship_proposals)")}
    assert {"character_a_id", "character_b_id", "evidence_json", "status"} <= columns
