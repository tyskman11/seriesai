from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from scenedetect import ContentDetector, detect

from ..config import AppConfig
from ..db import Database
from ..paths import project_path


@dataclass(frozen=True)
class SceneDetectionResult:
    episode_id: str
    scene_count: int
    frame_directory: Path
    status: str


class SceneDetectionService:
    """Detects scenes and one middle-frame reference per scene, never every frame."""

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database

    def analyze_episode(self, episode_id: str, threshold: float = 27.0) -> SceneDetectionResult:
        episode = self._episode(episode_id)
        artifact_dir = project_path(self.config.paths.project_root, episode["source_metadata_json"]).parent
        frame_dir = artifact_dir / "scene_frames"
        completed = self._stage(episode_id)
        if completed and completed["status"] == "completed":
            count = self._scene_count(episode_id)
            return SceneDetectionResult(episode_id, count, frame_dir, "already_analyzed")

        if episode["source_deleted"]:
            raise FileNotFoundError(f"Rohvideo fuer {episode_id} wurde nach Training geloescht (cleanup-raw-video); fuer eine (erneute) Szenenerkennung muesste die Episode neu importiert werden.")
        frame_dir.mkdir(parents=True, exist_ok=True)
        self._stage_upsert(episode_id, "running", json.dumps({"threshold": threshold, "frame_directory": str(frame_dir)}))
        source_path = project_path(self.config.paths.project_root, episode["source_path"])
        scene_list = detect(str(source_path), ContentDetector(threshold=threshold), show_progress=False)
        detected = []
        for position, (start, end) in enumerate(scene_list, start=1):
            start_seconds, end_seconds = start.get_seconds(), end.get_seconds()
            midpoint = (start_seconds + end_seconds) / 2
            output = frame_dir / f"scene_{position:04d}_{midpoint:010.3f}.jpg"
            if not output.exists():
                self._extract_frame(source_path, midpoint, output)
            detected.append((f"{episode_id}_scene_{position:04d}", position, start_seconds, end_seconds))
        # Keep the write lock short: FFmpeg frame work has already completed above.
        with self.database.connect() as connection:
            connection.execute("DELETE FROM shots WHERE scene_id IN (SELECT scene_id FROM scenes WHERE episode_id = ?)", (episode_id,))
            connection.execute("DELETE FROM scenes WHERE episode_id = ?", (episode_id,))
            connection.executemany("INSERT INTO scenes(scene_id, episode_id, sequence_number, start_seconds, end_seconds) VALUES (?, ?, ?, ?, ?)", [(scene_id, episode_id, position, start, end) for scene_id, position, start, end in detected])
        self._stage_upsert(episode_id, "completed", json.dumps({"threshold": threshold, "frame_directory": str(frame_dir), "scene_count": len(scene_list)}))
        return SceneDetectionResult(episode_id, len(scene_list), frame_dir, "completed")

    def analyze_ready_episodes(self, series_id: str, threshold: float = 27.0, progress: Callable[[int, str, str], None] | None = None) -> list[SceneDetectionResult]:
        with self.database.connect() as connection:
            episode_ids = [row[0] for row in connection.execute("SELECT e.episode_id FROM episodes e JOIN import_artifacts a ON a.episode_id = e.episode_id LEFT JOIN analysis_runs r ON r.episode_id = e.episode_id AND r.stage_name = 'scene_detection' AND r.status = 'completed' WHERE e.series_id = ? AND a.artifact_type = 'audio_16khz_mono' AND a.status = 'completed' AND r.analysis_run_id IS NULL ORDER BY e.season_number, e.episode_number", (series_id,))]
        results = []
        for index, episode_id in enumerate(episode_ids, start=1):
            result = self.analyze_episode(episode_id, threshold)
            results.append(result)
            if progress: progress(index, episode_id, result.status)
        return results

    def _episode(self, episode_id: str):
        with self.database.connect() as connection:
            row = connection.execute("SELECT episode_id, source_path, source_metadata_json, source_deleted FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown episode: {episode_id}")
        return row

    def _stage(self, episode_id: str):
        with self.database.connect() as connection:
            return connection.execute("SELECT status FROM analysis_runs WHERE episode_id = ? AND stage_name = 'scene_detection'", (episode_id,)).fetchone()

    def _stage_upsert(self, episode_id: str, status: str, checkpoint_json: str) -> None:
        with self.database.connect() as connection:
            connection.execute("INSERT INTO analysis_runs(analysis_run_id, episode_id, stage_name, status, language, checkpoint_json, finished_at) VALUES (?, ?, 'scene_detection', ?, ?, ?, CASE WHEN ? = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END) ON CONFLICT(episode_id, stage_name) DO UPDATE SET status=excluded.status, checkpoint_json=excluded.checkpoint_json, finished_at=excluded.finished_at", (str(uuid.uuid4()), episode_id, status, self.config.localization.analysis_language, checkpoint_json, status))

    def _scene_count(self, episode_id: str) -> int:
        with self.database.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM scenes WHERE episode_id = ?", (episode_id,)).fetchone()[0])

    def _extract_frame(self, source: Path, timestamp: float, output: Path) -> None:
        temporary = output.with_suffix(".partial.jpg")
        try:
            subprocess.run([self.config.paths.ffmpeg, "-y", "-ss", f"{timestamp:.3f}", "-i", str(source), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(temporary)], check=True, capture_output=True, text=True, timeout=120)
            temporary.replace(output)
        except subprocess.CalledProcessError as error:
            if temporary.exists():
                temporary.unlink()
            raise RuntimeError(error.stderr[-2000:]) from error
