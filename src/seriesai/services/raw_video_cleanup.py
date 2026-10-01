"""Delete a raw imported episode video only once every stage that still needs
it has finished, to save disk space without silently breaking anything.

scene_detection.py reads episodes.source_path again on every (re-)run, not
just at import time, so deleting the raw file is unsafe until scene
detection has completed for that episode. Training-clip extraction (for a
character's video LoRA) also needs the raw file, so that must run first too.
"""
from __future__ import annotations

from ..config import AppConfig
from ..db import Database
from ..paths import project_path


class RawVideoCleanupService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def check(self, episode_id: str) -> dict:
        """Report what's blocking (or not) deletion, without deleting anything."""
        with self.database.connect() as connection:
            episode = connection.execute("SELECT episode_id,source_path,source_deleted FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
            if not episode:
                raise ValueError(f"Unknown episode: {episode_id}")
            audio_done = bool(connection.execute("SELECT 1 FROM import_artifacts WHERE episode_id=? AND artifact_type='audio_16khz_mono' AND status='completed'", (episode_id,)).fetchone())
            scenes_done = bool(connection.execute("SELECT 1 FROM analysis_runs WHERE episode_id=? AND stage_name='scene_detection' AND status='completed'", (episode_id,)).fetchone())
        blockers = []
        if episode["source_deleted"]:
            blockers.append("Rohvideo wurde bereits geloescht.")
        if not audio_done:
            blockers.append("Audio-Extraktion ist noch nicht abgeschlossen.")
        if not scenes_done:
            blockers.append("Szenenerkennung ist noch nicht abgeschlossen.")
        # NOTE: once video-LoRA training-clip extraction exists (plan section B), add a check
        # here that no pending clip extraction for this episode is still outstanding - that
        # step also needs the raw file and must run before this cleanup is allowed.
        return {"episode_id": episode_id, "ready": not blockers and not episode["source_deleted"], "already_deleted": bool(episode["source_deleted"]), "blockers": blockers, "source_path": episode["source_path"]}

    def cleanup(self, episode_id: str, confirm: bool) -> dict:
        status = self.check(episode_id)
        if status["already_deleted"]:
            return {**status, "action": "none"}
        if status["blockers"]:
            raise ValueError("Loeschen blockiert: " + "; ".join(status["blockers"]))
        if not confirm:
            return {**status, "action": "dry_run", "message": "Alles bereit. Mit --confirm tatsaechlich loeschen."}
        with self.database.connect() as connection:
            source = connection.execute("SELECT source_path FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()["source_path"]
            path = project_path(self.config.paths.project_root, source)
            freed_bytes = path.stat().st_size if path.is_file() else 0
            if path.is_file():
                path.unlink()
            connection.execute("UPDATE episodes SET source_deleted=1 WHERE episode_id=?", (episode_id,))
        return {**status, "action": "deleted", "freed_bytes": freed_bytes}
