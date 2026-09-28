"""Explicit timeline writes with recalculated episode intervals."""
from __future__ import annotations
from datetime import date
from ..db import Database

class TimelineService:
    def __init__(self, database: Database) -> None: self.database=database
    def set_episode_date(self, episode_id: str, value: str) -> dict:
        parsed=date.fromisoformat(value)
        with self.database.connect() as c:
            row=c.execute("SELECT series_id FROM episodes WHERE episode_id=?",(episode_id,)).fetchone()
            if not row: raise ValueError("Episode not found.")
            series_id=row[0]; c.execute("UPDATE episodes SET episode_date=? WHERE episode_id=?",(parsed.isoformat(),episode_id))
            episodes=c.execute("SELECT episode_id,episode_date FROM episodes WHERE series_id=? AND episode_date IS NOT NULL ORDER BY episode_date,season_number,episode_number",(series_id,)).fetchall()
            previous=None
            for current in episodes:
                seconds=None if previous is None else int((date.fromisoformat(current[1])-date.fromisoformat(previous[1])).total_seconds())
                c.execute("UPDATE episodes SET time_since_previous_seconds=? WHERE episode_id=?",(seconds,current[0])); previous=current
        return {"episode_id":episode_id,"episode_date":parsed.isoformat()}
