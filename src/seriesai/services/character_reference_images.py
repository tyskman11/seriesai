"""Local face-reference selection for human character review.

The detector only finds a clear face in a candidate's evidence episode.  It
does not claim that the face belongs to the named candidate; the dashboard
labels it accordingly for human verification.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative


class CharacterReferenceImageService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    @staticmethod
    def evidence_episode_ids(evidence: list[str]) -> list[str]:
        ids: list[str] = []
        for item in evidence:
            ids.extend(re.findall(r"[a-z0-9_]+_s\d{2}e\d{2}", item.lower()))
        return list(dict.fromkeys(ids))

    def create_for_series(self, series_id: str) -> dict:
        with self.database.connect() as connection:
            candidates = connection.execute("SELECT candidate_id,evidence_json FROM character_candidates WHERE series_id=? AND status IN ('proposed','approved')", (series_id,)).fetchall()
        created = skipped = 0
        for candidate in candidates:
            if self.create_for_candidate(candidate["candidate_id"], json.loads(candidate["evidence_json"])):
                created += 1
            else:
                skipped += 1
        return {"series_id": series_id, "created": created, "skipped": skipped}

    def create_for_candidate(self, candidate_id: str, evidence: list[str]) -> bool:
        with self.database.connect() as connection:
            existing = connection.execute("SELECT 1 FROM visual_references WHERE entity_type='character_candidate' AND entity_id=? AND reference_kind='review_face'", (candidate_id,)).fetchone()
            if existing:
                return False
            episode_ids = self.evidence_episode_ids(evidence)
            audio_rows = [connection.execute("SELECT file_path FROM import_artifacts WHERE episode_id=? AND artifact_type='audio_16khz_mono' AND status='completed'", (episode_id,)).fetchone() for episode_id in episode_ids]
        for episode_id, audio in zip(episode_ids, audio_rows):
            if not audio:
                continue
            frame_dir = project_path(self.config.paths.project_root, audio["file_path"]).parent / "scene_frames"
            frame = self._best_face_frame(frame_dir)
            if frame:
                with self.database.connect() as connection:
                    connection.execute("INSERT INTO visual_references(reference_id,entity_type,entity_id,reference_kind,file_path,quality_score,source_episode_id) VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), "character_candidate", candidate_id, "review_face", project_relative(self.config.paths.project_root, frame), 0.4, episode_id))
                return True
        return False

    @staticmethod
    def _best_face_frame(frame_dir: Path) -> Path | None:
        if not frame_dir.is_dir():
            return None
        import cv2
        frames = list(frame_dir.glob("*.jpg"))
        if not frames:
            return None
        # Some lightweight OpenCV builds expose image decoding but omit the
        # cascade classifier.  A deterministic largest-frame fallback still
        # gives the reviewer an episode reference instead of failing the job.
        if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
            return max(frames, key=lambda path: path.stat().st_size)
        cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        best: tuple[int, Path] | None = None
        for path in frames:
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            faces = cascade.detectMultiScale(image, scaleFactor=1.15, minNeighbors=5, minSize=(55, 55))
            area = max((int(width * height) for _, _, width, height in faces), default=0)
            if area and (best is None or area > best[0]):
                best = (area, path)
        return best[1] if best else max(frames, key=lambda path: path.stat().st_size)
