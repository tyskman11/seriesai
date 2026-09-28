from __future__ import annotations

from datetime import datetime, timezone

from .db import Database
from .models import CharacterCreate, CharacterMemoryCreate, GenerationJobCreate, JobStatus, RelationshipUpdate, TimelineEventCreate, TraitChange


class SeriesRepository:
    """The small canonical write surface for Phase 1; services must use this, not raw SQL."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_series(self, series_id: str, title: str, timezone_name: str = "UTC") -> None:
        with self.database.connect() as connection:
            connection.execute("INSERT INTO series(series_id, title, canonical_timezone) VALUES (?, ?, ?)", (series_id, title, timezone_name))

    def create_character(self, character: CharacterCreate) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO characters(character_id, series_id, canonical_name, birth_date, apparent_age, life_stage) VALUES (?, ?, ?, ?, ?, ?)",
                (character.character_id, character.series_id, character.canonical_name, character.birth_date, character.apparent_age, character.life_stage),
            )

    def record_trait_change(self, change: TraitChange) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO character_personality(character_id, trait_name, trait_layer, value, effective_at, reason, episode_id, scene_id, old_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (change.character_id, change.trait_name, change.trait_layer, change.new_value, change.effective_at.isoformat(), change.reason, change.episode_id, change.scene_id, change.old_value),
            )
            trait_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO character_personality_history(trait_id, confidence) VALUES (?, ?)",
                (trait_id, change.confidence),
            )
        return int(trait_id)

    def create_memory(self, memory: CharacterMemoryCreate) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO character_memories(memory_id, character_id, event_id, memory_type, description, emotional_weight, importance, created_at, source_episode_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory.memory_id, memory.character_id, memory.event_id, memory.memory_type, memory.description, memory.emotional_weight, memory.importance, memory.created_at.isoformat(), memory.source_episode_id),
            )

    def record_relationship_update(self, update: RelationshipUpdate) -> str:
        if update.character_a_id == update.character_b_id:
            raise ValueError("A relationship requires two different characters.")
        character_a_id, character_b_id = sorted((update.character_a_id, update.character_b_id))
        relationship_id = f"{update.series_id}:{character_a_id}:{character_b_id}"
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO relationships(relationship_id, series_id, character_a_id, character_b_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (relationship_id, update.series_id, character_a_id, character_b_id, update.effective_at.isoformat()),
            )
            connection.execute(
                "INSERT INTO relationship_history(relationship_id, effective_at, trust, closeness, romantic_interest, conflict_level, respect, dependency, shared_history, reason, episode_id, scene_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (relationship_id, update.effective_at.isoformat(), update.trust, update.closeness, update.romantic_interest, update.conflict_level, update.respect, update.dependency, update.shared_history, update.reason, update.episode_id, update.scene_id),
            )
        return relationship_id

    def add_timeline_event(self, event: TimelineEventCreate) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO timeline(series_id, occurred_at, entity_type, entity_id, event_type, description, source_episode_id, source_scene_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event.series_id, event.occurred_at.isoformat(), event.entity_type, event.entity_id, event.event_type, event.description, event.source_episode_id, event.source_scene_id),
            )
        return int(cursor.lastrowid)

    def create_job(self, job: GenerationJobCreate) -> None:
        with self.database.connect() as connection:
            connection.execute("INSERT INTO generation_jobs(job_id, series_id, episode_id, job_type, status, payload_json) VALUES (?, ?, ?, ?, ?, ?)", (job.job_id, job.series_id, job.episode_id, job.job_type, JobStatus.PENDING, job.payload_json))

    def update_job(self, job_id: str, status: JobStatus, *, progress: float | None = None, checkpoint_json: str | None = None, error_message: str | None = None) -> None:
        clauses, values = ["status = ?", "updated_at = ?"], [status, datetime.now(timezone.utc).isoformat()]
        for column, value in (("progress", progress), ("checkpoint_json", checkpoint_json), ("error_message", error_message)):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        values.append(job_id)
        with self.database.connect() as connection:
            connection.execute(f"UPDATE generation_jobs SET {', '.join(clauses)} WHERE job_id = ?", values)

    def resumable_jobs(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM generation_jobs WHERE status IN ('pending', 'running', 'retry') ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]
