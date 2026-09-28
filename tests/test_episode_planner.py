from seriesai.services.episode_planner import EpisodeOutline, EpisodePlannerService, PlannedScene


def test_outline_validation_rejects_unknown_character_and_duration():
    outline = EpisodeOutline(title="Test", logline="Test", scenes=[PlannedScene(sequence_number=1, location="Studio", character_ids=["unknown_001"], duration_seconds=10, purpose="Test", conflict_or_turn="Test"), PlannedScene(sequence_number=2, location="Studio", character_ids=["known_001"], duration_seconds=10, purpose="Test", conflict_or_turn="Test")])
    issues = EpisodePlannerService.validate(outline, {"characters": [{"character_id": "known_001"}]}, 600)
    assert any("unknown character" in issue for issue in issues)
    assert any("duration" in issue for issue in issues)


def test_draft_normalization_resolves_names_and_scales_duration():
    outline = EpisodeOutline(title="Test", logline="Test", scenes=[PlannedScene(sequence_number=1, location="Studio", character_ids=["Babe"], duration_seconds=60, purpose="Test", conflict_or_turn="Test"), PlannedScene(sequence_number=2, location="Studio", character_ids=["babe_001"], duration_seconds=60, purpose="Test", conflict_or_turn="Test")])
    result = EpisodePlannerService.normalize_draft(outline, {"characters": [{"character_id": "babe_001", "canonical_name": "Babe"}]}, 600)
    assert result.scenes[0].character_ids == ["babe_001"]
    assert sum(scene.duration_seconds for scene in result.scenes) == 600
