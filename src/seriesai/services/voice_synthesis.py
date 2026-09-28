"""Explicit local dialogue synthesis from stable voice profiles."""
from __future__ import annotations

import json
from pathlib import Path

from ..backends.piper import PiperVoiceBackend
from ..config import AppConfig
from ..db import Database
from ..paths import project_relative


class VoiceSynthesisService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def synthesize(self, character_id: str, text: str, output_path: Path) -> dict:
        if self.config.models.tts_backend != "piper":
            raise RuntimeError("Set models.tts_backend to 'piper' to synthesize with a local Piper model.")
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
