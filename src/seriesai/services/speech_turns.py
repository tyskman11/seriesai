"""Local, resumable speech-turn preparation for later diarization and voice review.

This intentionally does not claim to identify speakers.  It preserves clean
transcript-derived speech windows so a real local diarization backend can be
plugged in later without reprocessing every MP4.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ..config import AppConfig
from ..db import Database


@dataclass(frozen=True)
class SpeechTurnResult:
    episode_id: str
    turn_count: int
    status: str


class SpeechTurnService:
    stage = "speech_turn_preparation"

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    @staticmethod
    def group_segments(segments: list[dict], max_gap: float = 0.45, max_duration: float = 18.0) -> list[tuple[float, float]]:
        turns: list[tuple[float, float]] = []
        start = end = None
        for segment in segments:
            segment_start, segment_end = float(segment["start_seconds"]), float(segment["end_seconds"])
            if start is None or segment_start - end > max_gap or segment_end - start > max_duration:
                if start is not None:
                    turns.append((start, end))
                start, end = segment_start, segment_end
            else:
                end = segment_end
        if start is not None:
            turns.append((start, end))
        return turns

    def prepare_episode(self, episode_id: str) -> SpeechTurnResult:
        with self.database.connect() as connection:
            done = connection.execute("SELECT status FROM analysis_runs WHERE episode_id=? AND stage_name=?", (episode_id, self.stage)).fetchone()
            if done and done[0] == "completed":
                count = connection.execute("SELECT COUNT(*) FROM speaker_segments WHERE episode_id=? AND source_backend='transcript_turns'", (episode_id,)).fetchone()[0]
                return SpeechTurnResult(episode_id, count, "already_prepared")
            rows = connection.execute(
                "SELECT start_seconds,end_seconds FROM transcript_segments WHERE transcript_id=(SELECT transcript_id FROM transcripts WHERE episode_id=? AND status='completed') ORDER BY sequence_number",
                (episode_id,),
            ).fetchall()
        if not rows:
            raise ValueError("Completed transcript required before speech-turn preparation.")
        turns = self.group_segments([dict(row) for row in rows])
        self._stage(episode_id, "running", {"segment_count": len(rows)})
        try:
            with self.database.connect() as connection:
                connection.execute("DELETE FROM speaker_segments WHERE episode_id=? AND source_backend='transcript_turns'", (episode_id,))
                for index, (start, end) in enumerate(turns, start=1):
                    connection.execute(
                        "INSERT INTO speaker_segments(speaker_segment_id,episode_id,start_seconds,end_seconds,provisional_speaker_id,source_backend,status) VALUES(?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), episode_id, start, end, f"unassigned_turn_{index:04d}", "transcript_turns", "provisional"),
                    )
            self._stage(episode_id, "completed", {"turn_count": len(turns)})
            return SpeechTurnResult(episode_id, len(turns), "completed")
        except Exception as error:
            self._stage(episode_id, "failed", {"error": str(error)})
            raise

    def prepare_ready_episodes(self, series_id: str, progress: Callable[[int, str, str], None] | None = None) -> list[SpeechTurnResult]:
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT e.episode_id FROM episodes e JOIN transcripts t ON t.episode_id=e.episode_id AND t.status='completed' "
                "LEFT JOIN analysis_runs r ON r.episode_id=e.episode_id AND r.stage_name=? AND r.status='completed' "
                "WHERE e.series_id=? AND r.analysis_run_id IS NULL ORDER BY e.season_number,e.episode_number",
                (self.stage, series_id),
            )]
        results = []
        for index, episode_id in enumerate(ids, start=1):
            try:
                result = self.prepare_episode(episode_id)
            except Exception:
                result = SpeechTurnResult(episode_id, 0, "failed")
            results.append(result)
            if progress:
                progress(index, episode_id, result.status)
        return results

    def _stage(self, episode_id: str, status: str, checkpoint: dict) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO analysis_runs(analysis_run_id,episode_id,stage_name,status,language,checkpoint_json,finished_at,error_message) VALUES(?,?,?,?,?,?,CASE WHEN ? IN ('completed','failed') THEN CURRENT_TIMESTAMP END,?) "
                "ON CONFLICT(episode_id,stage_name) DO UPDATE SET status=excluded.status,checkpoint_json=excluded.checkpoint_json,finished_at=excluded.finished_at,error_message=excluded.error_message",
                (str(uuid.uuid4()), episode_id, self.stage, status, self.config.localization.analysis_language, json.dumps(checkpoint), status, checkpoint.get("error")),
            )
