from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..backends.ollama import OllamaBackend
from ..config import AppConfig
from ..db import Database


class SemanticEvent(BaseModel):
    event_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    importance: float = Field(ge=0, le=1)


class SemanticOutput(BaseModel):
    summary_de: str = Field(min_length=1)
    events: list[SemanticEvent] = []


class SemanticAnalysisService:
    stage = "semantic_analysis"

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def analyze_episode(self, episode_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT e.series_id, e.episode_date, group_concat(s.text, ' ') FROM episodes e JOIN transcripts t ON t.episode_id=e.episode_id JOIN transcript_segments s ON s.transcript_id=t.transcript_id WHERE e.episode_id=? GROUP BY e.episode_id", (episode_id,)).fetchone()
            done = connection.execute("SELECT status FROM analysis_runs WHERE episode_id=? AND stage_name=?", (episode_id, self.stage)).fetchone()
        if done and done[0] == "completed": return {"episode_id": episode_id, "status": "already_analyzed", "events": 0}
        if not row or not row[2]: raise ValueError("Completed transcript required")
        if not self.config.ollama.model: raise RuntimeError("No Ollama model configured")
        self._stage(episode_id, "running", {})
        try:
            prompt = "Analysiere dieses deutsche Episodentranskript. Extrahiere nur belegbare, wichtige Serienereignisse. Erfinde keine Fakten.\n\n" + row[2][:120000]
            output = OllamaBackend(self.config.ollama.base_url, self.config.ollama.model).structured_generate(prompt, SemanticOutput, system="Du bist ein praeziser Serienkontinuitaets-Analyst.")
            occurred_at = row[1] or datetime.now(timezone.utc).isoformat()
            with self.database.connect() as connection:
                for event in output.events:
                    event_id = str(uuid.uuid4())
                    connection.execute("INSERT INTO world_events(world_event_id,series_id,occurred_at,event_type,description,source_episode_id) VALUES(?,?,?,?,?,?)", (event_id,row[0],occurred_at,event.event_type,event.description,episode_id))
                    connection.execute("INSERT INTO timeline(series_id,occurred_at,entity_type,entity_id,event_type,description,source_episode_id) VALUES(?,?, 'world_event',?,?,?,?)", (row[0],occurred_at,event_id,event.event_type,event.description,episode_id))
            self._stage(episode_id, "completed", {"summary_de": output.summary_de, "event_count": len(output.events)})
            return {"episode_id": episode_id, "status": "completed", "events": len(output.events)}
        except Exception as error:
            self._stage(episode_id, "failed", {"error": str(error)})
            raise

    def analyze_ready_episodes(
        self,
        series_id: str,
        progress: Callable[[int, str, str], None] | None = None,
    ) -> list[dict]:
        """Analyze only transcripts without a completed semantic checkpoint.

        A completed per-episode analysis run is the durable resume marker.  A
        crash, an invalid model response, or a shutdown leaves just that
        episode eligible for the next invocation.
        """
        with self.database.connect() as connection:
            episode_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT e.episode_id FROM episodes e "
                    "JOIN transcripts t ON t.episode_id=e.episode_id AND t.status='completed' "
                    "LEFT JOIN analysis_runs r ON r.episode_id=e.episode_id "
                    "AND r.stage_name=? AND r.status='completed' "
                    "WHERE e.series_id=? AND r.analysis_run_id IS NULL "
                    "ORDER BY e.season_number, e.episode_number",
                    (self.stage, series_id),
                )
            ]
        results: list[dict] = []
        for index, episode_id in enumerate(episode_ids, start=1):
            try:
                result = self.analyze_episode(episode_id)
                status = result["status"]
            except Exception as error:
                result = {"episode_id": episode_id, "status": "failed", "error": str(error)}
                status = "failed"
            results.append(result)
            if progress:
                progress(index, episode_id, status)
        return results

    def _stage(self, episode_id: str, status: str, checkpoint: dict) -> None:
        with self.database.connect() as connection:
            connection.execute("INSERT INTO analysis_runs(analysis_run_id,episode_id,stage_name,status,language,checkpoint_json,finished_at,error_message) VALUES(?,?,?,?,?,?,CASE WHEN ? IN ('completed','failed') THEN CURRENT_TIMESTAMP END,?) ON CONFLICT(episode_id,stage_name) DO UPDATE SET status=excluded.status,checkpoint_json=excluded.checkpoint_json,finished_at=excluded.finished_at,error_message=excluded.error_message", (str(uuid.uuid4()),episode_id,self.stage,status,self.config.localization.analysis_language,json.dumps(checkpoint,ensure_ascii=False),status,checkpoint.get("error")))
