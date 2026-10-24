"""Train a per-character LoRA (image or video) for the local LTX-2.3 model.

Uses ai-toolkit (ostris/ai-toolkit, https://github.com/ostris/ai-toolkit), vendored on
first use like ComfyUI/llama.cpp but into its own git clone + its own Python venv (its
dependency set is heavy and would otherwise fight the project's own torch/TTS install).

HONEST CAVEAT (see this session's plan): ai-toolkit's LTX-2 support loads the model via
HuggingFace diffusers (toolkit/models/v2/diffusion_models/ltx2.py -> LTX2VideoTransformer3DModel),
not the quantized GGUF file this project's own inference pipeline uses - training needs the
full diffusers-format weights in memory, which is a much higher bar than the already-tight
6GB this machine has for inference alone. The LoRA weights themselves are architecture-
compatible either way (a LoRA is just small added deltas; the base model's quantization for
*inference* doesn't matter for loading them), so a successfully trained LoRA should still
work in render_shot() - the open question is purely whether *training* can run here at all.
image LoRA training has no such diffusers-vs-GGUF mismatch and is the safer first attempt.

ai-toolkit's exact LTX-2 job config schema could not be verified against a public working
example at the time this was written (no example file shipped yet, only the model class).
train() below is a best-effort config built from ai-toolkit's documented legacy schema; if
ai-toolkit rejects it, the fix is to look at whatever example config ships by then under
<ai_toolkit>/config/examples/ and adjust _build_config() to match - not to keep guessing blind.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import AppConfig
from ..db import Database
from ..paths import project_path
from .character_dataset import CharacterDatasetService

AI_TOOLKIT_REPO = "https://github.com/ostris/ai-toolkit.git"


class LoraTrainingService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    # -- data preparation -----------------------------------------------------
    def prepare_image_dataset(self, character_id: str) -> dict:
        return CharacterDatasetService(self.config, self.database).build(character_id)

    def prepare_video_clips(self, character_id: str, max_clips: int = 20) -> dict:
        """Cut short clips from the character's own scenes for video-LoRA training.
        Needs the episodes' raw source video - must run before cleanup-raw-video (Abschnitt D)."""
        with self.database.connect() as connection:
            character = connection.execute("SELECT character_id,series_id FROM characters WHERE character_id=? AND active=1", (character_id,)).fetchone()
            if not character:
                raise ValueError("Approved canonical character not found.")
            scenes = connection.execute(
                "SELECT DISTINCT s.episode_id,s.scene_id,s.start_seconds,s.end_seconds,e.source_path,e.source_deleted "
                "FROM character_events ev JOIN scenes s ON s.scene_id=ev.scene_id JOIN episodes e ON e.episode_id=s.episode_id "
                "WHERE ev.character_id=? AND s.start_seconds IS NOT NULL ORDER BY s.episode_id,s.start_seconds LIMIT ?",
                (character_id, max_clips),
            ).fetchall()
        if not scenes:
            raise ValueError("No scene-level appearances found for this character (character_events); run the semantic-analysis stage first.")
        root = self.config.paths.series_root / "datasets" / character_id / "video_clips"
        root.mkdir(parents=True, exist_ok=True)
        clip_seconds = self.config.training.video_clip_frames / 24
        entries = []
        for index, scene in enumerate(scenes, start=1):
            if scene["source_deleted"]:
                continue   # that episode's raw video is gone; skip rather than fail the whole batch
            source = project_path(self.config.paths.project_root, scene["source_path"])
            if not source.is_file():
                continue
            duration = min(clip_seconds, max(0.5, (scene["end_seconds"] or 0) - (scene["start_seconds"] or 0)))
            target = root / f"clip_{index:04d}.mp4"
            if not target.is_file():
                subprocess.run(
                    [self.config.paths.ffmpeg, "-y", "-ss", f"{scene['start_seconds']:.3f}", "-i", str(source), "-t", f"{duration:.3f}", "-an", "-vf", f"scale={self.config.training.resolution}:-2", str(target)],
                    check=True, capture_output=True, timeout=120,
                )
            caption_path = target.with_suffix(".txt")
            caption_path.write_text(f"{character_id}, character video reference", encoding="utf-8")
            entries.append({"clip": str(target), "source_episode_id": scene["episode_id"]})
        if not entries:
            raise ValueError("No video clips could be cut (every source episode's raw video is deleted or missing).")
        manifest = root / "dataset.json"
        manifest.write_text(json.dumps({"dataset_type": "character_video_clips", "character_id": character_id, "entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO training_datasets(dataset_id,series_id,dataset_type,entity_type,entity_id,manifest_path,status) VALUES(?,?,?,?,?,?,'ready') "
                "ON CONFLICT(dataset_id) DO UPDATE SET manifest_path=excluded.manifest_path,status=excluded.status",
                (f"character_lora_video:{character_id}", character["series_id"], "character_lora_video_clips", "character", character_id, str(manifest)),
            )
        return {"character_id": character_id, "clip_count": len(entries), "clips_root": str(root)}

    # -- ai-toolkit vendoring --------------------------------------------------
    def _ai_toolkit_root(self) -> Path:
        root = self.config.paths.ai_toolkit or (self.config.paths.project_root / "runtime" / "ai-toolkit")
        if not (root / "run.py").is_file():
            root.parent.mkdir(parents=True, exist_ok=True)
            print(f"[LoRA] ai-toolkit nicht gefunden; klone nach {root} ...", flush=True)
            subprocess.run(["git", "clone", "--depth", "1", AI_TOOLKIT_REPO, str(root)], check=True)
        venv_python = root / "venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        if not venv_python.is_file():
            print("[LoRA] Erzeuge eigenes venv fuer ai-toolkit (eigene, schwere Abhaengigkeiten) ...", flush=True)
            subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True)
            subprocess.run([str(venv_python), "-m", "pip", "install", "-U", "pip"], check=True)
            subprocess.run([str(venv_python), "-m", "pip", "install", "-r", str(root / "requirements.txt")], check=True)
        return root

    @staticmethod
    def _venv_python(ai_toolkit_root: Path) -> Path:
        return ai_toolkit_root / "venv" / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")

    # -- training ---------------------------------------------------------------
    def _build_config(self, character_id: str, kind: str, dataset_root: Path, output_root: Path) -> dict:
        training = self.config.training
        steps = training.video_steps if kind == "video" else training.image_steps
        # NOTE: unverified against a real example for kind="video" (see module docstring).
        return {
            "job": "extension",
            "config": {
                "name": f"{character_id}_{kind}_lora",
                "process": [{
                    "type": "sd_trainer",
                    "training_folder": str(output_root),
                    "device": "cuda:0",
                    "network": {"type": "lora", "linear": training.rank, "linear_alpha": training.rank},
                    "save": {"dtype": "float16", "save_every": max(100, steps // 4), "max_step_saves_to_keep": 1},
                    "datasets": [{
                        "folder_path": str(dataset_root),
                        "caption_ext": "txt",
                        "cache_latents_to_disk": True,
                        "resolution": [training.resolution],
                    }],
                    "train": {
                        "batch_size": training.batch_size,
                        "steps": steps,
                        "gradient_accumulation_steps": 1,
                        "gradient_checkpointing": True,
                        "optimizer": "adamw8bit",
                        "lr": training.learning_rate,
                        "dtype": "bf16",
                    },
                    "model": {
                        "name_or_path": "Lightricks/LTX-2.3-22B-dev",
                        "arch": "ltx2",
                        "quantize": True,
                        "low_vram": True,
                    },
                }],
            },
        }

    def train(self, character_id: str, kind: str) -> dict:
        if kind not in {"image", "video"}:
            raise ValueError("kind must be 'image' or 'video'")
        dataset = self.prepare_image_dataset(character_id) if kind == "image" else self.prepare_video_clips(character_id)
        dataset_root = (self.config.paths.project_root / "datasets" / character_id / "images") if kind == "image" else Path(dataset["clips_root"])
        ai_toolkit_root = self._ai_toolkit_root()
        output_root = self.config.paths.series_root / "models" / f"lora_{kind}_runs" / character_id
        config_path = output_root / "config.yaml"
        output_root.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(self._build_config(character_id, kind, dataset_root, output_root), sort_keys=False), encoding="utf-8")

        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO training_runs(training_run_id,dataset_id,backend_name,settings_json,status,started_at) VALUES(?,?,?,?,?,?)",
                (run_id, f"character_lora_{kind}:{character_id}" if kind == "video" else f"character_lora_images:{character_id}", "ai-toolkit", json.dumps({"kind": kind, "config_path": str(config_path)}), "running", started_at),
            )
        try:
            subprocess.run([str(self._venv_python(ai_toolkit_root)), str(ai_toolkit_root / "run.py"), str(config_path)], check=True, cwd=str(ai_toolkit_root))
            checkpoint = self._find_output_lora(output_root, character_id, kind)
            # ComfyUI's LoraLoaderModelOnly node only offers files already sitting in its own
            # models/loras/ folder (not an arbitrary path), same constraint as a checkpoint.
            loras_dir = self.config.paths.comfyui / "models" / "loras"; loras_dir.mkdir(parents=True, exist_ok=True)
            lora_filename = f"{character_id}_{kind}.safetensors"
            shutil.copy2(checkpoint, loras_dir / lora_filename)
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute("UPDATE training_runs SET status='failed',error_message=?,finished_at=? WHERE training_run_id=?", (str(error)[:4000], datetime.now(timezone.utc).isoformat(), run_id))
            raise
        with self.database.connect() as connection:
            connection.execute("UPDATE training_runs SET status='completed',output_path=?,finished_at=? WHERE training_run_id=?", (str(checkpoint), datetime.now(timezone.utc).isoformat(), run_id))
            connection.execute(
                "INSERT INTO model_registry(model_id,model_type,backend_name,local_path,minimum_vram_gb,active) VALUES(?,?,?,?,?,1) "
                "ON CONFLICT(model_id) DO UPDATE SET local_path=excluded.local_path,active=1",
                (f"lora_{kind}:{character_id}", f"lora_{kind}", "comfyui", lora_filename, 16.0 if kind == "video" else 8.0),
            )
        return {"character_id": character_id, "kind": kind, "training_run_id": run_id, "checkpoint": str(checkpoint), "lora_filename": lora_filename}

    @staticmethod
    def _find_output_lora(output_root: Path, character_id: str, kind: str) -> Path:
        candidates = sorted(output_root.glob(f"{character_id}_{kind}_lora/*.safetensors"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError(f"Keine trainierte LoRA-Datei unter {output_root} gefunden.")
        return candidates[0]
