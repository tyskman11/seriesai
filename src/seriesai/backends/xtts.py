"""Local XTTS-v2 voice cloning from the reviewed reference clips of a character."""
from __future__ import annotations

from pathlib import Path


class XttsVoiceBackend:
    """Loads XTTS-v2 from ``models/tts/xtts_v2`` (fetched and verified by start.py)."""
    def __init__(self, model_dir: Path, language: str = "de") -> None:
        self.model_dir, self.language, self._model, self._config = model_dir, language, None, None

    def _load(self):
        if self._model is None:
            import torch
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts
            if not (self.model_dir / "model.pth").exists(): raise FileNotFoundError(f"XTTS-v2 fehlt in {self.model_dir}; python start.py laedt es.")
            config = XttsConfig(); config.load_json(str(self.model_dir / "config.json"))
            model = Xtts.init_from_config(config)
            model.load_checkpoint(config, checkpoint_dir=str(self.model_dir), eval=True)
            # The GPU is normally busy with the video render; only use it when clearly free.
            if torch.cuda.is_available() and torch.cuda.mem_get_info()[0] > 4 * 1024 ** 3: model.cuda()
            self._model, self._config = model, config
        return self._model, self._config

    def synthesize(self, text: str, reference_dir: Path, output: Path) -> str:
        import soundfile
        references = sorted(reference_dir.glob("ref_*.wav"))
        if not references: raise FileNotFoundError(f"Keine Referenzstimme in {reference_dir}; build-voice-references ausfuehren.")
        model, config = self._load()
        result = model.synthesize(text, config, speaker_wav=[str(path) for path in references], language=self.language)
        output.parent.mkdir(parents=True, exist_ok=True)
        soundfile.write(str(output), result["wav"], 24000)
        return str(output)
