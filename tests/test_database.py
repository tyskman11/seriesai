from seriesai.db import Database
from seriesai.db.schema import DDL, MIGRATION_2


def test_database_initializes_idempotently(tmp_path):
    db = Database(tmp_path / "series.db")
    db.initialize()
    db.initialize()
    assert db.schema_version() == 9
    with db.connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"series", "characters", "character_memories", "generation_jobs", "rendered_shots", "commit_history", "continuity_issues"} <= tables


def test_existing_v2_database_migrates_to_current_version_with_backup(tmp_path):
    db = Database(tmp_path / "series.db")
    with db.connect() as connection:
        connection.executescript(DDL)
        connection.executescript(MIGRATION_2)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
    db.initialize(tmp_path / "backups")
    assert db.schema_version() == 9
    assert list((tmp_path / "backups").glob("series-*.db"))


def test_character_identity_is_immutable_primary_key(tmp_path):
    db = Database(tmp_path / "series.db")
    db.initialize()
    with db.connect() as connection:
        connection.execute("INSERT INTO series(series_id, title) VALUES ('demo', 'Demo')")
        connection.execute("INSERT INTO characters(character_id, series_id, canonical_name) VALUES ('babe_001', 'demo', 'Babe')")
        row = connection.execute("SELECT character_id FROM characters WHERE canonical_name = 'Babe'").fetchone()
    assert row["character_id"] == "babe_001"
