# Series Brain database

`Series\series.db` uses SQLite foreign keys, WAL mode, a busy timeout, transactional writes, and `schema_migrations`. The current schema is v9, including explicit review/commit, model, training, off-screen, continuity, state-history, job-item, worker-claim, character-profile, voice-candidate, and relationship-proposal records. Before a migration, `init` writes a consistent SQLite backup to `backups\migrations`. All canonical IDs are application-assigned strings: for example `babe_001`. They never encode mutable visual traits or display names.

## Main groups

| Group | Tables | Purpose |
| --- | --- | --- |
| Narrative source | `series`, `episodes`, `scenes`, `shots` | Source and planned episode structure |
| Character continuity | `characters`, aliases, state, appearance, personality, skills, goals, memories, events | Immutable identity plus justified historical changes |
| World continuity | relationships/history, locations/state, objects/ownership, world events, timeline | Time-aware facts and histories |
| Story/voice/reference | arcs, gags, dialogue, voice profiles, visual references | Retrieval material for writing and rendering |
| Resumable production | `generation_jobs`, `rendered_shots` | Checkpoints and per-shot recovery |

Trait history requires an effective time, reason, prior value, new value, and episode/scene provenance. `core` and `developed` traits are constrained separately by `trait_layer`. Relationship changes are append-only history snapshots. Core memories remain distinguishable through `memory_type = 'core'`.

`open_story_arcs` is retained as the canonical arc row after resolution; `resolved_story_arcs` records resolution timing and text. This preserves stable arc IDs and complete audit history.
