# SeriesAI

SeriesAI is a **local-first, resumable** system for building a persistent “Series Brain” from existing TV episodes and using it to plan, generate, render, review, and commit new episodes. It never requires OpenAI, Claude, cloud services, API keys, or token-based services.

## Portability

The project uses paths relative to its own root. `config.yaml` resolves `.` as the project directory, `Series` as the data directory, and ComfyUI as a sibling portable folder. SQLite stores source, cache, artifact, and transcript paths relative to the project root. Move the entire `SeriesAI` folder together with its sibling `ComfyUI_windows_portable` folder without editing global drive paths.

Status: the local analysis, review, planning, continuity, shot-queue, assembly, explicit-commit and ComfyUI-wrapper foundations are implemented. Video, vision, and TTS backends remain deliberately unconfigured until suitable local models are selected for the available hardware.

## Project layout

```text
<PROJECT_ROOT>\
├── config.yaml                # local paths and backend selection
├── start.py                   # the one live-monitor start script
├── src\seriesai\             # application logic (not ComfyUI-specific)
├── tests\                     # unit and integration tests
├── docs\                      # architecture and operational documentation
├── logs\                      # process logs (not committed)
└── Series\
    ├── Input\                 # source MP4 episodes
    ├── Generated\             # reviewed generated episodes
    ├── voices\                # per-character clean voice references
    ├── reference_images\      # character, location, and object references
    ├── datasets\              # future LoRA datasets
    └── series.db               # persistent Series Brain (created locally)
```

## Installation and start

Requires Python 3.10+ and Git. FFmpeg was detected on this PC. The launcher installs Python dependencies; Ollama remains strictly local. Vision, TTS, and video models are intentionally interchangeable local backends rather than bundled downloads.

```text
cd <PROJECT_ROOT>
python start.py
```

`start.py` is the only start script. Double-click it on Windows: it opens exactly one terminal and runs **all services in it** — Ollama, ComfyUI, the Coordinator and the local render worker — with every output line prefixed (`[ComfyUI]`, `[Ollama]`, `[Koordinator]`, `[Worker]`) next to the live monitor. Services that already answer (for example a running ComfyUI) are not started twice. `Strg+C` stops the monitor and every service started by it. In a terminal use `python start.py`. It creates a local virtual environment and installs required dependencies. It uses no administrator rights or `sudo`; on Linux/Ubuntu pip calls include `--break-system-packages`. Use `python -m seriesai.cli doctor` to inspect the local machine without making changes.

## ComfyUI integration

ComfyUI remains separate. SeriesAI adds only this wrapper package to the sibling installation:

```text
..\ComfyUI_windows_portable\ComfyUI\custom_nodes\SeriesAI\
```

Before creating or changing any pre-existing ComfyUI file, SeriesAI development creates a timestamped backup beside the original. The nodes call the `src\seriesai` package; business logic does not live in a ComfyUI graph. Available nodes cover loading, import, analysis, character/relationship state, timeline, review-only off-screen simulation, drafting, continuity checks, shot queues, assembly, and explicit commit.

## Local ComfyUI render workers

The coordinator is the only SQLite writer. A second PC can render a claimed
shot with its own local ComfyUI and upload the finished MP4 to the coordinator;
it never opens or writes the master's database. Export a working ComfyUI API
workflow as JSON, use `${seriesai.prompt}`, `${seriesai.negative_prompt}`,
`${seriesai.seed}` and `${seriesai.duration_seconds}` where appropriate, then
set `models.video_backend: comfyui` and `comfyui_api.workflow_template` in
that PC's portable config.

```text
python -m seriesai.worker --master http://MASTER-IP:8765 --worker-id render-pc-01
```

Each worker claims one pending shot atomically, uses only `127.0.0.1:8188` for
its own ComfyUI API, streams its completed output to the master and marks only
that shot completed. A crash leaves a `rendering` item recoverable through
`recover-queue`; a model or ComfyUI error becomes a retry for that one shot.

Verify the configured local Wan workflow without touching the Series Brain:

```text
python -m seriesai.cli render-self-test --frames 9
```

## Planned workflow

1. Import source MP4s from `Series\Input`.
2. Analyze audio, scenes, frames, characters, events, relationships, and timeline into the Series Brain.
3. Generate a proposed episode and individual resumable shot jobs.
4. Render and review output under `Series\Generated\SxxExx`.
5. Explicitly commit only an approved episode into the canonical Series Brain.

Generated episodes never update continuity automatically.

To ingest an existing local episode folder without copying or modifying it:

```text
python -m seriesai.cli import-folder "./Series_Source/01 - game shakers" --series-id game_shakers --series-title "Game Shakers"
```

The command hashes every source, skips completed imports, and extracts only missing artifacts. German is the default analysis and output language in `config.yaml`.

