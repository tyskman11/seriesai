"""Local FFmpeg assembly for completed individually rendered shots."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative


class EpisodeEditorService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def assemble(self, job_id: str, episode_code: str) -> dict:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT sequence_number,status,checkpoint_json FROM generation_job_items WHERE job_id=? AND item_type='shot' ORDER BY sequence_number", (job_id,)).fetchall()
        if not rows: raise ValueError("Shot queue does not exist.")
        if any(row[1] != "completed" for row in rows): raise ValueError("All shots must be completed before assembly.")
        specs = [json.loads(row[2] or "{}") for row in rows]
        paths = [project_path(self.config.paths.project_root, spec.get("output_path", "")) for spec in specs]
        missing = [str(path) for path in paths if not path.is_file()]
        if missing: raise FileNotFoundError("Completed shot output missing: " + ", ".join(missing[:3]))
        output = self.config.paths.series_root / "Generated" / episode_code
        output.mkdir(parents=True, exist_ok=True)
        concat = output / "ffmpeg_concat.txt"
        # FFmpeg concat format uses one single-quoted filename per line.
        concat.write_text("".join("file '" + str(path.resolve()).replace("'", "'\\\\''") + "'\n" for path in paths), encoding="utf-8")
        episode = output / "episode.mp4"
        command = [self.config.paths.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(episode)]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode:
            raise RuntimeError("FFmpeg assembly failed: " + completed.stderr[-2000:])
        manifest = {"source_queue_job_id": job_id, "episode_code": episode_code, "created_at": datetime.now(timezone.utc).isoformat(), "shot_count": len(specs), "episode_path": project_relative(self.config.paths.project_root, episode), "shots": specs}
        (output / "metadata.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "shotlist.json").write_text(json.dumps(specs, ensure_ascii=False, indent=2), encoding="utf-8")
        payload_template = output / "commit_payload.template.json"
        payload_template.write_text(json.dumps({
            "review_note": "Fill only facts that the reviewed rendered episode actually establishes.",
            "world_events": [], "character_state_changes": [], "memories": [], "relationship_updates": [], "open_story_arcs": []
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "assembled", "episode_path": str(episode), "metadata_path": str(output / "metadata.json"), "commit_payload_template": str(payload_template), "shot_count": len(specs)}
