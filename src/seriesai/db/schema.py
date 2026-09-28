"""Versioned SQLite schema. IDs are application-assigned and never reused."""

SCHEMA_VERSION = 9

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS series (series_id TEXT PRIMARY KEY, title TEXT NOT NULL, canonical_timezone TEXT NOT NULL DEFAULT 'UTC', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS episodes (episode_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), season_number INTEGER, episode_number INTEGER, title TEXT, source_path TEXT, episode_date TEXT, time_since_previous_seconds INTEGER, status TEXT NOT NULL DEFAULT 'imported', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS scenes (scene_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), sequence_number INTEGER NOT NULL, start_seconds REAL, end_seconds REAL, location_id TEXT, summary TEXT, UNIQUE(episode_id, sequence_number));
CREATE TABLE IF NOT EXISTS shots (shot_id TEXT PRIMARY KEY, scene_id TEXT NOT NULL REFERENCES scenes(scene_id), sequence_number INTEGER NOT NULL, start_seconds REAL, end_seconds REAL, camera TEXT, action TEXT, UNIQUE(scene_id, sequence_number));

CREATE TABLE IF NOT EXISTS characters (character_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), canonical_name TEXT NOT NULL, birth_date TEXT, apparent_age REAL, life_stage TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS character_aliases (alias_id INTEGER PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), alias TEXT NOT NULL, source_episode_id TEXT REFERENCES episodes(episode_id), UNIQUE(character_id, alias));
CREATE TABLE IF NOT EXISTS character_state (state_id INTEGER PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), episode_id TEXT REFERENCES episodes(episode_id), scene_id TEXT REFERENCES scenes(scene_id), effective_at TEXT NOT NULL, field_name TEXT NOT NULL, old_value TEXT, new_value TEXT NOT NULL, reason TEXT NOT NULL, event_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS character_appearance (appearance_id INTEGER PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), episode_id TEXT REFERENCES episodes(episode_id), effective_at TEXT NOT NULL, attributes_json TEXT NOT NULL, reference_path TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS character_personality (trait_id INTEGER PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), trait_name TEXT NOT NULL, trait_layer TEXT NOT NULL CHECK(trait_layer IN ('core','developed')), value REAL NOT NULL CHECK(value >= 0 AND value <= 1), effective_at TEXT NOT NULL, reason TEXT NOT NULL, episode_id TEXT REFERENCES episodes(episode_id), scene_id TEXT REFERENCES scenes(scene_id), old_value REAL, UNIQUE(character_id, trait_name, effective_at));
CREATE TABLE IF NOT EXISTS character_skills (skill_id INTEGER PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), skill_name TEXT NOT NULL, level REAL NOT NULL CHECK(level >= 0 AND level <= 1), effective_at TEXT NOT NULL, reason TEXT NOT NULL, episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS character_goals (goal_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, resolved_at TEXT, source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS character_memories (memory_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), event_id TEXT, memory_type TEXT NOT NULL CHECK(memory_type IN ('short_term','long_term','core')), description TEXT NOT NULL, emotional_weight REAL NOT NULL CHECK(emotional_weight >= 0 AND emotional_weight <= 1), importance REAL NOT NULL CHECK(importance >= 0 AND importance <= 1), created_at TEXT NOT NULL, last_referenced TEXT, source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS character_events (event_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), episode_id TEXT REFERENCES episodes(episode_id), scene_id TEXT REFERENCES scenes(scene_id), occurred_at TEXT NOT NULL, event_type TEXT NOT NULL, description TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.5);