Scene detection is a separate resumable local step using PySceneDetect and extracts one compressed reference frame per detected scene:

```text
python -m seriesai.cli analyze-scenes game_shakers_s01e01
```

For the one-time local Whisper model download followed by an offline German transcript:

```text
python -m seriesai.cli transcribe game_shakers_s01e01
```

After an import has completed, process all ready episodes sequentially (each already completed transcript is skipped):

```text
python -m seriesai.cli transcribe-series --series-id game_shakers
```

Each Python CLI stage is resumable and skips completed records. A consolidated Python pipeline command will replace the former platform-specific shell wrappers.

## Series Brain: completed baseline and safe character proposals

After German transcripts are available, create resumable event summaries with
the configured local Ollama model:

```text
python -m seriesai.cli analyze-series-semantics --series-id game_shakers
```

For the currently imported Game Shakers material this stage is complete for
all 62 episodes. The dashboard displays `Series Brain fertig` separately from
currently active jobs; `Keine aktive Aufgabe` therefore correctly means that
the queued work has finished.

The next step proposes *reviewable* recurring character identities from the
whole series:

```text
python -m seriesai.cli propose-characters --series-id game_shakers
```

Candidates are stored in `character_candidates` with evidence and confidence.
They are not canonical `characters`, receive no immutable ID, and cannot
change continuity until a later review/approval step assigns one. This avoids
accidentally turning a transcription mistake or a one-off guest into a series
character.

The local dashboard exposes these proposals under **Figuren-Pruefung**. Enter
the proposed stable ID (for example `babe_001`) and choose **Freigeben**, or
choose **Ablehnen** for a false recognition. The same audited actions are
available in the CLI with `list-character-candidates`, `approve-character`,
and `reject-character`.

Each open proposal receives at least one local reference frame from one of its
evidence episodes. The dashboard labels it as an episode reference, not a
confirmed face match: visual confirmation remains a human review decision.
Click the thumbnail to open it large. In a group frame, click the intended
person in the large view to save a local cropped review reference for that
candidate.
Rebuild missing references with:

```text
python -m seriesai.cli prepare-character-reference-images --series-id game_shakers
```

Speech turns for future local diarization and voice-sample review are prepared
without claiming an identity or downloading a cloud model:

```text
python -m seriesai.cli prepare-series-speech-turns --series-id game_shakers
```

This preserves resumable provisional windows in `speaker_segments`. A later
local diarization backend may cluster them; only a reviewed mapping can attach
a window to a canonical character.

Compact local OGG voice-candidate clips can then be generated with:

```text
python -m seriesai.cli extract-series-voice-candidates --series-id game_shakers
```

The selector keeps up to three medium-length, distributed clips per episode
under `Series/voice_candidates/`. They are intentionally review candidates,
not yet a cloned voice profile.

## Reviewable character profiles

Create cautious German profile proposals for every approved character from the
stored Series-Brain events:

```text
python -m seriesai.cli propose-character-profiles --series-id game_shakers
```

The command uses the configured local Ollama model and records a checkpoint in
`generation_jobs`. Its output lives only in `character_profile_proposals`:
it does **not** change canonical personality, goals, memories, relationships,
or continuity. The dashboard lists the summary, traits, goals, confidence,
and evidence for human review.

## Reviewable relationship evidence

Create local relationship proposals without changing a canonical relationship:

```text
python -m seriesai.cli propose-relationships --series-id game_shakers
```

SeriesAI scans the already stored event descriptions and saves only repeated
co-mentions with their source episodes in `relationship_proposals`. It does
not invent trust, romance, conflict, or relationship-history values. Those
require later human review and an explicit approval action.

The dashboard's **Beziehungsbelege** section lists the participating figures,
event count and source episodes for every proposal.

## Draft continuity gate

Before any shot queue is created, run the deterministic checker for a draft:

```text
python -m seriesai.cli check-continuity --outline "Series/Generated/drafts/<job-id>/episode_outline.json"
```

It writes `continuity_report.json` beside the draft and checks known immutable
character IDs, scene numbers, locations, scene purpose, and duration against
the frozen Series-Brain context. It never modifies canonical continuity.

## Resumable shot queue

After a continuity-clean draft, create individual backend-neutral shots:

```text
python -m seriesai.cli create-shot-queue --outline "Series/Generated/drafts/<job-id>/episode_outline.json"
```

`shotlist.json` and `generation_job_items` contain one pending item per shot,
with a stable seed, prompt, output path and checkpoint. A later local LTX,
Wan or FramePack backend can claim only pending/failed items; completed ones
are never recreated.

The queue uses an atomic SQLite claim: several local workers can safely join
the same database and receive different pending shots. A crash leaves only the
claimed unfinished item to be retried; successful output items remain complete.

