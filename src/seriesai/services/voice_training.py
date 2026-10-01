"""Fine-tune XTTS-v2's GPT layer per character, instead of only zero-shot cloning.

build_references() in voice_identity.py only ever cuts 6 short clips per
character for zero-shot speaker conditioning at inference time. This service
uses ALL of a character's approved cluster segments (hours, not seconds, for
a well-covered character) as actual training data for Coqui TTS's own GPT
fine-tuning recipe (TTS.tts.layers.xtts.trainer.gpt_trainer), reusing the
already-downloaded base XTTS-v2 checkpoint rather than any new dependency.

Low-VRAM defaults throughout: this is a 6 GB card, nowhere near the ~16GB+
XTTS fine-tuning normally wants. Batch size 1, aggressive gradient
accumulation to approximate the recipe's recommended effective batch size,
no multi-worker data loading. Whether this actually fits can only be
confirmed by running it on the real machine (see this session's plan).
"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import AppConfig
from ..db import Database
from ..paths import project_path

MIN_CLIP_SECONDS, MAX_CLIP_SECONDS = 1.0, 20.0
TRAINING_SAMPLE_RATE = 22050   # XTTS GPT trainer's expected input rate (LJSpeech convention)


class VoiceTrainingService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def prepare_dataset(self, character_id: str) -> dict:
        with self.database.connect() as connection:
            character = connection.execute("SELECT character_id,series_id FROM characters WHERE character_id=? AND active=1", (character_id,)).fetchone()
            if not character:
                raise ValueError("Approved canonical character not found.")
            cluster_ids = [row["cluster_id"] for row in connection.execute("SELECT cluster_id FROM voice_clusters WHERE character_id=? AND status='approved'", (character_id,))]
            if not cluster_ids:
                raise ValueError("No approved voice cluster assigned to this character yet; run assign-voice first.")
            placeholders = ",".join("?" * len(cluster_ids))
            rows = connection.execute(
                f"SELECT v.episode_id,v.start_seconds,v.end_seconds,t.text FROM voice_embeddings v JOIN transcript_segments t ON t.segment_id=v.segment_id "
                f"WHERE v.cluster_id IN ({placeholders}) AND v.end_seconds-v.start_seconds BETWEEN ? AND ? ORDER BY v.episode_id,v.start_seconds",
                [*cluster_ids, MIN_CLIP_SECONDS, MAX_CLIP_SECONDS],
            ).fetchall()
        if not rows:
            raise ValueError("Assigned voice clusters have no usable embedded segments.")

        root = self.config.paths.series_root / "datasets" / character_id / "voice_finetune"
        wavs_dir = root / "wavs"; wavs_dir.mkdir(parents=True, exist_ok=True)
        entries: list[tuple[str, str]] = []
        audio_by_episode: dict[str, Path | None] = {}
        with self.database.connect() as connection:
            for index, row in enumerate(rows, start=1):
                text = (row["text"] or "").strip()
                if not text:
                    continue
                episode_id = row["episode_id"]
                if episode_id not in audio_by_episode:
                    audio = connection.execute("SELECT file_path FROM import_artifacts WHERE episode_id=? AND artifact_type='audio_16khz_mono' AND status='completed'", (episode_id,)).fetchone()
                    audio_by_episode[episode_id] = project_path(self.config.paths.project_root, audio["file_path"]) if audio else None
                source = audio_by_episode[episode_id]
                if not source:
                    continue
                clip_id = f"clip_{index:05d}"
                self._extract(source, wavs_dir / f"{clip_id}.wav", row["start_seconds"], row["end_seconds"] - row["start_seconds"])
                entries.append((clip_id, text))
        if not entries:
            raise ValueError("No training clips could be cut (missing audio artifacts or empty transcripts).")
        metadata = root / "metadata.csv"
        metadata.write_text("\n".join(f"{clip_id}|{text}|{text}" for clip_id, text in entries), encoding="utf-8", newline="\n")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO training_datasets(dataset_id,series_id,dataset_type,entity_type,entity_id,manifest_path,status) VALUES(?,?,?,?,?,?,'ready') "
                "ON CONFLICT(dataset_id) DO UPDATE SET manifest_path=excluded.manifest_path,status=excluded.status",
                (f"voice_finetune:{character_id}", character["series_id"], "voice_finetune", "character", character_id, str(metadata)),
            )
        return {"character_id": character_id, "clip_count": len(entries), "dataset_root": str(root), "total_seconds": sum(r["end_seconds"] - r["start_seconds"] for r in rows)}

    def _extract(self, source: Path, target: Path, start: float, duration: float) -> None:
        if target.is_file() and target.stat().st_size > 0:
            return   # idempotent: safe to re-run prepare_dataset after a partial failure
        temporary = target.with_suffix(".partial.wav")
        subprocess.run(
            [self.config.paths.ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source), "-ac", "1", "-ar", str(TRAINING_SAMPLE_RATE), str(temporary)],
            check=True, capture_output=True, timeout=120,
        )
        temporary.replace(target)

    def train(self, character_id: str, num_epochs: int = 4, batch_size: int = 1, grad_accum_steps: int = 252) -> dict:
        """Blocking: runs Coqui's GPT fine-tune recipe in-process. Can take hours and may
        simply not fit in 6GB VRAM - see this session's plan for the honest caveat."""
        dataset = self.prepare_dataset(character_id)
        base_model = self.config.paths.project_root / "models" / "tts" / "xtts_v2"
        for required in ("model.pth", "config.json", "vocab.json", "dvae.pth", "mel_stats.pth"):
            if not (base_model / required).is_file():
                raise FileNotFoundError(f"XTTS-Basismodell unvollstaendig ({required} fehlt in {base_model}); python start.py laedt es.")

        from trainer import Trainer, TrainerArgs
        from TTS.config.shared_configs import BaseDatasetConfig
        from TTS.tts.datasets import load_tts_samples
        from TTS.tts.layers.xtts.trainer.gpt_trainer import GPTArgs, GPTTrainer, GPTTrainerConfig
        from TTS.tts.models.xtts import XttsAudioConfig

        dataset_root = Path(dataset["dataset_root"])
        output_root = self.config.paths.series_root / "models" / "xtts_finetune_runs" / character_id
        run_name = f"xtts_gpt_{character_id}"

        dataset_config = BaseDatasetConfig(formatter="ljspeech", dataset_name=character_id, path=str(dataset_root), meta_file_train="metadata.csv", language=self.config.localization.output_language)
        model_args = GPTArgs(
            max_conditioning_length=132300, min_conditioning_length=66150,
            max_wav_length=int(MAX_CLIP_SECONDS * TRAINING_SAMPLE_RATE), max_text_length=200,
            mel_norm_file=str(base_model / "mel_stats.pth"), dvae_checkpoint=str(base_model / "dvae.pth"),
            xtts_checkpoint=str(base_model / "model.pth"), tokenizer_file=str(base_model / "vocab.json"),
            gpt_num_audio_tokens=1026, gpt_start_audio_token=1024, gpt_stop_audio_token=1025,
            gpt_use_masking_gt_prompt_approach=True, gpt_use_perceiver_resampler=True,
        )
        audio_config = XttsAudioConfig(sample_rate=TRAINING_SAMPLE_RATE, dvae_sample_rate=TRAINING_SAMPLE_RATE, output_sample_rate=24000)
        config = GPTTrainerConfig(
            output_path=str(output_root), model_args=model_args, run_name=run_name, project_name="seriesai_voice_finetune",
            dashboard_logger="tensorboard", audio=audio_config, batch_size=batch_size, batch_group_size=4,
            eval_batch_size=batch_size, num_loader_workers=0, num_eval_loader_workers=0, eval_split_max_size=16,
            print_step=25, save_step=500, save_n_checkpoints=1, save_checkpoints=True, print_eval=False,
            optimizer="AdamW", optimizer_wd_only_on_weights=True, optimizer_params={"betas": [0.9, 0.96], "eps": 1e-8, "weight_decay": 1e-2},
            lr=5e-06, epochs=num_epochs,
        )

        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO training_runs(training_run_id,dataset_id,backend_name,settings_json,status,started_at) VALUES(?,?,?,?,?,?)",
                (run_id, f"voice_finetune:{character_id}", "xtts_gpt_trainer", f'{{"batch_size":{batch_size},"grad_accum_steps":{grad_accum_steps},"epochs":{num_epochs}}}', "running", started_at),
            )
        try:
            model = GPTTrainer.init_from_config(config)
            train_samples, eval_samples = load_tts_samples([dataset_config], eval_split=True, eval_split_max_size=config.eval_split_max_size, eval_split_size=0.05)
            trainer = Trainer(
                TrainerArgs(restore_path=None, skip_train_epoch=False, start_with_eval=False, grad_accum_steps=grad_accum_steps),
                config, output_path=str(output_root), model=model, train_samples=train_samples, eval_samples=eval_samples,
            )
            trainer.fit()
            checkpoint_dir = self._collect_checkpoint(output_root, base_model, character_id)
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute("UPDATE training_runs SET status='failed',error_message=?,finished_at=? WHERE training_run_id=?", (str(error)[:4000], datetime.now(timezone.utc).isoformat(), run_id))
            raise
        with self.database.connect() as connection:
            connection.execute("UPDATE training_runs SET status='completed',output_path=?,finished_at=? WHERE training_run_id=?", (str(checkpoint_dir), datetime.now(timezone.utc).isoformat(), run_id))
            connection.execute(
                "INSERT INTO model_registry(model_id,model_type,backend_name,local_path,active) VALUES(?,?,?,?,1) "
                "ON CONFLICT(model_id) DO UPDATE SET local_path=excluded.local_path,active=1",
                (f"voice_finetune:{character_id}", "voice_finetune", "xtts", str(checkpoint_dir)),
            )
        return {"character_id": character_id, "training_run_id": run_id, "checkpoint_dir": str(checkpoint_dir), **dataset}

    @staticmethod
    def _collect_checkpoint(output_root: Path, base_model: Path, character_id: str) -> Path:
        """The GPT trainer writes a full XTTS checkpoint (best_model.pth) under a timestamped
        run folder; assemble it with the base model's non-trained files into one directory
        XttsVoiceBackend can load unchanged - the same layout community XTTS finetune tools use."""
        candidates = sorted(output_root.glob("*/best_model.pth"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not candidates:
            candidates = sorted(output_root.glob("*/checkpoint_*.pth"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError(f"Kein Trainings-Checkpoint unter {output_root} gefunden.")
        checkpoint_dir = output_root.parent.parent / "xtts_finetuned" / character_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidates[0], checkpoint_dir / "model.pth")
        for name in ("config.json", "vocab.json", "speakers_xtts.pth", "mel_stats.pth"):
            shutil.copy2(base_model / name, checkpoint_dir / name)
        return checkpoint_dir
