"""Evidence-first, review-only relationship discovery.

This intentionally uses deterministic co-mentions from already extracted
world-events.  It does not infer emotion scores from noisy transcripts and
never writes to ``relationships`` or ``relationship_history``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from itertools import combinations

from ..config import AppConfig
from ..db import Database


class RelationshipProposalService:
    job_type = "relationship_proposals"

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def propose(self, series_id: str, job_id: str | None = None) -> dict:
        with self.database.connect() as connection:
            characters = [dict(row) for row in connection.execute(
                "SELECT character_id,canonical_name FROM characters WHERE series_id=? AND active=1 ORDER BY character_id",
                (series_id,),
            )]
            events = [dict(row) for row in connection.execute(
                "SELECT world_event_id,description,source_episode_id FROM world_events WHERE series_id=? ORDER BY occurred_at,world_event_id",
                (series_id,),
            )]
        if len(characters) < 2:
            raise ValueError("At least two approved canonical characters are required.")
        job_id = job_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json,progress,attempts,updated_at) "
                "VALUES(?,?,?,'running','{}',0,1,?) "
                "ON CONFLICT(job_id) DO UPDATE SET status='running',updated_at=excluded.updated_at",
                (job_id, series_id, self.job_type, now),
            )
        evidence_by_pair: dict[tuple[str, str], list[dict]] = {}
        try:
            for event in events:
                text = event["description"].casefold()
                present = [character for character in characters if character["canonical_name"].casefold() in text]
                for first, second in combinations(present, 2):
                    key = tuple(sorted((first["character_id"], second["character_id"])))
                    evidence_by_pair.setdefault(key, []).append({
                        "event_id": event["world_event_id"],
                        "episode_id": event["source_episode_id"],
                        "description": event["description"],
                    })
            names = {character["character_id"]: character["canonical_name"] for character in characters}
            proposed = 0
            total = max(1, len(evidence_by_pair))
            for index, ((first, second), evidence) in enumerate(sorted(evidence_by_pair.items()), start=1):
                # Confidence only reflects repeated textual co-mention, capped
                # deliberately below a reviewed relationship certainty.
                count = len(evidence)
                confidence = min(0.75, 0.25 + 0.10 * count)
                summary = f"{names[first]} und {names[second]} werden in {count} gespeicherten Serienereignis(sen) gemeinsam genannt. Inhaltliche Beziehung und Werte müssen geprüft werden."
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT INTO relationship_proposals(proposal_id,series_id,character_a_id,character_b_id,summary_de,evidence_json,co_mentioned_event_count,confidence,source_job_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(series_id,character_a_id,character_b_id) DO UPDATE SET summary_de=excluded.summary_de,evidence_json=excluded.evidence_json,co_mentioned_event_count=excluded.co_mentioned_event_count,confidence=excluded.confidence,source_job_id=excluded.source_job_id,status='proposed',created_at=CURRENT_TIMESTAMP",
                        (str(uuid.uuid4()), series_id, first, second, summary, json.dumps(evidence[-12:], ensure_ascii=False), count, confidence, job_id),
                    )
                    connection.execute(
                        "UPDATE generation_jobs SET progress=?,checkpoint_json=?,updated_at=? WHERE job_id=?",
                        (index / total, json.dumps({"completed": index, "total": total, "pair": [first, second]}), datetime.now(timezone.utc).isoformat(), job_id),
                    )
                proposed += 1
            with self.database.connect() as connection:
                connection.execute("UPDATE generation_jobs SET status='completed',progress=1,updated_at=? WHERE job_id=?", (datetime.now(timezone.utc).isoformat(), job_id))
            return {"job_id": job_id, "status": "completed", "proposals": proposed, "events_scanned": len(events)}
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute("UPDATE generation_jobs SET status='failed',error_message=?,updated_at=? WHERE job_id=?", (str(error), datetime.now(timezone.utc).isoformat(), job_id))
            raise
