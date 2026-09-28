"""Atomic, resumable local worker operations for generated shot items."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..db import Database


class QueueWorkerService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def claim_next(self, job_id: str, worker_id: str) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT item_id,checkpoint_json FROM generation_job_items WHERE job_id=? AND status IN ('pending','retry') ORDER BY sequence_number LIMIT 1", (job_id,)).fetchone()
            if not row:
                return None
            changed = connection.execute("UPDATE generation_job_items SET status='rendering',worker_id=?,attempts=attempts+1,started_at=?,error_message=NULL WHERE item_id=? AND status IN ('pending','retry')", (worker_id, now, row[0])).rowcount
            if not changed:
                return None
            connection.execute("UPDATE generation_jobs SET status='running',updated_at=? WHERE job_id=?", (now, job_id))
            spec = json.loads(row[1] or "{}")
            return {"item_id": row[0], "job_id": job_id, "worker_id": worker_id, "specification": spec}

    def claim_next_any(self, worker_id: str) -> dict | None:
        """Atomically claim one pending shot across all jobs for a remote worker."""
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A single local ComfyUI/GPU process must never receive a second
            # item while it is still producing the first one. Other workers
            # remain free to claim their own independent item.
            busy = connection.execute(
                "SELECT 1 FROM generation_job_items WHERE worker_id=? AND status='rendering' LIMIT 1", (worker_id,)
            ).fetchone()
            if busy: return None
            row = connection.execute(
                "SELECT item_id,job_id,checkpoint_json FROM generation_job_items "
                "WHERE item_type='shot' AND status IN ('pending','retry') "
                "ORDER BY CASE status WHEN 'retry' THEN 0 ELSE 1 END,job_id,sequence_number LIMIT 1"
            ).fetchone()
            if not row: return None
            changed = connection.execute(
                "UPDATE generation_job_items SET status='rendering',worker_id=?,attempts=attempts+1,started_at=?,error_message=NULL "
                "WHERE item_id=? AND status IN ('pending','retry')", (worker_id, now, row[0])
            ).rowcount
            if not changed: return None
            connection.execute("UPDATE generation_jobs SET status='running',updated_at=? WHERE job_id=?", (now, row[1]))
            return {"item_id": row[0], "job_id": row[1], "worker_id": worker_id, "specification": json.loads(row[2] or "{}")} 

    def complete(self, item_id: str, worker_id: str, output_path: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            row = connection.execute("SELECT job_id,checkpoint_json FROM generation_job_items WHERE item_id=? AND status='rendering' AND worker_id=?", (item_id, worker_id)).fetchone()
            if not row:
                raise ValueError("Item is not claimed by this worker.")
            spec = json.loads(row[1] or "{}")
            spec["output_path"] = output_path; spec["status"] = "completed"
            connection.execute("UPDATE generation_job_items SET status='completed',checkpoint_json=?,finished_at=? WHERE item_id=?", (json.dumps(spec, ensure_ascii=False), now, item_id))
            self._refresh_job(connection, row[0], now)
        return {"item_id": item_id, "status": "completed", "output_path": output_path}

    def record_backend_submission(self, item_id: str, worker_id: str, backend_name: str, prompt_id: str) -> None:
        """Persist external local backend identity before waiting for its result."""
        with self.database.connect() as connection:
            row = connection.execute("SELECT checkpoint_json FROM generation_job_items WHERE item_id=? AND status='rendering' AND worker_id=?", (item_id, worker_id)).fetchone()
            if not row: raise ValueError("Item is not claimed by this worker.")
            specification = json.loads(row[0] or "{}")
            specification["backend_submission"] = {"backend": backend_name, "prompt_id": prompt_id, "submitted_at": datetime.now(timezone.utc).isoformat()}
            connection.execute("UPDATE generation_job_items SET checkpoint_json=? WHERE item_id=?", (json.dumps(specification, ensure_ascii=False), item_id))

    def adopt_local_output(self, item_id: str, worker_id: str, source_path: str, project_root: str) -> dict:
        """Safely adopt a completed local backend output after a worker crash."""
        source = Path(source_path).resolve(); root = Path(project_root).resolve()
        if not source.is_file(): raise FileNotFoundError(f"Rendered source file not found: {source}")
        with self.database.connect() as connection:
            row = connection.execute("SELECT checkpoint_json FROM generation_job_items WHERE item_id=? AND status='rendering' AND worker_id=?", (item_id, worker_id)).fetchone()
        if not row: raise ValueError("Item is not claimed by this worker.")
        specification = json.loads(row[0] or "{}"); target = (root / specification.get("output_path", "")).resolve()
        if root not in target.parents: raise ValueError("Unsafe queue output path.")
        target.parent.mkdir(parents=True, exist_ok=True); temporary = target.with_suffix(target.suffix + ".adopting")
        shutil.copy2(source, temporary); temporary.replace(target)
        return self.complete(item_id, worker_id, str(specification["output_path"]))

    def fail(self, item_id: str, worker_id: str, error: str, retry: bool = True) -> dict:
        now = datetime.now(timezone.utc).isoformat(); status = "retry" if retry else "failed"
        with self.database.connect() as connection:
            row = connection.execute("SELECT job_id FROM generation_job_items WHERE item_id=? AND status='rendering' AND worker_id=?", (item_id, worker_id)).fetchone()
            if not row:
                raise ValueError("Item is not claimed by this worker.")
            connection.execute("UPDATE generation_job_items SET status=?,error_message=?,finished_at=? WHERE item_id=?", (status, error[:4000], now, item_id))
            self._refresh_job(connection, row[0], now)
        return {"item_id": item_id, "status": status}

    def recover_interrupted(self, job_id: str | None = None) -> dict:
        """Return orphaned rendering items to retry after a coordinator/worker crash."""
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            where, values = ("WHERE status='rendering'", []) if job_id is None else ("WHERE status='rendering' AND job_id=?", [job_id])
            rows = connection.execute(f"SELECT DISTINCT job_id FROM generation_job_items {where}", values).fetchall()
            changed = connection.execute(f"UPDATE generation_job_items SET status='retry',worker_id=NULL,error_message=COALESCE(error_message,'Recovered after interrupted worker'),finished_at=? {where}", [now, *values]).rowcount
            for row in rows:
                self._refresh_job(connection, row[0], now)
        return {"recovered_items": changed, "jobs": [row[0] for row in rows]}

    @staticmethod
    def _refresh_job(connection, job_id: str, now: str) -> None:
        counts = {row[0]: row[1] for row in connection.execute("SELECT status,COUNT(*) FROM generation_job_items WHERE job_id=? GROUP BY status", (job_id,))}
        total = sum(counts.values()) or 1
        if counts.get("rendering") or counts.get("pending") or counts.get("retry"):
            status = "running" if counts.get("rendering") else "pending"
        elif counts.get("failed"):
            status = "failed"
        else:
            status = "completed"
        connection.execute("UPDATE generation_jobs SET status=?,progress=?,updated_at=? WHERE job_id=?", (status, counts.get("completed", 0) / total, now, job_id))
