"""Create resumable, backend-neutral shot items from a checked episode draft."""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..backends.comfyui import WAN_NEGATIVE_PROMPT
from ..config import AppConfig
from ..db import Database
from ..paths import project_relative


class ShotQueueService:
    job_type = "draft_shot_queue"

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def create(self, outline_path: Path, job_id: str | None = None) -> dict:
        document = json.loads(outline_path.read_text(encoding="utf-8"))
        if document.get("status") not in {"continuity_checked", "draft_ready"}:
            raise ValueError("Run continuity check and resolve blocking issues before creating a shot queue.")
        if any(item.get("severity") == "error" for item in document.get("continuity_issues", [])):
            raise ValueError("Draft has blocking continuity errors.")
        series_id = document["series_id"]
        job_id = job_id or str(uuid.uuid4())
        output_root = self.config.paths.series_root / "Generated" / "drafts" / job_id
        shots_root = output_root / "shots"
        shots_root.mkdir(parents=True, exist_ok=True)
        items: list[dict] = []
        sequence = 0
        for scene in sorted(document["outline"]["scenes"], key=lambda item: item["sequence_number"]):
            remaining = float(scene["duration_seconds"])
            part = 0
            while remaining > 0:
                sequence += 1; part += 1
                duration = min(5.0, remaining); remaining -= duration   # Wan2.2: 5 s clips (121 frames @ 24 fps)
                seed = random.SystemRandom().randint(0, 2_147_483_647)
                filename = f"shot_{sequence:04d}.mp4"
                spec = {
                    "shot_id": f"draft:{job_id}:{sequence:04d}", "sequence_number": sequence,
                    "scene_sequence": scene["sequence_number"], "scene_part": part,
                    "duration_seconds": duration, "location": scene["location"],
                    "character_ids": scene["character_ids"], "action": scene["purpose"],
                    "camera": "auto", "prompt": scene["purpose"] + " " + scene["conflict_or_turn"],
                    "negative_prompt": WAN_NEGATIVE_PROMPT,
                    "seed": seed, "backend": self.config.models.video_backend,
                    "fps": 24, "frame_count": max(9, round(duration * 24 / 8) * 8 + 1),
                    "output_prefix": f"seriesai/shot_{sequence:04d}_{seed}",
                    "output_path": project_relative(self.config.paths.project_root, shots_root / filename),
                    "status": "pending",
                }
                items.append(spec)
        with self.database.connect() as connection:
            connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json,progress,attempts,checkpoint_json,updated_at) VALUES(?,?,?,'pending',?,0,0,?,?) ON CONFLICT(job_id) DO NOTHING", (job_id, series_id, self.job_type, json.dumps({"outline_path": str(outline_path)}, ensure_ascii=False), json.dumps({"item_count": len(items), "shots_root": project_relative(self.config.paths.project_root, shots_root)}), datetime.now(timezone.utc).isoformat()))
            for spec in items:
                connection.execute("INSERT OR IGNORE INTO generation_job_items(item_id,job_id,item_type,sequence_number,status,checkpoint_json) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), job_id, "shot", spec["sequence_number"], "pending", json.dumps(spec, ensure_ascii=False)))
        manifest = output_root / "shotlist.json"
        manifest.write_text(json.dumps({"job_id": job_id, "series_id": series_id, "outline_path": str(outline_path), "shots": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"job_id": job_id, "status": "pending", "shot_count": len(items), "manifest_path": str(manifest)}

    def status(self, job_id: str) -> dict:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT status,COUNT(*) FROM generation_job_items WHERE job_id=? GROUP BY status", (job_id,)).fetchall()
        return {"job_id": job_id, "items": {row[0]: row[1] for row in rows}}
