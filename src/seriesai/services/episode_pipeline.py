"""Chains plan -> continuity check -> shot queue -> (workers render) -> dialogue mux -> assembly.

Rendering itself stays exactly as before: independent local/remote workers claim
pending shots from the queue this creates. This service only adds the waiting
and the two final steps, so one admin action turns an idea into a finished,
voice-dubbed episode.mp4 without further manual CLI calls.

Progress is persisted to ``generation_jobs`` (job_type='episode_pipeline') after
every stage, not just kept in the coordinator's process memory. A coordinator
restart during the multi-hour render wait therefore does not lose the pipeline:
``resume_pending`` finds it again and continues from the saved shot_job_id
without re-planning or re-queueing.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import AppConfig
from ..db import Database
from .continuity_checker import ContinuityChecker
from .dialogue_mux import DialogueMuxService
from .episode_editor import EpisodeEditorService
from .episode_planner import EpisodePlannerService
from .shot_queue import ShotQueueService

TERMINAL_SHOT_STATUSES = {"completed", "failed", "cancelled"}


class EpisodePipelineService:
    def __init__(self, config: AppConfig, database: Database, poll_seconds: float = 20.0) -> None:
        self.config, self.database, self.poll_seconds = config, database, poll_seconds

    def _shot_counts(self, shot_job_id: str) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT status,COUNT(*) FROM generation_job_items WHERE job_id=? AND item_type='shot' GROUP BY status", (shot_job_id,)).fetchall()
        return {row[0]: row[1] for row in rows}

    def _save(self, job_id: str, series_id: str, payload: dict, checkpoint: dict, status: str, error: str | None = None) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json,checkpoint_json,error_message,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,checkpoint_json=excluded.checkpoint_json,error_message=excluded.error_message,updated_at=excluded.updated_at",
                (job_id, series_id, "episode_pipeline", status, json.dumps(payload, ensure_ascii=False),
                 json.dumps(checkpoint, ensure_ascii=False), error, datetime.now(timezone.utc).isoformat()),
            )

    def run(self, series_id: str, idea: str, duration_minutes: float, episode_code: str, notes: str | None = None,
             progress: Callable[[str, dict], None] | None = None, job_id: str | None = None, resume_checkpoint: dict | None = None,
             character_ids: list[str] | None = None, crossover_series_id: str | None = None, crossover_character_ids: list[str] | None = None) -> dict:
        job_id = job_id or str(uuid.uuid4())
        payload = {"idea": idea, "duration_minutes": duration_minutes, "episode_code": episode_code, "notes": notes,
                   "character_ids": character_ids, "crossover_series_id": crossover_series_id, "crossover_character_ids": crossover_character_ids}
        checkpoint = dict(resume_checkpoint or {})

        def report(stage: str, **detail: object) -> None:
            checkpoint.update(stage=stage, **detail)
            self._save(job_id, series_id, payload, checkpoint, "running")
            if progress: progress(stage, detail)

        try:
            if "shot_job_id" not in checkpoint:
                report("planning")
                plan = EpisodePlannerService(self.config, self.database).plan(series_id, idea, duration_minutes, notes,
                                                                                character_ids=character_ids, crossover_series_id=crossover_series_id, crossover_character_ids=crossover_character_ids)
                outline_path = Path(plan["outline_path"])
                title = json.loads(outline_path.read_text(encoding="utf-8")).get("outline", {}).get("title", episode_code)
                report("continuity_check", outline_path=str(outline_path), title=title)
                continuity = ContinuityChecker().check_document(outline_path)
                blocking = [issue for issue in continuity["issues"] if issue.get("severity") == "error"]
                if blocking:
                    raise RuntimeError("Kontinuitaetsfehler blockieren die Episode: " + "; ".join(issue["description"] for issue in blocking))
                report("creating_shot_queue", outline_path=str(outline_path), title=title)
                queue = ShotQueueService(self.config, self.database).create(outline_path)
                report("rendering", shot_job_id=queue["job_id"], shot_count=queue["shot_count"], counts=self._shot_counts(queue["job_id"]), title=title)
            shot_job_id = checkpoint["shot_job_id"]
            title = checkpoint.get("title", episode_code)

            counts = self._shot_counts(shot_job_id)
            report("rendering", shot_job_id=shot_job_id, shot_count=sum(counts.values()), counts=counts, title=title)  # report immediately, e.g. right after a resume
            while True:
                total = sum(counts.values())
                done = sum(counts.get(status, 0) for status in TERMINAL_SHOT_STATUSES)
                if total and done >= total: break
                time.sleep(self.poll_seconds)
                counts = self._shot_counts(shot_job_id)
                report("rendering", shot_job_id=shot_job_id, shot_count=total, counts=counts, title=title)
            if counts.get("failed"):
                raise RuntimeError(f"{counts['failed']} von {sum(counts.values())} Shots endgueltig fehlgeschlagen (shot_job_id {shot_job_id}); Episode wird nicht automatisch zusammengesetzt.")
            if counts.get("cancelled"):
                raise RuntimeError(f"{counts['cancelled']} Shots wurden abgebrochen (shot_job_id {shot_job_id}); Episode wird nicht automatisch zusammengesetzt.")

            report("dubbing", shot_job_id=shot_job_id)
            mux_results = DialogueMuxService(self.config, self.database).process_job(shot_job_id)

            report("assembling", shot_job_id=shot_job_id)
            assembled = EpisodeEditorService(self.config, self.database).assemble(shot_job_id, episode_code)
            result = {"job_id": shot_job_id, "outline_path": checkpoint.get("outline_path"),
                      "dialogue_mux": {status: sum(1 for item in mux_results if item.status == status) for status in {item.status for item in mux_results}},
                      **assembled}
            checkpoint.update(stage="completed", result=result)
            self._save(job_id, series_id, payload, checkpoint, "completed")
            return result
        except Exception as error:
            checkpoint["stage"] = "failed"
            self._save(job_id, series_id, payload, checkpoint, "failed", error=str(error)[:4000])
            raise

    def resume_pending(self, progress: Callable[[str, dict], None] | None = None) -> dict | None:
        """Continue the most recently interrupted pipeline after a coordinator restart.

        Only a job that already reached shot-queue creation can be resumed (its
        rendering happens in independent worker processes and kept going through
        the restart); an interruption during planning is cheap to just retry.
        """
        with self.database.connect() as connection:
            rows = connection.execute("SELECT job_id,series_id,payload_json,checkpoint_json FROM generation_jobs WHERE job_type='episode_pipeline' AND status='running' ORDER BY updated_at DESC").fetchall()
        resumable = None
        for job_id, series_id, payload_json, checkpoint_json in rows:
            checkpoint = json.loads(checkpoint_json or "{}")
            payload = json.loads(payload_json or "{}")
            if "shot_job_id" in checkpoint:
                if resumable is None: resumable = (job_id, series_id, payload, checkpoint)
                continue
            self._save(job_id, series_id, payload, checkpoint, "failed", error="Koordinator wurde neu gestartet, bevor die Shot-Queue angelegt war; bitte erneut starten.")
        if not resumable: return None
        job_id, series_id, payload, checkpoint = resumable
        return self.run(series_id, payload["idea"], payload["duration_minutes"], payload["episode_code"], payload.get("notes"), progress, job_id=job_id, resume_checkpoint=checkpoint,
                         character_ids=payload.get("character_ids"), crossover_series_id=payload.get("crossover_series_id"), crossover_character_ids=payload.get("crossover_character_ids"))

    def enqueue(self, series_id: str, idea: str, duration_minutes: float, episode_code: str, notes: str | None = None,
                character_ids: list[str] | None = None, crossover_series_id: str | None = None, crossover_character_ids: list[str] | None = None) -> dict:
        """Add an episode request to the queue; run_forever() picks it up once free. Never blocks or rejects."""
        job_id = str(uuid.uuid4())
        payload = {"idea": idea, "duration_minutes": duration_minutes, "episode_code": episode_code, "notes": notes,
                   "character_ids": character_ids, "crossover_series_id": crossover_series_id, "crossover_character_ids": crossover_character_ids}
        # 'pending' doubles as "queued": the schema's status values predate this queue and have no separate state for it.
        self._save(job_id, series_id, payload, {"stage": "queued"}, "pending")
        return {"job_id": job_id, "position": self.queue_position(job_id)}

    def queue_position(self, job_id: str) -> int:
        """1 = next in line (or already running); 0 if not found/not queued."""
        with self.database.connect() as connection:
            rows = connection.execute("SELECT job_id FROM generation_jobs WHERE job_type='episode_pipeline' AND status IN ('running','pending') ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,created_at").fetchall()
        ids = [row[0] for row in rows]
        return ids.index(job_id) + 1 if job_id in ids else 0

    def queue_snapshot(self, limit: int = 20) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT job_id,series_id,status,payload_json,checkpoint_json,created_at,updated_at,error_message FROM generation_jobs WHERE job_type='episode_pipeline' "
                "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,"
                "CASE WHEN status IN ('running','pending') THEN created_at END ASC,"
                "CASE WHEN status NOT IN ('running','pending') THEN updated_at END DESC LIMIT ?", (limit,)).fetchall()
        results = []
        for row in rows:
            payload, checkpoint = json.loads(row["payload_json"] or "{}"), json.loads(row["checkpoint_json"] or "{}")
            results.append({"job_id": row["job_id"], "series_id": row["series_id"], "status": row["status"], "episode_code": payload.get("episode_code"), "idea": payload.get("idea"),
                             "title": checkpoint.get("title"), "stage": checkpoint.get("stage"), "counts": checkpoint.get("counts"),
                             "created_at": row["created_at"], "updated_at": row["updated_at"], "error": row["error_message"]})
        return results

    def _next_runnable(self) -> tuple[str, str, dict, dict] | None:
        with self.database.connect() as connection:
            running = connection.execute("SELECT job_id,series_id,payload_json,checkpoint_json FROM generation_jobs WHERE job_type='episode_pipeline' AND status='running' ORDER BY updated_at DESC LIMIT 1").fetchone()
            if running: return running["job_id"], running["series_id"], json.loads(running["payload_json"] or "{}"), json.loads(running["checkpoint_json"] or "{}")
            queued = connection.execute("SELECT job_id,series_id,payload_json FROM generation_jobs WHERE job_type='episode_pipeline' AND status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if queued: return queued["job_id"], queued["series_id"], json.loads(queued["payload_json"] or "{}"), {}
        return None

    def run_forever(self, progress: Callable[[str, dict], None] | None = None, poll_seconds: float = 3.0) -> None:
        """One persistent worker: resumes an in-flight job, then works through the queue, forever.

        Only one episode renders at a time (the local GPU is the bottleneck), but
        any number of requests can queue up without being rejected.
        """
        while True:
            item = self._next_runnable()
            if not item:
                time.sleep(poll_seconds); continue
            job_id, series_id, payload, checkpoint = item
            try:
                self.run(series_id, payload["idea"], payload["duration_minutes"], payload["episode_code"], payload.get("notes"), progress, job_id=job_id, resume_checkpoint=checkpoint,
                         character_ids=payload.get("character_ids"), crossover_series_id=payload.get("crossover_series_id"), crossover_character_ids=payload.get("crossover_character_ids"))
            except Exception:
                pass  # already recorded on the job row by run(); move on to the next queued episode
