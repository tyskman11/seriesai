from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from ..backends.faster_whisper import FasterWhisperBackend
from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative


@dataclass(frozen=True)
class TranscriptionResult:
    episode_id: str
    segment_count: int
    transcript_path: Path
    status: str


class TranscriptionService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database

    def transcribe_episode(self, episode_id: str) -> TranscriptionResult:
        existing = self._existing(episode_id)
        if existing and existing["status"] == "completed":
            return TranscriptionResult(episode_id, self._segment_count(existing["transcript_id"]), Path(existing["json_path"]), "already_transcribed")
        audio = self._audio_artifact(episode_id)
        audio_path = project_path(self.config.paths.project_root, audio["file_path"])
        cache = audio_path.parent
        json_path, text_path = cache / "transcript_de.json", cache / "transcript_de.txt"
        self._stage(episode_id, "running", json.dumps({"audio": audio["file_path"]}))
        backend = FasterWhisperBackend(self.config.transcription.model, self.config.paths.project_root / "models" / "whisper", self.config.transcription.device, self.config.transcription.compute_type, self.config.transcription.beam_size)
        try:
            result = self._pool_result(episode_id) or backend.transcribe(str(audio_path), language=self.config.localization.analysis_language)
            json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            text_path.write_text("\n".join(item["text"] for item in result["segments"]), encoding="utf-8")
            transcript_id = str(uuid.uuid4())
            with self.database.connect() as connection:
                connection.execute("INSERT INTO transcripts(transcript_id, episode_id, backend_name, model_name, language, text_path, json_path, status) VALUES (?, ?, 'faster_whisper', ?, ?, ?, ?, 'completed') ON CONFLICT(episode_id) DO UPDATE SET backend_name=excluded.backend_name, model_name=excluded.model_name, language=excluded.language, text_path=excluded.text_path, json_path=excluded.json_path, status='completed'", (transcript_id, episode_id, self.config.transcription.model, self.config.localization.analysis_language, project_relative(self.config.paths.project_root, text_path), project_relative(self.config.paths.project_root, json_path)))
                stored_id = connection.execute("SELECT transcript_id FROM transcripts WHERE episode_id = ?", (episode_id,)).fetchone()[0]
                connection.execute("DELETE FROM transcript_segments WHERE transcript_id = ?", (stored_id,))
                for index, segment in enumerate(result["segments"], start=1):
                    connection.execute("INSERT INTO transcript_segments(segment_id, transcript_id, sequence_number, start_seconds, end_seconds, text, language_probability, no_speech_probability) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), stored_id, index, segment["start"], segment["end"], segment["text"], result["language_probability"], segment["no_speech_prob"]))
            self._stage(episode_id, "completed", json.dumps({"json": str(json_path), "text": str(text_path)}))
            return TranscriptionResult(episode_id, len(result["segments"]), json_path, "completed")
        except Exception as error:
            self._stage(episode_id, "failed", json.dumps({"audio": audio["file_path"]}), str(error))
            raise

    def transcribe_ready_episodes(self, series_id: str, progress: Callable[[int, str, str], None] | None = None) -> list[TranscriptionResult]:
        with self.database.connect() as connection:
            episode_ids = [row[0] for row in connection.execute("SELECT e.episode_id FROM episodes e JOIN import_artifacts a ON a.episode_id = e.episode_id LEFT JOIN transcripts t ON t.episode_id = e.episode_id AND t.status = 'completed' WHERE e.series_id = ? AND a.artifact_type = 'audio_16khz_mono' AND a.status = 'completed' AND t.transcript_id IS NULL ORDER BY e.season_number, e.episode_number", (series_id,))]
        results: list[TranscriptionResult] = []
        for index, episode_id in enumerate(episode_ids, start=1):
            result = self.transcribe_episode(episode_id)
            results.append(result)
            if progress: progress(index, episode_id, result.status)
        return results

    def _pool_result(self, episode_id: str) -> dict | None:
        """Transkript aus dem Power Pool (pool/results/<quelldatei>.transcript.json), falls Teilnehmer es berechnet haben."""
        with self.database.connect() as connection:
            row = connection.execute("SELECT source_path FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        if not row or not row[0]:
            return None
        pooled = self.config.paths.project_root / "pool" / "results" / f"{Path(row[0]).stem}.transcript.json"
        if not pooled.is_file():
            return None
        data = json.loads(pooled.read_text(encoding="utf-8"))
        return {"language": self.config.localization.analysis_language, "language_probability": 1.0,
                "segments": [{"start": g["start"], "end": g["end"], "text": g["text"].strip(), "avg_logprob": 0.0, "no_speech_prob": 0.0} for g in data["segments"] if g["text"].strip()]}

    def _existing(self, episode_id: str):
        with self.database.connect() as connection:
            return connection.execute("SELECT transcript_id, json_path, status FROM transcripts WHERE episode_id = ?", (episode_id,)).fetchone()

    def _audio_artifact(self, episode_id: str):
        with self.database.connect() as connection:
            row = connection.execute("SELECT file_path FROM import_artifacts WHERE episode_id = ? AND artifact_type = 'audio_16khz_mono' AND status = 'completed'", (episode_id,)).fetchone()
        if row is None:
            raise ValueError(f"No completed audio artifact for {episode_id}")
        return row

    def _stage(self, episode_id: str, status: str, checkpoint_json: str, error: str | None = None) -> None:
        with self.database.connect() as connection:
            connection.execute("INSERT INTO analysis_runs(analysis_run_id, episode_id, stage_name, status, language, checkpoint_json, error_message, finished_at) VALUES (?, ?, 'transcription', ?, ?, ?, ?, CASE WHEN ? IN ('completed','failed') THEN CURRENT_TIMESTAMP ELSE NULL END) ON CONFLICT(episode_id, stage_name) DO UPDATE SET status=excluded.status, checkpoint_json=excluded.checkpoint_json, error_message=excluded.error_message, finished_at=excluded.finished_at", (str(uuid.uuid4()), episode_id, status, self.config.localization.analysis_language, checkpoint_json, error, status))

    def _segment_count(self, transcript_id: str) -> int:
        with self.database.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?", (transcript_id,)).fetchone()[0])
