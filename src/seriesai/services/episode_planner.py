"""Local, review-first episode-outline generation from the persistent Series Brain."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from ..backends.ollama import OllamaBackend
from ..config import AppConfig
from ..db import Database
from ..paths import project_relative


class DialogueLine(BaseModel):
    character_id: str = Field(min_length=1)
    line: str = Field(min_length=1, max_length=400)


class PlannedScene(BaseModel):
    sequence_number: int = Field(ge=1)
    location: str = Field(min_length=1)
    character_ids: list[str] = Field(min_length=1, max_length=12)
    duration_seconds: float = Field(gt=5, le=900)
    purpose: str = Field(min_length=1, max_length=800)
    conflict_or_turn: str = Field(min_length=1, max_length=800)
    dialogue: list[DialogueLine] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def dialogue_speakers_are_scene_characters(self) -> "PlannedScene":
        unknown = {line.character_id for line in self.dialogue} - set(self.character_ids)
        if unknown:
            raise ValueError(f"Dialogue speakers must be in character_ids: {sorted(unknown)}")
        return self


class EpisodeOutline(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    logline: str = Field(min_length=1, max_length=1000)
    continuity_notes: list[str] = Field(default_factory=list, max_length=30)
    scenes: list[PlannedScene] = Field(min_length=2, max_length=40)

    @model_validator(mode="after")
    def unique_scene_numbers(self) -> "EpisodeOutline":
        if len({scene.sequence_number for scene in self.scenes}) != len(self.scenes):
            raise ValueError("Scene sequence numbers must be unique.")
        return self


class EpisodePlannerService:
    """Creates a draft only. It never commits events or changes canonical state."""

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def context(self, series_id: str) -> dict:
        with self.database.connect() as connection:
            series = connection.execute("SELECT series_id,title FROM series WHERE series_id=?", (series_id,)).fetchone()
            if not series:
                raise ValueError(f"Unknown series: {series_id}")
            characters = [dict(row) for row in connection.execute("SELECT character_id,canonical_name,apparent_age,life_stage FROM characters WHERE series_id=? AND active=1 ORDER BY canonical_name", (series_id,))]
            events = [dict(row) for row in connection.execute("SELECT occurred_at,event_type,description,source_episode_id FROM world_events WHERE series_id=? ORDER BY occurred_at DESC LIMIT 30", (series_id,))]
            arcs = [dict(row) for row in connection.execute("SELECT title,description,priority FROM open_story_arcs WHERE series_id=? AND status='open' ORDER BY priority DESC LIMIT 20", (series_id,))]
            relationships = [dict(row) for row in connection.execute("SELECT a.canonical_name AS character_a,b.canonical_name AS character_b,h.trust,h.closeness,h.romantic_interest,h.conflict_level,h.respect,h.dependency,h.shared_history,h.reason FROM relationship_history h JOIN relationships r ON r.relationship_id=h.relationship_id JOIN characters a ON a.character_id=r.character_a_id JOIN characters b ON b.character_id=r.character_b_id WHERE r.series_id=? ORDER BY h.effective_at DESC LIMIT 40", (series_id,))]
            latest_episode = connection.execute("SELECT episode_id,season_number,episode_number,episode_date FROM episodes WHERE series_id=? ORDER BY season_number DESC,episode_number DESC LIMIT 1", (series_id,)).fetchone()
        return {"series": dict(series), "characters": characters, "recent_events": events, "open_story_arcs": arcs, "relationship_history": relationships, "latest_episode": dict(latest_episode) if latest_episode else None}

    @staticmethod
    def validate(outline: EpisodeOutline, context: dict, target_duration_seconds: int) -> list[str]:
        issues: list[str] = []
        valid_ids = {character["character_id"] for character in context["characters"]}
        if not valid_ids:
            issues.append("No approved canonical characters exist yet.")
        for scene in outline.scenes:
            unknown = set(scene.character_ids) - valid_ids
            if unknown:
                issues.append(f"Scene {scene.sequence_number} uses unknown character IDs: {', '.join(sorted(unknown))}")
        total = sum(scene.duration_seconds for scene in outline.scenes)
        if abs(total - target_duration_seconds) > max(90, target_duration_seconds * 0.15):
            issues.append(f"Planned duration {round(total)}s differs too much from target {target_duration_seconds}s.")
        return issues

    @staticmethod
    def normalize_draft(outline: EpisodeOutline, context: dict, target_duration_seconds: int) -> EpisodeOutline:
        """Resolve harmless display-name output and target timing in a draft.

        Local small models often emit canonical display names despite being
        given immutable IDs. This maps only unambiguous approved names and
        aliases; unknown values remain errors for the continuity checker.
        """
        names = {str(item["character_id"]).lower(): item["character_id"] for item in context["characters"]}
        names.update({str(item["canonical_name"]).lower(): item["character_id"] for item in context["characters"]})
        normalized = []
        for scene in outline.scenes:
            normalized_ids = [names.get(str(value).strip().lower(), value) for value in scene.character_ids]
            normalized_dialogue = [line.model_copy(update={"character_id": names.get(str(line.character_id).strip().lower(), line.character_id)}) for line in scene.dialogue]
            normalized_dialogue = [line for line in normalized_dialogue if line.character_id in normalized_ids]
            normalized.append(scene.model_copy(update={"character_ids": normalized_ids, "dialogue": normalized_dialogue}))
        total = sum(scene.duration_seconds for scene in normalized)
        if total > 0 and abs(total - target_duration_seconds) > max(90, target_duration_seconds * .15):
            factor = target_duration_seconds / total
            normalized = [scene.model_copy(update={"duration_seconds": round(scene.duration_seconds * factor, 2)}) for scene in normalized]
        return outline.model_copy(update={"scenes": normalized})

    def plan(self, series_id: str, idea: str, target_duration_minutes: float, notes: str | None = None, job_id: str | None = None) -> dict:
        if not self.config.ollama.model:
            raise RuntimeError("No local Ollama model configured.")
        if not idea.strip():
            raise ValueError("episode idea cannot be empty")
        target_seconds = int(target_duration_minutes * 60)
        if not 60 <= target_seconds <= 7200:
            raise ValueError("target duration must be between 1 and 120 minutes")
        context = self.context(series_id)
        if not context["characters"]:
            raise ValueError("At least one approved canonical character is required before planning an episode.")
        job_id = job_id or str(uuid.uuid4())
        with self.database.connect() as connection:
            prior = connection.execute("SELECT status,checkpoint_json FROM generation_jobs WHERE job_id=?", (job_id,)).fetchone()
            if prior and prior["status"] == "completed":
                checkpoint = json.loads(prior["checkpoint_json"] or "{}")
                output = Path(checkpoint.get("outline_path", ""))
                if output.is_file():
                    return {"job_id": job_id, "status": "already_planned", "outline_path": str(output), "issues": checkpoint.get("issues", [])}
            payload = json.dumps({"idea": idea, "target_duration_minutes": target_duration_minutes, "notes": notes}, ensure_ascii=False)
            connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json,progress,attempts,updated_at) VALUES(?,?,?,'running',?,0.05,1,?) ON CONFLICT(job_id) DO UPDATE SET status='running',payload_json=excluded.payload_json,progress=.05,attempts=generation_jobs.attempts+1,updated_at=excluded.updated_at,error_message=NULL", (job_id, series_id, "episode_outline", payload, datetime.now(timezone.utc).isoformat()))
        # Keep the planning prompt compact.  Low-confidence transcript-derived
        # event summaries can otherwise drown out the user's new idea and make
        # a small local model retell an old episode instead of planning one.
        prompt_context = {"series": context["series"], "characters": context["characters"],
                          "open_story_arcs": context["open_story_arcs"], "relationship_history": context["relationship_history"],
                          "latest_episode": context["latest_episode"]}
        prompt = (
            "Plane eine neue deutsche Serienepisode als strukturierten Entwurf. Das oberste JSON-Objekt MUSS "
            "genau title, logline, continuity_notes und scenes enthalten. Jedes Element in scenes MUSS genau "
            "sequence_number, location, character_ids, duration_seconds, purpose, conflict_or_turn und dialogue enthalten. "
            "dialogue ist eine Liste gesprochener Zeilen in Szenenreihenfolge; jede Zeile hat character_id (aus "
            "character_ids der Szene) und line (kurzer, natuerlich gesprochener deutscher Satz im Ton der Serie, "
            "keine Regieanweisungen, keine Anfuehrungszeichen). Schreibe fuer jede Szene mindestens 2 Zeilen, "
            "insgesamt genug Zeilen, dass die Szene bei normalem Sprechtempo etwa ihre duration_seconds fuellt. "
            "Nutze ausschliesslich die "
            "genehmigten character_ids aus dem Kontext. Erfinde keine Figuren-IDs und widersprich keinem "
            "Ereignis. Dies ist ein Entwurf, keine kanonische Fortsetzung. Ziel: " + str(target_seconds) + " Sekunden.\n"
            f"Idee: {idea}\nOptionale Hinweise: {notes or '-'}\n\nKOMPAKTER SERIES-BRAIN-KONTEXT:\n" + json.dumps(prompt_context, ensure_ascii=False)
        )
        try:
            outline = OllamaBackend(self.config.ollama.base_url, self.config.ollama.model).structured_generate(prompt, EpisodeOutline, system="Du bist ein deutscher Serienautor mit strenger Kontinuitaetsdisziplin.")
            outline = self.normalize_draft(outline, context, target_seconds)
            issues = self.validate(outline, context, target_seconds)
            directory = self.config.paths.series_root / "Generated" / "drafts" / job_id
            directory.mkdir(parents=True, exist_ok=True)
            output = directory / "episode_outline.json"
            output.write_text(json.dumps({"job_id": job_id, "series_id": series_id, "idea": idea, "target_duration_seconds": target_seconds, "context_snapshot": context, "outline": outline.model_dump(), "continuity_issues": issues, "status": "needs_review" if issues else "draft_ready"}, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.database.connect() as connection:
                connection.execute("UPDATE generation_jobs SET status='completed',progress=1,checkpoint_json=?,updated_at=? WHERE job_id=?", (json.dumps({"outline_path": str(output), "issues": issues}), datetime.now(timezone.utc).isoformat(), job_id))
            return {"job_id": job_id, "status": "needs_review" if issues else "draft_ready", "outline_path": str(output), "issues": issues}
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute("UPDATE generation_jobs SET status='failed',error_message=?,updated_at=? WHERE job_id=?", (str(error), datetime.now(timezone.utc).isoformat(), job_id))
            raise
