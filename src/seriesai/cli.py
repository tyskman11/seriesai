from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
from pathlib import Path

from .config import load_config
from .paths import project_relative
from .console import ProgressReporter
from .db import Database
from .services import EpisodeImporter
from .services.scene_detection import SceneDetectionService
from .services.transcription import TranscriptionService
from .services.semantic_analysis import SemanticAnalysisService
from .services.character_inventory import CharacterInventoryService
from .services.character_review import CharacterReviewService
from .services.speech_turns import SpeechTurnService
from .services.voice_candidates import VoiceCandidateService
from .services.character_reference_images import CharacterReferenceImageService
from .services.episode_planner import EpisodePlannerService
from .services.character_profiles import CharacterProfileService
from .services.profile_review import ProfileReviewService
from .services.relationship_proposals import RelationshipProposalService
from .services.continuity_checker import ContinuityChecker
from .services.shot_queue import ShotQueueService
from .services.queue_worker import QueueWorkerService
from .services.offscreen_life import OffscreenLifeService
from .services.episode_editor import EpisodeEditorService
from .services.character_dataset import CharacterDatasetService
from .services.timeline import TimelineService
from .services.episode_commit import EpisodeCommitService
from .services.voice_synthesis import VoiceSynthesisService
from .services.render_readiness import render_readiness
from .backends.comfyui import ComfyUIWorkflowBackend, WAN_NEGATIVE_PROMPT


