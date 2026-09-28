from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative


_EPISODE_PATTERN = re.compile(r"S(?P<season>\d{1,2})E(?P<episode>\d{1,2})", re.IGNORECASE)


@dataclass(frozen=True)
class ImportResult:
    episode_id: str
    source_sha256: str
    status: str
    artifact_directory: Path


class EpisodeImporter:
    """Resumable local import stages; source videos are read-only inputs."""

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database

    def import_episode(self, source: Path | str, series_id: str, series_title: str) -> ImportResult:
        source_path = Path(source).resolve()
        if not source_path.is_file() or source_path.suffix.lower() != ".mp4":
            raise ValueError(f"Expected an existing MP4 file: {source_path}")
        source_hash = self._sha256(source_path)
        self._ensure_series(series_id, series_title)
        existing = self._episode_for_hash(series_id, source_hash)
        if existing:
            artifact_dir = project_path(self.config.paths.project_root, existing["source_metadata_json"]).parent
            audio_path = artifact_dir / "audio_16khz_mono.flac"
            if self._audio_complete(existing["episode_id"], audio_path):
                return ImportResult(existing["episode_id"], source_hash, "already_imported", artifact_dir)
            self._extract_audio(source_path, audio_path)
            self._upsert_artifact(existing["episode_id"], "audio_16khz_mono", audio_path, "completed")
            self._mark_analysis_stage(existing["episode_id"], "audio_extraction", "completed", checkpoint_json=json.dumps({"audio": str(audio_path)}))
            return ImportResult(existing["episode_id"], source_hash, "resumed", artifact_dir)

        season, episode = self._episode_numbers(source_path.name)
        episode_id = f"{series_id}_s{season:02d}e{episode:02d}"
        artifact_dir = self.config.paths.series_root / series_id / "cache" / "imports" / f"s{season:02d}e{episode:02d}-{source_hash[:12]}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = artifact_dir / "ffprobe.json"
        audio_path = artifact_dir / "audio_16khz_mono.flac"
        metadata = self._ffprobe(source_path)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        self._insert_episode(episode_id, series_id, source_path, season, episode, source_hash, metadata_path)
        self._upsert_artifact(episode_id, "ffprobe", metadata_path, "completed")
        if not audio_path.exists():
            self._extract_audio(source_path, audio_path)
        self._upsert_artifact(episode_id, "audio_16khz_mono", audio_path, "completed")
        self._mark_analysis_stage(episode_id, "audio_extraction", "completed", checkpoint_json=json.dumps({"audio": str(audio_path)}))
        return ImportResult(episode_id, source_hash, "imported", artifact_dir)

    def import_folder(self, folder: Path | str, series_id: str, series_title: str, progress: Callable[[int, str, str], None] | None = None) -> list[ImportResult]:
        """Imports MP4s in natural episode order; a failure leaves other episodes usable."""
        source_folder = Path(folder)
        files = sorted(source_folder.rglob("*.mp4"), key=self._sort_key)
        if not files:
            raise ValueError(f"No MP4 files found below: {source_folder}")
        results: list[ImportResult] = []
        for index, source in enumerate(files, start=1):
            try:
                result = self.import_episode(source, series_id, series_title)
                results.append(result)
                if progress: progress(index, source.name, result.status)
            except Exception as error:
                self._record_import_failure(series_id, source, error)
                results.append(ImportResult(f"failed:{source.name}", "", "failed", source))
                if progress: progress(index, source.name, "failed")
        return results

    def _record_import_failure(self, series_id: str, source: Path, error: Exception) -> None:
        log_path = self.config.paths.project_root / "logs" / "import.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{series_id}\t{source}\t{type(error).__name__}: {error}\n")

    @staticmethod
    def _sort_key(path: Path) -> tuple[int, int, str]:
        match = _EPISODE_PATTERN.search(path.name)
        if not match:
            return (9999, 9999, path.name.lower())
        return (int(match["season"]), int(match["episode"]), path.name.lower())

    def _ensure_series(self, series_id: str, title: str) -> None:
        with self.database.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO series(series_id, title, canonical_timezone) VALUES (?, ?, 'Europe/Berlin')", (series_id, title))

    def _episode_for_hash(self, series_id: str, source_hash: str):
        with self.database.connect() as connection:
            return connection.execute("SELECT episode_id, source_metadata_json FROM episodes WHERE series_id = ? AND source_sha256 = ?", (series_id, source_hash)).fetchone()

    def _audio_complete(self, episode_id: str, audio_path: Path) -> bool:
        with self.database.connect() as connection:
            row = connection.execute("SELECT status FROM import_artifacts WHERE episode_id = ? AND artifact_type = 'audio_16khz_mono'", (episode_id,)).fetchone()
        return bool(row and row["status"] == "completed" and audio_path.exists())

    def _insert_episode(self, episode_id: str, series_id: str, source: Path, season: int, episode: int, source_hash: str, metadata_path: Path) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO episodes(episode_id, series_id, season_number, episode_number, title, source_path, status, source_sha256, source_metadata_json, analysis_language) VALUES (?, ?, ?, ?, ?, ?, 'imported', ?, ?, ?)",
                (episode_id, series_id, season, episode, source.stem, project_relative(self.config.paths.project_root, source), source_hash, project_relative(self.config.paths.project_root, metadata_path), self.config.localization.analysis_language),
            )

    def _upsert_artifact(self, episode_id: str, artifact_type: str, path: Path, status: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO import_artifacts(artifact_id, episode_id, artifact_type, file_path, content_sha256, status) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(episode_id, artifact_type) DO UPDATE SET file_path=excluded.file_path, content_sha256=excluded.content_sha256, status=excluded.status",
                (str(uuid.uuid4()), episode_id, artifact_type, project_relative(self.config.paths.project_root, path), self._sha256(path), status),
            )

    def _mark_analysis_stage(self, episode_id: str, stage: str, status: str, checkpoint_json: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO analysis_runs(analysis_run_id, episode_id, stage_name, status, language, checkpoint_json, finished_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(episode_id, stage_name) DO UPDATE SET status=excluded.status, checkpoint_json=excluded.checkpoint_json, finished_at=excluded.finished_at",
                (str(uuid.uuid4()), episode_id, stage, status, self.config.localization.analysis_language, checkpoint_json),
            )

    @staticmethod
    def _episode_numbers(name: str) -> tuple[int, int]:
        match = _EPISODE_PATTERN.search(name)
        if not match:
            raise ValueError(f"Episode name needs an SxxExx identifier: {name}")
        return int(match["season"]), int(match["episode"])

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _ffprobe(self, source: Path) -> dict[str, object]:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)], check=True, capture_output=True, text=True, timeout=90)
        return json.loads(result.stdout)

    def _extract_audio(self, source: Path, target: Path) -> None:
        temporary = target.with_suffix(".partial.flac")
        try:
            subprocess.run([self.config.paths.ffmpeg, "-y", "-i", str(source), "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(temporary)], check=True, capture_output=True, text=True, timeout=3600)
            temporary.replace(target)
        except subprocess.CalledProcessError as error:
            if temporary.exists():
                temporary.unlink()
            raise RuntimeError(error.stderr[-2000:]) from error
