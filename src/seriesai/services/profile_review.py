"""Explicit application of reviewed, non-empty profile fields only."""
from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from ..db import Database
from ..models import TraitChange, TraitLayer
from ..repositories import SeriesRepository

class ProfileReviewService:
    def __init__(self,database:Database): self.database=database
    def approve(self,proposal_id:str)->dict:
        with self.database.connect() as c:
            row=c.execute("SELECT character_id,profile_json,status FROM character_profile_proposals WHERE proposal_id=?",(proposal_id,)).fetchone()
        if not row or row[2]!="proposed": raise ValueError("Reviewable profile proposal not found.")
        p=json.loads(row[1]); now=datetime.now(timezone.utc); repo=SeriesRepository(self.database); traits=0
        for layer,key in ((TraitLayer.CORE,"core_traits"),(TraitLayer.DEVELOPED,"developed_traits")):
            for name,value in p.get(key,{}).items():
                repo.record_trait_change(TraitChange(character_id=row[0],trait_name=name,trait_layer=layer,new_value=value,effective_at=now,reason="Explicitly approved profile proposal",confidence=p.get("confidence"))) ; traits+=1
        with self.database.connect() as c:
            for goal in p.get("current_goals",[]): c.execute("INSERT INTO character_goals(goal_id,character_id,description,source_episode_id) VALUES(?,?,?,NULL)",(str(uuid.uuid4()),row[0],goal))
            c.execute("UPDATE character_profile_proposals SET status='approved',reviewed_at=CURRENT_TIMESTAMP WHERE proposal_id=?",(proposal_id,))
        return {"proposal_id":proposal_id,"traits_applied":traits,"goals_applied":len(p.get("current_goals",[]))}