After an unexpected worker or PC stop, recover only interrupted items:

```text
python -m seriesai.cli recover-queue
```

## Off-screen life

For elapsed time between episodes, create low-impact review-only proposals:

```text
python -m seriesai.cli simulate-offscreen-life --series-id game_shakers --days 90 --start-date 2029-06-01
```

The simulation produces only minor hobbies, skill practice, project progress,
and social routines. It does not invent moves, breakups, death, injury, or
major career changes; all output stays `proposed` in `offscreen_events`.
When no imported episode has an `episode_date`, `--start-date` is required;
SeriesAI never fabricates a timeline date.

Set canonical dates explicitly when source episodes do not contain them:

```text
python -m seriesai.cli set-episode-date game_shakers_s01e01 --date 2026-01-01
```

SeriesAI recalculates `time_since_previous_episode` across the dated episodes.

## Local episode assembly

Once every shot in a queue is completed, assemble only those completed files:

```text
python -m seriesai.cli assemble-episode --job-id <queue-job-id> --episode-code S04E01
```

FFmpeg writes `Series/Generated/S04E01/episode.mp4`, `shotlist.json`, and
`metadata.json`. Missing or unfinished shots block assembly rather than
producing a silently incomplete episode.

## Review-first episode planning

After approving at least one canonical character, create a local outline with
the configured Ollama model:

```text
python -m seriesai.cli plan-episode --series-id game_shakers --idea "Eine Figur entwickelt einen experimentellen AI-Controller." --duration-minutes 22
```

The command snapshots current canonical characters, recent events, approved
relationship history and open arcs. It writes an outline below
`Series/Generated/drafts/<job-id>/episode_outline.json` and records a
resumable `generation_jobs` entry. It never commits the outline, creates no
canonical event, and submits no render job. Use `--resume-job <job-id>` after
an interrupted generation.

## Multiple series, sequentially

Use `<PROJECT_ROOT>\Series_Source` as the separate source root. Create one subfolder per series and place that series' MP4s in it, for example:

```text
<PROJECT_ROOT>\Series_Source\
├── 01 - Game Shakers\
│   ├── Game Shakers - S01E01.mp4
│   └── ...
└── 02 - Another Series\
    └── Another Series - S01E01.mp4
```

Run every series serially:

```text
python -m seriesai.cli import-all-series
```

Folder prefixes set processing order and are removed from the generated stable series ID (`01 - Game Shakers` becomes `game_shakers`). Finished episode stages are skipped on every rerun. Source episodes under `Series_Source` are ignored by Git.

## Multiple PCs

On a trusted LAN, simply run `python start.py` on every PC. The first PC starts the Coordinator; later PCs discover it with local UDP broadcast and automatically join as workers. `--join http://PRIMARY_PC_IP:8765` remains available as a direct fallback. Worker registration is implemented now; distributed job claiming/execution is the next queue phase, so the current CPU-intensive transcription loop remains local to its starter PC.

## Backup and restore

Close SeriesAI before copying SQLite files. Back up all three files when present:

```powershell
Copy-Item Series\series.db, Series\series.db-wal, Series\series.db-shm D:\SeriesAI-Backup\ -Force
```

Restore the complete set while SeriesAI is stopped. The database is intentionally ignored by Git because it is user data; source, schema migrations, and tests are version-controlled.

## Troubleshooting

- `ollama` is currently not installed. This does not block Phase 1 or database work; Phase 4 will use a local Ollama endpoint only when installed.
- With 6 GB VRAM, keep `prefer_cpu_offload: true`. Backends will expose VRAM/offload options instead of assuming a large GPU.
- If a job crashes later, its `generation_jobs` record, checkpoint JSON, attempts, and individual `rendered_shots` statuses will determine precisely what is resumed.

`Strg+C` beendet nur die Monitoranzeige, nicht den Koordinator oder laufende Analysejobs. See [Architecture](docs/ARCHITECTURE.md), [database design](docs/DATABASE.md), [ComfyUI integration](docs/COMFYUI.md), and [model backends](docs/MODELS.md).

The coordinator always runs with the project's `.venv` Python. This keeps the
single visible `start.py` monitor while ensuring dashboard restarts retain all
local project dependencies.

## Local Ollama and models

`start.py` keeps Ollama entirely inside the project: the Windows build is downloaded once into `runtime\ollama` (size and SHA-256 are checked against the GitHub release; an interrupted download resumes from its `.part` file), and it runs on its own port `11435` so it never collides with a system-wide Ollama. Models are stored in `models\ollama` (`OLLAMA_MODELS`). The configured model (`qwen3.5:9b`) is verified on every start with a load-and-generate test; if it is missing or incomplete, `ollama pull` resumes and re-verifies every blob, up to three attempts.
