from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class TraitLayer(str, Enum):
    CORE = "core"
    DEVELOPED = "developed"


class MemoryType(str, Enum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    CORE = "core"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY = "retry"
    CANCELLED = "cancelled"


class CharacterCreate(BaseModel):
    character_id: str = Field(pattern=r"^[a-z0-9_]+$")
    series_id: str
    canonical_name: str = Field(min_length=1)
    birth_date: date | None = None
    apparent_age: float | None = Field(default=None, ge=0)
    life_stage: str | None = None


class TraitChange(BaseModel):
    character_id: str
    trait_name: str
    trait_layer: TraitLayer
    old_value: float | None = Field(default=None, ge=0, le=1)
    new_value: float = Field(ge=0, le=1)
    effective_at: datetime
    reason: str = Field(min_length=1)
    episode_id: str | None = None
    scene_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class GenerationJobCreate(BaseModel):
    job_id: str
    series_id: str
    job_type: str
    payload_json: str = "{}"
    episode_id: str | None = None


class CharacterMemoryCreate(BaseModel):
    memory_id: str
    character_id: str
    description: str = Field(min_length=1)
    memory_type: MemoryType
    emotional_weight: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    created_at: datetime
    event_id: str | None = None
    source_episode_id: str | None = None


class RelationshipUpdate(BaseModel):
    series_id: str
    character_a_id: str
    character_b_id: str
    effective_at: datetime
    reason: str = Field(min_length=1)
    trust: float | None = Field(default=None, ge=0, le=1)
    closeness: float | None = Field(default=None, ge=0, le=1)
    romantic_interest: float | None = Field(default=None, ge=0, le=1)
    conflict_level: float | None = Field(default=None, ge=0, le=1)
    respect: float | None = Field(default=None, ge=0, le=1)
    dependency: float | None = Field(default=None, ge=0, le=1)
    shared_history: str | None = None
    episode_id: str | None = None
    scene_id: str | None = None


class TimelineEventCreate(BaseModel):
    series_id: str
    occurred_at: datetime
    entity_type: str
    event_type: str
    description: str = Field(min_length=1)
    entity_id: str | None = None
    source_episode_id: str | None = None
    source_scene_id: str | None = None
