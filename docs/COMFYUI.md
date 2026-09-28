# ComfyUI integration

SeriesAI does not edit portable ComfyUI code, launchers, models, or existing custom nodes. Its installation is an additive package under `ComfyUI\custom_nodes\SeriesAI`.

Nodes will validate inputs, call `seriesai` services, and return ComfyUI-friendly values. They will not duplicate SQLite rules, model logic, prompt construction, or queue handling. That separation allows the web UI and CLI to run the same workflow without ComfyUI.

Before an existing ComfyUI file is modified in any future phase, a timestamped copy will be made to `<PROJECT_ROOT>\backups\comfyui\`. New SeriesAI files do not need a backup because they have no previous user content.

## Implemented node contracts

- **Load Series**: opens database/configuration without rendering.
- **Import / Analyze Episode**: creates resumable import jobs.
- **Character / Relationship State**: retrieves date-bound canonical state.
- **Advance Time / Simulate Offscreen Life**: creates reviewable proposed events.
- **Generate Episode / Continuity Check**: produces structured plans and violations.
- **Generate Shot Queue / Queue Status / Render Episode**: creates backend-neutral per-shot work, exposes resumable status, and assembles completed shots.
- **Commit Episode**: the only node allowed to write a reviewed episode into canonical history.
