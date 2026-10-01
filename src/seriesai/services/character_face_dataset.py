"""Collect many identity-matched face crops per character for LoRA training.

character_reference_images.py picks exactly one "best guess" face per character,
trusted only because it comes from a human-reviewed evidence episode. That is
nowhere near enough images for LoRA training (15-50+ is typical). This service
reuses that single reviewed crop as an identity anchor: it computes a face
embedding for it with OpenCV's SFace recognizer (unlike YuNet, which only
answers "is there a face here", SFace answers "is this the SAME face"), then
scans every scene frame of every episode in the series and keeps crops whose
embedding is close enough to the anchor.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative
from .character_reference_images import YUNET_MODEL, CROP_MARGIN

SFACE_MODEL = Path(__file__).resolve().parent.parent.parent.parent / "models" / "opencv" / "face_recognition_sface_2021dec.onnx"

# YuNet detection quality gate, same bar as the single-image reviewer picker.
MIN_FACE_CONFIDENCE = 0.85
MIN_FACE_SIZE = 40
# Cosine similarity to the anchor face required to accept a candidate crop as
# "the same person". SFace's own recommended match threshold is ~0.363; a bit
# higher here trades recall for fewer wrong-person crops polluting the dataset.
IDENTITY_SIMILARITY_MIN = 0.42
DEFAULT_TARGET_COUNT = 40


class CharacterFaceDatasetService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database
        self._detector = None
        self._recognizer = None

    def _load_detector(self):
        if self._detector is None:
            import cv2
            if not YUNET_MODEL.is_file():
                raise FileNotFoundError(f"Face detection model missing: {YUNET_MODEL}; python start.py laedt es.")
            self._detector = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (320, 320), 0.8, 0.3, 5000)
        return self._detector

    def _load_recognizer(self):
        if self._recognizer is None:
            import cv2
            if not SFACE_MODEL.is_file():
                raise FileNotFoundError(f"Face recognition model missing: {SFACE_MODEL}; python start.py laedt es.")
            self._recognizer = cv2.FaceRecognizerSF.create(str(SFACE_MODEL), "")
        return self._recognizer

    def _embed_best_face(self, image, detector, recognizer):
        """Detect the largest qualifying face in an already-loaded image and return its embedding, or None."""
        height, width = image.shape[:2]
        detector.setInputSize((width, height))
        _, faces = detector.detect(image)
        if faces is None:
            return None
        best = None
        for face in faces:
            w, h, confidence = face[2], face[3], face[14]
            if confidence < MIN_FACE_CONFIDENCE or w < MIN_FACE_SIZE or h < MIN_FACE_SIZE:
                continue
            if best is None or w * h > best[2] * best[3]:
                best = face
        if best is None:
            return None
        aligned = recognizer.alignCrop(image, best)
        return recognizer.feature(aligned), best

    def collect_training_images(self, character_id: str, target_count: int = DEFAULT_TARGET_COUNT) -> dict:
        import cv2
        with self.database.connect() as connection:
            character = connection.execute("SELECT character_id,series_id,canonical_name FROM characters WHERE character_id=? AND active=1", (character_id,)).fetchone()
            if not character:
                raise ValueError("Approved canonical character not found.")
            candidate = connection.execute("SELECT candidate_id FROM character_candidates WHERE canonical_name=? AND status='approved' LIMIT 1", (character["canonical_name"],)).fetchone()
            anchor = connection.execute(
                "SELECT file_path FROM visual_references WHERE entity_type='character_candidate' AND entity_id=? AND reference_kind='review_face'",
                (candidate["candidate_id"],),
            ).fetchone() if candidate else None
            if not anchor:
                raise ValueError("No reviewed reference face exists yet for this character; run collect-reference-images first.")
            existing_count = connection.execute("SELECT COUNT(*) FROM visual_references WHERE entity_type='character' AND entity_id=? AND reference_kind='training_crop'", (character_id,)).fetchone()[0]
            episodes = connection.execute(
                "SELECT e.episode_id, a.file_path AS audio_path FROM episodes e JOIN import_artifacts a ON a.episode_id=e.episode_id AND a.artifact_type='audio_16khz_mono' AND a.status='completed' WHERE e.series_id=? ORDER BY e.season_number, e.episode_number",
                (character["series_id"],),
            ).fetchall()

        detector, recognizer = self._load_detector(), self._load_recognizer()
        anchor_image = cv2.imread(str(project_path(self.config.paths.project_root, anchor["file_path"])))
        if anchor_image is None:
            raise ValueError(f"Reference face image unreadable: {anchor['file_path']}")
        anchor_embedded = self._embed_best_face(anchor_image, detector, recognizer)
        if anchor_embedded is None:
            raise ValueError("No usable face detected in the existing reference image; cannot build an identity anchor.")
        anchor_embedding, _ = anchor_embedded

        collected = 0
        index = existing_count
        for episode in episodes:
            if collected >= target_count:
                break
            frame_dir = project_path(self.config.paths.project_root, episode["audio_path"]).parent / "scene_frames"
            if not frame_dir.is_dir():
                continue
            for frame_path in sorted(frame_dir.glob("*.jpg")):
                if collected >= target_count:
                    break
                image = cv2.imread(str(frame_path))
                if image is None:
                    continue
                found = self._embed_best_face(image, detector, recognizer)
                if found is None:
                    continue
                embedding, face = found
                score = float(recognizer.match(anchor_embedding, embedding, cv2.FaceRecognizerSF_FR_COSINE))
                if score < IDENTITY_SIMILARITY_MIN:
                    continue
                index += 1
                crop_path = frame_dir.parent / "character_face_crops" / character_id / f"{index:04d}.jpg"
                self._save_crop(image, (int(face[0]), int(face[1]), int(face[2]), int(face[3])), crop_path)
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT INTO visual_references(reference_id,entity_type,entity_id,reference_kind,file_path,quality_score,source_episode_id) VALUES(?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), "character", character_id, "training_crop", project_relative(self.config.paths.project_root, crop_path), score, episode["episode_id"]),
                    )
                collected += 1
        return {"character_id": character_id, "collected": collected, "total_training_crops": index}

    @staticmethod
    def _save_crop(image, box: tuple[int, int, int, int], target: Path) -> None:
        import cv2
        height, width = image.shape[:2]
        x, y, w, h = box
        margin_x, margin_y = int(w * CROP_MARGIN), int(h * CROP_MARGIN)
        left, top = max(0, x - margin_x), max(0, y - int(margin_y * 1.3))
        right, bottom = min(width, x + w + margin_x), min(height, y + h + margin_y)
        target.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(target), image[top:bottom, left:right])
