"""Local, review-only character profile proposals from existing Series Brain evidence."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..backends.ollama import OllamaBackend
from ..config import AppConfig
from ..db import Database


class CharacterProfileOutput(BaseModel):
    summary_de: str = Field(min_length=1, max_length=1500)
    core_traits: dict[str, float] = Field(default_factory=dict)
    developed_traits: dict[str, float] = Field(default_factory=dict)
    current_goals: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0, le=1)


class CharacterProfileService:
    job_type = "character_profile_proposals"

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def propose(self, series_id: str, job_id: str | None = None) -> dict:
        if not self.config.ollama.model:
            raise RuntimeError("No local Ollama model configured.")
        with self.database.connect() as connection:
            characters = [dict(row) for row in connection.execute("SELECT character_id,canonical_name FROM characters WHERE series_id=? AND active=1 ORDER BY canonical_name", (series_id,))]
            events = [dict(row) for row in connection.execute("SELECT description,source_episode_id,event_type FROM world_events WHERE series_id=? ORDER BY occurred_at", (series_id,))]
        if not characters:
            raise ValueError("Approved canonical characters required.")
        job_id = job_id or str(uuid.uuid4())
        with self.database.connect() as connection:
            connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json,progress,attempts,updated_at) VALUES(?,?,?,'running','{}',0,1,?) ON CONFLICT(job_id) DO UPDATE SET status='running',updated_at=excluded.updated_at", (job_id, series_id, self.job_type, datetime.now(timezone.utc).isoformat()))
        completed = 0
        try:
            for index, character in enumerate(characters, start=1):
                relevant = [event for event in events if character["canonical_name"].lower() in event["description"].lower()]
                prompt = "Erstelle ein vorsichtiges deutsches Charakterprofil nur aus diesen belegten Serienereignissen. Unsichere Fakten nicht erfinden. Trait-Werte liegen zwischen 0 und 1.\nFigur: " + json.dumps(character, ensure_ascii=False) + "\nBelege:\n" + json.dumps(relevant[-40:], ensure_ascii=False)
                profile = OllamaBackend(self.config.ollama.base_url, self.config.ollama.model).structured_generate(prompt, CharacterProfileOutput, system="Du extrahierst nur reviewbare Vorschlaege; keine kanonischen Behauptungen.")
                with self.database.connect() as connection:
                    connection.execute("INSERT INTO character_profile_proposals(proposal_id,character_id,profile_json,evidence_json,confidence,source_job_id) VALUES(?,?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET profile_json=excluded.profile_json,evidence_json=excluded.evidence_json,confidence=excluded.confidence,source_job_id=excluded.source_job_id,status='proposed',created_at=CURRENT_TIMESTAMP", (str(uuid.uuid4()), character["character_id"], profile.model_dump_json(), json.dumps(profile.evidence, ensure_ascii=False), profile.confidence, job_id))
                    connection.execute("UPDATE generation_jobs SET progress=?,checkpoint_json=?,updated_at=? WHERE job_id=?", (index / len(characters), json.dumps({"completed": index, "total": len(characters), "character_id": character["character_id"]}), datetime.now(timezone.utc).isoformat(), job_id))
                completed += 1
            with self.database.connect() as connection:
                connection.execute("UPDATE generation_jobs SET status='completed',progress=1,updated_at=? WHERE job_id=?", (datetime.now(timezone.utc).isoformat(), job_id))
            return {"job_id": job_id, "status": "completed", "proposals": completed}
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute("UPDATE generation_jobs SET status='failed',error_message=?,updated_at=? WHERE job_id=?", (str(error), datetime.now(timezone.utc).isoformat(), job_id))
            raise
