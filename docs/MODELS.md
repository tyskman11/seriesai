# Local model backends

No backend receives an API key or uses a cloud endpoint. Selection lives in `config.yaml`; concrete implementations will be local and replaceable.

| Contract | Current local adapter | Replaceable alternatives |
| --- | --- | --- |
| `TranscriptionBackend` | WhisperX | faster-whisper, local diarization stack |
| `LLMBackend` | Ollama at `127.0.0.1:11435` | another local OpenAI-compatible runtime, llama.cpp |
| `VisionBackend` | configurable local VLM | Qwen-VL/Qwen3-VL, other local VLMs |
| `VoiceBackend` | configurable local TTS | XTTS, Chatterbox |
| `VideoBackend` | configurable local ComfyUI workflow | LTX, Wan, FramePack |
| `AudioBackend` | local FFmpeg/Python | other local DSP chains |

The RTX 3060 Laptop has 6 GB VRAM, so backends must offer CPU/RAM offload, low-VRAM profiles, sequential processing, and explicit cleanup. Quality is favored over speed; no architecture assumes an entire episode fits in VRAM.

Ollama is configured locally on this PC (`qwen3.5:9b` at `127.0.0.1:11435`) for German structured analysis, planning and reviewable continuity candidates. It has no API key and no cloud dependency. Video, vision and TTS adapters remain intentionally unconfigured until a locally installed model and workflow are selected; SeriesAI will not silently replace them with a cloud service.

## ComfyUI video worker

Set `models.video_backend: comfyui` only after exporting a tested local API
workflow JSON. Set `comfyui_api.workflow_template` to that relative JSON path.
`ComfyUIWorkflowBackend` accepts loopback API URLs only, recursively replaces
`${seriesai.<field>}` placeholders from an immutable shot specification, polls
the local ComfyUI history endpoint and atomically copies its output before the
worker uploads it to the coordinator. The default remains disabled, so an
existing portable ComfyUI installation is never changed or guessed.

## Piper dialogue backend

For non-cloned local dialogue synthesis set `models.tts_backend: piper`, place
Piper voice models beneath the configured `piper.models_root`, and create a
stable `voice_profiles` record with a model filename as `voice_identity`.
`synthesize-dialogue` then produces a WAV plus a local JSON manifest. It
refuses to run when no local backend/model/profile exists; it never falls back
to an online TTS provider.
