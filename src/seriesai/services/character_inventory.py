"""Create reviewable, evidence-backed character candidates from Series Brain data."""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from pydantic import BaseModel, Field

from ..backends.llamacpp import LlamaCppBackend
from ..config import AppConfig
from ..db import Database


class CharacterCandidate(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    role_description: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=5)


class CharacterInventoryOutput(BaseModel):
    candidates: list[CharacterCandidate] = Field(default_factory=list, max_length=80)


class CharacterInventoryService:
    """A proposal step; it never creates canonical ``characters`` rows."""

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def propose(self, series_id: str) -> dict:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT e.episode_id, r.checkpoint_json FROM episodes e "
                "JOIN analysis_runs r ON r.episode_id=e.episode_id "
                "WHERE e.series_id=? AND r.stage_name='semantic_analysis' AND r.status='completed' "
                "ORDER BY e.season_number, e.episode_number",
                (series_id,),
            ).fetchall()
        if not rows:
            raise ValueError("Completed semantic analyses are required before character inventory.")

        evidence = []
        for row in rows:
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
            summary = checkpoint.get("summary_de", "")
            if summary:
                evidence.append(f"{row['episode_id']}: {summary}")
        source = "\n".join(evidence)
        signature = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT COUNT(*) FROM character_candidates WHERE series_id=? AND source_signature=?",
                (series_id, signature),
            ).fetchone()[0]
        if existing:
            return {"series_id": series_id, "status": "already_proposed", "candidates": existing}

        prompt = (
            "Erstelle eine konservative Figureninventur fuer eine Fernsehserie aus den "
            "folgenden Episodenzusammenfassungen. Nenne nur wiederkehrende oder klar "
            "benannte Figuren. Fuehre Spitznamen als Aliase zusammen. Keine erfundenen "
            "Namen, keine Schauspielernamen, keine einmaligen Hintergrundpersonen. "
            "Die evidence-Eintraege muessen konkrete Episoden-IDs oder kurze belegbare Hinweise enthalten.\n\n"
            + source[:110_000]
        )
        output = LlamaCppBackend(self.config.llm.base_url, self.config.llm.model_name, timeout_seconds=self.config.llm.timeout_seconds).structured_generate(
            prompt, CharacterInventoryOutput, system="Du bist ein vorsichtiger deutscher Serienkontinuitaets-Archivist."
        )
        candidates = output.candidates or self._recurring_name_candidates(evidence)
        inserted = 0
        with self.database.connect() as connection:
            for candidate in candidates:
                name = re.sub(r"\s+", " ", candidate.canonical_name).strip()
                if not name:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO character_candidates(candidate_id,series_id,canonical_name,aliases_json,role_description,confidence,evidence_json,source_signature) VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), series_id, name, json.dumps(candidate.aliases, ensure_ascii=False), candidate.role_description, candidate.confidence, json.dumps(candidate.evidence, ensure_ascii=False), signature),
                )
                inserted += 1
        return {"series_id": series_id, "status": "proposed", "candidates": inserted}

    @staticmethod
    def _recurring_name_candidates(evidence: list[str]) -> list[CharacterCandidate]:
        """Low-confidence local fallback when a small LLM returns an empty list.

        It deliberately proposes only title-cased strings occurring in at least
        two episode summaries.  They remain review-only candidates, never
        canonical character identities.
        """
        ignored = {
            "Die", "Der", "Das", "Ein", "Eine", "Und", "Mit", "Von", "Zu", "In", "Am", "Im", "Auf", "Als", "Sie", "Ihr", "Wir", "Er", "Es", "Game", "Games", "Game Shakers", "Shakers", "Serie", "Episode", "Brooklyn", "Skywhale", "Team", "Das Team", "Kinder", "Firma", "Spiel", "Spiele", "Serienereignisse", "Ereignisse", "Hauptereignis", "Transkriptabschnitt", "Deutsch", "JSON", "Person", "Freunde", "Problem", "Reihe", "Vater", "Arm", "Boot", "Preis", "Menschheit",
        }
        occurrences: dict[str, list[str]] = {}
        pattern = re.compile(r"\b[A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]*)?\b")
        for item in evidence:
            episode_id = item.partition(":")[0]
            for name in set(pattern.findall(item)):
                if name not in ignored and len(name) > 2:
                    occurrences.setdefault(name, []).append(episode_id)
        return [
            CharacterCandidate(
                canonical_name=name,
                role_description="Automatisch als wiederkehrende Namensnennung erkannt; vor Freigabe prüfen.",
                confidence=min(0.55, 0.25 + len(episodes) * 0.03),
                evidence=[f"Wiederkehrend in: {', '.join(episodes[:5])}"],
            )
            for name, episodes in sorted(occurrences.items(), key=lambda pair: (-len(pair[1]), pair[0]))
            if len(episodes) >= 5
        ]
