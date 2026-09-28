"""Explicit local dialogue synthesis from stable voice profiles."""
from __future__ import annotations

import json
from pathlib import Path

from ..backends.piper import PiperVoiceBackend
from ..backends.xtts import XttsVoiceBackend
from ..config import AppConfig
from ..db import Database
from ..paths import project_relative


class VoiceSynthesisService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def synthesize(self, character_id: str, text: str, output_path: Path) -> dict:
        if self.config.models.tts_backend == "xtts": return self._synthesize_xtts(character_id, text, output_path)
        if self.config.models.tts_backend != "piper":
            raise RuntimeError("Set models.tts_backend to 'xtts' (cloned original voices) or 'piper'.")
        with self.database.connect() as connection:
            profile = connection.execute(
                "SELECT voice_identity,pitch_modifier,speech_speed,energy,timbre_age_factor FROM voice_profiles WHERE character_id=?",
                (character_id,),
            ).fetchone()
        if not profile: raise ValueError("No stable voice profile exists for this character.")
        final = output_path.resolve()
        result = PiperVoiceBackend(self.config.piper.executable, self.config.piper.models_root).synthesize(
            text, profile[0], str(final), length_scale=max(.5, min(2.0, 1 / float(profile[2] or 1))), timeout=300
        )
        manifest = final.with_suffix(".json")
        manifest.write_text(json.dumps({"character_id": character_id, "voice_identity": profile[0], "text": text, "audio_path": project_relative(self.config.paths.project_root, Path(result)), "speech_speed": profile[2], "pitch_modifier": profile[1], "energy": profile[3], "timbre_age_factor": profile[4]}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"character_id": character_id, "audio_path": result, "manifest_path": str(manifest)}

    def _synthesize_xtts(self, character_id: str, text: str, output_path: Path) -> dict:
        with self.database.connect() as connection:
            profile = connection.execute("SELECT reference_dir FROM voice_profiles WHERE character_id=? AND backend_name='xtts'", (character_id,)).fetchone()
        if not profile: raise ValueError("No reviewed voice reference for this character; run assign-voice and build-voice-references.")
        backend = getattr(self, "_xtts", None) or XttsVoiceBackend(self.config.paths.project_root / "models" / "tts" / "xtts_v2", self.config.localization.output_language)
        self._xtts = backend
        result = backend.synthesize(text, self.config.paths.project_root / profile[0], output_path.resolve())
        return {"character_id": character_id, "audio_path": result, "backend": "xtts"}
