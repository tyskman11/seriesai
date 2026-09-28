"""Explicit, backup-first commit of a reviewed generated episode."""
from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from ..config import AppConfig
from ..db import Database
from ..paths import project_relative

class EpisodeCommitService:
    def __init__(self, config: AppConfig, database: Database) -> None: self.config,self.database=config,database
    def commit(self, series_id: str, episode_id: str, episode_code: str, episode_date: str | None = None) -> dict:
        directory=self.config.paths.series_root/"Generated"/episode_code; video=directory/"episode.mp4"; metadata=directory/"metadata.json"
        if not video.is_file(): raise FileNotFoundError("Reviewed episode.mp4 is required before commit.")
        if not metadata.is_file(): raise FileNotFoundError("metadata.json is required before commit.")
        with self.database.connect() as c:
            if c.execute("SELECT 1 FROM episodes WHERE episode_id=?",(episode_id,)).fetchone(): raise ValueError("Episode ID already exists; commits never overwrite history.")
            if not c.execute("SELECT 1 FROM series WHERE series_id=?",(series_id,)).fetchone(): raise ValueError("Series not found.")
        payload_path=directory/"commit_payload.json"
        payload=json.loads(payload_path.read_text(encoding="utf-8")) if payload_path.is_file() else {}
        if not isinstance(payload,dict): raise ValueError("commit_payload.json must contain an object.")
        backup=self.database.backup_to(self.config.paths.project_root/"backups"/"commits")
        now=datetime.now(timezone.utc).isoformat(); commit_id=str(uuid.uuid4())
        with self.database.connect() as c:
            c.execute("INSERT INTO episodes(episode_id,series_id,title,source_path,episode_date,status) VALUES(?,?,?,?,?,'committed')",(episode_id,series_id,episode_code,project_relative(self.config.paths.project_root,video),episode_date))
            c.execute("INSERT INTO review_status(review_id,episode_id,status,updated_at) VALUES(?,?,?,?)",(str(uuid.uuid4()),episode_id,"committed",now))
            event_ids: list[str] = []
            for item in payload.get("world_events",[]):
                description=str(item.get("description","")).strip(); event_type=str(item.get("event_type","story_event")).strip()
                if not description: raise ValueError("Every commit world_event needs a description.")
                event_id=str(uuid.uuid4()); event_ids.append(event_id)
                c.execute("INSERT INTO world_events(world_event_id,series_id,occurred_at,event_type,description,source_episode_id) VALUES(?,?,?,?,?,?)",(event_id,series_id,str(item.get("occurred_at") or episode_date or now),event_type,description,episode_id))
                c.execute("INSERT INTO timeline(series_id,occurred_at,entity_type,entity_id,event_type,description,source_episode_id) VALUES(?,?,?,?,?,?,?)",(series_id,str(item.get("occurred_at") or episode_date or now),"world",event_id,event_type,description,episode_id))
            for item in payload.get("character_state_changes",[]):
                character_id=str(item.get("character_id","")).strip(); field=str(item.get("field_name","")).strip(); value=item.get("new_value")
                if not character_id or not field or value is None: raise ValueError("Each character state change needs character_id, field_name and new_value.")
                if not c.execute("SELECT 1 FROM characters WHERE character_id=? AND series_id=?",(character_id,series_id)).fetchone(): raise ValueError(f"Unknown character in commit payload: {character_id}")
                c.execute("INSERT INTO character_state(character_id,episode_id,effective_at,field_name,old_value,new_value,reason,event_id) VALUES(?,?,?,?,?,?,?,?)",(character_id,episode_id,str(item.get("effective_at") or episode_date or now),field,item.get("old_value"),str(value),str(item.get("reason") or "Reviewed generated episode commit"),item.get("event_id")))
            for item in payload.get("memories",[]):
                character_id=str(item.get("character_id","")).strip(); description=str(item.get("description","")).strip(); kind=str(item.get("memory_type","long_term"))
                if kind not in {"short_term","long_term","core"} or not character_id or not description: raise ValueError("Each memory needs character_id, description and a valid memory_type.")
                c.execute("INSERT INTO character_memories(memory_id,character_id,event_id,memory_type,description,emotional_weight,importance,created_at,source_episode_id) VALUES(?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),character_id,item.get("event_id"),kind,description,float(item.get("emotional_weight",.5)),float(item.get("importance",.5)),now,episode_id))
            for item in payload.get("relationship_updates",[]):
                a,b=sorted((str(item.get("character_a_id","")).strip(),str(item.get("character_b_id","")).strip()))
                if not a or not b or a==b: raise ValueError("Each relationship update needs two different character IDs.")
                rid=f"{series_id}:{a}:{b}"; c.execute("INSERT OR IGNORE INTO relationships(relationship_id,series_id,character_a_id,character_b_id,created_at) VALUES(?,?,?,?,?)",(rid,series_id,a,b,now))
                c.execute("INSERT INTO relationship_history(relationship_id,effective_at,trust,closeness,romantic_interest,conflict_level,respect,dependency,shared_history,reason,episode_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(rid,str(item.get("effective_at") or episode_date or now),item.get("trust"),item.get("closeness"),item.get("romantic_interest"),item.get("conflict_level"),item.get("respect"),item.get("dependency"),item.get("shared_history"),str(item.get("reason") or "Reviewed generated episode commit"),episode_id))
            for item in payload.get("open_story_arcs",[]):
                title=str(item.get("title","")).strip(); description=str(item.get("description","")).strip()
                if not title or not description: raise ValueError("Each open story arc needs title and description.")
                c.execute("INSERT INTO open_story_arcs(arc_id,series_id,title,description,status,opened_at,source_episode_id,priority) VALUES(?,?,?,?,'open',?,?,?)",(str(uuid.uuid4()),series_id,title,description,str(item.get("opened_at") or episode_date or now),episode_id,float(item.get("priority",.5))))
            note=str(payload.get("review_note") or "Explicit local generated-episode commit")
            c.execute("INSERT INTO commit_history(commit_id,episode_id,database_backup_path,committed_at,notes) VALUES(?,?,?,?,?)",(commit_id,episode_id,str(backup),now,note))
        return {
            "commit_id": commit_id, "episode_id": episode_id, "backup_path": str(backup), "status": "committed",
            "applied": {
                "world_events": len(payload.get("world_events", [])),
                "character_state_changes": len(payload.get("character_state_changes", [])),
                "memories": len(payload.get("memories", [])),
                "relationship_updates": len(payload.get("relationship_updates", [])),
                "open_story_arcs": len(payload.get("open_story_arcs", [])),
            },
            "commit_payload": str(payload_path) if payload_path.is_file() else None,
        }
