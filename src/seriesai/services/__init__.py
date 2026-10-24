"""Lazy service exports so optional AI backends do not block the dashboard."""
from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "EpisodeImporter": ("importer", "EpisodeImporter"), "ImportResult": ("importer", "ImportResult"),
    "SceneDetectionResult": ("scene_detection", "SceneDetectionResult"), "SceneDetectionService": ("scene_detection", "SceneDetectionService"),
    "TranscriptionResult": ("transcription", "TranscriptionResult"), "TranscriptionService": ("transcription", "TranscriptionService"),
    "CharacterInventoryService": ("character_inventory", "CharacterInventoryService"), "CharacterReviewService": ("character_review", "CharacterReviewService"),
    "SpeechTurnService": ("speech_turns", "SpeechTurnService"), "VoiceCandidateService": ("voice_candidates", "VoiceCandidateService"),
    "CharacterReferenceImageService": ("character_reference_images", "CharacterReferenceImageService"), "EpisodePlannerService": ("episode_planner", "EpisodePlannerService"),
    "CharacterProfileService": ("character_profiles", "CharacterProfileService"), "RelationshipProposalService": ("relationship_proposals", "RelationshipProposalService"),
    "RelationshipReviewService": ("relationship_review", "RelationshipReviewService"), "ContinuityChecker": ("continuity_checker", "ContinuityChecker"),
    "ShotQueueService": ("shot_queue", "ShotQueueService"), "QueueWorkerService": ("queue_worker", "QueueWorkerService"),
    "OffscreenLifeService": ("offscreen_life", "OffscreenLifeService"), "EpisodeEditorService": ("episode_editor", "EpisodeEditorService"),
    "CharacterDatasetService": ("character_dataset", "CharacterDatasetService"), "EpisodeCommitService": ("episode_commit", "EpisodeCommitService"),
    "ProfileReviewService": ("profile_review", "ProfileReviewService"), "VoiceSynthesisService": ("voice_synthesis", "VoiceSynthesisService"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value
