"""Local XTTS-v2 voice cloning from the reviewed reference clips of a character."""
from __future__ import annotations

from pathlib import Path


class XttsVoiceBackend:
    """Loads XTTS-v2 from ``models/tts/xtts_v2`` (fetched and verified by start.py), or a
    per-character fine-tuned checkpoint when one is passed in (see voice_training.py)."""
    def __init__(self, model_dir: Path, language: str = "de") -> None:
        self.model_dir, self.language = model_dir, language
        self._models: dict[str, tuple] = {}   # checkpoint dir -> (model, config), loaded lazily

    def _load(self, checkpoint_dir: Path | None = None):
        source = checkpoint_dir or self.model_dir
        key = str(source)
        if key not in self._models:
            import torch
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts
            if not (source / "model.pth").exists(): raise FileNotFoundError(f"XTTS-v2-Checkpoint fehlt in {source}; python start.py laedt das Basismodell, ein Finetune muss zuerst trainiert werden.")
            config = XttsConfig(); config.load_json(str(source / "config.json"))
            model = Xtts.init_from_config(config)
            model.load_checkpoint(config, checkpoint_dir=str(source), eval=True)
            # The GPU is normally busy with the video render; only use it when clearly free.
            if torch.cuda.is_available() and torch.cuda.mem_get_info()[0] > 4 * 1024 ** 3: model.cuda()
            self._models[key] = (model, config)
        return self._models[key]

    def synthesize(self, text: str, reference_dir: Path, output: Path, checkpoint_dir: Path | None = None) -> str:
        import soundfile
        references = sorted(reference_dir.glob("ref_*.wav"))
        if not references: raise FileNotFoundError(f"Keine Referenzstimme in {reference_dir}; build-voice-references ausfuehren.")
        model, config = self._load(checkpoint_dir)
        result = model.synthesize(text, config, speaker_wav=[str(path) for path in references], language=self.language)
        output.parent.mkdir(parents=True, exist_ok=True)
        soundfile.write(str(output), result["wav"], 24000)
        return str(output)
