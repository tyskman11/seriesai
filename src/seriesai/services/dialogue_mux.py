"""Synthesize a shot's planned dialogue with cloned original voices and mux it into the rendered clip.

Runs only after a shot is rendered.  The clip's own (LTX-generated) audio track
is kept as a sibling ``*.ltx_audio.mp4`` backup the first time a shot is muxed,
so the step is safe to re-run.  A shot without dialogue, or whose speakers have
no reviewed voice yet, is left untouched.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..backends.xtts import XttsVoiceBackend
from ..config import AppConfig
from ..db import Database
from ..paths import project_path

GAP_SECONDS = 0.35
SAMPLE_RATE = 24000


@dataclass(frozen=True)
class MuxResult:
    sequence_number: int
    status: str
    detail: str = ""


class DialogueMuxService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database
        self._xtts = XttsVoiceBackend(config.paths.project_root / "models" / "tts" / "xtts_v2", config.localization.output_language)

    def _voiced_characters(self) -> dict[str, Path]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT character_id,reference_dir FROM voice_profiles WHERE backend_name='xtts'").fetchall()
        return {row[0]: self.config.paths.project_root / row[1] for row in rows}

    def _finetune_checkpoint(self, character_id: str) -> Path | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT local_path FROM model_registry WHERE model_id=? AND active=1", (f"voice_finetune:{character_id}",)).fetchone()
        return self.config.paths.project_root / row["local_path"] if row else None

    def _render_line(self, character_id: str, text: str, reference_dir: Path, scratch: Path) -> np.ndarray | None:
        import soundfile
        target = scratch / f"{character_id}_{abs(hash(text)) % 10**8}.wav"
        try:
            self._xtts.synthesize(text, reference_dir, target, checkpoint_dir=self._finetune_checkpoint(character_id))
        except (FileNotFoundError, RuntimeError):
            return None
        wave, rate = soundfile.read(str(target), dtype="float32")
        if rate != SAMPLE_RATE:
            raise RuntimeError(f"Unexpected XTTS sample rate {rate}.")
        return wave

    def mux_shot(self, output_path: Path, dialogue: list[dict], duration_seconds: float) -> MuxResult:
        import soundfile
        voiced = self._voiced_characters()
        usable = [line for line in dialogue if line.get("character_id") in voiced]
        if not usable:
            return MuxResult(0, "no_voiced_dialogue")
        backup = output_path.with_suffix(".ltx_audio.mp4")
        if not backup.exists():
            output_path.replace(backup)
        scratch = output_path.parent / ".dialogue_scratch"; scratch.mkdir(exist_ok=True)
        clips = [clip for clip in (self._render_line(line["character_id"], line["line"], voiced[line["character_id"]], scratch) for line in usable) if clip is not None]
        if not clips:
            backup.replace(output_path)
            return MuxResult(0, "synthesis_failed")
        gap = np.zeros(int(GAP_SECONDS * SAMPLE_RATE), dtype="float32")
        track = np.concatenate([segment for clip in clips for segment in (clip, gap)])
        dialogue_wav = scratch / "dialogue.wav"
        soundfile.write(str(dialogue_wav), track, SAMPLE_RATE)
        muxed = output_path.with_suffix(".muxed.mp4")
        # Video stays the rendered length; dialogue is trimmed or padded with silence to match.
        command = [self.config.paths.ffmpeg, "-y", "-i", str(backup), "-i", str(dialogue_wav),
                   "-filter_complex", f"[1:a]apad,atrim=0:{duration_seconds}[a]",
                   "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", str(muxed)]
        completed = subprocess.run(command, capture_output=True, text=True)
        for stale in scratch.glob("*"): stale.unlink(missing_ok=True)
        scratch.rmdir()
        if completed.returncode:
            return MuxResult(0, "ffmpeg_failed: " + completed.stderr[-500:])
        muxed.replace(output_path)
        return MuxResult(0, f"muxed {len(clips)} line(s)")

    def process_job(self, job_id: str, progress=None) -> list[MuxResult]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT sequence_number,checkpoint_json FROM generation_job_items WHERE job_id=? AND item_type='shot' AND status='completed' ORDER BY sequence_number", (job_id,)).fetchall()
        results = []
        for index, (sequence_number, checkpoint) in enumerate(rows, start=1):
            spec = json.loads(checkpoint or "{}")
            dialogue = spec.get("dialogue") or []
            output = project_path(self.config.paths.project_root, spec["output_path"])
            if not dialogue or not output.is_file():
                result = MuxResult(sequence_number, "skipped_no_dialogue" if not dialogue else "skipped_missing_file")
            else:
                inner = self.mux_shot(output, dialogue, spec["duration_seconds"])
                result = MuxResult(sequence_number, inner.status)
            results.append(result)
            if progress: progress(index, len(rows), result)
        return results
