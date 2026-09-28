"""Explicit review gate between fallible model proposals and canonical identities."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..db import Database


class CharacterReviewService:
    """Approve/reject candidates without ever changing an existing identity ID."""

    _IDENTITY = re.compile(r"^[a-z0-9_]+$")

    def __init__(self, database: Database) -> None:
        self.database = database

    def candidates(self, series_id: str, status: str | None = "proposed") -> list[dict]:
        with self.database.connect() as connection:
            statement = "SELECT candidate_id,canonical_name,aliases_json,role_description,confidence,evidence_json,status FROM character_candidates WHERE series_id=?"
            parameters: tuple[str, ...] = (series_id,)
            if status:
                statement += " AND status=?"
                parameters = (series_id, status)
            rows = connection.execute(statement + " ORDER BY confidence DESC, canonical_name", parameters).fetchall()
        return [
            {**dict(row), "aliases": json.loads(row["aliases_json"]), "evidence": json.loads(row["evidence_json"])}
            for row in rows
        ]

    def approve(self, series_id: str, candidate_id: str, character_id: str, canonical_name: str | None = None) -> dict:
        if not self._IDENTITY.fullmatch(character_id):
            raise ValueError("character_id must contain only lowercase letters, digits, and underscores.")
        with self.database.connect() as connection:
            candidate = connection.execute(
                "SELECT * FROM character_candidates WHERE candidate_id=? AND series_id=? AND status='proposed'",
                (candidate_id, series_id),
            ).fetchone()
            if not candidate:
                raise ValueError("Proposed character candidate not found.")
            name = (canonical_name or candidate["canonical_name"]).strip()
            if not name:
                raise ValueError("canonical_name cannot be empty.")
            existing = connection.execute("SELECT series_id, canonical_name FROM characters WHERE character_id=?", (character_id,)).fetchone()
            if existing and existing["series_id"] != series_id:
                raise ValueError("character_id is already assigned to another series.")
            if not existing:
                connection.execute(
                    "INSERT INTO characters(character_id,series_id,canonical_name) VALUES(?,?,?)",
                    (character_id, series_id, name),
                )
            elif existing["canonical_name"] != name:
                # Renaming is allowed, but the stable internal ID is not.
                connection.execute("UPDATE characters SET canonical_name=? WHERE character_id=?", (name, character_id))
            aliases = {name, candidate["canonical_name"], *json.loads(candidate["aliases_json"])}
            for alias in aliases:
                if alias.strip():
                    connection.execute(
                        "INSERT OR IGNORE INTO character_aliases(character_id,alias) VALUES(?,?)",
                        (character_id, alias.strip()),
                    )
            connection.execute(
                "UPDATE character_candidates SET status='approved', reviewed_at=? WHERE candidate_id=?",
                (datetime.now(timezone.utc).isoformat(), candidate_id),
            )
        return {"candidate_id": candidate_id, "character_id": character_id, "canonical_name": name, "status": "approved"}

    def reject(self, series_id: str, candidate_id: str) -> dict:
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE character_candidates SET status='rejected', reviewed_at=? WHERE candidate_id=? AND series_id=? AND status='proposed'",
                (datetime.now(timezone.utc).isoformat(), candidate_id, series_id),
            ).rowcount
        if not updated:
            raise ValueError("Proposed character candidate not found.")
        return {"candidate_id": candidate_id, "status": "rejected"}
