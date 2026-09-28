"""Local Piper TTS adapter; no network/model download behavior."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import VoiceBackend


class PiperVoiceBackend(VoiceBackend):
    def __init__(self, executable: str, models_root: Path | None = None) -> None:
        self.executable, self.models_root = executable, models_root

    def synthesize(self, text: str, voice_identity: str, output_path: str, **options: Any) -> str:
        if not text.strip(): raise ValueError("TTS text must not be empty")
        executable = shutil.which(self.executable) or self.executable
        model = Path(voice_identity)
        if not model.is_absolute() and self.models_root: model = self.models_root / model
        if not model.is_file(): raise FileNotFoundError(f"Local Piper model not found for voice_identity: {model}")
        output = Path(output_path); output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".partial")
        command = [str(executable), "--model", str(model), "--output_file", str(temporary)]
        if options.get("length_scale") is not None: command += ["--length_scale", str(options["length_scale"])]
        try:
            completed = subprocess.run(command, input=text, text=True, capture_output=True, timeout=options.get("timeout", 180))
        except FileNotFoundError as error:
            raise RuntimeError("Piper executable is not installed locally.") from error
        if completed.returncode: raise RuntimeError("Piper synthesis failed: " + completed.stderr[-2000:])
        temporary.replace(output)
        return str(output)
