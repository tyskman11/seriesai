"""Chains plan -> continuity check -> shot queue -> (workers render) -> dialogue mux -> assembly.

Rendering itself stays exactly as before: independent local/remote workers claim
pending shots from the queue this creates. This service only adds the waiting
and the two final steps, so one admin action turns an idea into a finished,
voice-dubbed episode.mp4 without further manual CLI calls.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from ..config import AppConfig
from ..db import Database
from .continuity_checker import ContinuityChecker
from .dialogue_mux import DialogueMuxService
from .episode_editor import EpisodeEditorService
from .episode_planner import EpisodePlannerService
from .shot_queue import ShotQueueService

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class EpisodePipelineService:
    def __init__(self, config: AppConfig, database: Database, poll_seconds: float = 20.0) -> None:
        self.config, self.database, self.poll_seconds = config, database, poll_seconds

    def _shot_counts(self, job_id: str) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT status,COUNT(*) FROM generation_job_items WHERE job_id=? AND item_type='shot' GROUP BY status", (job_id,)).fetchall()
        return {row[0]: row[1] for row in rows}

    def run(self, series_id: str, idea: str, duration_minutes: float, episode_code: str,
             notes: str | None = None, progress: Callable[[str, dict], None] | None = None) -> dict:
        def report(stage: str, detail: dict) -> None:
            if progress: progress(stage, detail)

        report("planning", {})
        plan = EpisodePlannerService(self.config, self.database).plan(series_id, idea, duration_minutes, notes)
        outline_path = Path(plan["outline_path"])

        report("continuity_check", {})
        continuity = ContinuityChecker().check_document(outline_path)
        blocking = [issue for issue in continuity["issues"] if issue.get("severity") == "error"]
        if blocking:
            raise RuntimeError("Kontinuitaetsfehler blockieren die Episode: " + "; ".join(issue["description"] for issue in blocking))

        report("creating_shot_queue", {})
        queue = ShotQueueService(self.config, self.database).create(outline_path)
        job_id = queue["job_id"]
        report("rendering", {"job_id": job_id, "shot_count": queue["shot_count"], "counts": self._shot_counts(job_id)})

        while True:
            time.sleep(self.poll_seconds)
            counts = self._shot_counts(job_id)
            total = sum(counts.values())
            done = sum(counts.get(status, 0) for status in TERMINAL_STATUSES)
            report("rendering", {"job_id": job_id, "shot_count": total, "counts": counts})
            if total and done >= total:
                break
        if counts.get("failed"):
            raise RuntimeError(f"{counts['failed']} von {total} Shots endgueltig fehlgeschlagen (job_id {job_id}); Episode wird nicht automatisch zusammengesetzt. Fehlgeschlagene Shots im Dashboard pruefen und Queue nach Behebung erneut anstossen.")
        if counts.get("cancelled"):
            raise RuntimeError(f"{counts['cancelled']} Shots wurden abgebrochen (job_id {job_id}); Episode wird nicht automatisch zusammengesetzt.")

        report("dubbing", {"job_id": job_id})
        mux_results = DialogueMuxService(self.config, self.database).process_job(job_id)

        report("assembling", {"job_id": job_id})
        assembled = EpisodeEditorService(self.config, self.database).assemble(job_id, episode_code)
        return {"job_id": job_id, "outline_path": str(outline_path),
                "dialogue_mux": {status: sum(1 for item in mux_results if item.status == status) for status in {item.status for item in mux_results}},
                **assembled}
