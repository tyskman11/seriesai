"""Explicit human review boundary for relationship proposals."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..db import Database
from ..models import RelationshipUpdate
from ..repositories import SeriesRepository


class RelationshipReviewService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def approve(self, series_id: str, proposal_id: str, reviewer_note: str | None = None) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT character_a_id,character_b_id,summary_de,evidence_json,status FROM relationship_proposals WHERE proposal_id=? AND series_id=?",
                (proposal_id, series_id),
            ).fetchone()
        if not row:
            raise KeyError("Relationship proposal not found.")
        if row[4] != "proposed":
            raise ValueError(f"Relationship proposal is already {row[4]}.")
        evidence = json.loads(row[3])
        source_episode_id = next((item.get("episode_id") for item in evidence if item.get("episode_id")), None)
        reason = "Human review approved evidence-only relationship proposal."
        if reviewer_note:
            reason += " Note: " + reviewer_note.strip()
        relationship_id = SeriesRepository(self.database).record_relationship_update(RelationshipUpdate(
            series_id=series_id,
            character_a_id=row[0], character_b_id=row[1],
            effective_at=datetime.now(timezone.utc), reason=reason,
            shared_history=row[2], episode_id=source_episode_id,
        ))
        with self.database.connect() as connection:
            connection.execute("UPDATE relationship_proposals SET status='approved',reviewed_at=CURRENT_TIMESTAMP WHERE proposal_id=?", (proposal_id,))
        return {"proposal_id": proposal_id, "relationship_id": relationship_id, "status": "approved"}

    def reject(self, series_id: str, proposal_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT status FROM relationship_proposals WHERE proposal_id=? AND series_id=?", (proposal_id, series_id)).fetchone()
            if not row:
                raise KeyError("Relationship proposal not found.")
            if row[0] != "proposed":
                raise ValueError(f"Relationship proposal is already {row[0]}.")
            connection.execute("UPDATE relationship_proposals SET status='rejected',reviewed_at=CURRENT_TIMESTAMP WHERE proposal_id=?", (proposal_id,))
        return {"proposal_id": proposal_id, "status": "rejected"}