def build_parser() -> argparse.ArgumentParser:
    """Build the full CLI parser without running anything. Reused by the WebUI's generic command runner for introspection."""
    parser = argparse.ArgumentParser(prog="seriesai")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create or migrate the Series Brain SQLite database")
    subparsers.add_parser("init", help="Alias for init-db")
    subparsers.add_parser("doctor", help="Report local SeriesAI prerequisites without changing them")
    import_parser = subparsers.add_parser("import", help="Import one local MP4 with resumable baseline stages")
    import_parser.add_argument("source", type=Path)
    import_parser.add_argument("--series-id", required=True)
    import_parser.add_argument("--series-title", required=True)
    folder_parser = subparsers.add_parser("import-folder", help="Import all MP4 episodes in a folder, resuming completed entries")
    folder_parser.add_argument("folder", type=Path)
    folder_parser.add_argument("--series-id", required=True)
    folder_parser.add_argument("--series-title", required=True)
    all_import_parser = subparsers.add_parser("import-all-series", help="Import every ordered series folder beneath Series_Source, resuming completed episodes")
    all_import_parser.add_argument("--source-root", type=Path, default=Path("Series_Source"))
    analyze_parser = subparsers.add_parser("analyze-scenes", help="Detect scenes and representative frames for one imported episode")
    analyze_parser.add_argument("episode_id")
    analyze_parser.add_argument("--threshold", type=float, default=27.0)
    analyze_series_parser = subparsers.add_parser("analyze-series-scenes", help="Detect scenes for every imported episode, resuming finished episodes")
    analyze_series_parser.add_argument("--series-id", required=True)
    analyze_series_parser.add_argument("--threshold", type=float, default=27.0)
    transcript_parser = subparsers.add_parser("transcribe", help="Transcribe an imported episode locally in German")
    transcript_parser.add_argument("episode_id")
    transcript_folder_parser = subparsers.add_parser("transcribe-series", help="Transcribe all locally ready episodes in series order")
    transcript_folder_parser.add_argument("--series-id", required=True)
    semantic_parser = subparsers.add_parser("analyze-semantics", help="Analyze one completed transcript using local Ollama")
    semantic_parser.add_argument("episode_id")
    semantic_series_parser = subparsers.add_parser("analyze-series-semantics", help="Create resumable Series-Brain event summaries for every completed transcript")
    semantic_series_parser.add_argument("--series-id", required=True)
    inventory_parser = subparsers.add_parser("propose-characters", help="Propose reviewable character identities from all completed Series-Brain analyses")
    inventory_parser.add_argument("--series-id", required=True)
    candidate_list_parser = subparsers.add_parser("list-character-candidates", help="List reviewable character proposals without changing Series Brain")
    candidate_list_parser.add_argument("--series-id", required=True)
    approve_parser = subparsers.add_parser("approve-character", help="Approve one proposed character and assign its permanent internal ID")
    approve_parser.add_argument("--series-id", required=True)
    approve_parser.add_argument("--candidate-id", required=True)
    approve_parser.add_argument("--character-id", required=True)
    approve_parser.add_argument("--name")
    reject_parser = subparsers.add_parser("reject-character", help="Reject one proposed character without deleting its audit trail")
    reject_parser.add_argument("--series-id", required=True)
    reject_parser.add_argument("--candidate-id", required=True)
    turns_parser = subparsers.add_parser("prepare-speech-turns", help="Prepare non-identified speech turns for a completed transcript")
    turns_parser.add_argument("episode_id")
    turns_series_parser = subparsers.add_parser("prepare-series-speech-turns", help="Prepare resumable speech turns for every completed transcript")
    turns_series_parser.add_argument("--series-id", required=True)
    voice_parser = subparsers.add_parser("extract-voice-candidates", help="Extract compact reviewable voice clips from prepared speech turns")
    voice_parser.add_argument("episode_id")
    voice_series_parser = subparsers.add_parser("extract-series-voice-candidates", help="Extract resumable voice clip candidates for a series")
    voice_series_parser.add_argument("--series-id", required=True)
    reference_parser = subparsers.add_parser("prepare-character-reference-images", help="Select one local face reference from evidence episodes for each character proposal")
    reference_parser.add_argument("--series-id", required=True)
    planner_parser = subparsers.add_parser("plan-episode", help="Create a local review-only episode outline from the Series Brain")
    planner_parser.add_argument("--series-id", required=True)
    planner_parser.add_argument("--idea", required=True)
    planner_parser.add_argument("--duration-minutes", type=float, default=22)
    planner_parser.add_argument("--notes")
    planner_parser.add_argument("--resume-job")
    profile_parser = subparsers.add_parser("propose-character-profiles", help="Generate review-only local personality and goal proposals for approved characters")
    profile_parser.add_argument("--series-id", required=True)
    profile_parser.add_argument("--resume-job")
    approve_profile_parser = subparsers.add_parser("approve-character-profile", help="Explicitly apply a reviewed non-empty character profile proposal")
    approve_profile_parser.add_argument("--proposal-id", required=True)
    relationship_parser = subparsers.add_parser("propose-relationships", help="Generate review-only evidence-based relationship proposals for approved characters")
    relationship_parser.add_argument("--series-id", required=True)
    relationship_parser.add_argument("--resume-job")
    continuity_parser = subparsers.add_parser("check-continuity", help="Run deterministic continuity checks for a draft outline before rendering")
    continuity_parser.add_argument("--outline", required=True, type=Path)
    shot_queue_parser = subparsers.add_parser("create-shot-queue", help="Create resumable backend-neutral shot items from a checked draft")
    shot_queue_parser.add_argument("--outline", required=True, type=Path)
    shot_queue_parser.add_argument("--job-id")
    recover_queue_parser = subparsers.add_parser("recover-queue", help="Move interrupted rendering shots back to retry")
    recover_queue_parser.add_argument("--job-id")
    offscreen_parser = subparsers.add_parser("simulate-offscreen-life", help="Create review-only, low-impact off-screen life proposals")
    offscreen_parser.add_argument("--series-id", required=True)
    offscreen_parser.add_argument("--days", required=True, type=int)
    offscreen_parser.add_argument("--start-date", help="Required if imported episodes do not have timeline dates (YYYY-MM-DD)")
    editor_parser = subparsers.add_parser("assemble-episode", help="Assemble a completed local shot queue into episode.mp4 with FFmpeg")
    editor_parser.add_argument("--job-id", required=True)
    editor_parser.add_argument("--episode-code", required=True, help="For example S04E01")
    dataset_parser = subparsers.add_parser("build-character-dataset", help="Build a character-only local LoRA dataset manifest from reviewed references")
    dataset_parser.add_argument("--character-id", required=True)
    date_parser = subparsers.add_parser("set-episode-date", help="Set a canonical episode date and recalculate series intervals")
    date_parser.add_argument("episode_id")
    date_parser.add_argument("--date", required=True)
    commit_parser=subparsers.add_parser("commit-episode",help="Backup and explicitly commit a reviewed generated episode")
    commit_parser.add_argument("--series-id",required=True); commit_parser.add_argument("--episode-id",required=True); commit_parser.add_argument("--episode-code",required=True); commit_parser.add_argument("--date")
    tts_parser = subparsers.add_parser("synthesize-dialogue", help="Synthesize one line with an explicitly configured local voice backend")
    tts_parser.add_argument("--character-id", required=True); tts_parser.add_argument("--text", required=True); tts_parser.add_argument("--output", type=Path, required=True)
    embed_voices_parser = subparsers.add_parser("embed-voices", help="Compute local speaker embeddings for every transcribed speech segment (resumable)")
    embed_voices_parser.add_argument("--series-id", required=True)
    cluster_voices_parser = subparsers.add_parser("cluster-voices", help="Group the embedded speech into voice clusters for review")
    cluster_voices_parser.add_argument("--series-id", required=True); cluster_voices_parser.add_argument("--force", action="store_true")
    review_voices_parser = subparsers.add_parser("review-voices", help="List voice clusters and write listening samples to Series/voice_review")
    review_voices_parser.add_argument("--series-id", required=True)
    assign_voice_parser = subparsers.add_parser("assign-voice", help="Approve a voice cluster as the original voice of an approved character")
    assign_voice_parser.add_argument("--cluster-id", required=True); assign_voice_parser.add_argument("--character-id", required=True)
    reject_voice_parser = subparsers.add_parser("reject-voice", help="Reject a voice cluster (background noise, laugh track, mixed voices)")
    reject_voice_parser.add_argument("--cluster-id", required=True)
    voice_refs_parser = subparsers.add_parser("build-voice-references", help="Cut TTS reference clips for every approved character voice")
    voice_refs_parser.add_argument("--series-id", required=True)
    mux_parser = subparsers.add_parser("mux-dialogue", help="Synthesize and mux planned dialogue with cloned voices into a completed shot queue")
    mux_parser.add_argument("--job-id", required=True)
    pipeline_parser = subparsers.add_parser("generate-episode", help="Plan, check continuity, queue, wait for rendering, dub with cloned voices, and assemble a finished episode in one call")
    pipeline_parser.add_argument("--series-id", required=True)
    pipeline_parser.add_argument("--idea", required=True)
    pipeline_parser.add_argument("--duration-minutes", type=float, default=22)
    pipeline_parser.add_argument("--episode-code", required=True)
    pipeline_parser.add_argument("--notes")
    pipeline_parser.add_argument("--poll-seconds", type=float, default=20.0)
    render_test_parser = subparsers.add_parser("render-self-test", help="Render one small local ComfyUI (LTX) verification shot without changing Series Brain")
    render_test_parser.add_argument("--prompt", default="eine ruhige filmische Einstellung eines leeren modernen Raums bei Abendlicht")
    render_test_parser.add_argument("--frames", type=int, default=9)
    render_test_parser.add_argument("--output", type=Path, default=Path("Series/Generated/verification/wan_self_test.mp4"))
    adopt_parser = subparsers.add_parser("adopt-rendered-shot", help="Safely adopt a completed local ComfyUI output for its reserved queue item")
    adopt_parser.add_argument("--item-id", required=True); adopt_parser.add_argument("--worker-id", required=True); adopt_parser.add_argument("--source", type=Path, required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command in {"init-db", "init"}:
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(f"Initialized {db.path} (schema v{db.schema_version()})")
    elif args.command == "doctor":
        db = Database(config.paths.series_root / "series.db")
        details = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "project_root": str(config.paths.project_root),
            "series_root": str(config.paths.series_root),
            "free_disk_gb": round(shutil.disk_usage(config.paths.project_root).free / 1024**3, 1),
            "ffmpeg": shutil.which(config.paths.ffmpeg) is not None,
            "comfyui": config.paths.comfyui.is_dir(),
            "custom_nodes": (config.paths.comfyui / "custom_nodes").is_dir(),
            "ollama": _local_service_available(config.ollama.base_url),
            "database_schema_version": db.schema_version(),
            "configured_backends": config.models.model_dump(),
            "render_readiness": render_readiness(config),
        }
        print(json.dumps(details, indent=2, ensure_ascii=False))
    elif args.command == "import":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        result = EpisodeImporter(config, db).import_episode(args.source, args.series_id, args.series_title)
        print(json.dumps({"episode_id": result.episode_id, "status": result.status, "artifact_directory": str(result.artifact_directory), "language": config.localization.analysis_language}, ensure_ascii=False))
    elif args.command == "import-folder":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        candidates = list(args.folder.rglob("*.mp4"))
        reporter = ProgressReporter("IMPORT", len(candidates)); reporter.starting(args.series_id)
        results = EpisodeImporter(config, db).import_folder(args.folder, args.series_id, args.series_title, reporter.update)
        counts = {status: sum(item.status == status for item in results) for status in {item.status for item in results}}
        print(json.dumps({"series_id": args.series_id, "total": len(results), "counts": counts, "language": config.localization.analysis_language}, ensure_ascii=False))
    elif args.command == "import-all-series":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        root = args.source_root if args.source_root.is_absolute() else config.paths.project_root / args.source_root
        folders = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()) if root.is_dir() else []
        if not folders: raise SystemExit(f"No series folders found below {root}")
        summary = []
        for folder in folders:
            title = re.sub(r"^\s*\d+\s*[-_.]\s*", "", folder.name).strip() or folder.name
            series_id = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
            candidates = list(folder.rglob("*.mp4"))
            reporter = ProgressReporter("IMPORT", len(candidates)); reporter.starting(f"{series_id} ({folder.name})")
            results = EpisodeImporter(config, db).import_folder(folder, series_id, title, reporter.update)
            counts = {status: sum(item.status == status for item in results) for status in {item.status for item in results}}
            summary.append({"series_id": series_id, "title": title, "folder": str(folder), "total": len(results), "counts": counts})
        print(json.dumps({"series_root": str(root), "series": summary}, ensure_ascii=False, indent=2))
    elif args.command == "analyze-scenes":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        result = SceneDetectionService(config, db).analyze_episode(args.episode_id, args.threshold)
        print(json.dumps({"episode_id": result.episode_id, "status": result.status, "scene_count": result.scene_count, "frame_directory": str(result.frame_directory)}, ensure_ascii=False))
    elif args.command == "analyze-series-scenes":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        with db.connect() as connection: total = connection.execute("SELECT COUNT(*) FROM episodes e LEFT JOIN analysis_runs r ON r.episode_id=e.episode_id AND r.stage_name='scene_detection' AND r.status='completed' WHERE e.series_id = ? AND r.analysis_run_id IS NULL", (args.series_id,)).fetchone()[0]
        reporter = ProgressReporter("SCENES", total); reporter.starting(args.series_id)
        results = SceneDetectionService(config, db).analyze_ready_episodes(args.series_id, args.threshold, reporter.update)
        counts = {status: sum(item.status == status for item in results) for status in {item.status for item in results}}
        print(json.dumps({"series_id": args.series_id, "total": len(results), "counts": counts}, ensure_ascii=False))
    elif args.command == "transcribe":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        result = TranscriptionService(config, db).transcribe_episode(args.episode_id)
        print(json.dumps({"episode_id": result.episode_id, "status": result.status, "segment_count": result.segment_count, "transcript_path": str(result.transcript_path), "language": config.localization.analysis_language}, ensure_ascii=False))
    elif args.command == "transcribe-series":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        with db.connect() as connection: total = connection.execute("SELECT COUNT(*) FROM episodes e LEFT JOIN transcripts t ON t.episode_id=e.episode_id AND t.status='completed' WHERE e.series_id = ? AND t.transcript_id IS NULL", (args.series_id,)).fetchone()[0]
        reporter = ProgressReporter("TRANSCRIBE", total); reporter.starting(args.series_id)
        results = TranscriptionService(config, db).transcribe_ready_episodes(args.series_id, reporter.update)
        counts = {status: sum(item.status == status for item in results) for status in {item.status for item in results}}
        print(json.dumps({"series_id": args.series_id, "total": len(results), "counts": counts, "language": config.localization.analysis_language}, ensure_ascii=False))
    elif args.command == "analyze-semantics":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(SemanticAnalysisService(config, db).analyze_episode(args.episode_id), ensure_ascii=False))
    elif args.command == "analyze-series-semantics":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        with db.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM episodes e JOIN transcripts t ON t.episode_id=e.episode_id AND t.status='completed' "
                "LEFT JOIN analysis_runs r ON r.episode_id=e.episode_id AND r.stage_name='semantic_analysis' AND r.status='completed' "
                "WHERE e.series_id=? AND r.analysis_run_id IS NULL",
                (args.series_id,),
            ).fetchone()[0]
        reporter = ProgressReporter("SEMANTIK", total)
        reporter.starting(args.series_id)
        results = SemanticAnalysisService(config, db).analyze_ready_episodes(args.series_id, reporter.update)
        counts = {status: sum(item["status"] == status for item in results) for status in {item["status"] for item in results}}
        print(json.dumps({"series_id": args.series_id, "queued": total, "counts": counts, "language": config.localization.analysis_language}, ensure_ascii=False))
    elif args.command == "propose-characters":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(CharacterInventoryService(config, db).propose(args.series_id), ensure_ascii=False))
    elif args.command == "list-character-candidates":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(CharacterReviewService(db).candidates(args.series_id), ensure_ascii=False, indent=2))
    elif args.command == "approve-character":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(CharacterReviewService(db).approve(args.series_id, args.candidate_id, args.character_id, args.name), ensure_ascii=False))
    elif args.command == "reject-character":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(CharacterReviewService(db).reject(args.series_id, args.candidate_id), ensure_ascii=False))
    elif args.command == "prepare-speech-turns":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        result = SpeechTurnService(config, db).prepare_episode(args.episode_id)
        print(json.dumps(result.__dict__, ensure_ascii=False))
    elif args.command == "prepare-series-speech-turns":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        with db.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM episodes e JOIN transcripts t ON t.episode_id=e.episode_id AND t.status='completed' LEFT JOIN analysis_runs r ON r.episode_id=e.episode_id AND r.stage_name='speech_turn_preparation' AND r.status='completed' WHERE e.series_id=? AND r.analysis_run_id IS NULL", (args.series_id,)).fetchone()[0]
        reporter = ProgressReporter("SPRECHSEGMENTE", total); reporter.starting(args.series_id)
        results = SpeechTurnService(config, db).prepare_ready_episodes(args.series_id, reporter.update)
        counts = {status: sum(item.status == status for item in results) for status in {item.status for item in results}}
        print(json.dumps({"series_id": args.series_id, "queued": total, "counts": counts}, ensure_ascii=False))
    elif args.command == "extract-voice-candidates":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(VoiceCandidateService(config, db).extract_episode(args.episode_id).__dict__, ensure_ascii=False))
    elif args.command == "extract-series-voice-candidates":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        with db.connect() as connection:
            total = connection.execute("SELECT COUNT(DISTINCT e.episode_id) FROM episodes e JOIN speaker_segments s ON s.episode_id=e.episode_id AND s.source_backend='transcript_turns' LEFT JOIN analysis_runs r ON r.episode_id=e.episode_id AND r.stage_name='voice_candidate_extraction' AND r.status='completed' WHERE e.series_id=? AND r.analysis_run_id IS NULL", (args.series_id,)).fetchone()[0]
        reporter = ProgressReporter("STIMM-PROBEN", total); reporter.starting(args.series_id)
        results = VoiceCandidateService(config, db).extract_ready_episodes(args.series_id, reporter.update)
        counts = {status: sum(item.status == status for item in results) for status in {item.status for item in results}}
        print(json.dumps({"series_id": args.series_id, "queued": total, "counts": counts}, ensure_ascii=False))
    elif args.command in {"embed-voices", "cluster-voices", "review-voices", "assign-voice", "reject-voice", "build-voice-references"}:
        from .services.voice_identity import VoiceIdentityService
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        service = VoiceIdentityService(config, db)
        if args.command == "embed-voices":
            results = service.embed_series(args.series_id, lambda done, total, item: print(f"[{done}/{total}] {item.episode_id}: {item.status} ({item.embedded})", flush=True))
            output = {"series_id": args.series_id, "embedded_segments": sum(item.embedded for item in results), "failed": [item.episode_id for item in results if item.status.startswith("failed")]}
        elif args.command == "cluster-voices": output = service.cluster_series(args.series_id, args.force)
        elif args.command == "review-voices": output = service.review_clusters(args.series_id)
        elif args.command == "assign-voice": output = service.assign_cluster(args.cluster_id, args.character_id)
        elif args.command == "reject-voice": output = service.reject_cluster(args.cluster_id)
        else: output = service.build_references(args.series_id)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif args.command == "mux-dialogue":
        from .services.dialogue_mux import DialogueMuxService
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        results = DialogueMuxService(config, db).process_job(args.job_id, lambda done, total, item: print(f"[{done}/{total}] Shot {item.sequence_number}: {item.status}", flush=True))
        counts = {}
        for item in results: counts[item.status] = counts.get(item.status, 0) + 1
        print(json.dumps({"job_id": args.job_id, "counts": counts}, ensure_ascii=False, indent=2))
    elif args.command == "generate-episode":
        from .services.episode_pipeline import EpisodePipelineService
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        def report(stage: str, detail: dict) -> None:
            print(f"[Episode] {stage}: {json.dumps(detail, ensure_ascii=False)}", flush=True)
        result = EpisodePipelineService(config, db, poll_seconds=args.poll_seconds).run(args.series_id, args.idea, args.duration_minutes, args.episode_code, args.notes, report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "prepare-character-reference-images":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(CharacterReferenceImageService(config, db).create_for_series(args.series_id), ensure_ascii=False))
    elif args.command == "plan-episode":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        result = EpisodePlannerService(config, db).plan(args.series_id, args.idea, args.duration_minutes, args.notes, args.resume_job)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "propose-character-profiles":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(CharacterProfileService(config, db).propose(args.series_id, args.resume_job), ensure_ascii=False, indent=2))
    elif args.command == "approve-character-profile":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(ProfileReviewService(db).approve(args.proposal_id), ensure_ascii=False, indent=2))
    elif args.command == "synthesize-dialogue":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(VoiceSynthesisService(config, db).synthesize(args.character_id, args.text, args.output), ensure_ascii=False, indent=2))
    elif args.command == "render-self-test":
        if not 5 <= args.frames <= 121: raise SystemExit("--frames must be between 5 and 121")
        readiness = render_readiness(config)
        if not readiness["ready"]: raise SystemExit("Local renderer is not ready: " + readiness["required_next_step"])
        output = args.output if args.output.is_absolute() else config.paths.project_root / args.output
        spec = {"prompt": args.prompt, "negative_prompt": WAN_NEGATIVE_PROMPT, "seed": 424242, "duration_seconds": args.frames / 24, "fps": 24, "frame_count": args.frames, "output_prefix": "seriesai/wan_self_test", "output_path": project_relative(config.paths.project_root, output)}
        result = ComfyUIWorkflowBackend(config.comfyui_api, config.paths.comfyui, config.paths.project_root).render_shot(spec)
        print(json.dumps({"status": "rendered", "output_path": result, "frames": args.frames, "backend": "local_comfyui"}, ensure_ascii=False, indent=2))
    elif args.command == "adopt-rendered-shot":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(QueueWorkerService(db).adopt_local_output(args.item_id, args.worker_id, str(args.source), str(config.paths.project_root)), ensure_ascii=False, indent=2))
    elif args.command == "propose-relationships":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(RelationshipProposalService(config, db).propose(args.series_id, args.resume_job), ensure_ascii=False, indent=2))
    elif args.command == "check-continuity":
        print(json.dumps(ContinuityChecker().check_document(args.outline), ensure_ascii=False, indent=2))
    elif args.command == "create-shot-queue":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(ShotQueueService(config, db).create(args.outline, args.job_id), ensure_ascii=False, indent=2))
    elif args.command == "recover-queue":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(QueueWorkerService(db).recover_interrupted(args.job_id), ensure_ascii=False, indent=2))
    elif args.command == "simulate-offscreen-life":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(OffscreenLifeService(config, db).simulate(args.series_id, args.days, args.start_date), ensure_ascii=False, indent=2))
    elif args.command == "assemble-episode":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(EpisodeEditorService(config, db).assemble(args.job_id, args.episode_code), ensure_ascii=False, indent=2))
    elif args.command == "build-character-dataset":
        db = Database(config.paths.series_root / "series.db")
        db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(CharacterDatasetService(config, db).build(args.character_id), ensure_ascii=False, indent=2))
    elif args.command == "set-episode-date":
        db = Database(config.paths.series_root / "series.db"); db.initialize(config.paths.project_root / "backups" / "migrations")
        print(json.dumps(TimelineService(db).set_episode_date(args.episode_id, args.date), ensure_ascii=False, indent=2))
    elif args.command == "commit-episode":
        db=Database(config.paths.series_root/"series.db"); db.initialize(config.paths.project_root/"backups"/"migrations")
        print(json.dumps(EpisodeCommitService(config,db).commit(args.series_id,args.episode_id,args.episode_code,args.date),ensure_ascii=False,indent=2))


def _local_service_available(url: str) -> bool:
    """No cloud fallback: probe only the configured loopback Ollama endpoint."""
    try:
        from urllib.request import urlopen
        with urlopen(f"{url.rstrip('/')}/api/tags", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


if __name__ == "__main__":
    main()
