"""Local face-reference selection for human character review.

The detector only finds a clear face in a candidate's evidence episode.  It
does not claim that the face belongs to the named candidate; the dashboard
labels it accordingly for human verification.

Face detection uses OpenCV's YuNet DNN detector (models/opencv/face_detection_yunet_2023mar.onnx,
downloaded and verified by start.py). Older opencv-python releases shipped the
Haar cascade classifier used here previously; opencv-python 5.x removed it
(cv2.CascadeClassifier and cv2.data.haarcascades no longer exist), which made
every "reference image" silently fall back to the largest JPEG file in the
scene-frames folder by file size alone, with no guarantee a face - let alone
the right person - was even in frame.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative

YUNET_MODEL = Path(__file__).resolve().parent.parent.parent.parent / "models" / "opencv" / "face_detection_yunet_2023mar.onnx"
# Fraction of the tight face box added on each side so the crop includes hair/chin, not just facial landmarks.
CROP_MARGIN = 0.6


class CharacterReferenceImageService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database
        self._detector = None

    def _load_detector(self):
        if self._detector is None:
            import cv2
            if not YUNET_MODEL.is_file():
                raise FileNotFoundError(f"Face detection model missing: {YUNET_MODEL}; python start.py laedt es.")
            self._detector = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (320, 320), 0.8, 0.3, 5000)
        return self._detector

    @staticmethod
    def evidence_episode_ids(evidence: list[str]) -> list[str]:
        ids: list[str] = []
        for item in evidence:
            ids.extend(re.findall(r"[a-z0-9_]+_s\d{2}e\d{2}", item.lower()))
        return list(dict.fromkeys(ids))

    def create_for_series(self, series_id: str, force: bool = False) -> dict:
        with self.database.connect() as connection:
            candidates = connection.execute("SELECT candidate_id,evidence_json FROM character_candidates WHERE series_id=? AND status IN ('proposed','approved')", (series_id,)).fetchall()
        created = skipped = 0
        for candidate in candidates:
            if self.create_for_candidate(candidate["candidate_id"], json.loads(candidate["evidence_json"]), force=force):
                created += 1
            else:
                skipped += 1
        return {"series_id": series_id, "created": created, "skipped": skipped}

    def create_for_candidate(self, candidate_id: str, evidence: list[str], force: bool = False) -> bool:
        with self.database.connect() as connection:
            existing = connection.execute("SELECT reference_id,file_path FROM visual_references WHERE entity_type='character_candidate' AND entity_id=? AND reference_kind='review_face'", (candidate_id,)).fetchone()
            if existing and not force:
                return False
            episode_ids = self.evidence_episode_ids(evidence)
            audio_rows = [connection.execute("SELECT file_path FROM import_artifacts WHERE episode_id=? AND artifact_type='audio_16khz_mono' AND status='completed'", (episode_id,)).fetchone() for episode_id in episode_ids]
        best: tuple[float, Path, tuple[int, int, int, int], str] | None = None  # area, frame_path, crop_box, episode_id
        for episode_id, audio in zip(episode_ids, audio_rows):
            if not audio:
                continue
            frame_dir = project_path(self.config.paths.project_root, audio["file_path"]).parent / "scene_frames"
            found = self._best_face(frame_dir)
            if found and (best is None or found[0] > best[0]):
                best = (*found, episode_id)
        if not best:
            return False
        area, frame_path, box, episode_id = best
        crop_path = frame_path.parent.parent / "character_face_crops" / f"{candidate_id}.jpg"
        self._save_crop(frame_path, box, crop_path)
        with self.database.connect() as connection:
            if existing:
                connection.execute("UPDATE visual_references SET file_path=?,quality_score=?,source_episode_id=? WHERE reference_id=?", (project_relative(self.config.paths.project_root, crop_path), min(1.0, area / 40000), episode_id, existing["reference_id"]))
            else:
                connection.execute("INSERT INTO visual_references(reference_id,entity_type,entity_id,reference_kind,file_path,quality_score,source_episode_id) VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), "character_candidate", candidate_id, "review_face", project_relative(self.config.paths.project_root, crop_path), min(1.0, area / 40000), episode_id))
        return True

    def _best_face(self, frame_dir: Path) -> tuple[float, Path, tuple[int, int, int, int]] | None:
        """The largest, most confident detected face across every scene frame of one episode."""
        if not frame_dir.is_dir():
            return None
        import cv2
        detector = self._load_detector()
        best: tuple[float, Path, tuple[int, int, int, int]] | None = None
        for path in sorted(frame_dir.glob("*.jpg")):
            image = cv2.imread(str(path))
            if image is None:
                continue
            height, width = image.shape[:2]
            detector.setInputSize((width, height))
            _, faces = detector.detect(image)
            if faces is None:
                continue
            for face in faces:
                x, y, w, h, confidence = face[0], face[1], face[2], face[3], face[14]
                if confidence < 0.85 or w < 40 or h < 40:
                    continue
                area = float(w * h)
                if best is None or area > best[0]:
                    best = (area, path, (int(x), int(y), int(w), int(h)))
        return best

    @staticmethod
    def _save_crop(frame_path: Path, box: tuple[int, int, int, int], target: Path) -> None:
        import cv2
        image = cv2.imread(str(frame_path))
        height, width = image.shape[:2]
        x, y, w, h = box
        margin_x, margin_y = int(w * CROP_MARGIN), int(h * CROP_MARGIN)
        left, top = max(0, x - margin_x), max(0, y - int(margin_y * 1.3))  # a bit more headroom above than below
        right, bottom = min(width, x + w + margin_x), min(height, y + h + margin_y)
        target.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(target), image[top:bottom, left:right])
