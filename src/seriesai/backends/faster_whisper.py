from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import TranscriptionBackend


class FasterWhisperBackend(TranscriptionBackend):
    """Fully local inference after the one-time model download into `models/`."""

    def __init__(self, model: str, model_cache: Path, device: str = "cpu", compute_type: str = "int8", beam_size: int = 5) -> None:
        self.model, self.model_cache, self.device = model, model_cache, device
        self.compute_type, self.beam_size = compute_type, beam_size

    def transcribe(self, audio_path: str, language: str = "de") -> dict[str, Any]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise RuntimeError("faster-whisper is not installed") from error
        self.model_cache.mkdir(parents=True, exist_ok=True)
        model = WhisperModel(self.model, device=self.device, compute_type=self.compute_type, download_root=str(self.model_cache))
        segments, info = model.transcribe(audio_path, language=language, beam_size=self.beam_size, vad_filter=True, condition_on_previous_text=True)
        return {"language": info.language, "language_probability": info.language_probability, "segments": [{"start": item.start, "end": item.end, "text": item.text.strip(), "avg_logprob": item.avg_logprob, "no_speech_prob": item.no_speech_prob} for item in segments]}
