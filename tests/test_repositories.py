from datetime import datetime, timezone

from seriesai.db import Database
from seriesai.models import CharacterCreate, CharacterMemoryCreate, GenerationJobCreate, JobStatus, MemoryType, RelationshipUpdate, TimelineEventCreate, TraitChange, TraitLayer
from seriesai.repositories import SeriesRepository


def test_repository_records_provenanced_trait_and_resumable_job(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    repository = SeriesRepository(database)
    repository.create_series("demo", "Demo")
    repository.create_character(CharacterCreate(character_id="babe_001", series_id="demo", canonical_name="Babe"))
    repository.record_trait_change(TraitChange(character_id="babe_001", trait_name="leadership", trait_layer=TraitLayer.DEVELOPED, old_value=0.52, new_value=0.56, effective_at=datetime.now(timezone.utc), reason="Led the team", confidence=0.9))
    repository.create_job(GenerationJobCreate(job_id="job_001", series_id="demo", job_type="analysis"))
    repository.update_job("job_001", JobStatus.RETRY, checkpoint_json='{"stage":"audio"}')
    assert repository.resumable_jobs()[0]["job_id"] == "job_001"


def test_relationship_memory_and_timeline_are_persistent_and_order_independent(tmp_path):
    database = Database(tmp_path / "series.db")
    database.initialize()
    repository = SeriesRepository(database)
    now = datetime.now(timezone.utc)
    repository.create_series("demo", "Demo")
    repository.create_character(CharacterCreate(character_id="babe_001", series_id="demo", canonical_name="Babe"))
    repository.create_character(CharacterCreate(character_id="kenzie_001", series_id="demo", canonical_name="Kenzie"))
    relationship_id = repository.record_relationship_update(RelationshipUpdate(series_id="demo", character_a_id="kenzie_001", character_b_id="babe_001", effective_at=now, reason="Worked through a conflict", trust=.72, conflict_level=.18))
    repository.create_memory(CharacterMemoryCreate(memory_id="memory_001", character_id="babe_001", memory_type=MemoryType.CORE, description="Founded the company together", emotional_weight=.9, importance=1, created_at=now))
    timeline_id = repository.add_timeline_event(TimelineEventCreate(series_id="demo", occurred_at=now, entity_type="relationship", entity_id=relationship_id, event_type="reconciliation", description="Babe and Kenzie reconciled."))
    with database.connect() as connection:
        row = connection.execute("SELECT character_a_id, character_b_id FROM relationships WHERE relationship_id = ?", (relationship_id,)).fetchone()
        assert (row["character_a_id"], row["character_b_id"]) == ("babe_001", "kenzie_001")
        assert connection.execute("SELECT memory_type FROM character_memories WHERE memory_id = 'memory_001'").fetchone()[0] == "core"
        assert connection.execute("SELECT description FROM timeline WHERE timeline_id = ?", (timeline_id,)).fetchone()[0] == "Babe and Kenzie reconciled."
