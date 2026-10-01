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
            name_collision = connection.execute(
                "SELECT 1 FROM characters WHERE series_id=? AND character_id!=? AND canonical_name=?",
                (series_id, character_id, name),
            ).fetchone()
            if name_collision:
                raise ValueError(f"Another character in this series is already named '{name}'.")
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
            # Copy the candidate's reviewed face crop to a character_id-keyed reference, so it
            # can still be found once candidate_id and character_id have nothing else in common
            # (approve() lets the reviewer pick an arbitrary character_id for the candidate_id).
            face = connection.execute(
                "SELECT file_path,quality_score,source_episode_id FROM visual_references WHERE entity_type='character_candidate' AND entity_id=? AND reference_kind IN ('review_face_crop','review_face') "
                "ORDER BY CASE reference_kind WHEN 'review_face_crop' THEN 0 ELSE 1 END LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if face:
                connection.execute(
                    "INSERT INTO visual_references(reference_id,entity_type,entity_id,reference_kind,file_path,quality_score,source_episode_id) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(reference_id) DO UPDATE SET file_path=excluded.file_path,quality_score=excluded.quality_score,source_episode_id=excluded.source_episode_id",
                    (f"approved_face:{character_id}", "character", character_id, "approved_face", face["file_path"], face["quality_score"], face["source_episode_id"]),
                )
        return {"candidate_id": candidate_id, "character_id": character_id, "canonical_name": name, "status": "approved"}

    def create_manual(self, series_id: str, character_id: str, canonical_name: str) -> dict:
        """Add a canonical character directly, without an AI-proposed candidate (a human already recognized them)."""
        if not self._IDENTITY.fullmatch(character_id):
            raise ValueError("character_id must contain only lowercase letters, digits, and underscores.")
        name = canonical_name.strip()
        if not name:
            raise ValueError("canonical_name cannot be empty.")
        with self.database.connect() as connection:
            if not connection.execute("SELECT 1 FROM series WHERE series_id=?", (series_id,)).fetchone():
                raise ValueError(f"Unknown series: {series_id}")
            if connection.execute("SELECT 1 FROM characters WHERE character_id=?", (character_id,)).fetchone():
                raise ValueError("character_id is already in use.")
            connection.execute("INSERT INTO characters(character_id,series_id,canonical_name) VALUES(?,?,?)", (character_id, series_id, name))
            # A matching candidate_id=character_id keeps the manual character visible in the existing review list.
            connection.execute(
                "INSERT INTO character_candidates(candidate_id,series_id,canonical_name,aliases_json,role_description,confidence,evidence_json,status,source_signature,reviewed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (character_id, series_id, name, "[]", "Manuell hinzugefuegt.", 1.0, json.dumps(["Manuell hinzugefuegt."], ensure_ascii=False), "approved", f"manual:{character_id}", datetime.now(timezone.utc).isoformat()),
            )
            connection.execute("INSERT OR IGNORE INTO character_aliases(character_id,alias) VALUES(?,?)", (character_id, name))
        return {"character_id": character_id, "canonical_name": name, "status": "approved"}

    def rename(self, character_id: str, canonical_name: str) -> dict:
        """Rename an existing canonical character. The stable character_id never changes."""
        name = canonical_name.strip()
        if not name:
            raise ValueError("canonical_name cannot be empty.")
        with self.database.connect() as connection:
            series_row = connection.execute("SELECT series_id FROM characters WHERE character_id=?", (character_id,)).fetchone()
            if not series_row:
                raise ValueError(f"Unknown character_id: {character_id}")
            name_collision = connection.execute(
                "SELECT 1 FROM characters WHERE series_id=? AND character_id!=? AND canonical_name=?",
                (series_row["series_id"], character_id, name),
            ).fetchone()
            if name_collision:
                raise ValueError(f"Another character in this series is already named '{name}'.")
            updated = connection.execute("UPDATE characters SET canonical_name=? WHERE character_id=?", (name, character_id)).rowcount
            # Keep the review list's display name and the searchable aliases in sync.
            connection.execute("UPDATE character_candidates SET canonical_name=? WHERE candidate_id=?", (name, character_id))
            connection.execute("INSERT OR IGNORE INTO character_aliases(character_id,alias) VALUES(?,?)", (character_id, name))
        return {"character_id": character_id, "canonical_name": name, "status": "renamed"}

    def reject(self, series_id: str, candidate_id: str) -> dict:
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE character_candidates SET status='rejected', reviewed_at=? WHERE candidate_id=? AND series_id=? AND status='proposed'",
                (datetime.now(timezone.utc).isoformat(), candidate_id, series_id),
            ).rowcount
        if not updated:
            raise ValueError("Proposed character candidate not found.")
        return {"candidate_id": candidate_id, "status": "rejected"}
