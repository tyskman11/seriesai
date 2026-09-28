"""Create compact, reviewable local voice clips from prepared speech turns."""
from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative


@dataclass(frozen=True)
class VoiceCandidateResult:
    episode_id: str
    candidate_count: int
    status: str


class VoiceCandidateService:
    stage = "voice_candidate_extraction"

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    @staticmethod
    def select_turns(turns: list[dict], limit: int = 3) -> list[dict]:
        """Prefer medium-length clips and distribute picks across an episode."""
        eligible = [turn for turn in turns if 3.0 <= turn["duration"] <= 12.0]
        ranked = sorted(eligible, key=lambda turn: (abs(turn["duration"] - 7.0), turn["start_seconds"]))
        selected: list[dict] = []
        for turn in ranked:
            if all(abs(turn["start_seconds"] - prior["start_seconds"]) >= 120 for prior in selected):
                selected.append(turn)
            if len(selected) == limit:
                break
        return selected

    def extract_episode(self, episode_id: str) -> VoiceCandidateResult:
        with self.database.connect() as connection:
            completed = connection.execute("SELECT status FROM analysis_runs WHERE episode_id=? AND stage_name=?", (episode_id, self.stage)).fetchone()
            if completed and completed[0] == "completed":
                count = connection.execute("SELECT COUNT(*) FROM voice_sample_candidates WHERE episode_id=?", (episode_id,)).fetchone()[0]
                return VoiceCandidateResult(episode_id, count, "already_extracted")
            audio = connection.execute("SELECT file_path FROM import_artifacts WHERE episode_id=? AND artifact_type='audio_16khz_mono' AND status='completed'", (episode_id,)).fetchone()
            turns = [dict(row) for row in connection.execute("SELECT speaker_segment_id,start_seconds,end_seconds FROM speaker_segments WHERE episode_id=? AND source_backend='transcript_turns' ORDER BY start_seconds", (episode_id,))]
        if not audio or not turns:
            raise ValueError("Completed audio and prepared speech turns are required.")
        for turn in turns:
            turn["duration"] = turn["end_seconds"] - turn["start_seconds"]
        selected = self.select_turns(turns)
        self._stage(episode_id, "running", {"selected": len(selected)})
        target_dir = self.config.paths.series_root / "voice_candidates" / episode_id
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self.database.connect() as connection:
                for index, turn in enumerate(selected, start=1):
                    target = target_dir / f"turn_{index:02d}_{turn['start_seconds']:.2f}.ogg"
                    self._extract(project_path(self.config.paths.project_root, audio["file_path"]), target, turn["start_seconds"], turn["duration"])
                    quality = max(0.0, 1 - abs(turn["duration"] - 7.0) / 7.0)
                    connection.execute(
                        "INSERT OR IGNORE INTO voice_sample_candidates(candidate_id,episode_id,speaker_segment_id,file_path,duration_seconds,quality_score) VALUES(?,?,?,?,?,?)",
                        (str(uuid.uuid4()), episode_id, turn["speaker_segment_id"], project_relative(self.config.paths.project_root, target), turn["duration"], quality),
                    )
            self._stage(episode_id, "completed", {"candidate_count": len(selected)})
            return VoiceCandidateResult(episode_id, len(selected), "completed")
        except Exception as error:
            self._stage(episode_id, "failed", {"error": str(error)})
            raise

    def extract_ready_episodes(self, series_id: str, progress: Callable[[int, str, str], None] | None = None) -> list[VoiceCandidateResult]:
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT e.episode_id FROM episodes e JOIN speaker_segments s ON s.episode_id=e.episode_id AND s.source_backend='transcript_turns' "
                "LEFT JOIN analysis_runs r ON r.episode_id=e.episode_id AND r.stage_name=? AND r.status='completed' "
                "WHERE e.series_id=? AND r.analysis_run_id IS NULL GROUP BY e.episode_id ORDER BY e.season_number,e.episode_number",
                (self.stage, series_id),
            )]
        results = []
        for index, episode_id in enumerate(ids, start=1):
            try:
                result = self.extract_episode(episode_id)
            except Exception:
                result = VoiceCandidateResult(episode_id, 0, "failed")
            results.append(result)
            if progress:
                progress(index, episode_id, result.status)
        return results

    def _extract(self, audio: Path, target: Path, start: float, duration: float) -> None:
        temporary = target.with_suffix(".partial.ogg")
        try:
            subprocess.run([self.config.paths.ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(audio), "-t", f"{duration:.3f}", "-ac", "1", "-c:a", "libopus", "-b:a", "24k", str(temporary)], check=True, capture_output=True, text=True, timeout=120)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _stage(self, episode_id: str, status: str, checkpoint: dict) -> None:
        with self.database.connect() as connection:
            connection.execute("INSERT INTO analysis_runs(analysis_run_id,episode_id,stage_name,status,language,checkpoint_json,finished_at,error_message) VALUES(?,?,?,?,?,?,CASE WHEN ? IN ('completed','failed') THEN CURRENT_TIMESTAMP END,?) ON CONFLICT(episode_id,stage_name) DO UPDATE SET status=excluded.status,checkpoint_json=excluded.checkpoint_json,finished_at=excluded.finished_at,error_message=excluded.error_message", (str(uuid.uuid4()), episode_id, self.stage, status, self.config.localization.analysis_language, json.dumps(checkpoint), status, checkpoint.get("error")))
