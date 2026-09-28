"""Deterministic, low-impact off-screen life proposals for elapsed series time."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone

from ..config import AppConfig
from ..db import Database


class OffscreenLifeService:
    job_type = "offscreen_life_simulation"

    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def simulate(self, series_id: str, days: int, start_date: str | None = None, job_id: str | None = None) -> dict:
        if not 1 <= days <= 3650: raise ValueError("days must be between 1 and 3650")
        job_id = job_id or str(uuid.uuid4())
        with self.database.connect() as connection:
            chars = [dict(row) for row in connection.execute("SELECT character_id,canonical_name FROM characters WHERE series_id=? AND active=1 ORDER BY character_id", (series_id,))]
            latest = start_date or connection.execute("SELECT MAX(episode_date) FROM episodes WHERE series_id=?", (series_id,)).fetchone()[0]
            if not latest:
                raise ValueError("Series has no episode_date yet. Supply an explicit --start-date (YYYY-MM-DD); SeriesAI will not invent timeline dates.")
            connection.execute("INSERT INTO generation_jobs(job_id,series_id,job_type,status,payload_json,progress,attempts,updated_at) VALUES(?,?,?,'running',?,0,1,?)", (job_id,series_id,self.job_type,json.dumps({"days":days,"start_date":latest}),datetime.now(timezone.utc).isoformat()))
        start = date.fromisoformat(str(latest)[:10]); events=[]
        templates=[("hobby", "hat ein bestehendes Hobby weiterverfolgt.", .15), ("skill_practice", "hat regelmäßig an einer bestehenden Fähigkeit gearbeitet.", .18), ("project_progress", "hat ein laufendes kleines Projekt weitergeführt.", .22), ("social_routine", "hat Kontakt zu ihrem/seinem bekannten Umfeld gehalten.", .16)]
        for char in chars:
            digest=int(hashlib.sha256(f"{series_id}:{char['character_id']}:{days}".encode()).hexdigest()[:8],16)
            count=1 if days<45 else min(3,1+days//120)
            for offset in range(count):
                kind, suffix, importance=templates[(digest+offset)%len(templates)]
                when=start+timedelta(days=min(days,(offset+1)*max(1,days//(count+1))))
                events.append((str(uuid.uuid4()),series_id,char['character_id'],when.isoformat(),kind,f"{char['canonical_name']} {suffix}",importance,"Niedrigschwellige Entwicklung für verstreichte Zeit; keine Beziehung, kein Umzug, keine Verletzung und kein großer Lebenswechsel ohne Beleg.",job_id))
        with self.database.connect() as connection:
            for event in events:
                connection.execute("INSERT INTO offscreen_events(offscreen_event_id,series_id,character_id,occurred_at,event_type,description,importance,plausibility_reason,source_job_id) VALUES(?,?,?,?,?,?,?,?,?)",event)
            connection.execute("UPDATE generation_jobs SET status='completed',progress=1,checkpoint_json=?,updated_at=? WHERE job_id=?",(json.dumps({"proposals":len(events)}),datetime.now(timezone.utc).isoformat(),job_id))
        return {"job_id":job_id,"status":"completed","proposals":len(events),"from_date":start.isoformat(),"days":days}