CREATE TABLE IF NOT EXISTS relationships (relationship_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), character_a_id TEXT NOT NULL REFERENCES characters(character_id), character_b_id TEXT NOT NULL REFERENCES characters(character_id), created_at TEXT NOT NULL, CHECK(character_a_id < character_b_id), UNIQUE(series_id, character_a_id, character_b_id));
CREATE TABLE IF NOT EXISTS relationship_history (history_id INTEGER PRIMARY KEY, relationship_id TEXT NOT NULL REFERENCES relationships(relationship_id), effective_at TEXT NOT NULL, trust REAL CHECK(trust BETWEEN 0 AND 1), closeness REAL CHECK(closeness BETWEEN 0 AND 1), romantic_interest REAL CHECK(romantic_interest BETWEEN 0 AND 1), conflict_level REAL CHECK(conflict_level BETWEEN 0 AND 1), respect REAL CHECK(respect BETWEEN 0 AND 1), dependency REAL CHECK(dependency BETWEEN 0 AND 1), shared_history TEXT, reason TEXT NOT NULL, episode_id TEXT REFERENCES episodes(episode_id), scene_id TEXT REFERENCES scenes(scene_id));

CREATE TABLE IF NOT EXISTS locations (location_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), name TEXT NOT NULL, location_type TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS location_state (location_state_id INTEGER PRIMARY KEY, location_id TEXT NOT NULL REFERENCES locations(location_id), effective_at TEXT NOT NULL, attributes_json TEXT NOT NULL, episode_id TEXT REFERENCES episodes(episode_id), reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects (object_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), name TEXT NOT NULL, object_type TEXT, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS object_ownership (ownership_id INTEGER PRIMARY KEY, object_id TEXT NOT NULL REFERENCES objects(object_id), character_id TEXT REFERENCES characters(character_id), location_id TEXT REFERENCES locations(location_id), valid_from TEXT NOT NULL, valid_to TEXT, reason TEXT NOT NULL, CHECK(character_id IS NOT NULL OR location_id IS NOT NULL));
CREATE TABLE IF NOT EXISTS world_events (world_event_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), occurred_at TEXT NOT NULL, event_type TEXT NOT NULL, description TEXT NOT NULL, source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS timeline (timeline_id INTEGER PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), occurred_at TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT, event_type TEXT NOT NULL, description TEXT NOT NULL, source_episode_id TEXT REFERENCES episodes(episode_id), source_scene_id TEXT REFERENCES scenes(scene_id));
CREATE TABLE IF NOT EXISTS open_story_arcs (arc_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', opened_at TEXT NOT NULL, source_episode_id TEXT REFERENCES episodes(episode_id), priority REAL NOT NULL DEFAULT 0.5);
CREATE TABLE IF NOT EXISTS resolved_story_arcs (arc_id TEXT PRIMARY KEY REFERENCES open_story_arcs(arc_id), resolved_at TEXT NOT NULL, resolution TEXT NOT NULL, resolution_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS running_gags (gag_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), description TEXT NOT NULL, first_episode_id TEXT REFERENCES episodes(episode_id), last_episode_id TEXT REFERENCES episodes(episode_id), use_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dialogue_examples (dialogue_id TEXT PRIMARY KEY, character_id TEXT REFERENCES characters(character_id), episode_id TEXT REFERENCES episodes(episode_id), scene_id TEXT REFERENCES scenes(scene_id), text TEXT NOT NULL, context TEXT, quality_score REAL);
CREATE TABLE IF NOT EXISTS voice_profiles (voice_profile_id TEXT PRIMARY KEY, character_id TEXT NOT NULL UNIQUE REFERENCES characters(character_id), voice_identity TEXT NOT NULL UNIQUE, backend_name TEXT, reference_dir TEXT NOT NULL, pitch_modifier REAL NOT NULL DEFAULT 0, speech_speed REAL NOT NULL DEFAULT 1, energy REAL NOT NULL DEFAULT 0.5, timbre_age_factor REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS visual_references (reference_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, reference_kind TEXT NOT NULL, file_path TEXT NOT NULL, perceptual_hash TEXT, embedding_json TEXT, quality_score REAL, source_episode_id TEXT REFERENCES episodes(episode_id), source_scene_id TEXT REFERENCES scenes(scene_id));

CREATE TABLE IF NOT EXISTS generation_jobs (job_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), episode_id TEXT REFERENCES episodes(episode_id), job_type TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','retry','cancelled')), payload_json TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 1), checkpoint_json TEXT, attempts INTEGER NOT NULL DEFAULT 0, error_message TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS rendered_shots (render_job_id TEXT PRIMARY KEY REFERENCES generation_jobs(job_id), shot_id TEXT NOT NULL REFERENCES shots(shot_id), seed INTEGER, prompt TEXT, negative_prompt TEXT, model_name TEXT, settings_json TEXT NOT NULL, references_json TEXT NOT NULL, output_path TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, completed_at TEXT);

CREATE INDEX IF NOT EXISTS idx_episode_series_date ON episodes(series_id, episode_date);
CREATE INDEX IF NOT EXISTS idx_character_state_lookup ON character_state(character_id, effective_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_lookup ON character_memories(character_id, memory_type, importance DESC);
CREATE INDEX IF NOT EXISTS idx_timeline_series_time ON timeline(series_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_job_resume ON generation_jobs(status, updated_at);
"""

# Additive migrations only. Existing user data is never rewritten implicitly.
MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS episode_versions (episode_version_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), version_number INTEGER NOT NULL, status TEXT NOT NULL, screenplay_path TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(episode_id, version_number));
CREATE TABLE IF NOT EXISTS character_state_history (history_id INTEGER PRIMARY KEY, state_id INTEGER NOT NULL REFERENCES character_state(state_id), confidence REAL CHECK(confidence BETWEEN 0 AND 1), source_timestamp_seconds REAL, triggering_event_id TEXT, explanation TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS character_appearance_history (history_id INTEGER PRIMARY KEY, appearance_id INTEGER NOT NULL REFERENCES character_appearance(appearance_id), confidence REAL CHECK(confidence BETWEEN 0 AND 1), source_timestamp_seconds REAL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS character_personality_history (history_id INTEGER PRIMARY KEY, trait_id INTEGER NOT NULL REFERENCES character_personality(trait_id), confidence REAL CHECK(confidence BETWEEN 0 AND 1), source_timestamp_seconds REAL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS character_habits (habit_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', established_at TEXT NOT NULL, ended_at TEXT, source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS character_routines (routine_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), description TEXT NOT NULL, cadence TEXT, status TEXT NOT NULL DEFAULT 'active', established_at TEXT NOT NULL, ended_at TEXT, source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS character_injuries (injury_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), description TEXT NOT NULL, severity REAL CHECK(severity BETWEEN 0 AND 1), incurred_at TEXT NOT NULL, recovered_at TEXT, status TEXT NOT NULL DEFAULT 'active', source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS character_jobs (job_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), title TEXT NOT NULL, employer TEXT, started_at TEXT NOT NULL, ended_at TEXT, source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS character_homes (home_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), location_id TEXT NOT NULL REFERENCES locations(location_id), moved_in_at TEXT NOT NULL, moved_out_at TEXT, source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS location_history (history_id INTEGER PRIMARY KEY, location_state_id INTEGER NOT NULL REFERENCES location_state(location_state_id), confidence REAL CHECK(confidence BETWEEN 0 AND 1), source_timestamp_seconds REAL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS object_state (object_state_id INTEGER PRIMARY KEY, object_id TEXT NOT NULL REFERENCES objects(object_id), effective_at TEXT NOT NULL, condition_description TEXT, status TEXT NOT NULL, location_id TEXT REFERENCES locations(location_id), reason TEXT NOT NULL, source_episode_id TEXT REFERENCES episodes(episode_id));
CREATE TABLE IF NOT EXISTS object_history (history_id INTEGER PRIMARY KEY, object_state_id INTEGER NOT NULL REFERENCES object_state(object_state_id), confidence REAL CHECK(confidence BETWEEN 0 AND 1), source_timestamp_seconds REAL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS voice_samples (voice_sample_id TEXT PRIMARY KEY, voice_profile_id TEXT NOT NULL REFERENCES voice_profiles(voice_profile_id), file_path TEXT NOT NULL, sample_state TEXT NOT NULL CHECK(sample_state IN ('raw','cleaned','approved','rejected')), duration_seconds REAL, quality_score REAL, source_episode_id TEXT REFERENCES episodes(episode_id), source_scene_id TEXT REFERENCES scenes(scene_id), speaker_confidence REAL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS reference_embeddings (embedding_id TEXT PRIMARY KEY, reference_id TEXT NOT NULL REFERENCES visual_references(reference_id), backend_name TEXT NOT NULL, vector_path TEXT, dimensions INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS training_datasets (dataset_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), dataset_type TEXT NOT NULL, entity_type TEXT, entity_id TEXT, manifest_path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS training_runs (training_run_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES training_datasets(dataset_id), backend_name TEXT NOT NULL, settings_json TEXT NOT NULL, status TEXT NOT NULL, output_path TEXT, started_at TEXT, finished_at TEXT, error_message TEXT);
CREATE TABLE IF NOT EXISTS generation_job_items (item_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES generation_jobs(job_id), item_type TEXT NOT NULL, sequence_number INTEGER NOT NULL, status TEXT NOT NULL, checkpoint_json TEXT, error_message TEXT, started_at TEXT, finished_at TEXT, UNIQUE(job_id, sequence_number));
CREATE TABLE IF NOT EXISTS render_attempts (attempt_id TEXT PRIMARY KEY, render_job_id TEXT NOT NULL REFERENCES rendered_shots(render_job_id), attempt_number INTEGER NOT NULL, profile TEXT, settings_json TEXT NOT NULL, status TEXT NOT NULL, error_message TEXT, started_at TEXT NOT NULL, finished_at TEXT, UNIQUE(render_job_id, attempt_number));
CREATE TABLE IF NOT EXISTS continuity_issues (issue_id TEXT PRIMARY KEY, episode_id TEXT REFERENCES episodes(episode_id), severity TEXT NOT NULL, rule_name TEXT NOT NULL, description TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS continuity_resolutions (resolution_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES continuity_issues(issue_id), resolution TEXT NOT NULL, resolved_by TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS offscreen_events (offscreen_event_id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(series_id), character_id TEXT REFERENCES characters(character_id), occurred_at TEXT NOT NULL, event_type TEXT NOT NULL, description TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.3 CHECK(importance BETWEEN 0 AND 1), plausibility_reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', source_job_id TEXT REFERENCES generation_jobs(job_id));
CREATE TABLE IF NOT EXISTS model_registry (model_id TEXT PRIMARY KEY, model_type TEXT NOT NULL, backend_name TEXT NOT NULL, local_path TEXT, license_name TEXT, precision TEXT, quantization TEXT, minimum_vram_gb REAL, recommended_vram_gb REAL, minimum_ram_gb REAL, capabilities_json TEXT NOT NULL DEFAULT '{}', notes TEXT, active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS backend_settings (setting_id TEXT PRIMARY KEY, backend_name TEXT NOT NULL, setting_key TEXT NOT NULL, setting_value_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(backend_name, setting_key));
CREATE TABLE IF NOT EXISTS review_status (review_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), status TEXT NOT NULL CHECK(status IN ('draft','rendered','approved','rejected','committed')), reviewer_note TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS commit_history (commit_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), database_backup_path TEXT NOT NULL, committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, rollback_of_commit_id TEXT, notes TEXT);
CREATE INDEX IF NOT EXISTS idx_job_item_resume ON generation_job_items(job_id, status, sequence_number);
CREATE INDEX IF NOT EXISTS idx_offscreen_events_time ON offscreen_events(series_id, occurred_at);
"""

MIGRATION_3 = """
ALTER TABLE episodes ADD COLUMN source_sha256 TEXT;
ALTER TABLE episodes ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE episodes ADD COLUMN analysis_language TEXT NOT NULL DEFAULT 'de';
CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_source_hash ON episodes(series_id, source_sha256) WHERE source_sha256 IS NOT NULL;
CREATE TABLE IF NOT EXISTS import_artifacts (artifact_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), artifact_type TEXT NOT NULL, file_path TEXT NOT NULL, content_sha256 TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(episode_id, artifact_type));
CREATE TABLE IF NOT EXISTS analysis_runs (analysis_run_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), stage_name TEXT NOT NULL, status TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'de', parameters_json TEXT NOT NULL DEFAULT '{}', checkpoint_json TEXT, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT, error_message TEXT, UNIQUE(episode_id, stage_name));
"""

MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS transcripts (transcript_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL UNIQUE REFERENCES episodes(episode_id), backend_name TEXT NOT NULL, model_name TEXT NOT NULL, language TEXT NOT NULL, text_path TEXT NOT NULL, json_path TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS transcript_segments (segment_id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL REFERENCES transcripts(transcript_id), sequence_number INTEGER NOT NULL, start_seconds REAL NOT NULL, end_seconds REAL NOT NULL, text TEXT NOT NULL, language_probability REAL, no_speech_probability REAL, UNIQUE(transcript_id, sequence_number));
CREATE TABLE IF NOT EXISTS speaker_segments (speaker_segment_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), start_seconds REAL NOT NULL, end_seconds REAL NOT NULL, provisional_speaker_id TEXT NOT NULL, character_id TEXT REFERENCES characters(character_id), confidence REAL CHECK(confidence BETWEEN 0 AND 1), source_backend TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'provisional');
CREATE INDEX IF NOT EXISTS idx_transcript_segments_episode ON transcript_segments(transcript_id, start_seconds);
CREATE INDEX IF NOT EXISTS idx_speaker_segments_episode ON speaker_segments(episode_id, start_seconds);
"""

# Candidates are deliberately separate from canonical characters.  A local
# model may propose names and aliases, but a stable immutable character_id is
# only assigned after review/approval.
MIGRATION_5 = """
CREATE TABLE IF NOT EXISTS character_candidates (
    candidate_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(series_id),
    canonical_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    role_description TEXT,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','approved','rejected','merged')),
    source_signature TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    UNIQUE(series_id, canonical_name, source_signature)
);
CREATE INDEX IF NOT EXISTS idx_character_candidates_series_status ON character_candidates(series_id, status);
"""

MIGRATION_6 = """
CREATE TABLE IF NOT EXISTS voice_sample_candidates (
    candidate_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id),
    speaker_segment_id TEXT NOT NULL UNIQUE REFERENCES speaker_segments(speaker_segment_id),
    file_path TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    quality_score REAL NOT NULL CHECK(quality_score BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','accepted','rejected')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_voice_sample_candidates_episode ON voice_sample_candidates(episode_id, status);
"""

MIGRATION_7 = """
CREATE TABLE IF NOT EXISTS character_profile_proposals (
    proposal_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL UNIQUE REFERENCES characters(character_id),
    profile_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','approved','rejected')),
    source_job_id TEXT REFERENCES generation_jobs(job_id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT
);
"""

# Proposed relationships are deliberately separate from relationship_history.
# Co-mentions are evidence of a possible shared story, never proof of trust,
# romance, conflict, or a canonical relationship state.
MIGRATION_8 = """
CREATE TABLE IF NOT EXISTS relationship_proposals (
    proposal_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(series_id),
    character_a_id TEXT NOT NULL REFERENCES characters(character_id),
    character_b_id TEXT NOT NULL REFERENCES characters(character_id),
    summary_de TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    co_mentioned_event_count INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','approved','rejected')),
    source_job_id TEXT REFERENCES generation_jobs(job_id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    CHECK(character_a_id < character_b_id),
    UNIQUE(series_id, character_a_id, character_b_id)
);
CREATE INDEX IF NOT EXISTS idx_relationship_proposals_series_status ON relationship_proposals(series_id, status);
"""

MIGRATION_9 = """
ALTER TABLE generation_job_items ADD COLUMN worker_id TEXT;
ALTER TABLE generation_job_items ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_job_item_claim ON generation_job_items(job_id, status, sequence_number);
"""

MIGRATIONS: dict[int, str] = {2: MIGRATION_2, 3: MIGRATION_3, 4: MIGRATION_4, 5: MIGRATION_5, 6: MIGRATION_6, 7: MIGRATION_7, 8: MIGRATION_8, 9: MIGRATION_9}
