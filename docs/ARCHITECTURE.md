# Architecture

## Design principles

SeriesAI treats continuity as canonical data, models as replaceable workers, and rendering as a proposed artifact until an explicit commit. SQLite is the source of truth; JSON output, images, audio, and MP4 files are derived artifacts.

```text
MP4 import → analysis workers → Series Brain (SQLite)
                                  ↓
idea → retrieval → outline → continuity checks → screenplay → scenes/shots
                                  ↓
                         persistent render queue → ComfyUI/video backend
                                  ↓
                           review → explicit episode commit
```

## Modules

- `config.py`: validated local paths, hardware capacity, and selected adapters.
- `db/`: versioned, transactional SQLite schema and access layer.
- `backends/`: provider-neutral contracts for LLM, transcription, vision, voice, video, and audio.
- `services/`: import, analysis, timeline, profile/relationship review, off-screen simulation, planning, continuity, queue, assembly, and commit orchestration.
- `cli.py` and `coordinator.py`: non-interactive local commands plus the portable local dashboard/coordinator.

## Resume policy

Every long operation becomes a `generation_jobs` record. Its payload describes immutable inputs; checkpoint data records completed units. Jobs progress through `pending`, `running`, `completed`, `failed`, or `retry`. A render maps one job to one shot, so an out-of-memory failure re-renders only that shot.

## Custom nodes

The additive `ComfyUI/custom_nodes/SeriesAI` package provides thin wrappers for Load Series, Import Episode, Analyze Episode, Character State, Relationship State, Advance Time, Simulate Offscreen Life, Generate Episode, Continuity Check, Generate Shot Queue, Queue Status, Render Episode, and Commit Episode. Rendering remains backend-neutral: a video backend must complete individual queue shots before the editor may assemble an episode.
